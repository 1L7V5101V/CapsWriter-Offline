# coding: utf-8
from core.runtime import setup_runtime

# 打包版是无控制台的 GUI 程序，子进程（ffmpeg / node / git …）会各自新建一个
# 控制台黑窗并抢走焦点。必须在导入 core.client 之前装好兜底补丁 ——
# 否则 websockets 等库在 import 期就可能已经拉起过子进程。
from core.tools.no_window import install_no_window_default

setup_runtime("client")
install_no_window_default()

from core.client import CapsWriterClient  # noqa: E402


if __name__ == "__main__":
    CapsWriterClient().start()