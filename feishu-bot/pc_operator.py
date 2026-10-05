# -*- coding: utf-8 -*-
"""
pc_operator.py —— 电脑操作层（阶段 5-2）

5-1：只读截图。
5-2：鼠标键盘动作，**全部走安全闸**。

⚠️ 安全设计的核心原则：默认拒绝
--------------------------------
这套东西一旦失控，代价是真实的（误删文件、误发消息、误付款）。
所以设计成**四道闸全部通过才动手**，任何一道不通过就是拒绝：

  闸1 总开关    ENABLE_MOUSE_KEYBOARD —— 默认 **关**。不打开，下面全部抛错。
  闸2 白名单    只允许对指定进程派发动作（默认只有 notepad / explorer）。
                **注意：检查的是"当前前台窗口"的进程**，不是"我想操作谁"——
                因为注入是发给前台窗口的，操作别的窗口根本没意义且危险。
  闸3 确认      命中危险关键词的动作必须先经用户明确同意。
  闸4 步数      同一任务步数超 MAX_STEPS 立即熔断，防无限点击。

另外还有一个容易被忽略但很关键的约束：
  **每步动作后必须重新确认前台窗口没变。**
  因为从"你确认"到"我点击"之间，用户的窗口焦点可能已经切走了
  （用户可能正在打字、可能切到了微信）。不重查就会把点击打到别的窗口上。

依赖说明
--------
底层用 `win_input.py`（Win32 SendInput），**不用 pyautogui**——
本机 pyautogui 导入即崩（pyscreeze → cv2.__version__ AttributeError，
主环境 cv2 残包问题，见记忆日志），修它风险不可控。
SendInput 零新依赖，且能发 KEYEVENTF_UNICODE 直接打中文。

已知限制
--------
- **UIPI**：普通权限进程无法控制管理员权限窗口（任务管理器等）。
  表现是函数返回成功但窗口没反应——Windows 设计如此，不是 bug。
- 剪贴板方案只在 Unicode 注入失效时兜底（部分 Electron 自绘输入框）。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import win_input as wi

SHOT_DIR = os.environ.get(
    "SHOT_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shots"),
)

# ============ 闸1：总开关（默认关）============
ENABLE_MOUSE_KEYBOARD = os.environ.get("ENABLE_MOUSE_KEYBOARD", "0") == "1"

# ============ 闸2：白名单（只对这些进程的前台窗口派发动作）============
# 用**别名**而非精确进程名：微信有 WeChat.exe(旧) / Weixin.exe(新4.0+)，
# 画图有 mspaint.exe(内置) / PaintApp.exe(商店版)，写死必然踩空。
# 实际匹配逻辑在 pc_task.is_allowed_app()，这里存的是 app_key。
# 一开始只给最无害的几个。往里加 = 扩大风险面。
APP_WHITELIST = ["notepad.exe", "explorer.exe", "wechat", "mspaint"]

# ============ 闸3：危险动作关键词（命中即需确认）============
CONFIRM_REQUIRED = ["删除", "发送", "付款", "上传", "覆盖", "格式化",
                    "回车", "enter", "确认", "保存", "另存"]

# ============ 闸4：步数熔断 ============
MAX_STEPS = 20

# 允许注入的按键（比"什么都能按"安全得多）
# 故意**不包含** enter/tab/alt+f4 等：这些在错误窗口里后果严重，
# 需要时由上层显式加白并配确认闸。
ALLOWED_KEYS = {"backspace", "delete", "del", "up", "down", "left", "right",
                "home", "end", "pageup", "pagedown", "f1", "f2", "f3", "f4"}

# ============ dry-run：只过闸、不派发 ============
# 安全测试必须能在**不碰用户电脑**的前提下验证"该放行的确实放行"。
# 打开后所有动作只记进 _DRY_LOG，真实注入一步都不执行。
# 由 /mouse dry 切换，默认关。
DRY_RUN = os.environ.get("MOUSE_DRY_RUN", "0") == "1"
_DRY_LOG = []


def set_dry_run(on=True):
    """开关 dry-run。返回新状态。"""
    global DRY_RUN
    DRY_RUN = bool(on)
    return DRY_RUN


def dry_log():
    """取 dry-run 期间记录的动作（不清空）。"""
    return list(_DRY_LOG)


def _dispatch(action_name, fn, *args, **kwargs):
    """派发或记录。dry-run 时**完全不碰真实输入**。"""
    if DRY_RUN:
        _DRY_LOG.append({"action": action_name, "args": list(args),
                         "kwargs": {k: v for k, v in kwargs.items()}})
        return {"dry_run": True}
    return fn(*args, **kwargs)



class SafetyError(RuntimeError):
    """安全闸拒绝。上层应把这个消息原样转达给用户，不要重试。"""


def _ensure_dir():
    os.makedirs(SHOT_DIR, exist_ok=True)
    return SHOT_DIR


# ---------- 5-1 截图（只读，无安全闸需求）----------
def _grab_all():
    """抓取虚拟屏幕（多显示器时为全部）。返回 (PIL.Image, monitor_dict)。

    mss 10 中 sct.monitors[1] 是 dict（键 left/top/width/height），不是对象。
    """
    import mss
    from PIL import Image
    with mss.MSS() as sct:
        mon = sct.monitors[1]          # 1 = 虚拟屏幕（多显示器时是全部）
        shot = sct.grab(mon)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        return img, dict(mon)


def screenshot(tag="shot"):
    """抓取全屏并保存为 PNG，返回文件路径。失败抛异常。

    只读操作：不移动鼠标、不按键、不改任何系统状态。
    """
    d = _ensure_dir()
    path = os.path.join(d, "%s_%s.png" % (tag, time.strftime("%H%M%S")))
    img, _ = _grab_all()
    img.save(path)
    return path


def screenshot_info():
    """返回屏幕分辨率信息（不抓图，给状态查询用）。"""
    _, mon = _grab_all()
    return {"width": mon.get("width"), "height": mon.get("height")}


# ---------- 闸2/3/4 的判定 ----------
def app_allowed(proc_name):
    """目标进程是否在白名单内。

    委托给 pc_task.is_allowed_app() 做别名模糊匹配（微信有 WeChat/Weixin
    两种进程名、画图有 mspaint/PaintApp，硬编码精确名必然踩空）。
    这里做延迟导入以避免循环依赖：pc_task 会 import 本模块。
    """
    try:
        import pc_task
        return pc_task.is_allowed_app(proc_name)
    except Exception:
        return (proc_name or "").lower() in [a.lower() for a in APP_WHITELIST]


def needs_confirm(action_text):
    """判断某个动作是否需要用户确认。"""
    return any(k in (action_text or "") for k in CONFIRM_REQUIRED)


def check_step(n):
    """步数熔断检查。超限抛异常。"""
    if n > MAX_STEPS:
        raise SafetyError("已达到最大步数 %d，任务熔断（可能陷入循环）" % MAX_STEPS)


def _gate(action, step=0, confirmed=False):
    """四道闸集中在这里。**每个动作派发前都必须过这里。**"""
    # 闸1
    if not ENABLE_MOUSE_KEYBOARD:
        raise SafetyError("鼠标键盘操作未启用。发 `/mouse on` 打开（当前是安全默认）。")
    # 闸4
    check_step(step)
    # 闸2：查的是**当前前台窗口**
    hwnd, title, proc = wi.foreground_window()
    if not hwnd:
        raise SafetyError("拿不到当前前台窗口，无法确认安全，已拒绝操作。")
    if not app_allowed(proc):
        raise SafetyError(
            "当前前台窗口是「%s」（%s），不在白名单 %s 内，已拒绝操作。\n"
            "请先手动切到白名单里的窗口（比如记事本），再让我操作。"
            % (title or "(无标题)", proc or "(未知进程)", APP_WHITELIST))
    # 闸3
    if not confirmed and needs_confirm(action):
        raise SafetyError(
            "动作「%s」涉及危险操作（%s），需要你先明确同意。\n"
            "回复「确认执行 %s」我再动手。"
            % (action, "/".join(k for k in CONFIRM_REQUIRED if k in action), action))
    return {"hwnd": hwnd, "title": title, "proc": proc}


# ---------- 5-2 鼠标键盘动作 ----------
def move(x, y, step=0, confirmed=False):
    """移动鼠标。"""
    info = _gate("移动鼠标到 (%d,%d)" % (x, y), step, confirmed)
    return _dispatch("move", wi.move, int(x), int(y))


def click(x=None, y=None, button="left", clicks=1,
          step=0, confirmed=False, label=""):
    """点击。label 用于确认闸判断（如 "点确认按钮"）。"""
    desc = label or ("点击 (%s,%s) %s" % (x, y, button))
    info = _gate(desc, step, confirmed)
    return _dispatch("click", wi.click, x, y, button=button, clicks=clicks)


def scroll(clicks, x=None, y=None, step=0, confirmed=False):
    """滚轮。正数上滚，负数下滚。"""
    info = _gate("滚动 %d" % clicks, step, confirmed)
    return _dispatch("scroll", wi.scroll, clicks, x, y)


def drag(x1, y1, x2, y2, step=0, confirmed=False, label=""):
    """拖拽。label 用于确认闸判断。"""
    desc = label or ("拖拽 (%d,%d)->(%d,%d)" % (x1, y1, x2, y2))
    info = _gate(desc, step, confirmed)
    return _dispatch("drag", wi.drag, x1, y1, x2, y2)


def type_text(text, step=0, confirmed=False, label=""):
    """输入文本（支持中文，Unicode 注入，不碰剪贴板）。

    ⚠️ 文本内容本身也参与确认闸判断：如果要打的内容含"删除/发送"等
    危险词，会要求确认——因为在记事本里打出"删除"再被别的流程回车，
    就等于执行了。
    """
    desc = label or ("输入文本「%s」" % (text or "")[:30])
    info = _gate(desc, step, confirmed)
    return _dispatch("type_text", wi.type_text, text)


def tap(key, step=0, confirmed=False):
    """敲一个键。必须在 ALLOWED_KEYS 内。"""
    k = (key or "").lower()
    if k not in ALLOWED_KEYS:
        raise SafetyError(
            "按键「%s」不在允许列表内。\n"
            "当前只允许：%s\n"
            "回车/Tab/Alt 组合这类会立即改变状态或焦点，我没有放进去——"
            "需要时请明确告诉我要按哪个键，我再单独加。"
            % (key, "、".join(sorted(ALLOWED_KEYS))))
    info = _gate("按键 %s" % key, step, confirmed)
    return _dispatch("tap", wi.tap, k)


def hotkey(*keys, confirmed=False):
    """组合键。**必须由调用方显式传 confirmed=True**，本函数不自作主张。

    ⚠️ 曾经的 bug：这里原来写死了 `_gate(desc, 0, confirmed=True)`，
    等于**无条件放行**——Ctrl+A、Ctrl+C、Alt+F4 全都能直接派发。
    安全闸最忌讳这种"以为自己很安全"的默认值：
    一旦上层忘了传确认，闸门形同虚设。
    正确做法是**默认拒绝，要放行必须显式声明**。
    """
    if not keys:
        raise SafetyError("没有指定按键")
    desc = "组合键 %s" % "+".join(keys)
    if not confirmed:
        raise SafetyError(
            "组合键 %s 必须在确认后执行。\n"
            "这类键（Ctrl+A、Delete、Alt+F4…）后果不可逆，"
            "不会在没有你明确同意的情况下派发。\n"
            "回复「确认执行 %s」我再动手。" % (desc, desc))
    info = _gate(desc, 0, confirmed=True)
    return _dispatch("hotkey", wi.hotkey, *keys)


def focus_check():
    """只做闸2检查、**不派发任何动作**。给上层做"干事前先看一眼"。"""
    hwnd, title, proc = wi.foreground_window()
    return {
        "hwnd": hwnd,
        "title": title,
        "proc": proc,
        "allowed": app_allowed(proc),
        "enabled": ENABLE_MOUSE_KEYBOARD,
    }


def status():
    return {
        "shot_dir": os.path.abspath(SHOT_DIR),
        "mouse_keyboard_enabled": ENABLE_MOUSE_KEYBOARD,
        "dry_run": DRY_RUN,
        "app_whitelist": APP_WHITELIST,
        "confirm_keywords": CONFIRM_REQUIRED,
        "allowed_keys": sorted(ALLOWED_KEYS),
        "max_steps": MAX_STEPS,
        "screen": "%dx%d" % wi.screen_size(),
        "foreground": focus_check(),
        "dry_log_count": len(_DRY_LOG),
    }


if __name__ == "__main__":
    import json
    print("=" * 56)
    print("电脑操作层 5-2 状态")
    print("=" * 56)
    print(json.dumps(status(), ensure_ascii=False, indent=2))
    if "--shot" in sys.argv:
        p = screenshot("manual")
        print("\n截图已保存:", p)
