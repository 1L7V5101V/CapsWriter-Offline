"""
系统音频（WASAPI Loopback）捕获模块

后端使用 `soundcard`（Windows 下为 WASAPI loopback），纯 Python，无需 Node 桥接。
默认跟随系统默认输出设备（含蓝牙耳机），也可用关键词指定输出设备。

输出统一为 **16000Hz 单声道 float32**，与麦克风送服务端的格式一致，
因此后续识别链路无需关心音频来源。

用法：
    devices = list_loopback_devices()
    capture = SystemAudioCapture(device_keyword=None)
    capture.start(callback_fn)     # callback 收到 np.float32 一维数组
    ...
    capture.stop()
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from types import ModuleType

import numpy as np

from . import logger

try:
    import soundcard as _sc
except Exception as _exc:  # 后端缺失时不影响麦克风听写
    _sc = None
    _IMPORT_ERROR: Exception | None = _exc
else:
    _IMPORT_ERROR = None


# 与服务端约定的送样格式：16kHz 单声道
SAMPLE_RATE = 16000
# 每次回调的块大小（100ms），兼顾延迟与开销
BLOCK_FRAMES = 1600

# COM 初始化相关常量（soundcard 底层是 WASAPI）
_COINIT_MULTITHREADED = 0x0
_COM_S_OK = 0x0
_COM_S_FALSE = 0x1
_COM_RPC_E_CHANGED_MODE = 0x80010106


def _com_initialize() -> bool:
    """在当前线程初始化 COM（MTA）

    soundcard 的 COM 初始化发生在 `import soundcard` 所在线程（通常是主线程），
    而采集跑在独立线程里。子线程未初始化 COM 就打开设备会报
    0x800401F0 (CO_E_NOTINITIALIZED)，所以采集线程必须自己初始化。

    Returns:
        本线程结束时是否需要调用 `CoUninitialize`
    """
    try:
        import ctypes
        hr = ctypes.windll.ole32.CoInitializeEx(None, _COINIT_MULTITHREADED) & 0xFFFFFFFF
    except Exception as e:
        logger.warning(f"[系统音频] COM 初始化异常: {e}")
        return False

    if hr in (_COM_S_OK, _COM_S_FALSE):  # 本次初始化成功（S_FALSE 表示已初始化过）
        return True
    if hr == _COM_RPC_E_CHANGED_MODE:  # 线程已用其它模式初始化，直接用
        return False
    logger.warning(f"[系统音频] COM 初始化失败: 0x{hr:08X}")
    return False


def _com_uninitialize() -> None:
    """与 `_com_initialize` 配对释放 COM"""
    try:
        import ctypes
        ctypes.windll.ole32.CoUninitialize()
    except Exception as e:
        logger.debug(f"[系统音频] COM 释放异常: {e}")


def _backend() -> ModuleType:
    """取 soundcard 模块，不可用时抛错"""
    if _sc is None:
        raise RuntimeError(f"soundcard 后端不可用: {_IMPORT_ERROR}")
    return _sc


def _default_speaker_name() -> str:
    """系统默认输出设备名（取不到时返回空串）"""
    try:
        return str(_backend().default_speaker().name)
    except Exception as e:
        logger.warning(f"[系统音频] 获取默认输出设备失败: {e}")
        return ''


def _loopback_sources() -> list:
    """枚举可作为 loopback 源的输出设备"""
    return [
        mic for mic in _backend().all_microphones(include_loopback=True)
        if getattr(mic, 'isloopback', False)
    ]


def _resolve_device(keyword: str | None = None):
    """
    选择 loopback 设备。

    优先按关键词匹配，其次系统默认输出设备，最后回退到第一个可用设备。
    """
    sources = _loopback_sources()
    if not sources:
        raise RuntimeError("未找到可用于 loopback 的输出设备")

    if keyword:
        lowered = keyword.lower()
        for mic in sources:
            if lowered in str(mic.name).lower():
                return mic
        logger.warning(f"[系统音频] 未找到匹配 '{keyword}' 的输出设备，回退到系统默认输出")

    default_name = _default_speaker_name()
    for mic in sources:
        if str(mic.name) == default_name:
            return mic
    return sources[0]


def list_loopback_devices(keyword: str | None = None) -> list[dict]:
    """
    枚举可用于系统音频捕获的输出设备。

    Args:
        keyword: 当前配置的设备关键词（用于标记被选中的设备）

    Returns:
        [{'idx': 序号, 'name': 设备名, 'default': 是否系统默认, 'selected': 是否被配置选中}]
    """
    # 可能从任意线程调用（如托盘菜单线程），需先初始化该线程的 COM
    com_ready = _com_initialize()
    try:
        try:
            sources = _loopback_sources()
        except Exception as e:
            logger.warning(f"[系统音频] 枚举输出设备失败: {e}")
            return []

        default_name = _default_speaker_name()
        devices: list[dict] = []
        for idx, mic in enumerate(sources):
            name = str(mic.name)
            devices.append({
                'idx': idx,
                'name': name,
                'default': name == default_name,
                'selected': bool(keyword) and keyword.lower() in name.lower(),
            })
        return devices
    finally:
        if com_ready:
            _com_uninitialize()


class SystemAudioCapture:
    """
    系统音频采集线程。

    在独立线程里跑 soundcard 的 loopback recorder，每 100ms 通过回调送出
    一块 16kHz 单声道 float32 数据。停止时释放设备句柄，下次 start 重新打开，
    这样输出设备热切换（插拔耳机 / 蓝牙重连）后重新开始录音即可生效。
    """

    SAMPLE_RATE = SAMPLE_RATE
    BLOCK_FRAMES = BLOCK_FRAMES

    def __init__(self, device_keyword: str | None = None):
        """
        Args:
            device_keyword: 输出设备名关键词，None 表示跟随系统默认输出设备
        """
        self._device_keyword = device_keyword
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._callback: Callable[[np.ndarray], None] | None = None
        self._device_name: str = ''

    @property
    def device_name(self) -> str:
        """当前采集的输出设备名"""
        return self._device_name

    @property
    def is_active(self) -> bool:
        """是否正在采集"""
        return self._thread is not None and self._thread.is_alive()

    def start(self, callback: Callable[[np.ndarray], None]) -> None:
        """启动采集（重复调用无副作用）"""
        if self.is_active:
            return
        self._callback = callback
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name='system-audio',
        )
        self._thread.start()

    def stop(self) -> None:
        """停止采集并等待线程退出"""
        self._stop_event.set()
        thread, self._thread = self._thread, None
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        self._callback = None

    def _loop(self) -> None:
        """采集线程主循环"""
        com_ready = _com_initialize()
        try:
            device = _resolve_device(self._device_keyword)
            self._device_name = str(device.name)
            logger.info(f"[系统音频] 开始采集: {self._device_name}")
            with device.recorder(
                samplerate=self.SAMPLE_RATE, channels=1,
            ) as recorder:
                while not self._stop_event.is_set():
                    frames = recorder.record(numframes=self.BLOCK_FRAMES)
                    if frames is None or len(frames) == 0:
                        continue
                    block = np.asarray(frames, dtype=np.float32).reshape(-1)
                    callback = self._callback
                    if callback is not None:
                        callback(block)
        except Exception as e:
            logger.error(f"[系统音频] 采集失败: {e}", exc_info=True)
        finally:
            logger.info(f"[系统音频] 采集结束: {self._device_name or '未知设备'}")
            if com_ready:
                _com_uninitialize()
