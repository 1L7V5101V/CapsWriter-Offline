# coding: utf-8
import os
from . import logger
import os, sys, subprocess
from config_client import ClientConfig as Config


class TrayManager:
    """
    托盘管理器：负责系统托盘图标的初始化、菜单构建及回调处理。
    """
    def __init__(self, app):
        self.app = app

    @property
    def state(self):
        return self.app.state

    def start(self):
        """初始化系统托盘图标"""
        if not Config.enable_tray:
            return

        try:
            from ..ui import enable_min_to_tray
        except ImportError as e:
            logger.warning(f"托盘模块导入失败，跳过托盘功能: {e}")
            return

        # 构建额外菜单项
        more = [
            ('📋 复制结果', self._copy_last_result),
            ('📝 上下文', self._add_context),
            ('✨ 热词', self._add_hotword),
            ('🧹 清除记忆', self._clear_memory),
            ('♻️ 重开音频', self._restart_audio),
        ]

        # 系统音频输出设备选择子菜单
        if Config.system_audio_enabled:
            dev_menu = self._build_loopback_menu()
            if dev_menu:
                from pystray import MenuItem as _MI
                more.append(_MI('🔊 切换输出设备', dev_menu))

        # 获取图标路径
        icon_path = os.path.join(self.app.base_dir, 'assets', 'icon.ico')

        # 启用托盘
        enable_min_to_tray(
            'CapsWriter Client',
            icon_path,
            exit_callback=self.app.stop,
            more_options=more,
        )
        logger.info("托盘图标已启用")

    def stop(self):
        """停止托盘图标"""
        if not Config.enable_tray:
            return
            
        try:
            from ..ui import stop_tray
            stop_tray()
            logger.info("TrayManager: 托盘图标已卸载")
        except Exception as e:
            logger.debug(f"TrayManager: 卸载托盘时发生错误: {e}")

    def _build_loopback_menu(self):
        """构建输出设备选择子菜单（动态生成，每次展开时刷新设备与选中标记）"""
        from pystray import Menu as _Menu

        return _Menu(lambda: self._loopback_menu_items())

    def _loopback_menu_items(self) -> list:
        """生成输出设备菜单项（pystray 每次展开菜单时调用）"""
        from pystray import MenuItem as _MI

        try:
            devices = self.app.stream.ensure_loopback_cache(max_age=2.0)
        except Exception as e:
            logger.warning(f"获取输出设备列表失败: {e}")
            devices = []

        if not devices:
            return [_MI('（无可用输出设备）', None, enabled=False)]

        items = []
        for dev in devices:
            name = dev['name']
            idx = dev['idx']
            label = f"● {name}" if dev.get('default') else f"  {name}"
            items.append(_MI(label, self._make_switch_action(idx)))
        return items

    def _make_switch_action(self, device_idx: int):
        """生成设备切换回调

        pystray 会把``(icon, item)`` 传给菜单回调；如果回调恰好只接收 1 个
        位置参数，pystray 会把它当成 ``action(icon)`` 调用，从而导致
        参数错位（设备索引变成 Icon 对象）。因此这里统一用无位置参数的
        闭包，并把索引固化在闭包里。
        """

        def _action(*_args: object) -> None:
            self._on_switch_device(device_idx)

        return _action

    def _on_switch_device(self, device_idx: int):
        """切换输出设备回调"""
        try:
            if hasattr(self.app, 'stream') and self.app.stream:
                self.app.stream.switch_loopback_device(device_idx)
                logger.info(f"用户切换输出设备: idx={device_idx}")
        except Exception as e:
            logger.error(f"切换输出设备失败: {e}")
    def _restart_audio(self):
        """重启音频流回调"""
        if hasattr(self.app, 'stream') and self.app.stream:
            self.app.stream.reopen()
            logger.info("用户请求重启音频")

    def _clear_memory(self):
        """清除 LLM 对话历史回调"""
        from ..ui import toast
        if self.app.llm:
            self.app.llm.clear_history()
            toast("清除成功：已清除所有角色的对话历史记录", duration=3000, bg="#075077")

    def _add_hotword(self):
        """用系统默认方式打开热词文件回调"""
        
        target = os.path.abspath('hot.txt')
        if sys.platform == 'win32':
            os.startfile(target)
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', target])
        else:
            subprocess.Popen(['xdg-open', target])

    def _add_context(self):
        """打开编辑上下文界面回调"""
        try:
            from ..ui import on_edit_context
            on_edit_context()
        except ImportError as e:
            logger.warning(f"无法导入上下文菜单处理器: {e}")

    def _copy_last_result(self):
        """复制最后一次识别结果到剪贴板回调"""
        text = self.state.last_output_text
        if text:
            from ..llm.llm_clipboard import copy_to_clipboard
            copy_to_clipboard(text)

    def _request_exit(self, icon=None, item=None):
        """托盘图标引用的退出回调"""
        logger.info("托盘退出: 用户点击退出菜单，准备清理资源并退出")
        self.app.stop()
