# coding: utf-8
"""
音频流管理模块

提供 AudioStreamManager 类用于管理音频输入流，包括流的创建、
启动、停止和设备检测。
"""

from __future__ import annotations

import sys
import time
import threading
from typing import TYPE_CHECKING, Optional

import numpy as np
import sounddevice as sd

from core.client.state import console
from . import logger

# 灵动岛浮窗（"正在听"实时反馈）
# 延迟导入放在方法内，避免在 __init__ 阶段触发 Tkinter 子线程
_ISLAND = None

def _get_island():
    global _ISLAND
    if _ISLAND is None:
        try:
            from core.listening_island import ListeningIsland, set_logger as _set_island_logger
            # 注入 client logger，使灵动岛内部日志写入 client_latest.log
            try:
                _set_island_logger(logger)
            except Exception:
                pass
            island = ListeningIsland()
            logger.info("[灵动岛] 实例创建成功，准备注入配置")
            # 注入用户配置
            try:
                from config_client import ClientConfig as Config
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

if TYPE_CHECKING:
    from core.client.state import ClientState
    from ..app import CapsWriterClient



class AudioStreamManager:
    """
    音频流管理器

    负责管理音频输入流的生命周期，包括：
    - 检测和选择音频设备
    - 创建和启动音频流
    - 处理音频数据回调
    - 流的重启和关闭

    Attributes:
        state: 客户端状态实例
        sample_rate: 采样率（默认 48000Hz）
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

        # WASAPI Loopback 系统音频捕获
        self._wasapi_capturer = None
        self._loopback_devices_cache = []
        self._system_audio_enabled = False

    @property
    def system_audio_enabled(self) -> bool:
        return self._system_audio_enabled

    # ========== WASAPI Loopback 系统音频捕获 ==========

    def ensure_loopback_cache(self) -> list[dict]:
        """惰性初始化 loopback 设备缓存"""
        if not self._loopback_devices_cache:
            try:
                from .wasapi_loopback import list_loopback_devices
                self._loopback_devices_cache = list_loopback_devices()
                logger.info(f"WASAPI loopback 设备缓存: {len(self._loopback_devices_cache)} 个设备")
            except Exception as e:
                logger.warning(f"WASAPI 设备枚举失败: {e}")
                self._loopback_devices_cache = []
        return self._loopback_devices_cache

    def list_loopback_devices_safe(self) -> list[dict]:
        """获取缓存的 loopback 设备列表"""
        return self._loopback_devices_cache

    def set_system_audio_mode(self, active: bool) -> None:
        """
        切换系统音频捕获模式。

        切换/关闭时如果正在录音，当前录音缓冲立刻 flush 并送入 Server。
        """
        if active == self._system_audio_enabled:
            return

        if active:
            # 切换到系统音频模式
            self._ensure_wasapi_capturer()
            self._system_audio_enabled = True
            self.state.system_audio_active = True
            logger.info("已切换到系统音频捕获模式")
        else:
            # 切换回麦克风模式
            self._stop_wasapi_capturer()
            self._system_audio_enabled = False
            self.state.system_audio_active = False
            logger.info("已切换到麦克风模式")

        # 更新灵动岛空闲文字
        self._update_island_idle_text()

    def switch_loopback_device(self, device_idx: int) -> None:
        """切换 WASAPI 输出设备"""
        is_active = self._system_audio_enabled
        # 先停掉当前捕获
        self._stop_wasapi_capturer()
        # 更新缓存
        devices = self.ensure_loopback_cache()
        if device_idx < 0 or device_idx >= len(devices):
            logger.warning(f"无效的设备索引: {device_idx}")
            return
        self._loopback_device_idx = device_idx
        # 如果之前在系统音频模式，重新启动
        if is_active:
            self._ensure_wasapi_capturer()
            self._wake_wasapi()
            logger.info(f"已切换到输出设备: {devices[device_idx]['name']}")

    def _ensure_wasapi_capturer(self) -> None:
        """确保 WASAPI 捕获器已创建"""
        if self._wasapi_capturer is not None:
            return
        try:
            from .wasapi_loopback import WasapiLoopbackCapture
            self._wasapi_capturer = WasapiLoopbackCapture(
                device_idx=getattr(self, '_loopback_device_idx', None)
            )
            self._wasapi_capturer.start(self._wasapi_callback)
        except Exception as e:
            logger.error(f"创建 WASAPI 捕获器失败: {e}")
            self._wasapi_capturer = None

    def _stop_wasapi_capturer(self) -> None:
        """停止并释放 WASAPI 捕获器"""
        if self._wasapi_capturer is not None:
            self._wasapi_capturer.stop()
            self._wasapi_capturer = None

    def _wake_wasapi(self) -> None:
        """唤醒 WASAPI 捕获（开始录音时）"""
        if self._wasapi_capturer is not None:
            self._wasapi_capturer.wake()

    def _wasapi_callback(self, data: np.ndarray) -> None:
        """WASAPI loopback 音频数据回调"""
        if not self._running or not self.state.recording:
            return
        if not self._system_audio_enabled:
            return

        import asyncio

        # 推送能量到灵动岛
        try:
            island = _get_island()
            if island:
                rms = float(np.sqrt(np.mean(np.square(data.astype(np.float32)))))
                island.push_level(rms)
        except Exception:
            pass

        if self.app.loop and self.state.queue_in:
            asyncio.run_coroutine_threadsafe(
                self.state.queue_in.put({
                    'type': 'data',
                    'time': time.time(),
                    'data': data.copy(),
                }),
                self.app.loop
            )

    def _update_island_idle_text(self) -> None:
        """根据当前音频模式更新灵动岛空闲文字"""
        try:
            island = _get_island()
            if island:
                if self._system_audio_enabled:
                    island.set_idle_text("系统音频监听中")
                else:
                    island.set_idle_text("就绪")
        except Exception:
            pass

    def notify_recording_start(self) -> None:
        """录音开始时通知（唤醒 WASAPI 捕获器和灵动岛）"""
        if self._system_audio_enabled:
            self._wake_wasapi()
        try:
            island = _get_island()
            if island:
                island.arm()
        except Exception:
            pass

    def notify_recording_stop(self) -> None:
        """录音停止时通知（灵动岛进入识别态）"""
        try:
            island = _get_island()
            if island:
                island.processing()
        except Exception:
            pass

    @property
    def state(self) -> ClientState:
        """快捷访问状态单例"""
        return self.app.state

    def _audio_callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info,
        status: sd.CallbackFlags
    ) -> None:
        """
        音频数据回调函数

        当音频流接收到新数据时调用，将数据放入异步队列中。
        """
        # 只在录音状态时处理数据
        if not self.state.recording:
            return

        # 系统音频模式时跳过麦克风输入
        if self._system_audio_enabled:
            return

        import asyncio

        # 计算本帧能量（RMS），推送给"正在听"灵动岛浮窗（用于音量条显示）
        try:
            island = _get_island()
            if island:
                # indata: float32, shape (blocksize, channels)
                mono = indata.mean(axis=1) if indata.ndim > 1 else indata
                rms = float(np.sqrt(np.mean(np.square(mono.astype(np.float32)))))
                island.push_level(rms)
        except Exception as e:
            logger.debug(f"[灵动岛] 推送能量失败: {e}")

        # 将数据放入队列
        if self.app.loop and self.state.queue_in:
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

    def start(self) -> Optional[sd.InputStream]:
        """
        启动音频流

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
        """停止音频流"""
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

    def reopen(self) -> Optional[sd.InputStream]:
        """
        重新启动音频流

        Returns:
            新创建的音频输入流
        """
        logger.info("正在重启音频流...")

        # 停止旧流
        self.stop()

        # 重载 PortAudio，更新设备列表
        try:
            sd._terminate()
            sd._ffi.dlclose(sd._lib)
            sd._lib = sd._ffi.dlopen(sd._libname)
            sd._initialize()
        except Exception as e:
            logger.warning(f"重载 PortAudio 时发生警告: {e}")

        # 等待设备稳定
        time.sleep(0.1)

        # 启动新流
        return self.start()
