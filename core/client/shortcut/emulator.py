# coding: utf-8
"""
按键模拟器

负责异步模拟键盘和鼠标按键输入
"""

import time

from pynput import keyboard, mouse
from . import logger
from core.client.shortcut.key_mapper import KeyMapper



class ShortcutEmulator:
    """
    快捷键模拟器

    使用常驻的 controller 对象，避免重复创建开销
    """

    def __init__(self):
        """初始化模拟器"""
        self._keyboard_controller = keyboard.Controller()
        self._mouse_controller = mouse.Controller()
        self._emulating_keys = set()

    # ========== 跨模块自造输入标记 ==========
    # 其它模块（如 Ctrl+V 粘贴）自己发出的按键，也要让快捷键监听器放过，
    # 否则 'ctrl' 这类修饰键快捷键会被程序自己的粘贴动作误触发。

    _synthetic_marks: dict[str, float] = {}

    @classmethod
    def mark_synthetic(cls, key_name: str, ttl: float = 1.0) -> None:
        """
        标记即将由程序自身发出的按键

        Args:
            key_name: 按键名称（如 'ctrl'）
            ttl: 标记有效期（秒），过期自动失效，防止误标导致按键永久失效
        """
        cls._synthetic_marks[key_name] = time.time() + ttl

    @classmethod
    def _is_synthetic_active(cls, key_name: str) -> bool:
        """判断某个按键是否有未过期的自造输入标记"""
        deadline = cls._synthetic_marks.get(key_name)
        if deadline is None:
            return False
        if time.time() > deadline:
            cls._synthetic_marks.pop(key_name, None)
            return False
        return True

    def is_emulating(self, key_name: str) -> bool:
        """检查是否正在模拟指定按键"""
        return key_name in self._emulating_keys or self._is_synthetic_active(key_name)

    def clear_emulating_flag(self, key_name: str) -> None:
        """清除模拟标志"""
        self._emulating_keys.discard(key_name)
        ShortcutEmulator._synthetic_marks.pop(key_name, None)

    def emulate_key(self, key_name: str) -> None:
        """
        异步模拟键盘按键

        Args:
            key_name: 按键名称（如 'caps_lock', 'f12'）
        """
        self._emulating_keys.add(key_name)

        key_obj = KeyMapper.name_to_key(key_name)
        if key_obj is not None:
            self._keyboard_controller.press(key_obj)
            self._keyboard_controller.release(key_obj)
            logger.debug(f"[{key_name}] 补发按键成功")
        else:
            logger.warning(f"[{key_name}] 无法识别的按键，跳过补发")

    def emulate_mouse_click(self, button_name: str) -> None:
        """
        异步模拟鼠标按键

        Args:
            button_name: 鼠标按键名称（'x1' 或 'x2'）
        """
        self._emulating_keys.add(button_name)

        # pynput 鼠标按键对象映射
        button_map = {
            'x1': mouse.Button.x1,
            'x2': mouse.Button.x2
        }

        if button_name in button_map:
            button = button_map[button_name]
            self._mouse_controller.press(button)
            self._mouse_controller.release(button)
            logger.debug(f"[{button_name}] 补发鼠标按键成功")
        else:
            logger.warning(f"[{button_name}] 无法识别的鼠标按键，跳过补发")
