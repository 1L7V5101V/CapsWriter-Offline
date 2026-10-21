"""
音频文件管理模块

提供 AudioFileManager 类用于管理音频文件的创建、写入、完成和重命名。
支持 MP3（需要 FFmpeg）和 WAV 两种格式。
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
import wave
from os import makedirs
from pathlib import Path
from subprocess import DEVNULL, PIPE, Popen

import numpy as np

from config_client import ClientConfig as Config
from core.tools.no_window import NO_WINDOW

from . import logger

# 音频文件句柄类型
AudioWriter = Popen | wave.Wave_write


def _open_wave_writer(path: Path) -> wave.Wave_write:
    """打开 WAV 写入句柄

    句柄需要跨多次 write 调用长期保持打开，不能交给 with 管理。
    """
    return wave.open(str(path), 'w')


class AudioFileManager:
    """
    音频文件管理器

    负责音频文件的完整生命周期管理：
    - 创建：根据是否安装 FFmpeg 选择 MP3 或 WAV 格式
    - 写入：将音频数据写入文件
    - 完成：关闭文件句柄
    - 重命名：根据识别文本重命名文件
    """

    SAMPLE_RATE = 48000

    def __init__(self):
        """初始化音频文件管理器"""
        self.file_path: Path | None = None
        self.file_handle: AudioWriter | None = None
        self.channels: int = 1
        self.samplerate: int = self.SAMPLE_RATE
        self._has_ffmpeg = shutil.which('ffmpeg') is not None

        if self._has_ffmpeg:
            logger.debug("检测到 FFmpeg，将使用 MP3 格式保存录音")
        else:
            logger.debug("未检测到 FFmpeg，将使用 WAV 格式保存录音")

    def create(
        self, channels: int, time_start: float, samplerate: int | None = None,
    ) -> tuple[Path, AudioWriter]:
        """
        创建音频文件

        Args:
            channels: 音频声道数
            time_start: 录音开始时间戳
            samplerate: 音频采样率，默认沿用 SAMPLE_RATE（麦克风 48000）

        Returns:
            (文件路径, 文件写入句柄) 元组
        """
        self.channels = channels
        self.samplerate = samplerate or self.SAMPLE_RATE

        # 构建目录和文件名
        local_time = time.localtime(time_start)
        time_year = time.strftime('%Y', local_time)
        time_month = time.strftime('%m', local_time)
        time_ymdhms = time.strftime("%Y%m%d-%H%M%S", local_time)

        folder_path = Path() / time_year / time_month / 'assets'
        makedirs(folder_path, exist_ok=True)

        # 创建临时文件名（mkstemp 保证唯一，随后关闭 fd 只用路径）
        fd, tmp_name = tempfile.mkstemp(prefix=f'({time_ymdhms})', dir=folder_path)
        os.close(fd)
        try:
            os.unlink(tmp_name)  # 先删掉占位文件，交给 ffmpeg/wave 重新创建
        except OSError as e:
            logger.debug(f"清理临时占位文件失败: {e}")
        file_path = Path(tmp_name)

        if self._has_ffmpeg:
            # 使用 FFmpeg 输出 MP3
            file_path = file_path.with_suffix('.mp3')
            ffmpeg_command = [
                'ffmpeg', '-y',
                '-f', 'f32le',
                '-ar', str(self.samplerate),
                '-ac', str(channels),
                '-i', '-',
                '-b:a', '192k',
                str(file_path),
            ]
            # 打包成无控制台的 GUI 程序后，ffmpeg 会弹出一个黑窗并抢走焦点，
            # 导致识别结果上屏到错误的窗口，所以显式要求不创建控制台窗口。
            file_handle: AudioWriter = Popen(
                ffmpeg_command, stdin=PIPE, stdout=DEVNULL, stderr=DEVNULL,
                creationflags=NO_WINDOW,
            )
            logger.debug(f"创建 MP3 文件: {file_path}（{self.samplerate}Hz）")
        else:
            # 使用 wave 模块输出 WAV
            file_path = file_path.with_suffix('.wav')
            file_handle = _open_wave_writer(file_path)
            file_handle.setnchannels(channels)
            file_handle.setsampwidth(2)  # 16-bit
            file_handle.setframerate(self.samplerate)
            logger.debug(f"创建 WAV 文件: {file_path}（{self.samplerate}Hz）")

        self.file_path = file_path
        self.file_handle = file_handle

        return file_path, file_handle

    def write(self, data: np.ndarray) -> None:
        """
        写入音频数据

        Args:
            data: 音频数据数组（float32 格式）
        """
        if self.file_handle is None:
            logger.warning("尝试写入数据但文件未打开")
            return

        if isinstance(self.file_handle, Popen):
            # FFmpeg 进程
            pipe = self.file_handle.stdin
            if pipe is None:
                logger.warning("FFmpeg stdin 不可用，丢弃写入")
                return
            pipe.write(data.tobytes())
            pipe.flush()
        elif isinstance(self.file_handle, wave.Wave_write):
            # WAV 文件：转换 float32 -> int16
            int_data = (data * (2**15 - 1)).astype(np.int16).tobytes()
            self.file_handle.writeframes(int_data)

    def finish(self) -> Path | None:
        """
        完成音频文件写入

        Returns:
            音频文件路径
        """
        if self.file_handle is None:
            return self.file_path

        try:
            if isinstance(self.file_handle, Popen):
                pipe = self.file_handle.stdin
                if pipe is not None:
                    pipe.close()
                logger.debug("FFmpeg 进程已关闭")
            elif isinstance(self.file_handle, wave.Wave_write):
                self.file_handle.close()
                logger.debug("WAV 文件已关闭")
        except Exception as e:
            logger.error(f"关闭音频文件时发生错误: {e}")
        finally:
            self.file_handle = None

        return self.file_path

    def rename(self, text: str, time_start: float) -> Path | None:
        """
        根据识别文本重命名音频文件

        Args:
            text: 识别出的文本
            time_start: 录音开始时间戳

        Returns:
            重命名后的文件路径，如果失败返回 None
        """
        if self.file_path is None or not self.file_path.exists():
            logger.warning(f"文件不存在，无法重命名: {self.file_path}")
            return None

        # 构建新文件名
        time_ymdhms = time.strftime("%Y%m%d-%H%M%S", time.localtime(time_start))

        # 截取文本并清理非法字符
        text_clean = text[:Config.audio_name_len]
        text_clean = re.sub(r'[\\/:\"*?<>|]', ' ', text_clean)

        file_stem = f'({time_ymdhms}){text_clean}'
        new_path = self.file_path.with_name(file_stem + self.file_path.suffix)

        try:
            self.file_path.rename(new_path)
            logger.debug(f"音频文件已重命名: {self.file_path.name} -> {new_path.name}")
            self.file_path = new_path
            return new_path
        except Exception as e:
            logger.error(f"重命名音频文件失败: {e}")
            return self.file_path
