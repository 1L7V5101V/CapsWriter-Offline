# coding: utf-8
"""
CapsWriter-Offline 绿色免安装版 —— 一键构建脚本
================================================================

用法（在项目根目录执行）::

    python build_portable.py                    # 默认：models/ 用 junction 链接（不占额外空间）
    python build_portable.py --models copy      # models/ 整个复制进发行包（可直接拷给别人）
    python build_portable.py --models none      # 发行包不含 models/（由用户自行放置）
    python build_portable.py --clean            # 构建前先删掉 build/ 与 dist/
    python build_portable.py --with-node        # 额外打包 node.exe + native-recorder-nodejs
    python build_portable.py --no-build         # 跳过 PyInstaller，只做产物整理（调试用）

产物::

    dist/CapsWriter-Offline/
        CapsWriter-Offline.exe   启动器（双击这个）
        start_server.exe         服务端（由启动器拉起）
        start_client.exe         客户端（由启动器拉起）
        internal/                便携式 Python 运行时 + 第三方依赖
        core/  LLM/  assets/     保留为源码，用户可修改
        config_client.py  config_server.py
        hot.txt  hot-rule.txt  hot-server.txt
        models/                  语音模型（junction 链接或复制）
        logs/                    运行日志
        启动 CapsWriter.bat       启动器不可用时的命令行兜底方案
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST_ROOT = ROOT / 'dist' / 'CapsWriter-Offline'
BUILD_ROOT = ROOT / 'build'

# 需要复制到发行根目录、作为【源码】保留的条目
SOURCE_ITEMS = [
    'core',
    'LLM',
    'config_client.py',
    'config_server.py',
    'hot.txt',
    'hot-rule.txt',
    'hot-server.txt',
]
SOURCE_FILES = [
    'readme.md',
    'LICENSE',
]
IGNORE = shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo', '.pytest_cache')

# models/ 下的模型目录名 → 用于 size 统计展示
MODEL_ITEMS = ['models']

SEP = '=' * 66


def log(msg: str = '') -> None:
    print(f'[打包] {msg}' if msg else '')


def human(size: float) -> str:
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return f'{size:.1f} {unit}'
        size /= 1024
    return f'{size:.1f} GB'


def models_size(path: Path) -> int:
    """复制模式下统计 models 体积（会走进目录）。"""
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(base) / name).stat().st_size
            except OSError:
                pass
    return total


def dir_size(path: Path) -> int:
    """统计目录体积；不跟随 junction / 符号链接（避免把 models 算进来）。"""
    total = 0
    for base, dirs, files in os.walk(path):
        if os.path.isjunction(base):       # Python 3.12+
            dirs[:] = []
            continue
        for name in files:
            try:
                total += (Path(base) / name).stat().st_size
            except OSError:
                pass
    return total


# ======================================================================
# 各步骤
# ======================================================================

def step_clean(args) -> None:
    if not args.clean:
        return
    log('清理 build/ 与 dist/ …')
    for target in (BUILD_ROOT, ROOT / 'dist'):
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)


def step_pyinstaller(args) -> None:
    if args.no_build:
        log('跳过 PyInstaller（--no-build）')
        return

    log('检查 PyInstaller …')
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        log('错误：当前 Python 环境没有安装 PyInstaller。')
        log('请执行：  .venv\\Scripts\\python.exe -m pip install pyinstaller')
        sys.exit(1)

    log('开始 PyInstaller 构建（首次约 3~10 分钟，请耐心等待）…')
    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--noconfirm',
        '--distpath', str(ROOT / 'dist'),
        '--workpath', str(BUILD_ROOT),
        str(ROOT / 'build.spec'),
    ]
    log('命令：' + ' '.join(cmd))
    started = time.time()
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        log(f'PyInstaller 构建失败（ExitCode: {result.returncode}）')
        sys.exit(result.returncode)
    log(f'PyInstaller 构建完成，耗时 {time.time() - started:.0f}s')


def step_copy_sources(args) -> None:
    if not DIST_ROOT.exists():
        log(f'错误：未找到产物目录 {DIST_ROOT}')
        sys.exit(1)

    log(f'复制源码资源到 {DIST_ROOT.name}/ …')
    for item in SOURCE_ITEMS:
        src = ROOT / item
        dst = DIST_ROOT / item
        if not src.exists():
            log(f'  跳过（不存在）: {item}')
            continue
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst, ignore=IGNORE)
        else:
            shutil.copy2(src, dst)
        log(f'  {item}')

    # assets/ 只需要运行时真正用到的图标（其余是演示素材，几十 MB）
    icon_src = ROOT / 'assets' / 'icon.ico'
    if icon_src.exists():
        (DIST_ROOT / 'assets').mkdir(parents=True, exist_ok=True)
        shutil.copy2(icon_src, DIST_ROOT / 'assets' / 'icon.ico')
        log('  assets/icon.ico')

    for name in SOURCE_FILES:
        src = ROOT / name
        if src.exists():
            shutil.copy2(src, DIST_ROOT / name)
            log(f'  {name}')

    (DIST_ROOT / 'logs').mkdir(parents=True, exist_ok=True)
    (DIST_ROOT / 'models').mkdir(parents=True, exist_ok=True)


def step_models(args) -> None:
    """按 --models 决定 models/ 的处理方式。"""
    src = ROOT / 'models'
    dst = DIST_ROOT / 'models'

    if args.models == 'none':
        log('models/：按 --models none 保持为空目录')
        return

    if not src.exists():
        log(f'警告：源目录不存在 {src}，models/ 将保持为空')
        return

    # 清空目标（可能是上次构建残留）
    if dst.exists() and not _is_junction(dst):
        shutil.rmtree(dst, ignore_errors=True)
    elif _is_junction(dst):
        _remove_junction(dst)

    if args.models == 'junction':
        if os.name != 'nt':
            log('警告：junction 仅支持 Windows，自动改用复制方式')
            shutil.copytree(src, dst, ignore=IGNORE)
        else:
            _make_junction(dst, src)
            log(f'models/ → junction 链接到 {src}（不占用额外空间）')
        return

    log('models/：正在复制模型（约 1 GB，请稍候）…')
    shutil.copytree(src, dst, ignore=IGNORE)
    log(f'models/：复制完成 {human(dir_size(dst))}')


def _is_junction(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        return bool(os.readlink(path))
    except OSError:
        return False
    except ValueError:
        return False


def _remove_junction(path: Path) -> None:
    try:
        os.rmdir(path)          # junction 是目录，但内容不会被删除
        return
    except OSError:
        pass
    subprocess.run(['cmd', '/c', 'rmdir', str(path)], capture_output=True)


def _make_junction(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists() or _is_junction(link):
        _remove_junction(link)
    try:
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
        return
    except Exception:
        pass
    result = subprocess.run(
        ['cmd', '/c', 'mklink', '/J', str(link), str(target)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log(f'错误：创建 junction 失败：{result.stdout} {result.stderr}')
        sys.exit(1)


def step_runtime_extras(args) -> None:
    """可选的运行时外部依赖（node.exe + native-recorder-nodejs）。"""
    if not args.with_node:
        return

    runtime = DIST_ROOT / 'runtime'
    node_dir = runtime / 'node'
    node_dir.mkdir(parents=True, exist_ok=True)

    node_exe = None
    for cand in (Path(r'D:/Node/node.exe'), Path(r'C:/Program Files/nodejs/node.exe')):
        if cand.exists():
            node_exe = cand
            break
    if node_exe is None:
        node_exe = shutil.which('node')
    if node_exe is None:
        log('警告：未找到 node.exe，跳过运行时打包（系统音频捕获将不可用）')
        return
    shutil.copy2(node_exe, node_dir / 'node.exe')
    log(f'runtime/node/node.exe ← {node_exe}')

    nm_src = Path(
        r'D:/.Projects/OpenCode/node_modules/.bun/'
        r'native-recorder-nodejs@1.2.0/node_modules/native-recorder-nodejs'
    )
    if not nm_src.exists():
        log(f'警告：未找到 native-recorder-nodejs 模块（{nm_src}），跳过')
        return
    nm_dst = runtime / 'node_modules' / 'native-recorder-nodejs'
    if nm_dst.exists():
        shutil.rmtree(nm_dst, ignore_errors=True)
    shutil.copytree(nm_src, nm_dst, ignore=IGNORE)
    log(f'runtime/node_modules/native-recorder-nodejs ← {nm_src}')


BAT_TEMPLATE = """@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo ============================================================
echo   CapsWriter-Offline 命令行启动（启动器不可用时的兜底方案）
echo ============================================================
echo.

if not exist "start_server.exe" (
    echo [错误] 未找到 start_server.exe，发行包似乎不完整。
    pause
    exit /b 1
)

echo [1/3] 启动服务端...
start "CapsWriter-Server" "start_server.exe"

echo [2/3] 等待模型加载完成（首次启动可能需要 1~3 分钟）...
:wait
powershell -NoProfile -Command "try{{$c=New-Object Net.Sockets.TcpClient;$c.Connect('127.0.0.1',{port});$c.Close();exit 0}}catch{{exit 1}}" >nul 2>&1
if errorlevel 1 (
    timeout /t 1 /nobreak >nul
    goto wait
)

echo [3/3] 服务端就绪，启动客户端...
start "" "start_client.exe"
echo.
echo 启动完成。服务端在独立的 "CapsWriter-Server" 窗口中运行，
echo 需要退出时请在该窗口按 Ctrl+C，或使用托盘图标菜单退出。
echo.
timeout /t 5 /nobreak >nul
exit /b 0
"""


def step_bat(args) -> None:
    port = 6016
    cfg = ROOT / 'config_client.py'
    if cfg.exists():
        try:
            text = cfg.read_text(encoding='utf-8', errors='replace')
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith('port') and '=' in stripped:
                    value = stripped.split('=', 1)[1].strip().strip("'\"")
                    if value.isdigit():
                        port = int(value)
                        break
        except OSError:
            pass

    bat = DIST_ROOT / '启动 CapsWriter.bat'
    bat.write_text(BAT_TEMPLATE.format(port=port), encoding='utf-8')
    log(f'写入兜底启动脚本 {bat.name}（端口 {port}）')


def step_console_hint(args) -> None:
    """在服务端 exe 旁放一个可选的"可见控制台"快捷方式记录。"""
    readme = DIST_ROOT / '打包说明.txt'
    readme.write_text(
        'CapsWriter-Offline 绿色免安装版\n'
        '=' * 40 + '\n\n'
        '启动方式\n'
        '  - 双击「CapsWriter-Offline.exe」：一键启动（推荐）\n'
        '  - 双击「启动 CapsWriter.bat」：启动器异常时的兜底方案\n'
        '  - 直接双击「start_server.exe」：可看到服务端完整日志（调试用）\n\n'
        '自定义\n'
        '  - 快捷键 / 音频 / 热词 / LLM：编辑 config_client.py 后重启客户端\n'
        '  - 模型选择 / 网络 / 格式化：编辑 config_server.py 后重启服务端\n'
        '  - LLM 角色：在 LLM\\ 目录增删 .py 文件\n'
        '  - 热词：hot.txt（音素匹配）、hot-rule.txt（正则替换）、hot-server.txt（服务端）\n\n'
        '模型放置\n'
        '  模型放在 models\\ 下，目录结构见 config_server.py 的 ModelPaths\n'
        '  下载页：见 config_server.py 的 ModelDownloadLinks.models_page\n\n'
        '日志\n'
        '  logs\\server_latest.log 与 logs\\client_latest.log\n',
        encoding='utf-8',
    )
    log('写入 打包说明.txt')


# ======================================================================

def summarize() -> None:
    print()
    print(SEP)
    log('构建完成！')
    print(SEP)
    if not DIST_ROOT.exists():
        return

    total = dir_size(DIST_ROOT)
    print(f'发行目录: {DIST_ROOT}')
    print(f'总体积  : {human(total)}')
    print()

    entries = [
        ('CapsWriter-Offline.exe', '启动器（双击它）'),
        ('internal', '便携式 Python 运行时 + 依赖'),
        ('core', '核心源码（可修改）'),
        ('LLM', 'LLM 角色（可增删）'),
    ]
    for name, note in entries:
        path = DIST_ROOT / name
        if not path.exists():
            continue
        size = dir_size(path) if path.is_dir() else path.stat().st_size
        print(f'  {name:<24} {human(size):>10}   {note}')

    # models/ 单独展示（junction 不计入总体积）
    models = DIST_ROOT / 'models'
    if os.path.isjunction(models):
        print(f'  {"models":<24} {"junction":>10}   → {os.readlink(models)}')
    elif models.exists():
        print(f'  {"models":<24} {human(models_size(models)):>10}   （已复制进发行包）')

    print()
    print('下一步：')
    print(f'  1) 进入 {DIST_ROOT}')
    print('  2) 双击 CapsWriter-Offline.exe')
    print()
    print('提示：模型目录当前是 junction 链接时，可以把整个 dist 文件夹拷到别的机器，')
    print('      但需要一并拷贝 models/ 的真实目录（或改用 --models copy 重新构建）。')
    print()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='构建 CapsWriter-Offline 绿色免安装版',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--models', choices=['junction', 'copy', 'none'], default='junction',
        help='models/ 的处理方式（默认 junction：建目录链接，不复制 943MB）',
    )
    parser.add_argument('--clean', action='store_true', help='构建前清理 build/ 与 dist/')
    parser.add_argument('--no-build', action='store_true', help='跳过 PyInstaller，只整理产物')
    parser.add_argument('--with-node', action='store_true',
                        help='额外打包 node.exe 与 native-recorder-nodejs（系统音频捕获）')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print(SEP)
    log('CapsWriter-Offline 绿色免安装版 构建')
    log(f'项目根目录: {ROOT}')
    log(f'Python    : {sys.executable}')
    log(f'模型策略  : {args.models}')
    print(SEP)

    started = time.time()
    step_clean(args)
    step_pyinstaller(args)
    step_copy_sources(args)
    step_models(args)
    step_runtime_extras(args)
    step_bat(args)
    step_console_hint(args)
    summarize()
    log(f'总耗时 {time.time() - started:.0f}s')
    return 0


if __name__ == '__main__':
    sys.exit(main())
