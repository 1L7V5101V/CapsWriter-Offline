"""
音频录制模块

提供 AudioRecorder 类用于管理录音会话，包括开始录音、
发送音频数据到服务端、结束录音等功能。

一个 AudioRecorder 实例只负责一个音频源（'mic' 或 'system'），
麦克风与系统音频各自独立实例、独立队列、独立 WebSocket 任务，
因此两者可以同时录音、同时出字，互不阻塞。
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from typing import TYPE_CHECKING, Literal

import numpy as np

from config_client import ClientConfig as Config
from core.client.audio.file_manager import AudioFileManager
from core.client.connection import WebSocketManager
from core.client.state import console
from core.protocol import AudioMessage

from . import logger

if TYPE_CHECKING:
    from core.client.app import CapsWriterClient
    from core.client.state import ClientState

# 音频源类型
AudioSource = Literal['mic', 'system']


class AudioRecorder:
    """
    音频录制器

    管理一次完整的录音会话，包括：
    - 从音频流接收数据
    - 可选地保存到本地文件
    - 将音频数据发送到识别服务端

    Attributes:
        source: 音频源（'mic' 麦克风 / 'system' 系统音频）
    """

    def __init__(self, app: CapsWriterClient, source: str = 'mic'):
        """
        初始化录制器

        Args:
            app: 客户端 App 实例
            source: 音频源（'mic' 或 'system'）
        """
        self.app = app
        self.source: AudioSource = 'system' if source == 'system' else 'mic'
        self.task_id: str = ''
        self._file_manager: AudioFileManager | None = None
        self._start_time: float = 0.0
        self._duration: float = 0.0
        self._cache: list[np.ndarray] = []
        # 麦克风 48kHz；系统音频在采集端已统一下混为 16kHz 单声道
        self._sample_rate = 16000 if self.source == 'system' else 48000

    @property
    def state(self) -> ClientState:
        """快捷访问状态单例"""
        return self.app.state

    @property
    def _ws_manager(self) -> WebSocketManager:
        """快捷访问桥接到 app.ws"""
        return self.app.ws

    @property
    def _queue(self) -> asyncio.Queue:
        """当前音频源对应的队列（麦克风与系统音频互不干扰）"""
        return self.state.queue(self.source)

    def _frames(self, data: np.ndarray) -> int:
        """数据块包含的采样帧数（多声道时只取帧数）"""
        return int(data.shape[0])

    def _to_pcm_bytes(self, data: np.ndarray) -> bytes:
        """
        编码为服务端约定的 16kHz 单声道 float32 字节流

        麦克风：48kHz（可能多声道）→ 下混单声道 → 3 倍抽取到 16kHz
        系统音频：采集端已经是 16kHz 单声道，直接使用
        """
        if self.source == 'system':
            return np.asarray(data, dtype=np.float32).reshape(-1).tobytes()

        mono = data[::3]
        if mono.ndim > 1:
            mono = mono.mean(axis=1)
        return mono.astype(np.float32).tobytes()

    def _seg_params(self) -> tuple:
        """流式切片的时长与重叠（优先使用听写流式配置）"""
        seg_dur = getattr(Config, 'listening_stream_seg_duration', None)
        seg_ov = getattr(Config, 'listening_stream_seg_overlap', None)
        if seg_dur is None:
            seg_dur, seg_ov = Config.mic_seg_duration, Config.mic_seg_overlap
        return seg_dur, seg_ov

    def _build_message(self, payload: str, is_final: bool) -> AudioMessage:
        """构造送往服务端的音频消息"""
        seg_dur, seg_ov = self._seg_params()
        return AudioMessage(
            task_id=self.task_id,
            source=self.source,
            data=payload,
            is_final=is_final,
            time_start=self._start_time,
            seg_duration=seg_dur,
            seg_overlap=seg_ov,
            context=Config.context,
            language=Config.language,
        )

    async def _send_message(self, message: AudioMessage) -> None:
        """发送消息到服务端"""
        if not self._ws_manager.is_connected:
            if message.is_final:
                self.state.pop_audio_file(message.task_id)
                console.print('    服务端未连接，无法发送\n')
                logger.warning("服务端未连接，无法发送音频数据")
            return

        # 使用 WebSocketManager 发送协议消息
        success = await self._ws_manager.send(message)
        if not success and message.is_final:
            self.state.pop_audio_file(message.task_id)
            # 具体错误日志由 WebSocketManager 记录

    def _write_audio(self, data: np.ndarray, file_path) -> None:
        """统计时长并按需写入本地音频文件"""
        self._duration += self._frames(data) / self._sample_rate
        if file_path is not None and self._file_manager is not None:
            self._file_manager.write(data)

    def _send_chunk(self, data: np.ndarray) -> None:
        """把一块音频流式发送给服务端（可返回中间结果）"""
        message = self._build_message(
            base64.b64encode(self._to_pcm_bytes(data)).decode('utf-8'),
            is_final=False,
        )
        asyncio.create_task(self._send_message(message))

    async def record_and_send(self) -> None:
        """
        录音并发送数据

        从当前音频源的队列中读取数据，保存到文件（如果启用），
        并发送到服务端进行识别。
        """
        try:
            # 生成唯一任务 ID
            self.task_id = str(uuid.uuid1())
            logger.debug(f"创建录音任务，音频源: {self.source}，任务ID: {self.task_id}")

            self._start_time = 0.0
            self._duration = 0.0
            self._cache = []

            # 音频文件管理
            file_path = None
            if Config.save_audio:
                self._file_manager = AudioFileManager()

            # 从队列读取数据
            while task := await self._queue.get():
                self._queue.task_done()

                if task['type'] == 'begin':
                    self._start_time = task['time']
                    logger.debug(f"录音开始，音频源: {self.source}，时间戳: {self._start_time}")

                elif task['type'] == 'data':
                    # 在阈值之前积攒音频数据
                    if task['time'] - self._start_time < Config.threshold:
                        self._cache.append(task['data'])
                        continue

                    # 创建音频文件
                    if Config.save_audio and self._file_manager and file_path is None:
                        channels = task['data'].shape[1] if task['data'].ndim > 1 else 1
                        file_path, _ = self._file_manager.create(
                            channels,
                            self._start_time,
                            self._sample_rate,
                        )
                        self.state.register_audio_file(self.task_id, file_path)
                        logger.debug(f"创建音频文件: {file_path}")

                    # 获取音频数据（先补发缓存的部分）
                    if self._cache:
                        data = np.concatenate(self._cache)
                        self._cache.clear()
                    else:
                        data = task['data']

                    # 保存音频至本地文件
                    self._write_audio(data, file_path)

                    # 发送音频数据用于识别（流式分段，让服务端实时返回中间文字）
                    self._send_chunk(data)

                elif task['type'] == 'finish':
                    # 如果有缓存的数据未发送，先发送缓存
                    if self._cache:
                        data = np.concatenate(self._cache)
                        self._cache.clear()
                        self._write_audio(data, file_path)
                        self._send_chunk(data)

                    # 完成写入本地文件
                    if Config.save_audio and self._file_manager:
                        self._file_manager.finish()
                        logger.debug("完成音频文件写入")

                    console.print(f'任务标识：{self.task_id}')
                    console.print(f'    音频源：{self.source}，录音时长：{self._duration:.2f}s')
                    logger.info(
                        f"录音任务完成，音频源: {self.source}，"
                        f"任务ID: {self.task_id}, 时长: {self._duration:.2f}s"
                    )

                    # 告诉服务端音频片段结束了
                    asyncio.create_task(self._send_message(self._build_message('', True)))
                    break

        except Exception as e:
            logger.error(f"录音任务错误: {e}", exc_info=True)

    def get_file_manager(self) -> AudioFileManager | None:
        """获取当前的文件管理器"""
        return self._file_manager
