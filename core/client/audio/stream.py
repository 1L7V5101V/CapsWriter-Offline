"""
音频流管理模块

提供 AudioStreamManager 类用于管理音频输入流，包括流的创建、
启动、停止和设备检测。

同时管理麦克风输入流与系统音频（WASAPI loopback）采集，两者并行工作：
- 麦克风：快捷键按下时从 sounddevice InputStream 取数据
- 系统音频：长按绑定了 `source='system'` 的快捷键（默认鼠标侧键 X1）时从 loopback 采集线程取数据
两条通道各自投递到独立的 asyncio 队列，互不阻塞。
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from typing import TYPE_CHECKING

import numpy as np
import sounddevice as sd

from config_client import ClientConfig as Config
from core.client.state import console

from . import logger

if TYPE_CHECKING:
    from core.client.state import ClientState

    from ..app import CapsWriterClient

# 灵动岛浮窗（"正在听"实时反馈）
# 延迟导入放在方法内，避免在 __init__ 阶段触发 Tkinter 子线程
_ISLAND = None


def _get_island():
    global _ISLAND
    if _ISLAND is None:
        try:
            from core.listening_island import ListeningIsland
            from core.listening_island import set_logger as _set_island_logger
            # 注入 client logger，使灵动岛内部日志写入 client_latest.log
            try:
                _set_island_logger(logger)
            except Exception as le:
                logger.debug(f"[灵动岛] 注入 logger 失败: {le}")
            island = ListeningIsland()
            logger.info("[灵动岛] 实例创建成功，准备注入配置")
            # 注入用户配置
            try:
                island.configure(
                    enabled=Config.listening_island_enabled,
                    min_threshold=Config.listening_island_min_threshold,
                    noise_factor=Config.listening_island_noise_factor,
                    confirm_frames=Config.listening_island_confirm_frames,
                    dock_edge=getattr(Config, 'listening_island_dock_edge', 'top'),
                    auto_hide=getattr(Config, 'listening_island_auto_hide', True),
                    beam=getattr(Config, 'listening_island_beam', True),
                    show_partial=getattr(Config, 'listening_island_show_partial', True),
                    beam_style=getattr(Config, 'listening_island_beam_style', 'comet'),
                    beam_custom=getattr(Config, 'listening_island_beam_custom', None),
                )
                logger.info(f"[灵动岛] 配置已注入: enabled={Config.listening_island_enabled}")
            except Exception as ce:
                logger.warning(f"[灵动岛] 注入配置失败: {ce}")
            _ISLAND = island
        except Exception as e:
            logger.warning(f"[灵动岛] 浮窗不可用: {e}", exc_info=True)
            _ISLAND = False
    return _ISLAND


class AudioStreamManager:
    """
    音频流管理器

    负责管理音频输入流的生命周期，包括：
    - 检测和选择音频设备
    - 创建和启动音频流
    - 处理音频数据回调
    - 流的重启和关闭
    - 系统音频（loopback）采集的启停

    Attributes:
        state: 客户端状态实例
        sample_rate: 麦克风采样率（默认 48000Hz）
        block_duration: 每个数据块的时长（秒，默认 0.05s）
    """

    SAMPLE_RATE = 48000
    BLOCK_DURATION = 0.05  # 50ms

    def __init__(self, app: CapsWriterClient):
        """
        初始化音频流管理器

        Args:
            app: 客户端 App 实例
        """
        self.app = app
        self._channels = 1
        self._running = False  # 标志是否应该运行

        # 系统音频（WASAPI loopback）采集：与麦克风并行，独立线程
        self._system_capture = None
        self._loopback_devices_cache: list[dict] = []
        self._loopback_devices_ts: float = 0.0

    # ========== 系统音频（WASAPI Loopback）采集 ==========

    @property
    def system_audio_available(self) -> bool:
        """系统音频采集是否已就绪"""
        return self._system_capture is not None

    @property
    def system_audio_device_name(self) -> str:
        """当前系统音频采集所用的输出设备名"""
        capture = self._system_capture
        return capture.device_name if capture is not None else ''

    def _loopback_keyword(self) -> str | None:
        """配置中的输出设备关键词（None 表示跟随系统默认输出）"""
        return getattr(Config, 'system_audio_loopback_device', None) or None

    def ensure_loopback_cache(self, max_age: float = 5.0) -> list[dict]:
        """获取 loopback 设备列表（缓存过期或为空时重新枚举）

        Args:
            max_age: 缓存有效期（秒），传 0 强制重新枚举

        Returns:
            设备信息列表，每项含 name / idx / default
        """
        now = time.monotonic()
        if self._loopback_devices_cache and (now - self._loopback_devices_ts) <= max_age:
            return self._loopback_devices_cache
        try:
            from .wasapi_loopback import list_loopback_devices
            devices = list_loopback_devices(self._loopback_keyword())
            self._loopback_devices_cache = devices
            self._loopback_devices_ts = now
            logger.info(f"loopback 设备缓存: {len(devices)} 个设备")
        except Exception as e:
            # 枚举失败时保留旧缓存，避免菜单直接变空
            logger.warning(f"loopback 设备枚举失败: {e}")
        return self._loopback_devices_cache

    def list_loopback_devices_safe(self) -> list[dict]:
        """获取缓存的 loopback 设备列表"""
        return self._loopback_devices_cache

    def start_system_capture(self) -> bool:
        """
        启动系统音频采集。

        首次启动后才真正打开设备；每次都重新打开可以自然适配输出设备的变化
        （插拔耳机、蓝牙重连、默认设备切换）。

        Returns:
            是否启动成功
        """
        if self._system_capture is not None:
            return True
        try:
            from .wasapi_loopback import SystemAudioCapture
            capture = SystemAudioCapture(device_keyword=self._loopback_keyword())
            capture.start(self._system_audio_callback)
            self._system_capture = capture
            self.state.system_audio_active = True
            self._update_island_idle_text()
            logger.info(f"系统音频采集已启动: {capture.device_name or '系统默认输出'}")
            return True
        except Exception as e:
            logger.error(f"启动系统音频采集失败: {e}", exc_info=True)
            self._system_capture = None
            self.state.system_audio_active = False
            return False

    def stop_system_capture(self) -> None:
        """停止并释放系统音频采集"""
        capture, self._system_capture = self._system_capture, None
        self.state.system_audio_active = False
        if capture is None:
            return
        try:
            capture.stop()
            logger.info(f"系统音频采集已停止: {capture.device_name or '系统默认输出'}")
        except Exception as e:
            logger.warning(f"停止系统音频采集时发生错误: {e}")
        self._update_island_idle_text()

    def switch_loopback_device(self, device_idx: int) -> None:
        """切换系统音频输出设备（写回运行时配置，必要时重启采集）"""
        if not isinstance(device_idx, int) or isinstance(device_idx, bool):
            logger.warning(f"忽略非法的设备索引: {device_idx!r}")
            return

        devices = self.ensure_loopback_cache(max_age=0)
        if device_idx < 0 or device_idx >= len(devices):
            logger.warning(f"无效的设备索引: {device_idx}")
            return

        name = devices[device_idx]['name']
        was_active = self._system_capture is not None
        if was_active:
            self.stop_system_capture()

        Config.system_audio_loopback_device = name
        self._loopback_devices_cache = []  # 重新枚举刷新选中标记
        self._loopback_devices_ts = 0.0
        logger.info(f"已切换系统音频输出设备: {name}")

        if was_active and not self.start_system_capture():
            logger.warning(f"切换到输出设备失败，可再次按 Alt 重试: {name}")

    def _system_audio_callback(self, data: np.ndarray) -> None:
        """
        loopback 音频数据回调（采集线程调用）

        仅在 Alt 录音期间向系统音频队列投递数据，避免常驻占用。
        """
        if not self._running or not self.state.is_recording('system'):
            return

        self._push_island_level(data)

        if self.app.loop:
            asyncio.run_coroutine_threadsafe(
                self.state.queue_system.put({
                    'type': 'data',
                    'time': time.time(),
                    'data': data.copy(),
                }),
                self.app.loop
            )

    def _update_island_idle_text(self) -> None:
        """根据系统音频状态更新灵动岛空闲文字"""
        try:
            island = _get_island()
            if island:
                if self._system_capture is not None:
                    island.set_idle_text("系统音频监听中")
                else:
                    island.set_idle_text("就绪")
        except Exception as e:
            logger.debug(f"[灵动岛] 更新空闲文字失败: {e}")

    def notify_recording_start(self, source: str = 'mic') -> None:
        """录音开始时通知（灵动岛上膛；系统音频源要确保采集已就绪）"""
        if (
            source == 'system'
            and Config.system_audio_enabled
            and self._system_capture is None
            and not self.start_system_capture()
        ):
            logger.warning("系统音频采集启动失败，本次录音将没有声音")
        try:
            island = _get_island()
            if island:
                island.arm()
        except Exception as e:
            logger.debug(f"[灵动岛] 上膛失败: {e}")

    def notify_recording_stop(self, source: str = 'mic') -> None:
        """录音停止时通知（灵动岛进入识别态）"""
        try:
            island = _get_island()
            if island:
                island.processing()
        except Exception as e:
            logger.debug(f"[灵动岛] 进入识别态失败: {e}")

    @property
    def state(self) -> ClientState:
        """快捷访问状态单例"""
        return self.app.state

    def _push_island_level(self, data: np.ndarray) -> None:
        """推送能量到"正在听"灵动岛浮窗（用于音量条显示）"""
        try:
            island = _get_island()
            if island:
                mono = data.mean(axis=1) if data.ndim > 1 else data
                rms = float(np.sqrt(np.mean(np.square(mono.astype(np.float32)))))
                island.push_level(rms)
        except Exception as e:
            logger.debug(f"[灵动岛] 推送能量失败: {e}")

    def _audio_callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info,
        status: sd.CallbackFlags
    ) -> None:
        """
        麦克风数据回调函数

        当音频流接收到新数据时调用，将数据放入对应的异步队列。
        与系统音频队列完全独立，两者可同时录音。
        """
        # 只在麦克风录音状态时处理数据
        if not self._running or not self.state.is_recording('mic'):
            return

        self._push_island_level(indata)

        if self.app.loop:
            asyncio.run_coroutine_threadsafe(
                self.state.queue_in.put({
                    'type': 'data',
                    'time': time.time(),
                    'data': indata.copy(),
                }),
                self.app.loop
            )

    def _on_stream_finished(self) -> None:
        """音频流结束回调"""
        if not threading.main_thread().is_alive():
            return
        if not self._running:
            return

        logger.info("音频流意外结束，正在尝试重启...")
        self.reopen()

    def start(self) -> sd.InputStream | None:
        """
        启动麦克风音频流

        Returns:
            创建的音频输入流，如果失败返回 None
        """
        if self._running:
            logger.debug("音频流已在运行，跳过启动")
            return self.state.stream

        # 检测音频设备
        try:
            device = sd.query_devices(kind='input')
            self._channels = min(2, device['max_input_channels'])
            device_name = device.get('name', '未知设备')
            console.print(
                f'使用默认音频设备：[italic]{device_name}，声道数：{self._channels}',
                end='\n\n'
            )
            logger.info(f"找到音频设备: {device_name}, 声道数: {self._channels}")
        except UnicodeDecodeError:
            logger.warning("无法获取音频设备名称（编码问题）")
        except sd.PortAudioError:
            logger.error("未找到麦克风设备")
            input('按回车键退出')
            sys.exit(1)

        # 创建音频流
        try:
            stream = sd.InputStream(
                samplerate=self.SAMPLE_RATE,
                blocksize=int(self.BLOCK_DURATION * self.SAMPLE_RATE),
                device=None,
                dtype="float32",
                channels=self._channels,
                callback=self._audio_callback,
                finished_callback=self._on_stream_finished,
            )
            stream.start()

            self.state.stream = stream
            self._running = True
            logger.debug(
                f"音频流已启动: 采样率={self.SAMPLE_RATE}, "
                f"块大小={int(self.BLOCK_DURATION * self.SAMPLE_RATE)}"
            )

            # 系统音频采集常驻打开（仅在两路录音时才真正投递数据），
            # 这样长按系统音频快捷键（默认鼠标侧键 X1）时无需等待设备打开，不会丢掉开头的声音
            if Config.system_audio_enabled:
                self.start_system_capture()

            return stream

        except sd.PortAudioError as e:
            logger.error(f"创建音频流失败: {e}", exc_info=True)
            if '-9999' in str(e):
                console.print("""
[bold red]检测到麦克风被占用或权限异常（错误码 -9999）[/bold red]
请尝试以下解决方案：

  1. 设置 > 隐私和安全性 > 麦克风，将「允许桌面应用访问麦克风」打开
  2. 状态栏右下角音量图标 > 右键菜单 > 声音 > 麦克风的属性，关闭「允许应用程序独占控制该设备」
  3. 状态栏右下角音量图标 > 右键菜单 > 声音 > 麦克风的属性，关闭「增强效果」
""")
            return None
        except Exception as e:
            logger.error(f"创建音频流失败: {e}", exc_info=True)
            return None

    def stop(self) -> None:
        """停止音频流（麦克风 + 系统音频）"""
        self.stop_system_capture()

        if not self._running:
            return

        self._running = False  # 标记为停止
        if self.state.stream is not None:
            try:
                self.state.stream.close()
                logger.debug("音频流已停止")
            except Exception as e:
                logger.debug(f"停止音频流时发生错误: {e}")
            finally:
                self.state.stream = None

    def reopen(self) -> sd.InputStream | None:
        """
        重新启动音频流

        Returns:
            新创建的音频输入流
        """
        logger.info("正在重启音频流...")

        # 停止旧流
        self.stop()

        # 重载 PortAudio，更新设备列表
        lib_name = getattr(sd, '_libname', None)
        if lib_name:
            try:
                sd._terminate()
                sd._ffi.dlclose(sd._lib)
                sd._lib = sd._ffi.dlopen(lib_name)
                sd._initialize()
            except Exception as e:
                logger.warning(f"重载 PortAudio 时发生警告: {e}")

        # 等待设备稳定
        time.sleep(0.1)

        # 启动新流
        return self.start()
