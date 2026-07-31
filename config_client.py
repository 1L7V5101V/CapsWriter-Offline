import os
from collections.abc import Iterable
from pathlib import Path

# 版本信息
__version__ = '3.0'

# 项目根目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# 客户端配置
class ClientConfig:
    addr = '127.0.0.1'          # Server 地址
    port = '6016'               # Server 端口

    # 快捷键配置列表
    shortcuts = [
        {
            'key': 'caps_lock',     # 监听大写锁定键
            'type': 'keyboard',     # 是键盘快捷键
            'suppress': True,      # 阻塞按键（短按会补发）
            'hold_mode': True,      # 长按模式
            'enabled': True         # 启用此快捷键
        },
        {
            'key': 'x2',
            'type': 'mouse',
            'suppress': True,
            'hold_mode': True,
            'enabled': True
        },
    ]

    threshold    = 0.3          # 快捷键触发阈值（秒）

    paste        = False        # 是否以写入剪切板然后模拟 Ctrl-V 粘贴的方式输出结果
    restore_clip = True         # 模拟粘贴后是否恢复剪贴板
    paste_apps   = ['WeiXin.exe', 'Telegram.exe']  # 匹配时强制粘贴

    enter_apps   = [('happ.exe', 0.5), ('hexin.exe', 0.5)]  # (应用名, 延迟秒数) 输出完成后自动回车，如同花顺，输入股票名后，需要回车才能切换

    save_audio = True           # 是否保存录音文件
    audio_name_len = 20         # 将录音识别结果的前多少个字存储到录音文件名中，建议不要超过200
    
    context = ''                # 提示词上下文，用于辅助 Fun-ASR-Nano 模型识别（例如输入人名、地名、专业术语等）
    language = 'auto'           # 识别语言：'auto', 'chinese', 'english', 'japanese' 等（各引擎支持范围不同）

    trash_punc = '，。,.'       # 识别结果要消除的末尾标点
    trash_punc_thresh = 8       # 识别结果的单词数量低于阈值时，强制去除末尾标点
    trash_punc_apps = ['WeiXin.exe', ]   # 对于指定的应用，强制去除末尾标点

    traditional_convert = False     # 是否将识别结果转换为繁体中文
    traditional_locale = 'zh-hant'  # 繁体地区：'zh-hant'（标准繁体）, 'zh-tw'（台湾繁体）, 'zh-hk'（香港繁体）

    mute_system_audio = True        # 录音时是否静音系统声音输出

    # 系统音频捕获（WASAPI Loopback）
    system_audio_enabled = True              # 是否启用系统音频捕获功能
    system_audio_loopback_device = None      # 指定输出设备名关键词（None=默认输出设备）
    system_audio_switch_key = 'tab+f1'       # 切换快捷键（当前仅支持 'tab+f1'）

    # "正在听" 灵动岛浮窗（录音时在屏幕顶部弹出黑色药丸提示，实时反馈麦克风状态）
    listening_island_enabled = True     # 是否启用
    listening_island_min_threshold = 0.006  # 人声检测/音量条满量程下限（float32 归一化 RMS）
    listening_island_noise_factor = 3.5     # 人声检测阈值 = 噪声底 x 该倍数
    listening_island_confirm_frames = 2     # 连续超阈值帧数（50ms/帧）即切换"正在听"
    # 停靠边：top/bottom/left/right。拖动浮窗松手后会自动吸附到最近的边并记忆，
    # 此项仅作为初始默认停靠边。
    listening_island_dock_edge = 'top'
    listening_island_auto_hide = True       # 空闲时自动缩到停靠边只露一小条；False 则空闲时也完整显示
    listening_island_beam = True            # 识别完成后的"送达流光"动画（一道光飞向鼠标位置）
    # 流光风格：'comet'(青绿彗星) / 'gold'(暖金流星) / 'sakura'(樱粉飘落)
    #          / 'neon'(霓虹紫电) / 'minimal'(极简白线)
    #          / 'vegeta'(连续能量弹——说多少字射多少发，每次随机换色，最后压轴一发大的)
    #          / 'final_flash'(终极闪光——单发金白能量波大爆炸)
    # 快速预览：python core/listening_island.py vegeta
    listening_island_beam_style = 'comet'
    # 自定义覆盖：在所选风格基础上改任意键（可用键详见 core/listening_island.py
    # 顶部 BEAM_PRESETS 注释）。color 可为 (R,G,B) 固定色 / 'random' 每次随机
    # / 'rainbow' 每发一色。示例：{'color': 'rainbow', 'spray_n': 30, 'fly_ms': 300}
    listening_island_beam_custom = {}
    listening_island_show_partial = True    # 录音时实时显示识别出的文字（弹幕冒字）
    # 流式实时反馈：录音时分段长度（秒），每段服务端返回中间文字实时显示在灵动岛上
    # 段越短冒字越快，但太短影响拼接质量。建议 1.5~3 秒
    listening_stream_seg_duration = 2.0    # 流式分段长度（秒）
    listening_stream_seg_overlap = 0.5     # 流式分段重叠（秒，用于去重）

    hot = True                 # 是否启用热词替换（统一 RAG 匹配）
    hot_thresh = 0.85           # RAG 替换热词阈值（高阈值，用于实际替换）
    hot_similar = 0.6           # RAG 相似热词阈值（低阈值，用于 LLM 上下文）
    hot_rule = True             # 是否启用自定义规则替换（基于正则表达式）

    llm_enabled = True          # 是否启用 LLM 润色功能，需要配置 LLM/ 目录下的角色文件
    llm_stop_key = 'esc'        # 中断 LLM 输出的快捷键

    enable_tray = True          # 客户端默认启用托盘图标功能

    # 日志配置
    log_level = 'DEBUG'          # 日志级别：'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'

    mic_seg_duration = 60       # 麦克风听写时分段长度：60秒
    mic_seg_overlap = 4         # 麦克风听写时分段重叠：4秒

    file_seg_duration = 60      # 转录文件时分段长度
    file_seg_overlap = 4        # 转录文件时分段重叠

    file_save_srt = True        # 转录文件时是否保存 srt 字幕
    file_save_txt = True        # 转录文件时是否保存 txt 文本（按标点切分后的）
    file_save_json = True       # 转录文件时是否保存 json 结果（含原始时间戳）
    file_save_merge = False      # 转录文件时是否保存 merge.txt（未切分的段落长文本）

    udp_broadcast = False               # 是否启用 UDP 广播输出结果
    udp_broadcast_targets = [           # UDP 广播目标地址列表，格式: (地址, 端口)
        ('127.255.255.255', 6017),      # 本地回环广播
        # ('192.168.1.255', 6017),      # 局域网广播（示例，按需启用）
    ]

    udp_control = False             # 是否启用 UDP 控制录音（外部程序发送 START/STOP 命令）
    udp_control_addr = '127.0.0.1'  # UDP 控制监听地址（'0.0.0.0' 允许外部访问）
    udp_control_port = 6018         # UDP 控制监听端口


# 快捷键配置说明
r"""
快捷键配置字段说明：
  key        - 按键名称（见下方可用按键列表）
  type       - 输入类型：'keyboard'（键盘）或 'mouse'（鼠标）
  suppress   - 是否阻塞按键（True=阻塞，False=不阻塞）
  hold_mode  - 长按模式（True=按下录音松开停止，False=单击开始再次单击停止）
  enabled    - 是否启用此快捷键

阻塞模式说明：
  - 阻塞模式  ：长按录音识别，短按（<0.3秒）则自动补发按键，不影响单击功能
  - 非阻塞模式：对于 CapsLock/NumLock/ScrollLock 这类切换键，松开时会自动补发，以恢复按键状态

可用按键名称：

  字母数字：a - z, 0 - 9（大键盘）

  符号键：, . / \ ` ' - = [ ] ; '


  功能键：f1 - f24

  控制键:
      ctrl_l,   ctrl_r,
      shift,  shift_r,
      alt_l,    alt_gr,
      cmd,    cmd_r

  特殊键：
      space, enter, tab, backspace, delete, insert, home, end
      page_up, page_down, esc, caps_lock, num_lock, scroll_lock
      print_screen, pause, menu

  方向键：up, down, left, right

  鼠标键：x1, x2

示例配置：
  {'key': 'caps_lock', 'type': 'keyboard', 'suppress': False, 'hold_mode': True, 'enabled': True}, 
  {'key': 'f12', 'type': 'keyboard', 'suppress': True, 'hold_mode': True, 'enabled': True}, 
  {'key': 'x2', 'type': 'mouse', 'suppress': True, 'hold_mode': True, 'enabled': True}, 
"""

