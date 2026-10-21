# coding: utf-8
"""
PyInstaller 运行时钩子

作用：把 exe 所在目录（发行根目录）加入 sys.path，使 `core/`、`LLM/`、
`config_client.py`、`config_server.py` 这些以【源码】形式保留在发行包里的
私有模块可以被正常 import。

这样做的原因：
1. 引擎资源（llama.cpp 的 DLL、korean_dict_jieba.dict、zhcdict.json 等）
   全部靠 `Path(__file__).parent` 定位 —— 只有保留源码目录结构才能找到；
2. LLM 角色通过 `__import__(f"LLM.{stem}")` 动态导入；
3. 保留"源码即发行"的项目定位，用户可自行修改源码。
"""

import sys
from os.path import dirname, exists, join

executable_dir = dirname(sys.executable)
if exists(executable_dir) and executable_dir not in sys.path:
    sys.path.insert(0, executable_dir)

# PyInstaller onedir 模式下二进制/扩展模块放在 internal/ 子目录
internal_dir = join(executable_dir, 'internal')
if exists(internal_dir) and internal_dir not in sys.path:
    sys.path.insert(0, internal_dir)
