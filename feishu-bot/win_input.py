# -*- coding: utf-8 -*-
"""
win_input.py —— Win32 输入原语（5-2 底层）

为什么不用 pyautogui
--------------------
本机 pyautogui **导入即崩**：
    pyautogui/__init__.py → import pyscreeze
    pyscreeze/__init__.py:788 → `cv2.__version__`
    AttributeError: module 'cv2' has no attribute '__version__'
这是之前记录的"主环境历史损坏包"问题（cv2 是残包）。
修它要动 cv2，风险不可控；而输入原语本身并不复杂，
**Win32 SendInput 几十行就够，且零新依赖**。

为什么不用 keybd_event
----------------------
keybd_event 是老 API，无法表达"扩展键"和 Unicode 输入。
SendInput 能表达 KEYEVENTF_UNICODE，**中文可以直接打**，不用走剪贴板。

关于中文输入的重要设计
----------------------
SendInput + KEYEVENTF_UNICODE 可以直接发 Unicode 码点，
所以 `type_text("你好")` 是**真·逐字符输入**，不碰剪贴板、不干扰用户剪贴板内容。
（pyautogui.typewrite 只能打 ASCII，中文得靠剪贴板中转，会清掉用户剪贴板。）

UIPI 限制（必须知道）
---------------------
Windows 的 User Interface Privilege Isolation：**高完整性级别的窗口会拦截
低完整性级别进程发来的输入**。所以以普通权限运行时，无法控制以管理员权限
运行的窗口（如任务管理器、部分系统设置）。这不是 bug，是 Windows 设计。
表现为：函数返回成功，但目标窗口毫无反应。
"""
import ctypes
import ctypes.wintypes as wt

user32 = ctypes.WinDLL("user32", use_last_error=True)

# ---------- 常量 ----------
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

WHEEL_DELTA = 120

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


# ---------- 结构体 ----------
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wt.UINT
user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetCursorPos.argtypes = (ctypes.POINTER(wt.POINT),)
user32.GetCursorPos.restype = wt.BOOL
user32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
user32.SetCursorPos.restype = wt.BOOL

SM_CXSCREEN = 0
SM_CYSCREEN = 1
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


def _send(inputs):
    """把 INPUT 数组送进系统。返回成功注入的个数。"""
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
    if sent != n:
        raise OSError("SendInput 只注入了 %d/%d 个事件，Win32 错误码=%d"
                      % (sent, n, ctypes.get_last_error()))
    return sent


# ---------- 屏幕 ----------
def screen_size():
    """主屏尺寸（宽, 高）。"""
    return user32.GetSystemMetrics(SM_CXSCREEN), user32.GetSystemMetrics(SM_CYSCREEN)


def virtual_screen():
    """虚拟桌面（多显示器并集）的 left/top/width/height。"""
    return (user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))


def cursor_pos():
    p = wt.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


# ---------- 鼠标 ----------
def move(x, y):
    """移动鼠标到屏幕绝对坐标 (x, y)。"""
    user32.SetCursorPos(int(x), int(y))


def _mouse_event(flags, dx=0, dy=0, data=0):
    inp = INPUT(type=INPUT_MOUSE)
    inp.mi = MOUSEINPUT(dx, dy, data, flags, 0, 0)
    _send([inp])


def click(x=None, y=None, button="left", clicks=1, delay=0.06):
    """点击。先移动到 (x,y)（给了才移动），再点 clicks 次。

    clicks>1 用来做双击/三击，**比连续调两次 click 可靠**
    （系统靠间隔判定双击，手抖间隔会被当成两次单击）。
    """
    if x is not None and y is not None:
        move(x, y)
        _tiny_sleep()
    down, up = {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }.get(button)
    if not down:
        raise ValueError("不支持的鼠标键: %r（left/right/middle）" % button)
    import time
    for i in range(max(1, clicks)):
        _mouse_event(down)
        _mouse_event(up)
        if i < clicks - 1:
            time.sleep(delay)


def drag(x1, y1, x2, y2, steps=20):
    """从 (x1,y1) 按下拖到 (x2,y2)。steps 越多越平滑。"""
    import time
    move(x1, y1)
    _tiny_sleep()
    _mouse_event(MOUSEEVENTF_LEFTDOWN)
    for i in range(1, steps + 1):
        move(x1 + (x2 - x1) * i // steps, y1 + (y2 - y1) * i // steps)
        time.sleep(0.01)
    _mouse_event(MOUSEEVENTF_LEFTUP)


def scroll(clicks, x=None, y=None):
    """滚轮。正数上滚，负数下滚。"""
    if x is not None and y is not None:
        move(x, y)
        _tiny_sleep()
    _mouse_event(MOUSEEVENTF_WHEEL, data=int(clicks) * WHEEL_DELTA)


def _tiny_sleep():
    """给系统一个极短的时间去处理鼠标移动，否则移动+点击会变成拖拽。"""
    import time
    time.sleep(0.03)


# ---------- 键盘 ----------
# 常用虚拟键码（只列真正会用到的）
VK = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10,
    "win": 0x5B, "lwin": 0x5B, "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
}
# 需要 EXTENDEDKEY 标志的键（否则在某些窗口里行为不同/无效）
_EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2E, 0x5B, 0x5C}


def _key(vk, up=False):
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in _EXTENDED:
        flags |= KEYEVENTF_EXTENDEDKEY
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(vk, 0, flags, 0, 0)
    return inp


def key_down(name):
    """按下不松开。用于组合键。"""
    _send([_key(_vk(name))])


def key_up(name):
    _send([_key(_vk(name), up=True)])


def tap(name):
    """敲一个键（按下+松开）。"""
    vk = _vk(name)
    _send([_key(vk), _key(vk, up=True)])


def _vk(name):
    k = (name or "").strip().lower()
    if k in VK:
        return VK[k]
    if len(k) == 1:
        return ord(k.upper())      # 单字符走 Unicode 通道更稳
    raise ValueError("未知按键: %r（可用: %s，或单个字符）"
                     % (name, ", ".join(sorted(set(VK)))))


def hotkey(*names):
    """组合键，如 hotkey("ctrl", "c")。逆序释放。"""
    vks = [_vk(n) for n in names]
    if not vks:
        return
    ins = [_key(v) for v in vks] + [_key(v, up=True) for v in reversed(vks)]
    _send(ins)


def _unicode_char(ch):
    """发一个 Unicode 码点（KEYEVENTF_UNICODE），支持中文/emoji。"""
    for unit in (ch,):
        code = ord(unit)
        down = INPUT(type=INPUT_KEYBOARD)
        down.ki = KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, 0)
        up = INPUT(type=INPUT_KEYBOARD)
        up.ki = KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)
        yield down
        yield up


def type_text(text, delay=0.012):
    """逐字符输入文本，**支持中文**（走 KEYEVENTF_UNICODE，不碰剪贴板）。

    注意：不会自动处理 IME。目标窗口若是原生 Win32 编辑框/记事本，
    Unicode 注入直接生效；若是某些 Electron/游戏自绘输入框可能无效，
    那种情况回退到剪贴板方案（见 clipboard_type_text）。
    """
    import time
    for ch in str(text):
        if ch in ("\n", "\r"):
            tap("enter")
        elif ch == "\t":
            tap("tab")
        else:
            batch = list(_unicode_char(ch))
            _send(batch)
        if delay:
            time.sleep(delay)


# ---------- 前台窗口（安全闸要用）----------
def foreground_window():
    """返回 (hwnd, 标题, 所属进程名)。取不到时字段为 None。"""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None, "", ""
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    title = buf.value
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    proc = ""
    try:
        import psutil
        proc = psutil.Process(pid.value).name()
    except Exception:
        try:
            import ctypes as _c
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if h:
                sb = _c.create_unicode_buffer(260)
                if kernel32.QueryFullProcessImageNameW(h, 0, sb, None):
                    proc = sb.value.rsplit("\\", 1)[-1]
                kernel32.CloseHandle(h)
        except Exception:
            proc = ""
    return hwnd, title, proc


if __name__ == "__main__":
    import json
    print("主屏尺寸 :", screen_size())
    print("虚拟桌面 :", virtual_screen())
    print("当前鼠标 :", cursor_pos())
    hwnd, title, proc = foreground_window()
    print("前台窗口 : hwnd=%s" % hwnd)
    print("  标题   :", title)
    print("  进程   :", proc)
