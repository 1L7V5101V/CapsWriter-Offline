# coding: utf-8
"""Windows 下隐藏子进程控制台窗口的通用标志。

打包成「无控制台」的 GUI 程序之后（PyInstaller ``console=False``），
再启动控制台子程序（``ffmpeg`` / ``node`` / ``ffprobe`` 等）时，Windows
会为它们**新建一个控制台窗口**。表现为黑窗一闪而过，更麻烦的是新窗口
会抢走前台焦点，导致语音识别出来的文字上屏到错误的窗口里。

源码运行时父进程本身有控制台，子进程会附着到同一个控制台，所以看不到
这个现象 —— 这是只在打包版才会暴露的坑。

用法::

    from core.tools.no_window import NO_WINDOW
    subprocess.Popen(cmd, creationflags=NO_WINDOW)
    await asyncio.create_subprocess_exec(*cmd, creationflags=NO_WINDOW)
"""

from __future__ import annotations

import sys

# Windows 的 CREATE_NO_WINDOW = 0x08000000。
# 其他平台填 0（Popen / create_subprocess_exec 都接受 0，等价于不设置）。
NO_WINDOW: int = 0x08000000 if sys.platform == 'win32' else 0

# CREATE_NEW_CONSOLE / DETACHED_PROCESS 与 CREATE_NO_WINDOW 互斥，
# 调用方显式要求这两种行为时不能强行叠加。
_NEW_CONSOLE = 0x00000010
_DETACHED = 0x00000008

_PATCHED = False


def install_no_window_default() -> bool:
    """兜底：把 ``subprocess.Popen`` 包一层，自动补上 ``CREATE_NO_WINDOW``。

    即使某处（包括第三方库）忘了传 ``creationflags``，也不会再弹出黑窗。
    只在 **Windows 且本进程确实没有控制台窗口** 时生效，所以：

    * 源码运行时（有控制台）完全不受影响；
    * 打包后的 ``start_client.exe``（``console=False``）自动获得保护。

    返回是否真的安装了补丁。
    """
    global _PATCHED
    if sys.platform != 'win32' or _PATCHED:
        return False

    try:
        import ctypes
        if ctypes.windll.kernel32.GetConsoleWindow():
            # 本进程有可见的控制台，子进程会附着上去，不会新建窗口。
            return False
    except Exception:
        return False

    import subprocess

    class _NoWindowPopen(subprocess.Popen):  # type: ignore[misc]
        def __init__(self, *args, **kwargs):
            flags = kwargs.get('creationflags') or 0
            if not flags & (_NEW_CONSOLE | _DETACHED):
                kwargs['creationflags'] = flags | NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = _NoWindowPopen  # type: ignore[assignment]
    _PATCHED = True
    return True

