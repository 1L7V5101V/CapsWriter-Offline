"""
客户端状态管理模块

提供 ClientState 类用于管理客户端的全局状态。
使用 dataclass 提供类型安全和清晰的状态定义。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from rich.theme import Theme

from core.tools.safe_console import SafeConsole

from . import logger

if TYPE_CHECKING:
    import sounddevice as sd
    from websockets.legacy.client import WebSocketClientProtocol

    from .app import CapsWriterClient


# 配置 Rich console
_theme = Theme({
    'markdown.code': 'cyan',
    'markdown.item.number': 'yellow'
})
# 用 SafeConsole：输出到已失效的 stdout 时静默丢弃，不拖垮识别主流程
console = SafeConsole(highlight=False, soft_wrap=True, theme=_theme)


@dataclass
class ClientState:
    """
    客户端运行状态

    管理客户端运行过程中的所有共享状态，包括事件循环、消息队列、
    WebSocket 连接、音频流和录音状态等。

    Attributes:
        queue_in: 麦克风音频数据队列
        queue_system: 系统音频数据队列（与麦克风并行，互不阻塞）
        queue_out: 处理结果输出队列（保留）
        websocket: WebSocket 客户端连接
        stream: 音频输入流
        recording: 是否有任意音频源正在录音
        recording_start_time: 最早一个音频源的开始时间戳
        recordings: 按音频源区分的录音状态
        audio_files: 任务ID到音频文件路径的映射
        last_recognition_text: 最近一次识别的最终文本（热词替换后），供"添加纠错记录"使用
    """

    # 麦克风与系统音频各自独立的音频队列，两者可同时录音互不干扰
    queue_in: asyncio.Queue = field(default_factory=asyncio.Queue)
    queue_system: asyncio.Queue = field(default_factory=asyncio.Queue)
    queue_out: asyncio.Queue = field(default_factory=asyncio.Queue)
    websocket: WebSocketClientProtocol | None = None
    stream: sd.InputStream | None = None
    app: CapsWriterClient | None = None

    recording: bool = False
    recording_start_time: float = 0.0
    # 按音频源（'mic' / 'system'）区分的录音状态
    recordings: dict[str, bool] = field(default_factory=dict)
    recording_start_times: dict[str, float] = field(default_factory=dict)
    audio_files: dict[str, Path] = field(default_factory=dict)

    # 最近一次识别结果（用于手动添加纠错记录）
    last_recognition_text: str | None = None

    # 最近一次输出内容（如果是 LLM 润色，则是润色结果；否则是原始识别结果）
    last_output_text: str | None = None

    # 系统音频采集是否已就绪（不影响麦克风录音）
    system_audio_active: bool = False

    def queue(self, source: str = 'mic') -> asyncio.Queue:
        """按音频源取对应的音频队列"""
        return self.queue_system if source == 'system' else self.queue_in

    def reset(self) -> None:
        """
        重置状态

        清理所有状态，关闭连接和流。用于重新初始化或退出时清理。
        """
        logger.debug("正在重置客户端状态...")

        # 关闭 WebSocket 连接
        ws = self.websocket
        if ws is not None:
            try:
                if not ws.closed and self.app and self.app.loop and self.app.loop.is_running():
                    asyncio.run_coroutine_threadsafe(ws.close(), self.app.loop)
            except Exception as e:
                logger.debug(f"关闭 WebSocket 时发生错误: {e}")
            self.websocket = None

        # 关闭音频流
        if self.stream is not None:
            try:
                self.stream.close()
                logger.debug("音频流已关闭")
            except Exception as e:
                logger.debug(f"关闭音频流时发生错误: {e}")
            self.stream = None

        # 重置其他状态
        self.recording = False
        self.recording_start_time = 0.0
        self.recordings.clear()
        self.recording_start_times.clear()
        self.audio_files.clear()

        logger.debug("客户端状态重置完成")

    def is_recording(self, source: str = 'mic') -> bool:
        """指定音频源是否正在录音"""
        return bool(self.recordings.get(source, False))

    def start_recording(self, start_time: float, source: str = 'mic') -> None:
        """
        开始录音

        Args:
            start_time: 录音开始的时间戳
            source: 音频源（'mic' 或 'system'）
        """
        self.recordings[source] = True
        self.recording_start_times[source] = start_time
        # 汇总字段：任一源在录音即视为录音中
        self.recording = True
        if self.recording_start_time <= 0:
            self.recording_start_time = start_time
        logger.debug(
            f"录音状态已更新: source={source}, recording=True, start_time={start_time:.2f}"
        )

    def stop_recording(self, source: str = 'mic') -> float:
        """
        停止录音

        Args:
            source: 音频源（'mic' 或 'system'）

        Returns:
            录音持续时间（秒）
        """
        start_time = self.recording_start_times.pop(source, 0.0)
        duration = time.time() - start_time if start_time > 0 else 0.0

        self.recordings[source] = False
        # 汇总字段：所有源都停止后才算停止录音
        if not any(self.recordings.values()):
            self.recording = False
            self.recording_start_time = 0.0
        logger.debug(
            f"录音状态已更新: source={source}, recording=False, duration={duration:.2f}s"
        )
        return duration

    @property
    def is_connected(self) -> bool:
        """检查 WebSocket 是否已连接"""
        if self.websocket is None:
            return False
        try:
            return not self.websocket.closed
        except AttributeError:
            return self.websocket is not None

    def register_audio_file(self, task_id: str, file_path: Path) -> None:
        """
        注册音频文件

        Args:
            task_id: 任务ID
            file_path: 音频文件路径
        """
        self.audio_files[task_id] = file_path
        logger.debug(f"注册音频文件: task_id={task_id}, path={file_path}")

    def pop_audio_file(self, task_id: str) -> Path | None:
        """
        获取并移除音频文件路径

        Args:
            task_id: 任务ID

        Returns:
            音频文件路径，如果不存在则返回 None
        """
        file_path = self.audio_files.pop(task_id, None)
        if file_path:
            logger.debug(f"获取音频文件: task_id={task_id}, path={file_path}")
        return file_path

    def set_output_text(self, text: str) -> None:
        """
        设置最近一次输出文本

        Args:
            text: 输出文本内容
        """
        self.last_output_text = text
