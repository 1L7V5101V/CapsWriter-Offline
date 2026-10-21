# coding: utf-8
"""
CapsWriter-Offline 一键启动器
================================================================

双击 ``CapsWriter-Offline.exe`` 后依次完成：

1. 在 exe 同级目录拉起 ``start_server.exe``（隐藏控制台，输出被捕获到本窗口）
2. 轮询 ``127.0.0.1:<port>``，端口可连接即代表服务就绪
3. 拉起 ``start_client.exe``（灵动岛 + 托盘），随后自动关闭本窗口

为什么"端口可连接"就等于"服务就绪"？
``core/server/app.py`` 的 ``CapsWriterServer.start()`` 顺序是：
    tray_manager.start() -> _print_banner() -> process_manager.start()
    -> socket_manager.start()
其中 ``process_manager.start()`` 内部会 ``_wait_for_models()`` 阻塞轮询，
直到子进程把模型全部加载完才返回；**端口 bind 发生在其之后**。
因此 TCP 能连上端口，就说明模型已经加载完毕、ASR 服务可用。

若服务端提前退出（通常是模型文件缺失），本窗口会展示捕获到的输出，
并提供"打开模型下载页"与"打开日志目录"两个入口。
"""

from __future__ import annotations

import importlib.util
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

# ======================================================================
# 常量
# ======================================================================

DEFAULT_PORT = 6016
FALLBACK_MODELS_PAGE = 'https://github.com/HaujetZhao/CapsWriter-Offline/releases/tag/models'
HOST = '127.0.0.1'

# Windows 进程创建标志
CREATE_NO_WINDOW = 0x08000000         # 不弹出控制台窗口
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008

# 服务端从"拉起"到"端口可连接"的等待上限（秒）
READY_TIMEOUT = 600
POLL_INTERVAL = 0.5

# 服务端控制台输出落盘的文件名（位于发行目录 logs/ 下）。
# 必须是真实文件而不是管道，否则启动器退出后识别子进程会写 stdout 失败。
SERVER_CONSOLE_LOG = 'server_console.log'

_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')


# ======================================================================
# 路径与配置辅助
# ======================================================================

def _base_dir() -> Path:
    """发行根目录：打包后是 exe 所在目录，源码运行时是脚本所在目录。"""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _find_exe(base: Path, name: str) -> Path | None:
    """在发行根目录下查找可执行文件（优先带 .exe 后缀）。"""
    for candidate in (base / f'{name}.exe', base / name, base / 'internal' / f'{name}.exe'):
        if candidate.exists():
            return candidate
    return None


def _exec_config(base: Path, filename: str, module_name: str):
    """以 importlib 方式加载根目录下的配置文件，避免污染 sys.modules。"""
    path = base / filename
    if not path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location(module_name, str(path))
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def _client_port(base: Path) -> int:
    """从 config_client.py 读取客户端连接端口，失败则回退默认值。"""
    module = _exec_config(base, 'config_client.py', '_launcher_config_client')
    if module is not None:
        cfg = getattr(module, 'ClientConfig', None)
        raw = getattr(cfg, 'port', None) if cfg is not None else None
        try:
            if raw is not None:
                return int(str(raw).strip())
        except (TypeError, ValueError):
            pass
    return DEFAULT_PORT


def _models_page(base: Path) -> str:
    """从 config_server.py 读取模型发布页链接，失败则回退默认值。"""
    module = _exec_config(base, 'config_server.py', '_launcher_config_server')
    if module is not None:
        links = getattr(module, 'ModelDownloadLinks', None)
        url = getattr(links, 'models_page', None) if links is not None else None
        if isinstance(url, str) and url.startswith('http'):
            return url
    return FALLBACK_MODELS_PAGE


def _port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    """端口是否可连接。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub('', text)


def _kill_tree(pid: int) -> None:
    """按进程树强制结束（PyInstaller onedir 启动的进程是父子两层）。"""
    try:
        subprocess.run(
            ['taskkill', '/T', '/F', '/PID', str(pid)],
            capture_output=True, creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        pass


def _process_running(image_name: str) -> bool:
    """image_name 形如 ``start_client.exe``，判定同名进程是否已在运行。"""
    try:
        result = subprocess.run(
            ['tasklist', '/FI', f'IMAGENAME eq {image_name}', '/NH'],
            capture_output=True, text=True, timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return False
    return image_name.lower() in (result.stdout or '').lower()


_MUTEX_HANDLE = None


def _single_instance() -> bool:
    """
    单实例保护：用命名互斥体防止重复双击。

    返回 True 表示本进程是唯一实例；返回 False 表示已有启动器在运行。
    互斥体句柄在进程退出时由系统释放，因此不需要手动关闭。
    """
    global _MUTEX_HANDLE
    if os.name != 'nt':
        return True
    try:
        import ctypes
        ERROR_ALREADY_EXISTS = 183
        kernel32 = ctypes.windll.kernel32
        _MUTEX_HANDLE = kernel32.CreateMutexW(None, False, 'Local\\CapsWriterOfflineLauncher')
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            return False
    except Exception:
        return True
    return True


def _child_env(base: Path) -> dict:
    """为子进程准备环境变量：强制 UTF-8，避免中文/emoji 在管道里编码报错。"""
    env = os.environ.copy()
    env['PYTHONUTF8'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUNBUFFERED'] = '1'
    env['PYTHONLEGACYWINDOWSSTDIO'] = '0'
    env['PYTHONPATH'] = str(base) + os.pathsep + env.get('PYTHONPATH', '')
    return env


# ======================================================================
# 启动器 GUI
# ======================================================================

class Launcher:
    """Tk 进度窗口 + 后台启动流程。"""

    def __init__(self) -> None:
        self.base = _base_dir()
        self.log_dir = self.base / 'logs'
        self.port = _client_port(self.base)
        self.models_page = _models_page(self.base)

        self.queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.server_proc: subprocess.Popen | None = None
        self.client_proc: subprocess.Popen | None = None
        # 服务端控制台输出落盘到文件（不是管道），并在本窗口里 tail 展示
        self.server_log_path = self.log_dir / SERVER_CONSOLE_LOG
        self._server_log_handle = None
        self._tail_stop = threading.Event()
        self.finished = False
        self.failed = False
        self._auto_close_job = None
        self._countdown: int | None = None

        self._build_ui()
        self._log_system(
            f'发行目录: {self.base}\n'
            f'服务端口: {HOST}:{self.port}\n'
            f'启动时间: {datetime.now():%Y-%m-%d %H:%M:%S}'
        )
        self.root.after(200, self._start_worker)

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.root = tk.Tk()
        self.root.title('CapsWriter-Offline 启动器')
        self.root.geometry('780x480')
        self.root.minsize(620, 380)
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

        icon = self.base / 'assets' / 'icon.ico'
        if icon.exists():
            try:
                self.root.iconbitmap(default=str(icon))
            except Exception:
                pass

        container = ttk.Frame(self.root, padding=14)
        container.pack(fill='both', expand=True)

        # 标题
        ttk.Label(
            container, text='CapsWriter-Offline', font=('Microsoft YaHei UI', 15, 'bold')
        ).pack(anchor='w')
        ttk.Label(
            container, text='本地离线语音输入 · 一键启动', foreground='#666666'
        ).pack(anchor='w', pady=(0, 10))

        # 状态行
        status_frame = ttk.Frame(container)
        status_frame.pack(fill='x')
        self.status_var = tk.StringVar(value='准备启动…')
        ttk.Label(
            status_frame, textvariable=self.status_var,
            font=('Microsoft YaHei UI', 10, 'bold'),
        ).pack(anchor='w')

        self.progress = ttk.Progressbar(container, mode='indeterminate')
        self.progress.pack(fill='x', pady=(6, 10))
        self.progress.start(14)

        # 日志区
        log_frame = ttk.LabelFrame(container, text='服务端输出', padding=6)
        log_frame.pack(fill='both', expand=True)
        self.log_view = ScrolledText(
            log_frame, height=14, wrap='none', font=('Consolas', 9),
            background='#1e1e1e', foreground='#d4d4d4', insertbackground='#d4d4d4',
            relief='flat', state='disabled',
        )
        self.log_view.pack(fill='both', expand=True)

        # 按钮行
        buttons = ttk.Frame(container)
        buttons.pack(fill='x', pady=(10, 0))

        self.open_log_btn = ttk.Button(buttons, text='打开日志目录', command=self._open_log_dir)
        self.open_log_btn.pack(side='left')

        self.open_models_btn = ttk.Button(
            buttons, text='打开模型下载页', command=self._open_models_page
        )
        self.open_models_btn.pack(side='left', padx=(8, 0))

        self.close_btn = ttk.Button(buttons, text='关闭', command=self._on_close)
        self.close_btn.pack(side='right')

        self.root.after(120, self._drain_queue)

    # ------------------------------------------------------------------
    # 队列 -> 界面
    # ------------------------------------------------------------------

    def _log_system(self, text: str) -> None:
        self.queue.put(('log', _strip_ansi(text)))

    def _append_log(self, text: str) -> None:
        self.log_view.configure(state='normal')
        self.log_view.insert('end', text if text.endswith('\n') else text + '\n')
        self.log_view.see('end')
        self.log_view.configure(state='disabled')

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == 'log':
                    self._append_log(payload)
                elif kind == 'status':
                    self.status_var.set(payload)
                elif kind == 'done':
                    self._on_success()
                elif kind == 'fail':
                    self._on_failure(payload)
        except queue.Empty:
            pass
        if not self.finished:
            self.root.after(120, self._drain_queue)

    # ------------------------------------------------------------------
    # 缓冲日志线程
    # ------------------------------------------------------------------

    @staticmethod
    def _tail_file(path: Path, sink: queue.Queue, stop: threading.Event) -> None:
        """跟随文件新增内容（类似 tail -f），逐行投递到队列。

        服务端的 stdout 之所以不接管道：服务端还会 fork 出识别子进程，
        两者共用同一个 stdout 句柄。本启动器窗口一关，管道读端就没了，
        识别子进程之后任何一次 ``console.print`` 都会抛
        ``OSError: [Errno 22] Invalid argument``，直接把整条识别链路打断。
        写成真实文件后，句柄在启动器退出后依然长期有效。
        """
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as fp:
                while not stop.is_set():
                    line = fp.readline()
                    if line:
                        sink.put(('log', line.rstrip('\r\n')))
                    else:
                        time.sleep(0.15)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 启动流程（后台线程）
    # ------------------------------------------------------------------

    def _start_worker(self) -> None:
        thread = threading.Thread(target=self._run, name='launcher', daemon=True)
        thread.start()

    def _run(self) -> None:
        try:
            self._run_impl()
        except Exception:
            detail = traceback.format_exc()
            self._log_system(detail)
            self.queue.put(('fail', '启动过程出现未预期的错误'))

    def _run_impl(self) -> None:
        # ---- 1. 服务端是否已经在跑 ----
        if _port_open(HOST, self.port):
            self.queue.put(('status', f'检测到服务已在运行（{HOST}:{self.port}）'))
            self._log_system(f'端口 {HOST}:{self.port} 已可连接，跳过启动服务端。')
        else:
            server_exe = _find_exe(self.base, 'start_server')
            if server_exe is None:
                self.queue.put(('fail', '未找到 start_server.exe，发行包似乎不完整'))
                return

            self.queue.put(('status', '正在启动服务端并加载模型…（首次启动较慢）'))
            self._log_system(f'启动服务端: {server_exe}')
            self.server_proc = self._spawn_server(server_exe)

            pump = threading.Thread(
                target=self._tail_file,
                args=(self.server_log_path, self.queue, self._tail_stop),
                daemon=True,
            )
            pump.start()

            if not self._wait_ready():
                return

        # ---- 2. 客户端 ----
        if _process_running('start_client.exe'):
            self.queue.put(('status', '客户端已在运行，无需重复启动'))
            self._log_system('检测到 start_client.exe 已在运行，跳过启动，避免重复上屏。')
            self.queue.put(('done', ''))
            return

        client_exe = _find_exe(self.base, 'start_client')
        if client_exe is None:
            self.queue.put(('fail', '未找到 start_client.exe，发行包似乎不完整'))
            return

        self.queue.put(('status', '模型加载完成，正在启动客户端…'))
        self._log_system(f'启动客户端: {client_exe}')
        self.client_proc = subprocess.Popen(
            [str(client_exe)],
            cwd=str(self.base),
            env=_child_env(self.base),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )

        time.sleep(1.0)
        if self.client_proc.poll() is not None:
            self.queue.put(('fail', f'客户端启动后立即退出（ExitCode: {self.client_proc.returncode}）'))
            return

        self.queue.put(('done', ''))

    def _spawn_server(self, server_exe: Path) -> subprocess.Popen:
        """
        拉起服务端。

        使用 ``CREATE_NO_WINDOW`` 隐藏控制台（避免黑窗闪烁），并把 stdout/stderr
        追加写入 ``logs/server_console.log``，由 ``_tail_file`` 读出来展示。

        **不要改回管道**：服务端还会 fork 出识别子进程，两者共用同一个
        stdout 句柄；本窗口一关管道读端就消失，识别子进程之后每次
        ``console.print`` 都会抛 ``OSError: [Errno 22] Invalid argument``，
        ``TaskPipeline.process`` 直接失败，结果是「录音有波形、但永远不上屏」。

        stdin 指向 devnull，同时 ``core/server/worker/process_manager.py``
        已对 ``sys.stdin is None`` 做了兜底。
        """
        self.server_log_path.parent.mkdir(parents=True, exist_ok=True)
        # 每次启动截断旧内容，避免把上一次运行的控制台输出重放到界面上
        self._server_log_handle = open(
            self.server_log_path, 'w', encoding='utf-8', errors='replace', buffering=1
        )
        self._server_log_handle.write(
            f'===== 启动器于 {datetime.now():%Y-%m-%d %H:%M:%S} 拉起服务端 =====\n'
        )
        return subprocess.Popen(
            [str(server_exe)],
            cwd=str(self.base),
            env=_child_env(self.base),
            stdin=subprocess.DEVNULL,
            stdout=self._server_log_handle,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )

    def _wait_ready(self) -> bool:
        """轮询端口等待服务就绪；服务端提前退出则判定失败。"""
        assert self.server_proc is not None
        deadline = time.time() + READY_TIMEOUT
        dots = 0
        while time.time() < deadline:
            if _port_open(HOST, self.port):
                elapsed = READY_TIMEOUT - (deadline - time.time())
                self._log_system(f'服务端就绪，耗时约 {elapsed:.1f}s')
                return True

            code = self.server_proc.poll()
            if code is not None:
                self._log_system(f'服务端进程已退出（ExitCode: {code}）')
                self.queue.put(('fail', self._diagnose_exit(code)))
                return False

            dots = (dots + 1) % 20
            self.queue.put(('status', f'正在加载模型{"." * dots}'))
            time.sleep(POLL_INTERVAL)

        self.queue.put(('fail', f'等待服务端就绪超时（超过 {READY_TIMEOUT}s）'))
        return False

    @staticmethod
    def _diagnose_exit(code: int) -> str:
        if code == 0:
            return '服务端正常退出，但服务未就绪，请查看日志排查'
        return (
            '服务端启动失败（最常见原因：models/ 下缺少模型文件）。\n'
            '请点击「打开模型下载页」下载模型，解压到 models/ 后重试；\n'
            '详细报错见「打开日志目录」中的 server_latest.log。'
        )

    # ------------------------------------------------------------------
    # 收尾
    # ------------------------------------------------------------------

    def _on_success(self) -> None:
        self.finished = True
        self.progress.stop()
        self.status_var.set('启动完成，客户端已运行')
        self._append_log(
            '\n' + '=' * 60 + '\n'
            '启动完成。\n'
            '  · 托盘图标在任务栏通知区域的 ^ 溢出区（Windows 10 默认如此），\n'
            '    可以把它从溢出区拖到可见区域固定下来。\n'
            '  · 服务端没有控制台窗口，退出请用托盘图标右键菜单。\n'
            + '=' * 60 + '\n'
        )
        self._countdown = 3
        self._tick_autoclose()

    def _tick_autoclose(self) -> None:
        if self._countdown is None:
            return
        if self._countdown <= 0:
            self._destroy()
            return
        self.close_btn.configure(text=f'{self._countdown} 秒后自动关闭')
        self._countdown -= 1
        self._auto_close_job = self.root.after(1000, self._tick_autoclose)

    def _on_failure(self, message: str) -> None:
        self.finished = True
        self.failed = True
        self.progress.stop()
        self.status_var.set(message.splitlines()[0] if message else '启动失败')
        self._append_log('\n' + '=' * 60)
        for line in message.splitlines():
            self._append_log(line)
        self._append_log('=' * 60 + '\n')
        self.close_btn.configure(text='关闭')

    def _on_close(self) -> None:
        if not self.finished:
            # 启动过程中关闭：终止仍在加载的服务端，避免留下孤儿进程。
            # PyInstaller 的 onedir bootloader 是「父进程 + 子进程」结构，
            # 单纯 terminate() 只会杀掉父进程，必须按进程树清理。
            if self.server_proc is not None and self.server_proc.poll() is None:
                _kill_tree(self.server_proc.pid)
        self._destroy()

    def _destroy(self) -> None:
        # 停止 tail 线程并释放服务端 stdout 的文件句柄。
        # 服务端进程自己持有同一句柄的另一份拷贝，所以这里关掉不影响它继续写日志。
        self._tail_stop.set()
        if self._server_log_handle is not None:
            try:
                self._server_log_handle.close()
            except Exception:
                pass
            self._server_log_handle = None
        if self._auto_close_job is not None:
            try:
                self.root.after_cancel(self._auto_close_job)
            except Exception:
                pass
            self._auto_close_job = None
        try:
            self.root.destroy()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 外部工具入口
    # ------------------------------------------------------------------

    def _open_log_dir(self) -> None:
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            os.startfile(str(self.log_dir))  # noqa: S606  (Windows 专用)
        except Exception:
            try:
                subprocess.Popen(['explorer', str(self.log_dir)])
            except Exception:
                pass

    def _open_models_page(self) -> None:
        try:
            webbrowser.open(self.models_page)
        except Exception:
            pass

    # ------------------------------------------------------------------

    def run(self) -> None:
        self.root.mainloop()


def _notify_already_running() -> None:
    """已有一个启动器在运行时的小提示窗（2.5 秒后自动消失，不阻塞）。"""
    try:
        import tkinter as tk
        root = tk.Tk()
        root.title('CapsWriter-Offline')
        root.geometry('380x150')
        root.resizable(False, False)
        root.attributes('-topmost', True)
        tk.Label(root, text='启动器已经在运行了',
                 font=('Microsoft YaHei UI', 12, 'bold')).pack(pady=(26, 8))
        tk.Label(root, text='请查看已打开的启动器窗口。',
                 foreground='#666666').pack()
        root.after(2500, root.destroy)
        root.mainloop()
    except Exception:
        pass


def main() -> int:
    if not _single_instance():
        _notify_already_running()
        return 0
    try:
        Launcher().run()
        return 0
    except Exception:
        # 启动器自身崩了，至少把错误写到 logs/launcher.log 并弹窗告知
        base = _base_dir()
        detail = traceback.format_exc()
        try:
            log_dir = base / 'logs'
            log_dir.mkdir(parents=True, exist_ok=True)
            with open(log_dir / 'launcher.log', 'a', encoding='utf-8') as fp:
                fp.write(f'\n[{datetime.now():%Y-%m-%d %H:%M:%S}]\n{detail}\n')
        except Exception:
            pass
        try:
            import tkinter.messagebox as messagebox
            messagebox.showerror('CapsWriter-Offline 启动器', f'启动器异常退出：\n\n{detail[-1200:]}')
        except Exception:
            pass
        return 1


if __name__ == '__main__':
    sys.exit(main())
