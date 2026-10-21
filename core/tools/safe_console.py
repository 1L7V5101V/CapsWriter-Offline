# coding: utf-8
"""写入失败也不会打断主流程的 rich ``Console``。

本项目多处 ``console.print`` 位于识别主链路上（例如
``core/server/worker/pipeline.py`` 的 ``TaskPipeline.process``）。而进程的
stdout 并不总是可写：

- 启动器曾用管道承接服务端 stdout，启动器窗口一关，管道读端消失，
  服务端 fork 出来的识别子进程再写 stdout 就抛
  ``OSError: [Errno 22] Invalid argument``；
- 用户手动关掉控制台窗口，也会让 stdout 句柄失效。

rich 默认把这类异常直接抛给调用方，于是「控制台输出」这种纯展示行为会把
整条识别链路打断：日志里只有 ``推理管线错误 / 任务执行出错``，客户端永远
收不到最终结果。这里把写入异常降级为「丢弃这次输出」。

注意：``Console.print`` 并不是唯一的写入点，``Console.log`` 会直接往
``self._buffer`` 里塞内容。所以真正的收口是 ``_check_buffer``，它被
``_exit_buffer`` 调用，覆盖 print / log / rule / capture 等全部出口。
"""

from __future__ import annotations

import os
import sys

from rich.console import Console


class SafeConsole(Console):
    """rich ``Console`` 的容错版本：输出写不出去时静默丢弃。"""

    # ------------------------------------------------------------------
    # 写入收口
    # ------------------------------------------------------------------
    def _check_buffer(self) -> None:  # type: ignore[override]
        """所有输出最终都汇聚到这里；写入失败时清空缓冲而不是抛出。"""
        try:
            super()._check_buffer()
        except Exception:
            self._clear_buffer()

    # ------------------------------------------------------------------
    # 兜底：渲染阶段自己就出错的场景（例如标记语法异常）
    # ------------------------------------------------------------------
    def print(self, *args, **kwargs):  # type: ignore[override]
        try:
            return super().print(*args, **kwargs)
        except Exception:
            self._clear_buffer()
            return None

    def log(self, *args, **kwargs):  # type: ignore[override]
        try:
            return super().log(*args, **kwargs)
        except Exception:
            self._clear_buffer()
            return None

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _clear_buffer(self) -> None:
        """丢掉残留缓冲。

        ``_exit_buffer`` 已经把 ``_buffer_index`` 减过了，这里**不能**
        再动它，否则缓冲区计数会错乱，所以只清内容。
        """
        try:
            del self._buffer[:]
        except Exception:
            pass

    def on_broken_pipe(self) -> None:  # type: ignore[override]
        """管道断开时别把进程干掉。

        rich 的默认实现会 ``raise SystemExit(1)``（面向命令行工具被
        ``head`` 截断的场景），但这里的 Console 属于长驻的识别服务与客户端，
        退出等于整个功能失效。改成把 stdout 接到 devnull 并静默输出。
        """
        self.quiet = True
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except Exception:
            pass
