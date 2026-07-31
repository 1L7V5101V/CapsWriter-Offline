# coding: utf-8
"""
"正在听" 灵动岛浮窗模块（常驻版，逐像素 alpha 药丸）

一个常驻屏幕边缘的黑色药丸形浮窗，可拖拽吸附到任意屏幕边缘，状态机：

    IDLE(就绪) --arm--> ARMED(可以说话了) --检测到声音--> SPEAKING(正在听)
        ^                                                       |
        |                                                     release
        |                                                       v
        +----结果完成---- PROCESSING(识别中…) <--finish(松开键)--+

渲染实现：WS_EX_LAYERED + UpdateLayeredWindow 逐像素 alpha 合成。
药丸主体由 Pillow 以 8x 超采样离屏绘制（端帽九宫格缓存，任意宽度拼接），
带抗锯齿圆角、垂直微渐变、1px 半透明描边与高斯柔影，
整帧以 premultiplied BGRA 位图交给 DWM 合成——与系统原生浮窗同一条渲染路径，
无 SetWindowRgn 硬裁剪毛边，无 -transparentcolor 色边。
分层窗口按 alpha 命中测试：全透明区域点击自然穿透，药丸本体可拖拽。

动画：60fps 临界阻尼弹簧（宽度/位置，带轻微过冲），状态切换内容交叉淡入；
静止时按帧签名跳帧，零 CPU 占用。自动适配系统 DPI 缩放。

线程模型：独立线程运行 Tkinter 主循环（仅作为窗口宿主、定时器与鼠标事件源），
音频回调线程通过线程安全队列推送能量。
本模块不依赖 core.ui 包的其他部分（toast 等），可独立导入。

对外 API（任意线程可调用，线程安全）：
    ListeningIsland().configure(...)
    ListeningIsland().ensure_visible()      # 启动时常驻显示
    ListeningIsland().arm()                  # 按下快捷键 → "可以说话了"
    ListeningIsland().push_level(rms)        # 推送一帧能量（50ms 一次）
    ListeningIsland().show_partial(text)     # 服务端流式中间结果 → "正在听"
    ListeningIsland().processing()           # 松开键，等识别结果 → "识别中…"
    ListeningIsland().idle()                 # 回到空闲态
"""

from __future__ import annotations

import colorsys
import ctypes
import json
import logging
import math
import random
import threading
import time
import tkinter as tk
from ctypes import wintypes
from pathlib import Path
from queue import Queue, Empty
from typing import Optional

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

# DPI 感知（幂等；toast_base 也会调一次）
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except (OSError, AttributeError):
    pass

# ============================================================
# Win32 API（分层窗口合成 / 多屏工作区）
# ============================================================

_gdi = ctypes.windll.gdi32
_user32 = ctypes.windll.user32

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080    # 不出现在 Alt-Tab
WS_EX_NOACTIVATE = 0x08000000    # 永不抢焦点（不打断正在输入的应用）
GA_ROOT = 2
ULW_ALPHA = 2
AC_SRC_OVER = 0
AC_SRC_ALPHA = 1
DIB_RGB_COLORS = 0
BI_RGB = 0
_MONF_DEFAULT = 0x00000001       # MonitorFromWindow: DEFAULTTOPRIMARY
HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
_TOPMOST_FLAGS = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE


class _RECT(ctypes.Structure):
    _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long),
                ('right', ctypes.c_long), ('bottom', ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [('cbSize', ctypes.c_uint), ('rcMonitor', _RECT),
                ('rcWork', _RECT), ('dwFlags', ctypes.c_uint)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ('biSize', wintypes.DWORD),
        ('biWidth', wintypes.LONG),
        ('biHeight', wintypes.LONG),
        ('biPlanes', wintypes.WORD),
        ('biBitCount', wintypes.WORD),
        ('biCompression', wintypes.DWORD),
        ('biSizeImage', wintypes.DWORD),
        ('biXPelsPerMeter', wintypes.LONG),
        ('biYPelsPerMeter', wintypes.LONG),
        ('biClrUsed', wintypes.DWORD),
        ('biClrImportant', wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [('bmiHeader', _BITMAPINFOHEADER), ('bmiColors', wintypes.DWORD * 3)]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ('BlendOp', ctypes.c_byte),
        ('BlendFlags', ctypes.c_byte),
        ('SourceConstantAlpha', ctypes.c_ubyte),
        ('AlphaFormat', ctypes.c_byte),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [('x', wintypes.LONG), ('y', wintypes.LONG)]


class _SIZE(ctypes.Structure):
    _fields_ = [('cx', wintypes.LONG), ('cy', wintypes.LONG)]


_user32.MonitorFromWindow.restype = ctypes.c_void_p
_user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
_user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_user32.GetAncestor.restype = wintypes.HWND
_user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetWindowLongPtrW.restype = ctypes.c_longlong
_user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.SetWindowLongPtrW.restype = ctypes.c_longlong
_user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_longlong]
_user32.UpdateLayeredWindow.restype = wintypes.BOOL
_user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC,
    ctypes.POINTER(_POINT), ctypes.POINTER(_SIZE),
    wintypes.HDC, ctypes.POINTER(_POINT),
    wintypes.COLORREF, ctypes.POINTER(_BLENDFUNCTION), wintypes.DWORD,
]
_user32.GetDC.restype = wintypes.HDC
_user32.GetDC.argtypes = [wintypes.HWND]
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_gdi.CreateCompatibleDC.restype = wintypes.HDC
_gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
_gdi.CreateDIBSection.restype = wintypes.HBITMAP
_gdi.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.POINTER(_BITMAPINFO), wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
]
_gdi.SelectObject.restype = wintypes.HGDIOBJ
_gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi.DeleteDC.argtypes = [wintypes.HDC]
_user32.SetWindowPos.restype = wintypes.BOOL
_user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.UINT,
]


class _GUITHREADINFO(ctypes.Structure):
    """GetGUIThreadInfo：取前台线程的文本光标（caret）位置"""
    _fields_ = [
        ('cbSize', wintypes.DWORD),
        ('flags', wintypes.DWORD),
        ('hwndActive', wintypes.HWND),
        ('hwndFocus', wintypes.HWND),
        ('hwndCapture', wintypes.HWND),
        ('hwndMenuOwner', wintypes.HWND),
        ('hwndMoveSize', wintypes.HWND),
        ('hwndCaret', wintypes.HWND),
        ('rcCaret', _RECT),
    ]


_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
_user32.GetGUIThreadInfo.restype = wintypes.BOOL
_user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(_GUITHREADINFO)]
_user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(_POINT)]
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
_user32.GetCursorPos.restype = wintypes.BOOL
_user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]


# ============================================================
# Logger 代理（允许主程序注入真正的 client logger）
# ============================================================

class _LoggerProxy:
    """日志代理：默认用独立 logger，可被 set_logger() 注入真正的 client logger"""
    def __init__(self):
        self._target = logging.getLogger('core.listening_island')

    def set_target(self, real_logger):
        self._target = real_logger

    def __getattr__(self, name):
        return getattr(self._target, name)


logger = _LoggerProxy()


def set_logger(real_logger):
    """注入真正的 logger（应由客户端启动时调用，使日志写入 client_latest.log）"""
    logger.set_target(real_logger)


# ============================================================
# 样式常量（逻辑像素，运行时按系统 DPI 缩放）
# ============================================================

PILL_TOP = (24, 24, 29, 255)      # 药丸顶部渐变色（微亮）
PILL_BOTTOM = (9, 9, 12, 255)     # 药丸底部渐变色（近纯黑）
PILL_ALPHA = 242                  # 药丸整体不透明度（轻微透出桌面，更像系统浮窗）
RIM_COLOR = (255, 255, 255, 30)   # 1px 外描边（半透明白，深浅背景都有边界感）
SHADOW_COLOR = (0, 0, 0, 110)     # 柔影颜色
SHADOW_BLUR = 9                   # 柔影模糊半径
SHADOW_DY = 3                     # 柔影垂直偏移

FG = (255, 255, 255)              # 文字色
FG_DIM = (154, 154, 158)          # 暗色文字（空闲态）
FG_ACCENT = (255, 214, 10)        # 识别中态强调色（黄）
REC_DOT_OFF = (74, 74, 80)        # 空闲圆点（暗灰）
REC_DOT_ON = (255, 59, 48)        # 激活红点（亮红）
REC_DOT_HI = (255, 138, 128)      # 呼吸高光
REC_DOT_PROC = (255, 214, 10)     # 识别中圆点（黄）
BAR_FG = (48, 209, 88)            # 波形条（系统绿）

FONT_BOLD = 'msyhbd.ttc'          # 微软雅黑 Bold
FONT_REGULAR = 'msyh.ttc'         # 微软雅黑 Regular
FONT_SIZE_MAIN = 15               # 主文字（逻辑 px）
FONT_SIZE_DIM = 13                # 空闲态文字

HEIGHT = 40                       # 药丸高度
IDLE_W = 96                       # 空闲态宽度
ARMED_W = 232                     # 上膛态宽度（"可以说话了" + 波形条）
SPEAK_W = 260                     # 说话态最小宽度（实时文字自适应）
PROC_W = 172                      # 识别中态宽度
DONE_W = 152                      # 完成确认态宽度（绿勾 + "已上屏"）
DONE_MS = 650                     # 完成确认展示时长，之后缩回并发射流光
CURSOR_BLINK_MS = 1060            # 冒字打字机光标闪烁周期
DOCK_MARGIN = 8                   # 激活态离停靠边的距离
DOCK_SLIVER = 5                   # 空闲态缩到顶边后露出的像素（不挡视线）
DOCK_SLIVER_SIDE = 14             # 底/左/右边露出的像素（贴任务栏/侧边时 5px
                                  # 无影衬托几乎不可见且点不到，需露出一段圆头）
PAD = 20                          # 窗口四周留白（容纳柔影）
MAX_W_RATIO = 0.6                 # 药丸最大宽度（相对工作区）

DOT_CX = 21                       # 圆点中心 x
DOT_R = 4.5                       # 圆点半径
TEXT_X = 34                       # 文字起点 x
RIGHT_PAD = 18                    # 右侧留白

WAVE_BARS = 5                     # 波形条数量
WAVE_BAR_W = 3.5                  # 波形条宽度
WAVE_BAR_GAP = 2.5                # 波形条间距
WAVE_BAR_MAX = 18                 # 波形条最大高度
WAVE_BAR_MIN = 4                  # 波形条最小高度

TICK_MS = 16                      # tk 轮询间隔（~60fps；静止时按帧签名跳帧）
NOISE_WIN_FRAMES = 20             # 噪声底滑动窗口帧数（~1s @50ms）
BREATH_PERIOD_MS = 1300           # 红点呼吸周期
PROC_WAVE_PERIOD_MS = 900         # 识别中三点波浪周期
FADE_MS = 150                     # 状态切换内容交叉淡入时长

SS = 8                            # 形状 sprite 超采样倍数

# ============================================================
# 送达流光：识别完成、药丸缩回时，一道光从灵动岛飞向鼠标位置
#
# 风格模板化：BEAM_PRESETS 内置多套预设，配置 listening_island_beam_style
# 一键切换；listening_island_beam_custom（dict）可在所选预设基础上覆盖
# 任意键。快速预览：python listening_island.py <风格名>
#
# 可用键说明（数值均为逻辑像素/毫秒）：
#   color            主色 (R,G,B)；或 'random'=每次发射随机一个鲜艳色，
#                    'rainbow'=每发弹独立随机色（彩虹弹幕）
#   core             头部亮核色
#   fly_ms           飞行时长                 bloom_ms   到达绽放时长
#   tail_ms          拖尾时间跨度             tail_n     拖尾采样点数
#   head_r           头部辉光半径             core_r     头部亮核半径
#   tail_r           拖尾最粗半径             tail_base_r 拖尾最细半径
#   sparkle          飞行时每帧洒星尘数(0=无)  spray_n    到达迸溅粒子数(0=无)
#   particle_life_ms 星尘寿命                 bend       弧线弯曲度(0=直线)
#   ease             缓动: 'smooth'平滑 / 'rush'加速冲刺 / 'glide'先快后慢
#   rings            到达光环列表 [(颜色, 最大半径, 起始延迟0~1), ...]
#   volley           连射发数：数字(1=单发)，或 'per_char'=按本轮识别的
#                    字数发弹——说多少字射多少发，说得越多火力越猛
#   volley_interval_ms 每发间隔          volley_max  per_char 模式发数上限
#   volley_window_ms 整梭弹发射窗口上限：发数多时自动加密射速压进窗口，
#                    保证弹幕总时长恒定（不随字数线性变长）
#   scatter          多发时落点散布半径（弹幕感）
#   finale           True=最后压轴一发大弹：空一拍蓄力、加粗、正中靶心、
#                    大爆炸收尾（仅多发时生效）
# ============================================================

BEAM_PRESETS = {
    # 青绿彗星（默认）：与波形条同色系，星尘 + 双环
    'comet': dict(
        color=(110, 245, 160), core=(255, 255, 255),
        fly_ms=400, bloom_ms=300, tail_ms=150, tail_n=34,
        head_r=7.0, core_r=3.6, tail_r=3.4, tail_base_r=1.4,
        sparkle=2, spray_n=14, particle_life_ms=400,
        bend=0.16, ease='smooth',
        rings=[((110, 245, 160), 26, 0.0), ((255, 255, 255), 15, 0.18)],
    ),
    # 暖金流星：加速冲刺，尾长粒子多
    'gold': dict(
        color=(255, 196, 80), core=(255, 250, 235),
        fly_ms=430, bloom_ms=320, tail_ms=190, tail_n=40,
        head_r=7.5, core_r=3.8, tail_r=3.6, tail_base_r=1.2,
        sparkle=3, spray_n=18, particle_life_ms=450,
        bend=0.20, ease='rush',
        rings=[((255, 196, 80), 28, 0.0), ((255, 250, 235), 16, 0.20)],
    ),
    # 樱粉飘落：柔和滑翔，星尘缓缓下坠
    'sakura': dict(
        color=(255, 150, 195), core=(255, 235, 245),
        fly_ms=480, bloom_ms=360, tail_ms=170, tail_n=36,
        head_r=6.5, core_r=3.2, tail_r=3.2, tail_base_r=1.4,
        sparkle=3, spray_n=16, particle_life_ms=550,
        bend=0.24, ease='glide',
        rings=[((255, 150, 195), 24, 0.0), ((255, 235, 245), 13, 0.22)],
    ),
    # 霓虹紫电：快、大环、少粒子
    'neon': dict(
        color=(175, 120, 255), core=(240, 230, 255),
        fly_ms=300, bloom_ms=280, tail_ms=120, tail_n=30,
        head_r=7.5, core_r=4.0, tail_r=3.8, tail_base_r=1.6,
        sparkle=1, spray_n=10, particle_life_ms=340,
        bend=0.10, ease='rush',
        rings=[((175, 120, 255), 32, 0.0), ((240, 230, 255), 18, 0.15)],
    ),
    # 连续能量弹（贝吉塔）："哒哒哒哒"——说多少字射多少发能量弹，
    # 每次发射随机换色（想固定金色：beam_custom={'color': (255,218,70)}；
    # 想每发一个颜色：beam_custom={'color': 'rainbow'}），
    # 弧线乱舞、落点散布、逐发轰击，最后压轴一发大的正中靶心大爆炸
    'vegeta': dict(
        color='random', core=(255, 255, 240),
        fly_ms=300, bloom_ms=260, tail_ms=90, tail_n=14,
        head_r=4.8, core_r=2.6, tail_r=2.6, tail_base_r=1.0,
        sparkle=1, spray_n=5, particle_life_ms=380,
        bend=0.22, ease='rush',
        rings=[((255, 218, 70), 15, 0.0), ((255, 255, 240), 9, 0.2)],
        volley='per_char', volley_interval_ms=50, scatter=26,
        volley_max=40, finale=True,
    ),
    # 终极闪光（单发大波）：金白炽烈能量波，粗壮近直线轰出，蓄力冲刺，
    # 到达三层光环大爆炸
    'final_flash': dict(
        color=(255, 232, 90), core=(255, 255, 250),
        fly_ms=330, bloom_ms=400, tail_ms=230, tail_n=48,
        head_r=10.5, core_r=5.5, tail_r=5.6, tail_base_r=2.2,
        sparkle=4, spray_n=30, particle_life_ms=520,
        bend=0.05, ease='rush',
        rings=[((255, 232, 90), 38, 0.0), ((255, 255, 250), 24, 0.12),
               ((255, 200, 60), 50, 0.28)],
    ),
    # 极简白线：无星尘无迸溅，细光一闪，单环收束
    'minimal': dict(
        color=(230, 235, 240), core=(255, 255, 255),
        fly_ms=320, bloom_ms=220, tail_ms=110, tail_n=26,
        head_r=4.5, core_r=2.6, tail_r=2.2, tail_base_r=0.9,
        sparkle=0, spray_n=0, particle_life_ms=300,
        bend=0.08, ease='smooth',
        rings=[((255, 255, 255), 16, 0.0)],
    ),
}

# 拖拽位置持久化文件（项目根目录，拖动松手时保存，启动时恢复）
POS_STATE_FILE = Path(__file__).resolve().parent.parent / 'island_state.json'

# 结构性常量（与风格无关）
BEAM_PAD = 110                    # 光束窗口包围盒留白（容纳辉光/弧线/粒子漂移）
BEAM_MIN_DIST = 60                # 起终点距离过近则不发射
BEAM_MAX_W = 2600                 # 光束画布尺寸上限（防超大跨屏开销）
BEAM_MAX_H = 1700


# ============================================================
# 临界阻尼弹簧（iOS 灵动岛同款手感，带轻微过冲）
# ============================================================

class _Spring:
    def __init__(self, value: float, omega: float = 17.0, zeta: float = 0.85):
        self.x = float(value)
        self.v = 0.0
        self.target = float(value)
        self._omega = omega
        self._zeta = zeta

    def jump(self, value: float) -> None:
        self.x = self.target = float(value)
        self.v = 0.0

    def step(self, dt: float) -> None:
        # 半隐式欧拉，稳定且足够精确
        a = self._omega * self._omega * (self.target - self.x) \
            - 2.0 * self._zeta * self._omega * self.v
        self.v += a * dt
        self.x += self.v * dt
        if abs(self.x - self.target) < 0.3 and abs(self.v) < 2.0:
            self.x = self.target
            self.v = 0.0


# ============================================================
# 灵动岛单例
# ============================================================

class ListeningIsland:
    """常驻"正在听"灵动岛浮窗（单例）

    在独立线程内运行 Tkinter，外部线程仅通过队列与之通信。
    """

    _instance: Optional['ListeningIsland'] = None
    _lock = threading.Lock()

    def __new__(cls) -> 'ListeningIsland':
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True

        # 配置（可被 configure() 覆盖）
        self.enabled: bool = True
        self.min_threshold: float = 0.004   # 降低下限，更敏感
        self.noise_factor: float = 2.5      # 降低倍数，更易触发
        self.confirm_frames: int = 2
        self.always_visible: bool = True
        self.dock_edge: str = 'top'         # 停靠边：top/bottom/left/right
        self.auto_hide: bool = True         # 空闲时自动缩到停靠边
        self.beam_enabled: bool = True      # 送达流光动画
        self.partial_enabled: bool = True   # 实时冒字（弹幕文字）
        # 流光风格（预设 + 自定义覆盖，见 BEAM_PRESETS）
        self._beam_cfg: dict = self._build_beam_cfg('comet', None)

        # 音频线程 -> tk 线程 的指令队列
        self._queue: Queue = Queue()

        # 视觉状态：HIDDEN / IDLE / ARMED / SPEAKING / PROCESSING
        self._state = 'HIDDEN'

        # 能量相关（波形条显示 + ARMED→SPEAKING 本地人声检测）
        self._noise_window: list = []
        self._noise_floor: float = 0.0
        self._cur_level: float = 0.0
        self._target_level: float = 0.0
        self._frame_counter: int = 0
        self._voice_frames: int = 0

        # 实时识别文字（服务端流式返回）
        self._partial_text: str = ''
        # 本轮是否真的说过话（用于识别完成时是否发射送达流光）
        self._round_had_speech: bool = False
        # 本轮识别文字字数（per_char 连射模式的弹药数）
        self._round_char_count: int = 0
        # 完成确认态（DONE）的进入时刻（真实时钟，tick 节拍有系统级抖动）
        self._done_since: float = 0.0

        # IDLE 态文字（可通过 set_idle_text() 修改，系统音频模式切换用）
        self._idle_text = "就绪"

        # 动画弹簧（药丸矩形，物理像素）
        self._spring_w = _Spring(IDLE_W)
        self._spring_x = _Spring(0.0, omega=15.0, zeta=0.9)
        self._spring_y = _Spring(0.0, omega=15.0, zeta=0.9)
        # 状态切换内容交叉淡入
        self._content_alpha: float = 1.0
        # 呼吸相位
        self._tick_count: int = 0

        # 停靠位置锚（沿停靠边的比例位置 0~1）
        self._anchor: float = 0.5
        # 是否已从状态文件恢复过位置（用户拖过的位置优先于配置默认停靠边）
        self._pos_restored: bool = False
        # 拖动状态
        self._dragging: bool = False
        self._drag_dx: int = 0      # 鼠标按下时相对药丸左上角的偏移
        self._drag_dy: int = 0

        # tk / 渲染资源
        self.root: Optional[tk.Tk] = None
        self.window: Optional[tk.Toplevel] = None
        self._hwnd: int = 0
        self._scale: float = 1.0
        self._screen_w: int = 0
        self._tk_ready: threading.Event = threading.Event()
        self._visible: bool = False

        # 送达流光窗口与动画状态
        self._beam_win: Optional[tk.Toplevel] = None
        self._beam_hwnd: int = 0
        self._beam_active: bool = False
        self._beam_t0: float = 0.0
        self._beam_origin: tuple = (0, 0)   # 光束窗口左上角
        self._beam_size: tuple = (1, 1)
        # 弹道列表（支持连射）：每发 {'t0':发射偏移ms, 'p0','p1','p2', 'arrived'}
        self._beam_shots: list = []
        # 到达爆闪环实例：[(x, y, 到达时刻 monotonic), ...]
        self._beam_rings: list = []
        # 星尘粒子：[x, y, vx, vy, birth_monotonic, size, color_idx]
        self._beam_particles: list = []

        # sprite / 字体 / 帧缓存
        self._font_cache: dict = {}
        self._pill_parts: Optional[tuple] = None
        self._shadow_parts: Optional[tuple] = None
        self._dot_cache: dict = {}
        self._bar_cache: dict = {}
        self._ellipsis_cache: dict = {}
        self._last_frame_key: Optional[tuple] = None
        self._last_win_rect: Optional[tuple] = None

        self._tk_thread = threading.Thread(
            target=self._run_tk, daemon=True, name='ListeningIslandTk'
        )
        self._tk_thread.start()

    # --------------------------------------------------------
    # 配置
    # --------------------------------------------------------

    def configure(
        self,
        enabled: Optional[bool] = None,
        min_threshold: Optional[float] = None,
        noise_factor: Optional[float] = None,
        confirm_frames: Optional[int] = None,
        always_visible: Optional[bool] = None,
        dock_edge: Optional[str] = None,
        auto_hide: Optional[bool] = None,
        beam: Optional[bool] = None,
        show_partial: Optional[bool] = None,
        beam_style: Optional[str] = None,
        beam_custom: Optional[dict] = None,
    ) -> None:
        if enabled is not None:
            self.enabled = bool(enabled)
        if min_threshold is not None:
            self.min_threshold = float(min_threshold)
        if noise_factor is not None:
            self.noise_factor = float(noise_factor)
        if confirm_frames is not None:
            self.confirm_frames = int(confirm_frames)
        if always_visible is not None:
            self.always_visible = bool(always_visible)
        if dock_edge is not None:
            e = str(dock_edge).lower()
            # 用户拖拽保存的位置优先于配置默认停靠边
            if e in ('top', 'bottom', 'left', 'right') and not self._pos_restored:
                self.dock_edge = e
        if auto_hide is not None:
            self.auto_hide = bool(auto_hide)
        if beam is not None:
            self.beam_enabled = bool(beam)
        if show_partial is not None:
            self.partial_enabled = bool(show_partial)
        if beam_style is not None or beam_custom is not None:
            style = str(beam_style or 'comet').lower()
            self._beam_cfg = self._build_beam_cfg(style, beam_custom)
            logger.info(f"[ListeningIsland] 流光风格: {style} "
                        f"{'(含自定义覆盖)' if beam_custom else ''}")

    @staticmethod
    def _build_beam_cfg(style: str, custom: Optional[dict]) -> dict:
        """预设 + 自定义覆盖 → 完整流光配置（补齐连射键默认值）"""
        cfg = dict(BEAM_PRESETS.get(style) or BEAM_PRESETS['comet'])
        cfg.setdefault('volley', 1)
        cfg.setdefault('volley_interval_ms', 0)
        cfg.setdefault('scatter', 0)
        cfg.setdefault('volley_max', 40)
        cfg.setdefault('volley_window_ms', 900)
        cfg.setdefault('finale', False)
        if isinstance(custom, dict):
            cfg.update({k: v for k, v in custom.items() if k in cfg})
        # 颜色允许 list 形式（配置文件写 [r,g,b] 也行）；
        # color 还可为 'random'/'rainbow' 动态模式
        for k in ('color', 'core'):
            if not isinstance(cfg[k], str):
                cfg[k] = tuple(cfg[k])
        cfg['rings'] = [(tuple(c), r, d) for c, r, d in cfg['rings']]
        return cfg

    def set_idle_text(self, text: str) -> None:
        """设置 IDLE 态显示的文字（线程安全）"""
        self._idle_text = text
        # 如果当前在 IDLE 态，强制刷新
        if self._state == 'IDLE':
            self._queue.put(('refresh', None))

    @staticmethod
    def _random_vivid() -> tuple:
        """随机鲜艳色（HSV 随机色相，高饱和高亮度）"""
        r, g, b = colorsys.hsv_to_rgb(
            random.random(), random.uniform(0.7, 1.0), 1.0)
        return (int(r * 255), int(g * 255), int(b * 255))

    # --------------------------------------------------------
    # 外部 API（任意线程调用，仅入队）
    # --------------------------------------------------------

    def arm(self) -> None:
        if not self.enabled:
            return
        self._queue.put(('arm', None))

    def push_level(self, rms: float) -> None:
        if not self.enabled:
            return
        try:
            self._queue.put(('level', float(rms)))
        except Exception:
            pass

    def processing(self) -> None:
        """松开键，进入识别中态"""
        if not self.enabled:
            return
        self._queue.put(('processing', None))

    def show_partial(self, text: str) -> None:
        """收到服务端流式中间识别结果：切换到 SPEAKING 并显示实时文字"""
        if not self.enabled:
            return
        try:
            self._queue.put(('partial', text))
        except Exception:
            pass

    def idle(self) -> None:
        """强制回到空闲态（取消录音等）"""
        if not self.enabled:
            return
        self._queue.put(('idle', None))

    def finish_round(self) -> None:
        """一轮识别结果处理完毕：仅当仍处于"识别中"态时回到空闲。

        与 idle() 的区别：若用户已按键开始新一轮录音（ARMED/SPEAKING），
        迟到的完成通知不会打断新一轮的状态。
        """
        if not self.enabled:
            return
        self._queue.put(('finish_round', None))

    def ensure_visible(self) -> None:
        if not self.enabled:
            return
        self._queue.put(('show', None))

    # 兼容旧调用名
    def dismiss(self) -> None:
        self.idle()

    # --------------------------------------------------------
    # Tk 主循环（子线程）
    # --------------------------------------------------------

    def _run_tk(self) -> None:
        try:
            self.root = tk.Tk()
            self.root.withdraw()

            self.window = tk.Toplevel(self.root)
            self.window.overrideredirect(True)
            self.window.attributes('-topmost', True)
            self.window.geometry('1x1+0+0')
            self.window.update_idletasks()

            # 取真正的顶层 HWND（Tk Toplevel 的 winfo_id 是内容 child）
            child = self.window.winfo_id()
            self._hwnd = _user32.GetAncestor(child, GA_ROOT) or child

            # 分层合成 + 不抢焦点 + 不进 Alt-Tab
            # （分层窗口按像素 alpha 命中测试：全透明区域点击穿透，药丸本体可拖）
            ex = _user32.GetWindowLongPtrW(self._hwnd, GWL_EXSTYLE)
            ex |= WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
            _user32.SetWindowLongPtrW(self._hwnd, GWL_EXSTYLE, ex)

            # 拖动交互（绑定在 Toplevel 上，药丸区域可拖）
            self.window.bind('<ButtonPress-1>', self._on_drag_start)
            self.window.bind('<B1-Motion>', self._on_drag_motion)
            self.window.bind('<ButtonRelease-1>', self._on_drag_release)

            # 送达流光窗口（全程鼠标穿透，无位图时完全不可见）
            self._beam_win = tk.Toplevel(self.root)
            self._beam_win.overrideredirect(True)
            self._beam_win.attributes('-topmost', True)
            self._beam_win.geometry('1x1+0+0')
            self._beam_win.update_idletasks()
            bchild = self._beam_win.winfo_id()
            self._beam_hwnd = _user32.GetAncestor(bchild, GA_ROOT) or bchild
            bex = _user32.GetWindowLongPtrW(self._beam_hwnd, GWL_EXSTYLE)
            bex |= (WS_EX_LAYERED | 0x00000020  # WS_EX_TRANSPARENT
                    | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
            _user32.SetWindowLongPtrW(self._beam_hwnd, GWL_EXSTYLE, bex)
            self._beam_win.deiconify()

            try:
                self._screen_w = self.window.winfo_screenwidth()
            except tk.TclError:
                self._screen_w = 1920
            try:
                dpi = _user32.GetDpiForSystem()
                self._scale = max(1.0, dpi / 96.0)
            except (OSError, AttributeError):
                self._scale = 1.0

            # 恢复上次拖拽保存的位置，然后应用初始停靠布局
            self._load_position()
            self._apply_dock_layout(initial=True)

            self._tk_ready.set()
            self._tick()
            self.root.mainloop()
        except Exception as e:
            logger.warning(f"[ListeningIsland] Tk 主循环异常，浮窗禁用: {e}", exc_info=True)
            self.enabled = False
            self._tk_ready.set()

    def _tick(self) -> None:
        try:
            latest_level = None
            while True:
                try:
                    kind, val = self._queue.get_nowait()
                except Empty:
                    break
                if kind == 'arm':
                    self._on_arm()
                elif kind == 'processing':
                    self._on_processing()
                elif kind == 'idle':
                    # 识别完成（PROCESSING→IDLE）且本轮说过话：
                    # 绿勾确认帧与弹幕同帧启动——文字上屏的瞬间弹药已出膛
                    if self._state == 'PROCESSING' and self._round_had_speech:
                        self._enter_done()
                        self._fire_beam()
                    else:
                        self._on_idle()
                elif kind == 'finish_round':
                    if self._state == 'PROCESSING':
                        if self._round_had_speech:
                            self._enter_done()
                            self._fire_beam()
                        else:
                            self._on_idle()
                elif kind == 'show':
                    self._on_show()
                elif kind == 'partial':
                    self._on_partial(val)
                elif kind == 'level':
                    latest_level = val
                elif kind == 'refresh':
                    self._show_window()

            if latest_level is not None:
                self._on_level(latest_level)

            # 完成确认帧展示结束 → 缩回（弹幕已在确认帧起始时发射）
            if self._state == 'DONE' and \
                    (time.monotonic() - self._done_since) * 1000 >= DONE_MS:
                self._on_idle()

            self._advance_anim()
            self._tick_count += 1
            self._draw()
            self._tick_beam()
        except Exception as e:
            logger.debug(f"[ListeningIsland] tick 异常: {e}")
        finally:
            if self.root is not None:
                try:
                    self.root.after(TICK_MS, self._tick)
                except tk.TclError:
                    pass

    # --------------------------------------------------------
    # 状态机
    # --------------------------------------------------------

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self._content_alpha = 0.0   # 内容交叉淡入

    def _on_show(self) -> None:
        if self._state == 'HIDDEN':
            self._set_state('IDLE')
            self._apply_dock_layout(initial=True)
        self._show_window()

    def _on_arm(self) -> None:
        # 按下快捷键：录音已就绪 → 放下来显示"可以说话了"
        # 即使还没检测到说话，也显示实时波形条，让用户确认麦克风是否收到声音
        self._set_state('ARMED')
        self._noise_window = []
        self._noise_floor = 0.0
        self._cur_level = 0.0
        self._target_level = 0.0
        self._frame_counter = 0
        self._voice_frames = 0
        self._partial_text = ''
        self._round_had_speech = False
        self._show_window()

    def _on_partial(self, text: str) -> None:
        # 收到服务端流式中间结果：确定有人在说话 → 切到 SPEAKING 显示实时文字
        if self._state in ('ARMED', 'SPEAKING'):
            # 冒字开关关闭时只确认说话状态，不显示文字（保持"正在听"+波形条）
            if self.partial_enabled:
                self._partial_text = text
            self._round_had_speech = True
            if self._state == 'ARMED':
                self._set_state('SPEAKING')
                logger.info(f"[ListeningIsland] ARMED→SPEAKING（收到流式结果）: {text[:30]}")

    def _enter_done(self) -> None:
        # 识别完成的仪式帧：绿勾 + "已上屏"，展示 DONE_MS 后缩回并发射流光
        # （此刻记下冒字文本的字数——per_char 连射模式的弹药数；
        #   只数字词字符，剔除标点/空格，贴近实际打出的字数）
        self._round_char_count = sum(
            1 for ch in self._partial_text if ch.isalnum())
        self._set_state('DONE')
        self._done_since = time.monotonic()

    def _on_processing(self) -> None:
        # 松开键：进入识别中态（放下来显示"识别中…"）
        self._set_state('PROCESSING')
        self._cur_level = 0.0
        self._target_level = 0.0
        self._show_window()

    def _on_idle(self) -> None:
        # 结果完成：缩回停靠边（仅露一小条）
        self._set_state('IDLE')
        self._cur_level = 0.0
        self._target_level = 0.0
        self._noise_window = []
        self._noise_floor = 0.0
        self._partial_text = ''
        self._show_window()

    def _on_level(self, rms: float) -> None:
        # RMS 双重职责：波形条显示 + ARMED→SPEAKING 本地人声检测。
        # 服务端流式结果有分段积累延迟（秒级），本地检测让"正在听"即刻响应；
        # 实时文字仍由服务端流式结果驱动（_on_partial）。
        if self._state not in ('ARMED', 'SPEAKING'):
            return

        self._frame_counter += 1

        # 采样噪声底用于波形满量程参考与人声阈值
        self._noise_window.append(rms)
        if len(self._noise_window) > NOISE_WIN_FRAMES:
            self._noise_window.pop(0)
        if len(self._noise_window) >= 4:
            sw = sorted(self._noise_window)
            self._noise_floor = sw[len(sw) // 4]  # 25 分位
        else:
            self._noise_floor = min(self._noise_window) if self._noise_window else 0.0

        # 本地人声检测：连续 confirm_frames 帧超阈值 → 立即切"正在听"
        # （跳过前 4 帧，避开按键敲击声与噪声底未建立期）
        if self._state == 'ARMED' and self._frame_counter > 4:
            thr = max(self.min_threshold, self._noise_floor * self.noise_factor)
            if rms > thr:
                self._voice_frames += 1
            else:
                self._voice_frames = 0
            if self._voice_frames >= self.confirm_frames:
                self._set_state('SPEAKING')
                self._round_had_speech = True
                logger.info(
                    f"[ListeningIsland] ARMED→SPEAKING（本地检测到人声 "
                    f"rms={rms:.4f} thr={thr:.4f}）"
                )

        # 波形跟随当前 RMS
        self._target_level = rms

        # 平滑显示能量
        if self._cur_level < self._target_level:
            self._cur_level = self._cur_level * 0.3 + self._target_level * 0.7
        else:
            self._cur_level = self._cur_level * 0.82 + self._target_level * 0.18
        self._target_level *= 0.9

    # --------------------------------------------------------
    # 工作区 / 停靠布局
    # --------------------------------------------------------

    def _px(self, v: float) -> int:
        return max(1, round(v * self._scale))

    def _get_work_rect(self) -> _RECT:
        """获取窗口当前所在显示器的工作区（已排除任务栏）"""
        try:
            hmon = _user32.MonitorFromWindow(self._hwnd, _MONF_DEFAULT)
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(mi)
            if _user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                return mi.rcWork
        except Exception:
            pass
        # 回退：主屏工作区（SystemParametersInfoW SPI_GETWORKAREA）
        rc = _RECT()
        if _user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rc), 0):
            return rc
        return _RECT(0, 0, self._screen_w or 1920, 1080)

    @staticmethod
    def _pick_edge(cx: int, cy: int, work: _RECT) -> str:
        """根据药丸中心点离哪条边最近，选停靠边"""
        d_top = cy - work.top
        d_bottom = work.bottom - cy
        d_left = cx - work.left
        d_right = work.right - cx
        m = min(d_top, d_bottom, d_left, d_right)
        if m == d_top:
            return 'top'
        if m == d_bottom:
            return 'bottom'
        if m == d_left:
            return 'left'
        return 'right'

    def _pill_target_w(self) -> float:
        """当前状态的目标药丸宽度（物理像素）"""
        if self._state == 'ARMED':
            w = self._px(ARMED_W)
        elif self._state == 'SPEAKING':
            if self._partial_text:
                font = self._font(FONT_SIZE_MAIN, bold=True)
                est = self._px(TEXT_X) + font.getlength(self._partial_text) \
                    + self._px(RIGHT_PAD)
                w = max(self._px(SPEAK_W), est)
            else:
                # 尚无流式文字（"正在听"+波形条），保持 ARMED 宽度不跳动
                w = self._px(ARMED_W)
        elif self._state == 'PROCESSING':
            w = self._px(PROC_W)
        elif self._state == 'DONE':
            w = self._px(DONE_W)
        else:  # IDLE
            w = self._px(IDLE_W)
        work = self._get_work_rect()
        return float(min(w, int((work.right - work.left) * MAX_W_RATIO)))

    def _apply_dock_layout(self, initial: bool = False) -> None:
        """根据当前状态 + 停靠边，计算药丸目标矩形（弹簧目标，物理像素）。

        激活态(ARMED/SPEAKING/PROCESSING)：完整显示，贴边 DOCK_MARGIN
        空闲态(IDLE)：缩到边外，只露 DOCK_SLIVER
        """
        w = self._pill_target_w()
        h = self._px(HEIGHT)
        work = self._get_work_rect()
        ww = work.right - work.left
        wh = work.bottom - work.top
        edge = self.dock_edge
        margin = self._px(DOCK_MARGIN)
        sliver = self._px(DOCK_SLIVER if edge == 'top' else DOCK_SLIVER_SIDE)

        # auto_hide 关闭时，空闲态也完整贴边显示（不缩进边里）
        tucked = self._state == 'IDLE' and self.auto_hide

        if edge in ('top', 'bottom'):
            anchor_x = work.left + int(ww * self._anchor)
            cx = max(work.left + int(w) // 2,
                     min(anchor_x, work.right - int(w) // 2))
            x = cx - w / 2
            if tucked:
                y = work.top - h + sliver if edge == 'top' else work.bottom - sliver
            else:
                y = work.top + margin if edge == 'top' else work.bottom - h - margin
        else:  # left / right（左右停靠也用横向药丸，沿垂直边定位）
            anchor_y = work.top + int(wh * self._anchor)
            cy = max(work.top + h // 2, min(anchor_y, work.bottom - h // 2))
            y = cy - h / 2
            if tucked:
                x = work.left - w + sliver if edge == 'left' else work.right - sliver
            else:
                x = work.left + margin if edge == 'left' else work.right - w - margin

        self._spring_w.target = float(w)
        self._spring_x.target = float(x)
        self._spring_y.target = float(y)

        if initial:
            self._spring_w.jump(w)
            self._spring_x.jump(x)
            self._spring_y.jump(y)

    # --------------------------------------------------------
    # 拖动交互
    # --------------------------------------------------------

    def _on_drag_start(self, event) -> None:
        self._dragging = True
        # 记录鼠标相对药丸左上角的偏移
        self._drag_dx = event.x_root - int(self._spring_x.x)
        self._drag_dy = event.y_root - int(self._spring_y.x)

    def _on_drag_motion(self, event) -> None:
        if not self._dragging:
            return
        # 拖动时药丸直接跟随鼠标（不走弹簧）
        self._spring_x.jump(event.x_root - self._drag_dx)
        self._spring_y.jump(event.y_root - self._drag_dy)

    def _on_drag_release(self, event) -> None:
        if not self._dragging:
            return
        self._dragging = False
        try:
            w = self._spring_w.x
            h = self._px(HEIGHT)
            cx = int(self._spring_x.x + w / 2)
            cy = int(self._spring_y.x + h / 2)
            work = self._get_work_rect()
            # 选最近边
            self.dock_edge = self._pick_edge(cx, cy, work)
            # 更新锚点（沿停靠边的比例位置）
            ww = work.right - work.left
            wh = work.bottom - work.top
            if self.dock_edge in ('top', 'bottom'):
                self._anchor = max(0.0, min(1.0, (cx - work.left) / max(ww, 1)))
            else:
                self._anchor = max(0.0, min(1.0, (cy - work.top) / max(wh, 1)))
            logger.info(
                f"[ListeningIsland] 拖动释放，吸附到 {self.dock_edge} 边，anchor={self._anchor:.2f}"
            )
            self._save_position()
        except Exception as e:
            logger.debug(f"[ListeningIsland] 拖动释放处理失败: {e}")

    def _save_position(self) -> None:
        """拖拽松手后保存停靠位置（下次启动恢复到同一位置）"""
        try:
            POS_STATE_FILE.write_text(
                json.dumps({'dock_edge': self.dock_edge,
                            'anchor': round(self._anchor, 4)}),
                encoding='utf-8')
        except Exception as e:
            logger.debug(f"[ListeningIsland] 保存位置失败: {e}")

    def _load_position(self) -> None:
        """启动时恢复上次拖拽保存的位置（优先于配置默认停靠边）"""
        try:
            if not POS_STATE_FILE.exists():
                return
            data = json.loads(POS_STATE_FILE.read_text(encoding='utf-8'))
            edge = str(data.get('dock_edge', '')).lower()
            anchor = float(data.get('anchor', 0.5))
            if edge in ('top', 'bottom', 'left', 'right'):
                self.dock_edge = edge
                self._anchor = max(0.0, min(1.0, anchor))
                self._pos_restored = True
                logger.info(f"[ListeningIsland] 恢复上次位置: "
                            f"{edge} 边, anchor={self._anchor:.2f}")
        except Exception as e:
            logger.debug(f"[ListeningIsland] 恢复位置失败: {e}")

    # --------------------------------------------------------
    # 动画
    # --------------------------------------------------------

    def _advance_anim(self) -> None:
        dt = TICK_MS / 1000.0
        if not self._dragging:
            # 每帧刷新停靠目标：SPEAKING 文字增长 / 显示器变化都能平滑跟随
            if self._state != 'HIDDEN':
                self._apply_dock_layout()
            self._spring_x.step(dt)
            self._spring_y.step(dt)
        self._spring_w.step(dt)
        if self._content_alpha < 1.0:
            self._content_alpha = min(1.0, self._content_alpha + TICK_MS / FADE_MS)

    def _ref_level(self) -> float:
        return max(self._noise_floor * 6, self.min_threshold * 6, 0.05)

    def _breath_factor(self) -> float:
        phase = (self._tick_count * TICK_MS % BREATH_PERIOD_MS) / BREATH_PERIOD_MS
        return 0.5 - 0.5 * math.cos(2 * math.pi * phase)

    @staticmethod
    def _mix_color(c1: tuple, c2: tuple, t: float) -> tuple:
        t = max(0.0, min(1.0, t))
        return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))

    # --------------------------------------------------------
    # 字体 / sprite（生成一次，反复拼接）
    # --------------------------------------------------------

    def _font(self, size_logical: int, bold: bool) -> ImageFont.FreeTypeFont:
        key = (size_logical, bold)
        f = self._font_cache.get(key)
        if f is None:
            px = max(8, round(size_logical * self._scale))
            names = (FONT_BOLD, FONT_REGULAR) if bold else (FONT_REGULAR, FONT_BOLD)
            for name in names:
                for idx in (1, 0):  # index 1 = "Microsoft YaHei UI" face
                    try:
                        f = ImageFont.truetype(f'C:/Windows/Fonts/{name}', px, index=idx)
                        break
                    except OSError:
                        continue
                if f is not None:
                    break
            if f is None:
                f = ImageFont.load_default(px)
            self._font_cache[key] = f
        return f

    def _make_pill_parts(self) -> tuple:
        """8x 超采样绘制药丸 sprite，切成 (左帽, 1px 中列, 右帽) 供任意宽度拼接"""
        h_p = self._px(HEIGHT)
        r_p = h_p // 2
        w0 = 4 * r_p
        W, H = w0 * SS, h_p * SS
        radius = H // 2

        # 垂直渐变主体
        grad = Image.linear_gradient('L').resize((W, H))
        top = Image.new('RGBA', (W, H), PILL_TOP)
        bottom = Image.new('RGBA', (W, H), PILL_BOTTOM)
        body = Image.composite(bottom, top, grad)

        mask = Image.new('L', (W, H), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, W - 1, H - 1), radius=radius, fill=PILL_ALPHA)
        body.putalpha(mask)

        # 1px 半透明白描边（rim light）
        rim = Image.new('RGBA', (W, H), (0, 0, 0, 0))
        inset = SS // 2
        ImageDraw.Draw(rim).rounded_rectangle(
            (inset, inset, W - 1 - inset, H - 1 - inset),
            radius=radius - inset, outline=RIM_COLOR, width=SS)
        body = Image.alpha_composite(body, rim)

        sprite = body.resize((w0, h_p), Image.LANCZOS)
        left = sprite.crop((0, 0, r_p, h_p))
        mid = sprite.crop((w0 // 2, 0, w0 // 2 + 1, h_p))
        right = sprite.crop((w0 - r_p, 0, w0, h_p))
        return left, mid, right, r_p, h_p

    def _make_shadow_parts(self) -> tuple:
        """高斯柔影 sprite（画布含 PAD 留白），同样切左/中/右拼接"""
        h_p = self._px(HEIGHT)
        r_p = h_p // 2
        pad_p = self._px(PAD)
        w0 = 4 * r_p + 2 * pad_p
        H = h_p + 2 * pad_p
        img = Image.new('RGBA', (w0, H), (0, 0, 0, 0))
        dy = self._px(SHADOW_DY)
        ImageDraw.Draw(img).rounded_rectangle(
            (pad_p, pad_p + dy, w0 - pad_p - 1, pad_p + h_p - 1 + dy),
            radius=r_p, fill=SHADOW_COLOR)
        img = img.filter(ImageFilter.GaussianBlur(self._px(SHADOW_BLUR)))
        half = w0 // 2
        left = img.crop((0, 0, half, H))
        mid = img.crop((half, 0, half + 1, H))
        right = img.crop((half, 0, w0, H))
        return left, mid, right

    @staticmethod
    def _assemble(left: Image.Image, mid: Image.Image, right: Image.Image,
                  total_w: int) -> Image.Image:
        h = left.height
        img = Image.new('RGBA', (total_w, h), (0, 0, 0, 0))
        mid_w = total_w - left.width - right.width
        img.paste(left, (0, 0))
        if mid_w > 0:
            img.paste(mid.resize((mid_w, h), Image.NEAREST), (left.width, 0))
        img.paste(right, (total_w - right.width, 0))
        return img

    def _dot_sprite(self, color: tuple, r_log: float, glow: float) -> Image.Image:
        """抗锯齿圆点，可带辉光。缓存 key 量化到 1/4px / 8 档辉光"""
        key = (color, round(r_log * 4), round(glow * 8))
        img = self._dot_cache.get(key)
        if img is not None:
            return img
        r_p = r_log * self._scale
        glow_r = r_p * 2.6
        d = int(math.ceil(glow_r * 2)) + 4
        c = d / 2
        ss4 = 4
        big = Image.new('RGBA', (d * ss4, d * ss4), (0, 0, 0, 0))
        if glow > 0.01:
            gd = ImageDraw.Draw(big)
            ga = int(90 * glow)
            gr = (r_p + (glow_r - r_p) * glow) * ss4
            gd.ellipse((c * ss4 - gr, c * ss4 - gr, c * ss4 + gr, c * ss4 + gr),
                       fill=color + (ga,))
            big = big.filter(ImageFilter.GaussianBlur(r_p * ss4 * 0.55))
        bd = ImageDraw.Draw(big)
        rr = r_p * ss4
        bd.ellipse((c * ss4 - rr, c * ss4 - rr, c * ss4 + rr, c * ss4 + rr),
                   fill=color + (255,))
        img = big.resize((d, d), Image.LANCZOS)
        if len(self._dot_cache) > 256:   # 彩虹弹幕色档较多，缓存放宽
            self._dot_cache.clear()
        self._dot_cache[key] = img
        return img

    def _bar_sprite(self, h_px: int) -> Image.Image:
        """圆头波形竖条（物理像素高度），抗锯齿，按高度缓存"""
        img = self._bar_cache.get(h_px)
        if img is not None:
            return img
        w_p = max(2, round(WAVE_BAR_W * self._scale))
        h_use = max(w_p, h_px)
        ss4 = 4
        big = Image.new('RGBA', (w_p * ss4, h_use * ss4), (0, 0, 0, 0))
        ImageDraw.Draw(big).rounded_rectangle(
            (0, 0, w_p * ss4 - 1, h_use * ss4 - 1),
            radius=w_p * ss4 // 2, fill=BAR_FG + (255,))
        img = big.resize((w_p, h_use), Image.LANCZOS)
        self._bar_cache[h_px] = img
        return img

    def _check_sprite(self) -> Image.Image:
        """完成态绿勾（抗锯齿，缓存一次）"""
        img = self._dot_cache.get('check')
        if img is not None:
            return img
        d = self._px(17)
        ss4 = 4
        D = d * ss4
        big = Image.new('RGBA', (D, D), (0, 0, 0, 0))
        bd = ImageDraw.Draw(big)
        lw = max(2, round(2.6 * self._scale)) * ss4
        # 勾的三个关键点（相对比例）
        p1 = (D * 0.16, D * 0.55)
        p2 = (D * 0.40, D * 0.78)
        p3 = (D * 0.84, D * 0.24)
        bd.line([p1, p2, p3], fill=BAR_FG + (255,), width=lw, joint='curve')
        for p in (p1, p3):  # 圆头端点
            r = lw / 2
            bd.ellipse((p[0] - r, p[1] - r, p[0] + r, p[1] + r),
                       fill=BAR_FG + (255,))
        img = big.resize((d, d), Image.LANCZOS)
        self._dot_cache['check'] = img
        return img

    def _fit_text(self, text: str, max_w_px: float,
                  font: ImageFont.FreeTypeFont) -> str:
        """文字超宽时保留尾部（识别中看最新内容更有用），前缀省略号"""
        if font.getlength(text) <= max_w_px:
            return text
        key = (text, int(max_w_px))
        cached = self._ellipsis_cache.get(key)
        if cached is not None:
            return cached
        lo, hi = 0, len(text)
        while lo < hi:  # 二分找最长可显示尾部
            mid = (lo + hi) // 2
            if font.getlength('…' + text[mid:]) <= max_w_px:
                hi = mid
            else:
                lo = mid + 1
        fitted = '…' + text[min(lo, len(text) - 1):]
        if len(self._ellipsis_cache) > 64:
            self._ellipsis_cache.clear()
        self._ellipsis_cache[key] = fitted
        return fitted

    # --------------------------------------------------------
    # 帧参数（同时作为跳帧签名）
    # --------------------------------------------------------

    def _compute_frame(self) -> tuple:
        t_ms = self._tick_count * TICK_MS
        breath = self._breath_factor()
        state = self._state

        w_p = max(self._px(IDLE_W), round(self._spring_w.x))
        x_p = round(self._spring_x.x)
        y_p = round(self._spring_y.x)
        ca = round(self._content_alpha * 20) / 20  # 量化，稳态可跳帧

        # 圆点
        if state == 'IDLE':
            dot_color, dot_r, glow = REC_DOT_OFF, DOT_R - 1.0, 0.0
        elif state == 'ARMED':
            dot_color, dot_r, glow = REC_DOT_ON, DOT_R, 0.25
        elif state == 'SPEAKING':
            # 呼吸叠加实时音量：声音越大，红点越亮越大（"它在听我"的活感）
            lvl = max(0.0, min(1.0, self._cur_level / max(self._ref_level(), 1e-6)))
            dot_color = self._mix_color(REC_DOT_ON, REC_DOT_HI,
                                        min(1.0, breath * 0.5 + lvl * 0.6))
            dot_r = DOT_R + breath * 1.0 + lvl * 1.8
            glow = min(1.0, 0.25 + breath * 0.35 + lvl * 0.6)
        elif state == 'DONE':
            dot_color, dot_r, glow = BAR_FG, DOT_R, 0.5   # 绘制时替换为绿勾
        else:  # PROCESSING
            dot_color, dot_r, glow = REC_DOT_PROC, DOT_R + breath * 0.8, 0.2 + breath * 0.4

        # 文字
        if state == 'IDLE':
            label, bold, col = self._idle_text, False, FG_DIM
        elif state == 'ARMED':
            label, bold, col = '可以说话了', True, FG
        elif state == 'PROCESSING':
            label, bold, col = '识别中', True, FG_ACCENT
        elif state == 'DONE':
            label, bold, col = '已上屏', True, BAR_FG
        else:
            label = self._partial_text if self._partial_text else '正在听'
            bold, col = True, FG

        # 冒字打字机光标闪烁相位（占空比 ~55%）
        blink = False
        if state == 'SPEAKING' and self._partial_text:
            blink = (t_ms % CURSOR_BLINK_MS) < CURSOR_BLINK_MS * 0.55

        # 波形条高度（物理 px，含相位微动）：ARMED / SPEAKING 尚无文字时显示
        bars: tuple = ()
        if state == 'ARMED' or (state == 'SPEAKING' and not self._partial_text):
            lvl = max(0.0, min(1.0, self._cur_level / max(self._ref_level(), 1e-6)))
            heights = []
            for i in range(WAVE_BARS):
                wob = 0.55 + 0.45 * math.sin(t_ms / 90.0 + i * 1.9)
                amp = WAVE_BAR_MIN + (WAVE_BAR_MAX - WAVE_BAR_MIN) * lvl * wob
                heights.append(self._px(amp))
            bars = tuple(heights)

        # PROCESSING 态三点波浪相位（量化到 24 档）
        dots_phase = -1
        if state == 'PROCESSING':
            dots_phase = int(t_ms % PROC_WAVE_PERIOD_MS / PROC_WAVE_PERIOD_MS * 24)

        return (state, w_p, x_p, y_p, ca, dot_color, round(dot_r * 4),
                round(glow * 8), label, bold, col, bars, dots_phase, blink)

    # --------------------------------------------------------
    # 绘制 + 合成上屏
    # --------------------------------------------------------

    def _draw(self) -> None:
        if self._state == 'HIDDEN' or not self._visible or self._hwnd == 0:
            return
        frame = self._compute_frame()
        if frame == self._last_frame_key:
            return  # 画面无变化，跳帧（IDLE 稳态零开销）
        self._last_frame_key = frame

        (state, w_p, x_p, y_p, ca, dot_color, dot_r_q,
         glow_q, label, bold, col, bars, dots_phase, blink) = frame

        if self._pill_parts is None:
            self._pill_parts = self._make_pill_parts()
        if self._shadow_parts is None:
            self._shadow_parts = self._make_shadow_parts()

        pad_p = self._px(PAD)
        h_p = self._pill_parts[4]
        win_w = w_p + 2 * pad_p
        win_h = h_p + 2 * pad_p

        img = Image.new('RGBA', (win_w, win_h), (0, 0, 0, 0))
        # 柔影（含 PAD 的整窗层）
        sl, sm, sr = self._shadow_parts
        img.alpha_composite(self._assemble(sl, sm, sr, win_w))
        # 药丸主体
        pl, pm, pr, _, _ = self._pill_parts
        img.alpha_composite(self._assemble(pl, pm, pr, w_p), (pad_p, pad_p))

        # ---- 内容层（整体乘交叉淡入 alpha）----
        content = Image.new('RGBA', (win_w, win_h), (0, 0, 0, 0))
        cd = ImageDraw.Draw(content)
        cy = pad_p + h_p / 2

        # 圆点（DONE 态换成绿勾）
        if state == 'DONE':
            dot = self._check_sprite()
        else:
            dot = self._dot_sprite(dot_color, dot_r_q / 4, glow_q / 8)
        dot_cx = pad_p + self._px(DOT_CX)
        content.alpha_composite(dot, (round(dot_cx - dot.width / 2),
                                      round(cy - dot.height / 2)))

        # 文字
        font = self._font(FONT_SIZE_DIM if state == 'IDLE' else FONT_SIZE_MAIN, bold)
        text_x = pad_p + self._px(TEXT_X)
        reserve = self._px(RIGHT_PAD)
        if bars:
            reserve += self._px(WAVE_BARS * WAVE_BAR_W
                                + (WAVE_BARS - 1) * WAVE_BAR_GAP + 10)
        elif state == 'PROCESSING':
            reserve += self._px(24)  # 三点波浪空间
        max_text_w = (pad_p + w_p) - reserve - text_x
        if max_text_w > self._px(20):
            shown = self._fit_text(label, max_text_w, font)
            cd.text((text_x, cy), shown, anchor='lm', font=font, fill=col + (255,))
            if state == 'SPEAKING' and self._partial_text:
                # 打字机光标：文字尾部闪烁竖条
                cur_x = text_x + font.getlength(shown) + self._px(3)
                cur_h = self._px(16)
                cur_w = self._px(2)
                a = 235 if blink else 45
                cd.rounded_rectangle(
                    (cur_x, cy - cur_h / 2, cur_x + cur_w, cy + cur_h / 2),
                    radius=cur_w / 2, fill=FG + (a,))
            if state == 'PROCESSING':
                # 三点波浪动画
                end_x = text_x + font.getlength(shown) + self._px(7)
                for i in range(3):
                    ph = dots_phase / 24 * 2 * math.pi - i * 0.9
                    s = math.sin(ph)
                    dy = -s * self._px(2.5) if s > 0 else 0
                    a = int(140 + 115 * max(0.0, s))
                    r = self._px(1.8)
                    x = end_x + i * self._px(7)
                    cd.ellipse((x - r, cy + dy - r, x + r, cy + dy + r),
                               fill=col + (a,))

        # ARMED 态波形条（右侧对齐，垂直居中）
        if bars:
            bar_w = max(2, round(WAVE_BAR_W * self._scale))
            gap = self._px(WAVE_BAR_GAP)
            x = pad_p + w_p - self._px(RIGHT_PAD) - (bar_w + gap) * WAVE_BARS + gap
            for hgt in bars:
                spr = self._bar_sprite(hgt)
                content.alpha_composite(spr, (x, round(cy - spr.height / 2)))
                x += bar_w + gap

        if ca < 1.0:
            r0, g0, b0, a0 = content.split()
            a0 = a0.point(lambda v: int(v * ca))
            content = Image.merge('RGBA', (r0, g0, b0, a0))
        img.alpha_composite(content)

        # 上屏（窗口矩形 = 药丸矩形外扩 PAD）
        win_x = x_p - pad_p
        win_y = y_p - pad_p
        self._sync_tk_geometry(win_w, win_h, win_x, win_y)
        self._present(img, win_x, win_y)

    def _sync_tk_geometry(self, w: int, h: int, x: int, y: int) -> None:
        """让 Tk 的内容 child 覆盖整窗，鼠标事件（拖动）才能到达"""
        rect = (w, h, x, y)
        if rect == self._last_win_rect:
            return
        self._last_win_rect = rect
        try:
            self.window.geometry(f'{w}x{h}+{x}+{y}')
        except tk.TclError:
            pass

    def _present(self, img: Image.Image, x: int, y: int,
                 hwnd: Optional[int] = None) -> None:
        """premultiplied BGRA → DIBSection → UpdateLayeredWindow"""
        if hwnd is None:
            hwnd = self._hwnd
        w, h = img.size
        r, g, b, a = img.split()
        pm = Image.merge('RGBA', (
            ImageChops.multiply(b, a),
            ImageChops.multiply(g, a),
            ImageChops.multiply(r, a),
            a,
        ))
        data = pm.tobytes()

        screen_dc = _user32.GetDC(None)
        mem_dc = _gdi.CreateCompatibleDC(screen_dc)
        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h  # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        bits = ctypes.c_void_p()
        hbmp = _gdi.CreateDIBSection(screen_dc, ctypes.byref(bmi), DIB_RGB_COLORS,
                                     ctypes.byref(bits), None, 0)
        if not hbmp:
            _gdi.DeleteDC(mem_dc)
            _user32.ReleaseDC(None, screen_dc)
            return
        ctypes.memmove(bits, data, len(data))
        old = _gdi.SelectObject(mem_dc, hbmp)

        pt_dst = _POINT(x, y)
        size = _SIZE(w, h)
        pt_src = _POINT(0, 0)
        blend = _BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        _user32.UpdateLayeredWindow(hwnd, screen_dc,
                                    ctypes.byref(pt_dst), ctypes.byref(size),
                                    mem_dc, ctypes.byref(pt_src),
                                    0, ctypes.byref(blend), ULW_ALPHA)

        _gdi.SelectObject(mem_dc, old)
        _gdi.DeleteObject(hbmp)
        _gdi.DeleteDC(mem_dc)
        _user32.ReleaseDC(None, screen_dc)

    # --------------------------------------------------------
    # 送达流光：识别完成后，一道光从药丸飞向输入光标
    # --------------------------------------------------------

    def _get_target_pos(self) -> Optional[tuple]:
        """流光落点：鼠标位置（用户正在输入的地方，永远精准）。

        文本 caret（GetGUIThreadInfo）在现代浏览器/Electron 下拿不到或
        属于错误控件，落点会飘；鼠标位置一行取到且 100% 可靠。
        """
        try:
            pt = _POINT()
            if _user32.GetCursorPos(ctypes.byref(pt)):
                return (pt.x, pt.y)
            # 极端退化：活动窗口中心
            hwnd_fg = _user32.GetForegroundWindow()
            rc = _RECT()
            if hwnd_fg and _user32.GetWindowRect(hwnd_fg, ctypes.byref(rc)) \
                    and rc.right > rc.left:
                return ((rc.left + rc.right) // 2, (rc.top + rc.bottom) // 2)
        except Exception as e:
            logger.debug(f"[ListeningIsland] 获取落点位置失败: {e}")
        return None

    def _fire_beam(self) -> None:
        """从药丸当前中心向鼠标位置发射送达流光"""
        try:
            if not self.beam_enabled:
                return
            if self._beam_hwnd == 0 or self._beam_active:
                return
            tgt = self._get_target_pos()
            if tgt is None:
                return
            h_p = self._px(HEIGHT)
            x0 = self._spring_x.x + self._spring_w.x / 2
            y0 = self._spring_y.x + h_p / 2
            x1, y1 = float(tgt[0]), float(tgt[1])
            dist = math.hypot(x1 - x0, y1 - y0)
            if dist < self._px(BEAM_MIN_DIST):
                return
            cfg = self._beam_cfg
            # 弹药数：'per_char' 模式按本轮识别字数发弹（说多少字射多少发）
            if cfg['volley'] == 'per_char':
                volley = max(1, min(self._round_char_count,
                                    int(cfg['volley_max'])))
            else:
                volley = max(1, int(cfg['volley']))
            finale = bool(cfg['finale']) and volley > 1
            # 弹色模式：固定色 / 'random' 每轮随机 / 'rainbow' 每发随机
            cmode = cfg['color']
            round_color = self._random_vivid() if cmode == 'random' else cmode
            scatter = self._px(cfg['scatter']) if volley > 1 else 0
            bend_max = min(self._px(120), dist * cfg.get('bend', 0.16))
            nx, ny = -(y1 - y0) / dist, (x1 - x0) / dist
            # 发射窗口自适应：发数多时加密射速，弹幕总时长不随字数膨胀
            interval = min(cfg['volley_interval_ms'],
                           cfg['volley_window_ms'] / max(volley, 1))

            # 构造弹道：每发独立的发射时刻 / 弧线抖动 / 落点散布
            # finale 时额外追加一发压轴大弹（N 发小 + 1 发大）
            shots = []
            xs, ys = [x0, x1], [y0, y1]
            for i in range(volley + (1 if finale else 0)):
                is_finale = finale and i == volley
                if volley > 1 and not is_finale:
                    tx = x1 + random.uniform(-scatter, scatter)
                    ty = y1 + random.uniform(-scatter, scatter)
                    bend = bend_max * random.uniform(-1.0, 1.0)  # 弧线乱舞
                else:
                    tx, ty = x1, y1        # 单发/压轴大弹：正中靶心
                    bend = bend_max
                mx, my = (x0 + tx) / 2, (y0 + ty) / 2
                cx_, cy_ = mx + nx * bend, my + ny * bend
                shots.append({
                    # 压轴大弹固定蓄力一拍（高密度射速下 interval*3 会失去停顿感）
                    't0': i * interval + (max(interval * 3, 130)
                                          if is_finale else 0),
                    'p0': (x0, y0), 'p1': (cx_, cy_), 'p2': (tx, ty),
                    'arrived': False,
                    'big': is_finale,      # 压轴大弹：空三拍蓄力再射
                    'color': (self._random_vivid() if cmode == 'rainbow'
                              else round_color),
                })
                xs += [tx, cx_]
                ys += [ty, cy_]

            pad = self._px(BEAM_PAD)
            min_x = int(min(xs)) - pad
            min_y = int(min(ys)) - pad
            w = int(max(xs)) + pad - min_x
            h = int(max(ys)) + pad - min_y
            if w > BEAM_MAX_W or h > BEAM_MAX_H:
                return  # 跨度过大（跨屏拖拽等极端情况），放弃本次特效

            self._beam_shots = shots
            self._beam_rings = []
            self._beam_origin = (min_x, min_y)
            self._beam_size = (w, h)
            self._beam_t0 = time.monotonic()
            self._beam_particles = []
            self._beam_active = True
        except Exception as e:
            logger.debug(f"[ListeningIsland] 发射流光失败: {e}")

    @staticmethod
    def _bezier(t: float, p0: tuple, p1: tuple, p2: tuple) -> tuple:
        """二次贝塞尔插值（屏幕坐标）"""
        u = 1.0 - t
        return (u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
                u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1])

    def _ease_fly(self, t: float) -> float:
        """飞行缓动：smooth 平滑 / rush 加速冲刺 / glide 先快后慢"""
        mode = self._beam_cfg.get('ease', 'smooth')
        if mode == 'rush':
            return t * t * t
        if mode == 'glide':
            return 1 - (1 - t) ** 3
        if t < 0.5:  # smooth（smoothstep 立方）
            return 4 * t * t * t
        return 1 - (-2 * t + 2) ** 3 / 2

    def _spawn_particles(self, x: float, y: float, n: int,
                         spd_lo: float, spd_hi: float,
                         base_color: tuple) -> None:
        # 由基色衍生三档离散色（有限档位保证 sprite 缓存命中）
        white = (255, 255, 255)
        shades = (tuple(base_color),
                  self._mix_color(base_color, white, 0.45),
                  self._mix_color(base_color, white, 0.8))
        now = time.monotonic()
        for _ in range(n):
            ang = random.uniform(0, 2 * math.pi)
            spd = random.uniform(spd_lo, spd_hi) * self._scale
            self._beam_particles.append([
                x, y,
                math.cos(ang) * spd, math.sin(ang) * spd - 12 * self._scale,
                now,
                random.uniform(0.8, 1.8),
                random.choice(shades),
            ])

    def _tick_beam(self) -> None:
        if not self._beam_active:
            return
        try:
            cfg = self._beam_cfg
            fly_ms, bloom_ms = cfg['fly_ms'], cfg['bloom_ms']
            core = cfg['core']
            now = time.monotonic()
            e_ms = (now - self._beam_t0) * 1000
            multi = len(self._beam_shots) > 1
            ring_scale = 0.55 if multi else 1.0  # 连射时每发小爆

            shots_done = all(s['arrived'] for s in self._beam_shots)
            rings_done = all((now - rt0) * 1000 > bloom_ms
                             for _, _, rt0, _, _ in self._beam_rings)
            if shots_done and rings_done and not self._beam_particles:
                # 动画结束：推一帧全透明隐藏
                self._present(Image.new('RGBA', (1, 1), (0, 0, 0, 0)),
                              self._beam_origin[0], self._beam_origin[1],
                              hwnd=self._beam_hwnd)
                self._beam_active = False
                self._beam_shots = []
                self._beam_rings = []
                return

            bw, bh = self._beam_size
            ox, oy = self._beam_origin
            img = Image.new('RGBA', (bw, bh), (0, 0, 0, 0))

            def paste_center(spr: Image.Image, sx: float, sy: float) -> None:
                img.alpha_composite(spr, (round(sx - ox - spr.width / 2),
                                          round(sy - oy - spr.height / 2)))

            # ---- 各发弹道：宽幅渐隐拖尾 + 双层亮核头部 + 沿途洒星尘 ----
            tail_n = cfg['tail_n']
            tail_dt = (cfg['tail_ms'] / fly_ms) / tail_n
            tail_r, tail_base = cfg['tail_r'], cfg['tail_base_r']
            for shot in self._beam_shots:
                e_i = e_ms - shot['t0']
                if e_i <= 0:
                    continue  # 该发尚未发射
                t = min(1.0, e_i / fly_ms)
                p0, p1, p2 = shot['p0'], shot['p1'], shot['p2']
                col = shot['color']
                big = 2.1 if shot.get('big') else 1.0   # 压轴大弹加粗
                if t < 1.0:
                    for i in range(tail_n, 0, -1):
                        ts = t - i * tail_dt
                        if ts <= 0:
                            continue
                        px, py = self._bezier(self._ease_fly(ts), p0, p1, p2)
                        fade = 1.0 - i / (tail_n + 1)
                        c = self._mix_color(col, core, fade * 0.55)
                        spr = self._dot_sprite(
                            c, (tail_base + (tail_r - tail_base) * fade) * big,
                            0.6 * fade)
                        paste_center(spr, px, py)
                    hx, hy = self._bezier(self._ease_fly(t), p0, p1, p2)
                    paste_center(self._dot_sprite(col, cfg['head_r'] * big,
                                                  1.0), hx, hy)
                    paste_center(self._dot_sprite(core, cfg['core_r'] * big,
                                                  0.8), hx, hy)
                    if cfg['sparkle'] > 0:
                        self._spawn_particles(
                            hx, hy, cfg['sparkle'] * (2 if big > 1 else 1),
                            10, 45, col)
                elif not shot['arrived']:
                    # ---- 该发到达：星尘迸溅 + 登记爆闪环 ----
                    shot['arrived'] = True
                    if cfg['spray_n'] > 0:
                        n = int(cfg['spray_n'] * (2.5 if big > 1 else 1))
                        self._spawn_particles(p2[0], p2[1], n, 60, 170, col)
                    # 环 scale：普通连射发小爆，压轴大弹完整大爆
                    self._beam_rings.append(
                        (p2[0], p2[1], now,
                         1.3 if big > 1 else ring_scale, col))

            # ---- 到达绽放（每个爆点独立）：光环扩散 + 中心闪光淡出 ----
            for rx, ry, rt0, r_scale, shot_col in self._beam_rings:
                bt = (now - rt0) * 1000 / bloom_ms
                if bt >= 1.0:
                    continue
                for ring_idx, (ring_col, r_max, delay) in enumerate(cfg['rings']):
                    if ring_idx == 0:
                        ring_col = shot_col   # 第一个环始终跟随弹体颜色
                    rt = max(0.0, min(1.0, (bt - delay) / max(1e-6, 1 - delay)))
                    if rt <= 0 or rt >= 1:
                        continue
                    ease_out = 1 - (1 - rt) ** 3
                    ring_r = self._px(4 + r_max * r_scale * ease_out)
                    ring_a = int(230 * (1 - rt))
                    ring_w = max(1, self._px(2.6 * (1 - rt) + 0.6))
                    ss4 = 4
                    d = ring_r * 2 + ring_w * 2 + 4
                    big = Image.new('RGBA', (d * ss4, d * ss4), (0, 0, 0, 0))
                    ImageDraw.Draw(big).ellipse(
                        (ring_w * ss4, ring_w * ss4,
                         (d - ring_w) * ss4 - 1, (d - ring_w) * ss4 - 1),
                        outline=ring_col + (ring_a,), width=ring_w * ss4)
                    ring = big.resize((d, d), Image.LANCZOS)
                    img.alpha_composite(ring, (round(rx - ox - d / 2),
                                               round(ry - oy - d / 2)))
                flash = max(0.0, 1 - bt * 1.5)
                if flash > 0.02:
                    paste_center(self._dot_sprite(
                        core, (0.6 + 3.4 * flash) * r_scale, flash), rx, ry)

            # ---- 星尘粒子：漂移 + 轻微下坠 + 渐隐 ----
            alive = []
            now = time.monotonic()
            life = cfg['particle_life_ms'] / 1000.0
            for p in self._beam_particles:
                age = now - p[4]
                if age >= life:
                    continue
                fade = 1.0 - age / life
                px = p[0] + p[2] * age
                py = p[1] + p[3] * age + 55 * self._scale * age * age
                spr = self._dot_sprite(p[6], 0.4 + p[5] * fade, 0.4 * fade)
                paste_center(spr, px, py)
                alive.append(p)
            self._beam_particles = alive

            self._present(img, ox, oy, hwnd=self._beam_hwnd)
        except Exception as e:
            logger.debug(f"[ListeningIsland] 流光帧渲染失败: {e}")
            self._beam_active = False
            self._beam_particles = []

    # --------------------------------------------------------
    # 窗口显隐
    # --------------------------------------------------------

    def _raise_topmost(self) -> None:
        """重新声明 Z 序置顶（不抢焦点）。每次按键 arm 等场景都会调用。"""
        try:
            if self.window is not None:
                self.window.attributes('-topmost', True)
            if self._hwnd:
                _user32.SetWindowPos(
                    self._hwnd, HWND_TOPMOST, 0, 0, 0, 0, _TOPMOST_FLAGS)
            if self._beam_hwnd:
                _user32.SetWindowPos(
                    self._beam_hwnd, HWND_TOPMOST, 0, 0, 0, 0, _TOPMOST_FLAGS)
        except (OSError, tk.TclError):
            pass

    def _show_window(self) -> None:
        if self.window is None:
            return
        try:
            if not self._visible:
                # 分层窗口在首次 UpdateLayeredWindow 前完全不可见，deiconify 不会闪
                self.window.deiconify()
                self._visible = True
            # 窗口已可见时 Tk 不会自动重置顶，需每次显式拉回最前
            self._raise_topmost()
            self._last_frame_key = None  # 强制重绘一帧
            logger.info(f"[ListeningIsland] 窗口显示: state={self._state} "
                        f"edge={self.dock_edge} scale={self._scale}")
        except tk.TclError as e:
            logger.warning(f"[ListeningIsland] 显示窗口失败: {e}")

    def _hide_window(self) -> None:
        if self.window is None:
            return
        try:
            self.window.withdraw()
            self._visible = False
        except tk.TclError:
            pass


# ============================================================
# 独立演示：python listening_island.py 循环走一遍状态机
# ============================================================

if __name__ == '__main__':
    import sys

    logging.basicConfig(level=logging.INFO)
    island = ListeningIsland()
    # 命令行选流光风格快速预览：python listening_island.py gold
    if len(sys.argv) > 1:
        style = sys.argv[1].lower()
        if style not in BEAM_PRESETS:
            print(f'未知风格 "{style}"，可用: {", ".join(BEAM_PRESETS)}')
            sys.exit(1)
        island.configure(beam_style=style)
        print(f'流光风格: {style}（可用: {", ".join(BEAM_PRESETS)}）')
    island._tk_ready.wait(5)
    island.ensure_visible()

    demo_text = '今天天气不错，我们去公园散步吧，顺便买一杯咖啡'

    def _feed_levels(seconds: float, loud: bool):
        end = time.time() + seconds
        while time.time() < end:
            island.push_level(random.uniform(0.05, 0.4) if loud
                              else random.uniform(0.002, 0.01))
            time.sleep(0.05)

    while True:
        time.sleep(2.5)                 # IDLE（可拖动药丸换停靠边）
        island.arm()                    # ARMED：静音 1s → 说话 0.6s
        _feed_levels(1.0, loud=False)
        _feed_levels(0.6, loud=True)
        for i in range(3, len(demo_text) + 1, 2):   # SPEAKING：流式文字
            island.show_partial(demo_text[:i])
            _feed_levels(0.12, loud=True)
        island.processing()             # PROCESSING
        time.sleep(2.2)
        island.finish_round()           # 回 IDLE + 送达流光飞向前台窗口光标
