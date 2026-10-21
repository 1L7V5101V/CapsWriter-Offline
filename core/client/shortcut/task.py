"""
快捷键任务模块

管理单个快捷键的录音任务状态
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event
from typing import TYPE_CHECKING

from config_client import ClientConfig as Config
from core.client.audio.mute_audio import SystemAudioMute
from core.tools.my_status import Status

from . import logger

if TYPE_CHECKING:
    from core.client.app import CapsWriterClient
    from core.client.audio.recorder import AudioRecorder
    from core.client.shortcut.shortcut_config import Shortcut
    from core.client.shortcut.shortcut_manager import ShortcutManager
    from core.client.state import ClientState



class ShortcutTask:
    """
    单个快捷键的录音任务

    跟踪每个快捷键独立的录音状态，防止互相干扰。
    每个任务只服务一个音频源（麦克风 / 系统音频），两路互不阻塞。
    """

    # 由 ShortcutManager 注入
    _manager_ref: Callable[[], ShortcutManager | None] | None = None
    pool: ThreadPoolExecutor | None = None
    threshold: float = 0.3

    def __init__(self, app: CapsWriterClient, shortcut: Shortcut, recorder_class=None):
        """
        初始化快捷键任务

        Args:
            app: 客户端 App 实例
            shortcut: 快捷键配置
            recorder_class: AudioRecorder 类（可选，用于延迟导入）
        """
        self.app = app
        self.shortcut = shortcut
        self._recorder_class = recorder_class

        # 任务状态
        self.task: Future[None] | None = None
        self.recording_start_time: float = 0.0
        self.is_recording: bool = False

        # hold_mode 状态跟踪
        self.pressed: bool = False
        self.released: bool = True
        self.event: Event = Event()

        # 线程池（用于 countdown）
        self.pool = None

        # 录音状态动画
        self._status = Status('开始录音', spinner='point')

    @property
    def state(self) -> ClientState:
        """快捷访问状态单例"""
        return self.app.state

    @property
    def source(self) -> str:
        """该快捷键录音的音频源（'mic' / 'system'）"""
        return self.shortcut.source

    def _get_recorder(self) -> AudioRecorder:
        """获取 AudioRecorder 实例（绑定当前快捷键的音频源）"""
        if self._recorder_class is None:
            from core.client.audio.recorder import AudioRecorder
            self._recorder_class = AudioRecorder
        return self._recorder_class(self.app, self.source)

    def _should_mute_system_audio(self) -> bool:
        """录音时是否要静音系统输出（录系统音频本身、或系统音频正在录音时都不用静音）"""
        if not Config.mute_system_audio:
            return False
        if self.source == 'system':
            return False
        return not self.state.is_recording('system')

    def launch(self) -> None:
        """启动录音任务"""
        logger.info(f"[{self.shortcut.key}] 触发：开始录音（音频源: {self.source}）")

        # 记录开始时间
        self.recording_start_time = time.time()
        self.is_recording = True

        # 将开始标志放入对应音频源的队列
        asyncio.run_coroutine_threadsafe(
            self.state.queue(self.source).put(
                {'type': 'begin', 'time': self.recording_start_time, 'data': None}
            ),
            self.app.loop
        )

        # 更新录音状态
        self.state.start_recording(self.recording_start_time, self.source)

        # 打印动画：正在录音
        self._status.start()

        # 通知音频流管理器（灵动岛上膛 + 系统音频唤醒）
        try:
            if self.app.stream:
                self.app.stream.notify_recording_start(self.source)
        except Exception as e:
            logger.warning(f"[录音] 通知音频流失败: {e}")

        # 静音系统声音（录系统音频时不静音）
        if self._should_mute_system_audio():
            SystemAudioMute.mute()

        # 启动识别任务
        recorder = self._get_recorder()
        self.task = asyncio.run_coroutine_threadsafe(
            recorder.record_and_send(),
            self.app.loop,
        )

    def cancel(self) -> None:
        """取消录音任务（时间过短）"""
        logger.debug(f"[{self.shortcut.key}] 取消录音任务（时间过短）")

        self.is_recording = False
        self.state.stop_recording(self.source)
        self._status.stop()

        # 灵动岛：取消 → 回空闲态
        try:
            from core.client.audio.stream import _get_island
            island = _get_island()
            if island:
                island.idle()
        except Exception as e:
            logger.debug(f"[灵动岛] 回空闲态失败: {e}")

        # 恢复系统声音（录系统音频时没静音，无需恢复）
        if self._should_mute_system_audio():
            SystemAudioMute.unmute()

        if self.task is not None:
            self.task.cancel()
        self.task = None

    def finish(self) -> None:
        """完成录音任务"""
        logger.info(f"[{self.shortcut.key}] 释放：完成录音（音频源: {self.source}）")

        self.is_recording = False
        self.state.stop_recording(self.source)
        self._status.stop()

        # 通知音频流管理器（灵动岛进入识别态）
        try:
            if self.app.stream:
                self.app.stream.notify_recording_stop(self.source)
        except Exception as e:
            logger.warning(f"[录音] 通知音频流停止失败: {e}")

        # 恢复系统声音（录系统音频时没静音，无需恢复）
        if self._should_mute_system_audio():
            SystemAudioMute.unmute()

        asyncio.run_coroutine_threadsafe(
            self.state.queue(self.source).put({
                'type': 'finish',
                'time': time.time(),
                'data': None
            }),
            self.app.loop
        )

        # 执行 restore（可恢复按键 + 非阻塞模式）
        # 阻塞模式下按键不会发送到系统，状态不会改变，不需要恢复
        if self.shortcut.is_toggle_key() and not self.shortcut.suppress:
            self._restore_key()

    def _restore_key(self) -> None:
        """恢复按键状态（防自捕获逻辑由 ShortcutManager 处理）"""
        # 通知管理器执行 restore
        # 防自捕获：管理器会设置 flag 再发送按键
        if self._manager_ref is None:
            logger.warning(f"[{self.shortcut.key}] manager 引用丢失，无法 restore")
            return
        manager = self._manager_ref()
        if manager:
            logger.debug(f"[{self.shortcut.key}] 自动恢复按键状态 (suppress={self.shortcut.suppress})")
            manager.schedule_restore(self.shortcut.key)
        else:
            logger.warning(f"[{self.shortcut.key}] manager 引用丢失，无法 restore")
