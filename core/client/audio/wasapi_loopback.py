# coding: utf-8
"""
WASAPI Loopback 音频捕获模块

通过 OpenCode 的 native-recorder-nodejs 原生模块
（已预编译的 C++ WASAPI 实现）捕获系统扬声器输出。

不需要 Python WASAPI 实现，通过 Node.js 子进程桥接。

用法：
    devices = list_loopback_devices()
    capturer = WasapiLoopbackCapture(device_idx=0)
    capturer.start(callback_fn)
    capturer.wake()
    ...
    capturer.stop()
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from . import logger

# ======================================================================
# Node.js 可执行文件路径
# ======================================================================

_NODE_PATH = None
import shutil as _su
for _p in ["D:/Node/node.exe", "C:/Program Files/nodejs/node.exe"]:
    if Path(_p).exists():
        _NODE_PATH = _p
        break
if not _NODE_PATH:
    _node_from_path = _su.which("node")
    if _node_from_path:
        _NODE_PATH = _node_from_path
if not _NODE_PATH:
    _NODE_PATH = "node"  # 最后回退

# ======================================================================
# OpenCode 项目 native-recorder-nodejs 模块路径
# ======================================================================

_OPENCODE_NM = Path(
    "D:/.Projects/OpenCode/node_modules/.bun/"
    "native-recorder-nodejs@1.2.0/node_modules/native-recorder-nodejs"
)
_HELPER_SCRIPT = Path(__file__).parent / "wasapi_helper.cjs"


def list_loopback_devices() -> list[dict]:
    """
    枚举输出设备。

    通过 Node.js helper 枚举 Windows WASAPI 输出设备，
    保证与 native-recorder-nodejs 兼容的设备 ID。
    """
    try:
        script = _ensure_helper()
        r = subprocess.run(
            [_NODE_PATH, str(script), "--list"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            raise RuntimeError(r.stderr[:200])

        devices = []
        for line in r.stdout.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                dev = json.loads(line)
                devices.append(dev)
            except json.JSONDecodeError:
                pass
        return devices
    except Exception as e:
        logger.warning(f"枚举输出设备失败: {e}")
        return []


def _ensure_helper() -> Path:
    """确保 helper 脚本存在"""
    if not _HELPER_SCRIPT.exists():
        raise FileNotFoundError(f"Helper 脚本未找到: {_HELPER_SCRIPT}")
    return _HELPER_SCRIPT


class WasapiLoopbackCapture:
    """
    WASAPI Loopback 音频捕获器。

    通过 Node.js 子进程 + native-recorder-nodejs 原生模块捕获音频。
    """

    SAMPLE_RATE = 16000

    def __init__(self, device_idx: Optional[int] = None):
        self._device_idx = device_idx
        self._device_id: Optional[str] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._wake_event = threading.Event()
        self._callback: Optional[Callable[[np.ndarray], None]] = None
        self._process: Optional[subprocess.Popen] = None

    def start(self, callback: Callable[[np.ndarray], None]) -> None:
        if self._running:
            return
        self._callback = callback
        self._running = True
        self._wake_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="wasapi-loopback")
        self._thread.start()
        logger.info(f"WASAPI loopback 已启动 (device_idx={self._device_idx})")

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._wake_event.set()
        self._stop_process()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None
        self._callback = None
        logger.info("WASAPI loopback 已停止")

    def wake(self) -> None:
        self._wake_event.set()

    @property
    def is_active(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def _stop_process(self):
        if self._process:
            try:
                self._process.terminate()
            except Exception:
                pass
            try:
                self._process.wait(timeout=2)
            except Exception:
                pass
            self._process = None

    def _loop(self):
        while self._running:
            self._wake_event.wait()
            if not self._running:
                break
            self._wake_event.clear()
            self._capture_once()

    def _capture_once(self):
        """通过 Node.js 子进程捕获一次录音会话"""
        if not _OPENCODE_NM.exists():
            logger.error(f"native-recorder-nodejs 未找到: {_OPENCODE_NM}")
            return

        try:
            script = _ensure_helper()

            args = [_NODE_PATH, str(script), "--capture"]
            if self._device_idx is not None:
                args.extend(["--device", str(self._device_idx)])

            self._process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            # 从 stdout 读取 PCM 数据（int16, 16kHz mono）
            # helper 输出头信息（JSON 行）后持续输出原始 PCM
            stdout = self._process.stdout
            stderr = self._process.stderr

            if not stdout:
                return

            # 读取头行（格式信息）
            header_line = stdout.readline()
            try:
                header = json.loads(header_line.decode("utf-8", errors="replace").strip())
                sample_rate = header.get("sampleRate", 48000)
                channels = header.get("channels", 1)
                sample_format = header.get("format", "int16")
                logger.debug(f"Helper header: {header}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                sample_rate, channels, sample_format = 48000, 1, "int16"

            # 计算帧大小
            if sample_format == "int16":
                bytes_per_sample = 2
                dtype = np.int16
            else:
                bytes_per_sample = 2
                dtype = np.int16

            frame_size = bytes_per_sample
            chunk_frames = int(sample_rate * 0.05)  # 50ms
            chunk_bytes = chunk_frames * frame_size

            # 持续读取音频
            while self._running and self._wake_event.is_set():
                raw = stdout.read(chunk_bytes)
                if not raw or len(raw) < frame_size:
                    break

                data = np.frombuffer(raw, dtype=dtype).astype(np.float32)
                if len(data) > 0:
                    # int16 → float32
                    data /= 32768.0
                    # 降采样到 16kHz（如需要）
                    if sample_rate != self.SAMPLE_RATE and len(data) > 1:
                        ratio = self.SAMPLE_RATE / sample_rate
                        new_len = max(1, int(len(data) * ratio))
                        indices = np.linspace(0, len(data) - 1, new_len)
                        data = np.interp(indices, np.arange(len(data)), data).astype(np.float32)

                    if self._callback:
                        self._callback(data)

            # 检查 stderr 中的错误
            err = stderr.read()
            if err:
                err_text = err.decode("utf-8", errors="replace").strip()
                if err_text:
                    logger.warning(f"Helper stderr: {err_text}")

        except Exception as e:
            if self._running:
                logger.error(f"WASAPI 捕获异常: {e}", exc_info=True)
        finally:
            self._stop_process()
