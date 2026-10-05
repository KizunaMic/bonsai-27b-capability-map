# -*- coding: utf-8 -*-
"""
pc_task.py —— 自然语言多步电脑操作（阶段 5-3）

你要的「说一句话，它自己动起来」在这里实现。

架构（关键：视觉很贵，必须省着用）
------------------------------------
每次视觉推理 40-70 秒。如果每步都看屏幕，10 步任务 = 10 分钟，不可用。
所以采用**分层执行**：

  1) 规划（1 次视觉，40-70s）
     看屏幕 + 看用户的话 → 生成步骤列表。每个步骤含：
     动作类型、目标描述、坐标、预期效果。

  2) 盲执行（0 成本）
     键盘快捷键、窗口切换这类**位置固定**的步骤直接做，不截图。
     验证靠 `GetForegroundWindow` 的窗口标题——这是免费的。

  3) 失败才重看（0.5 次视觉）
     盲执行后窗口标题没变 → 说明没生效 → 这时才重新截图视觉定位。

  10 步任务通常 2-3 次视觉，1.5-3 分钟。

为什么不每步都看
----------------
除了慢，还有个更实际的问题：**每步都看会让模型过度谨慎**。
它看到"发送"按钮就会反复犹豫，消耗大量 token 产出废话。
把"决定"和"观察"分离，模型只在规划时做一次完整判断。

不可逆动作闸（本文件最重要的部分）
------------------------------------
发送 / 删除 / 付款 / 覆盖 / 提交 —— 这些**一旦点下去无法撤销**。
本文件的铁律：

  **绝不自动执行不可逆动作。识别到目标后强制停下，把
  「要发给谁、发什么」原文报给用户，等明确同意。**

这不是保守，是必须的：发给错人、删错文件无法回滚。
模型有概率把"张三"识别成"李四"，所以**连"模型认为对"都不算依据**，
必须由人确认。

步骤熔断
--------
MAX_STEPS 之外还有两个：
  - 同一动作连续失败 2 次 → 停止并汇报（防原地打转）
  - 视觉重试上限 3 次 → 停止（防"看不清→重看→还看不清"死循环）
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pc_operator as pc
import win_input as wi

# ============ 不可逆动作：绝不自动执行 ============
IRREVERSIBLE = ["发送", "发���", "send", "删除", "删掉", "移除", "delete",
                "付款", "支付", "转账", "充值", "下单", "购买", "付款",
                "覆盖", "覆盖保存", "另存为", "保存", "确认提交", "提交",
                "覆盖", "清空", "格式化", "卸载", "结束进程", "关闭进程",
                "publish", "upload", "share"]

# 这些动作即使"看起来无害"也不自动做（因为会改变状态且难回滚）
NEEDS_STOP = IRREVERSIBLE

# ============ 目标白名单（不可逆动作的第二道闸）============
# 为什么需要这道闸
# --------------
# 原有设计是"所有不可逆动作都停下等你确认"。这本身安全，但**忽略了
# 一个事实：确认的对象可能是错的**。模型有概率把"张三"认成"李四"，
# 于是你确认的是"发给李四"，而实际发给了张三。
#
# 白名单是**更前置**的一道：只允许对**已明确授权的目标**执行。
# 不在白名单里的收件人，**连"要不要确认"都不问，直接拒绝**。
#
# ⚠️ 默认值是"空 = 全部拒绝"，不是"全部允许"。
#    安全默认值必须指向拒绝方向。
#
# 用法：
#   add_target("文件传输助手", reason="用户明确授权，仅测试")
#   remove_target("文件传输助手")   # 随时撤销
#
# 用户 2026-10-04 明确说明：「只能发给文件助手是测试可以发，
# 但日常真正使用都要停一下」→ 所以授权**必须由代码显式添加**，
# 且随时可撤销。不写在代码里就等于没有授权。
ALLOWED_TARGETS = {}


def add_target(name, reason=""):
    """授权一个不可逆操作的目标。返回当前授权列表。"""
    if name and name not in ALLOWED_TARGETS:
        ALLOWED_TARGETS[name] = {
            "reason": reason,
            "at": time.strftime("%Y-%m-%d %H:%M"),
        }
    return list(ALLOWED_TARGETS)


def remove_target(name):
    """撤销授权。返回是否真的撤销了。"""
    return ALLOWED_TARGETS.pop(name, None) is not None


def target_allowed(name):
    """目标是否已授权。空名单 = 全部拒绝。"""
    if not name:
        return False
    n = str(name).strip()
    return n in ALLOWED_TARGETS


def target_list():
    return [{"name": k, **v} for k, v in ALLOWED_TARGETS.items()]


class TaskAborted(RuntimeError):
    """任务被安全机制中止。消息应原样转达给用户。"""


# ============ 应用识别：别名模糊匹配 ============
# 为什么不用精确进程名：
#   微信有 WeChat.exe（旧版）和 Weixin.exe（新版 4.0+），QQNT 是 QQNT.exe，
#   画图有 mspaint.exe 和 PaintApp.exe（商店版）。写死必然踩空。
# 所以用**别名列表 + 精确/后缀匹配**。
#
# ⚠️ 匹配精度（安全相关）：
#   最初的纯 `alias in proc` 包含匹配会误判 —— `explorerX.exe`（恶意程序
#   常故意仿冒）会被当成 `explorer.exe` 放行。这等于白名单可被绕过。
#   现在的规则：**要么完全相等，要么以 `.` 分隔的扩展名前缀相等**
#   （`mspaint` 匹配 `mspaint.exe` / `mspaint.exe.manifest`，
#   但 `explorerX` 不匹配 `explorer`）。
APP_ALIASES = {
    "notepad": ["notepad"],
    "explorer": ["explorer"],
    "wechat":   ["wechat", "weixin"],
    "mspaint":  ["mspaint", "paintapp"],
    "chrome":   ["chrome"],
    "qq":       ["qqnt", "qq"],
}


def _proc_matches_alias(proc_lower, alias):
    """进程名是否匹配别名。**刻意不用纯包含匹配**（见上方说明）。"""
    if not proc_lower or not alias:
        return False
    if proc_lower == alias:
        return True
    # `mspaint.exe` -> 主体 `mspaint`；`explorerX.exe` -> `explorerx`（≠ explorer）
    base = proc_lower.split(".")[0]
    return base == alias


def resolve_app(name: str):
    """把用户说的应用名（"微信"/"画图"/"记事本"）解析成别名 key。"""
    if not name:
        return None
    t = name.strip().lower()
    # 直接命中别名列表
    for key, aliases in APP_ALIASES.items():
        if any(a in t for a in aliases):
            return key
    # 中文俗称
    cn = {"微信": "wechat", "qq": "qq", "企鹅": "qq",
          "画图": "mspaint", "画板": "mspaint", "绘图": "mspaint",
          "记事本": "notepad", "文本": "notepad",
          "浏览器": "chrome", "谷歌": "chrome", "edge": "chrome",
          "资源管理器": "explorer", "文件夹": "explorer", "我的电脑": "explorer"}
    for k, v in cn.items():
        if k in t:
            return v
    return None


def app_launch_cmd(key: str):
    """返回启动该应用的命令。找不到返回 None。

    微信没法用简单命令行启动（需要协议或已运行的实例），
    所以微信优先走 open_app() 的「已在运行就激活」分支。
    """
    cmds = {
        "notepad": ["notepad.exe"],
        "mspaint": ["mspaint.exe"],
        "chrome": ["chrome.exe"],
        "explorer": ["explorer.exe"],
    }
    return cmds.get(key)


# 窗口标题关键词（窗口标题/任务栏上可能叫这些，不等于 exe 名）
APP_WINDOW_KEYS = {
    "wechat":   ["微信"],
    "qq":       ["QQ"],
    "mspaint":  ["画图"],
    "notepad":  ["记事本", "Notepad"],
    "chrome":   ["Google Chrome", "Chrome"],
    "explorer": ["文件资源管理器", "此电脑"],
}


def activate_window(app_key):
    """把已运行的应用窗口切到前台。返回 (成功?, 窗口标题)。"""
    try:
        import uiautomation as auto
    except ImportError:
        return False, ""
    for kw in [app_key] + list(APP_WINDOW_KEYS.get(app_key, [])):
        w = auto.WindowControl(searchDepth=1, SubName=kw)
        if w.Exists(0.2):
            try:
                w.SetActive()
                time.sleep(0.2)
            except Exception:
                pass
            return True, (w.Name or kw)
    return False, ""


def describe_window(win_kw):
    """返回窗口的可读描述（标题 + 位置 + 大小）。找不到返回 ""。

    定位类任务必须**说清看到什么**，否则用户无法判断你到底定位到没。
    """
    try:
        import uiautomation as auto
    except ImportError:
        return ""
    for kw in [win_kw] + list(APP_WINDOW_KEYS.get(win_kw, [])):
        w = auto.WindowControl(searchDepth=1, SubName=kw)
        if w.Exists(0.2):
            try:
                r = w.BoundingRectangle
                title = w.Name or kw
                return ("窗口「%s」在屏幕 (%d,%d)，大小 %dx%d"
                        % (title[:30], r.left, r.top, r.width(), r.height()))
            except Exception:
                return "窗口「%s」已找到" % (kw,)
    return ""


def open_app(app_key):
    """打开**或激活**应用。

    ⚠️ **顺序很重要**（2026-10-04 用户真机测试踩到）：
    最初只写"用命令行启动"，结果用户**已经开着微信**，
    代码却说「无法用命令行启动，需要先在屏幕上定位图标」——
    **窗口明明就在那里**，却要用户手动点。

    正确顺序：
        1. 已经在跑 → 直接 SetActive 切前台（0.2 秒，最快）
        2. 没在跑   → 命令行启动
        3. 都不行   → 明确说"没找到窗口也没法启动"

    返回 (成功?, 说明)
    """
    import subprocess
    ok, title = activate_window(app_key)
    if ok:
        return True, "已在运行，已切到前台"
    cmd = app_launch_cmd(app_key)
    if cmd:
        try:
            subprocess.Popen(cmd)
        except Exception as e:
            return False, "启动失败：%s" % e
        time.sleep(2.5)
        ok2, _ = activate_window(app_key)
        if ok2:
            return True, "已启动并切到前台"
        return True, "已启动（窗口稍后才出现）"
    return False, ("找不到「%s」的窗口，也无法用命令行启动它。"
                   "请先手动打开这个应用，我再继续。" % app_key)


def is_allowed_app(proc_name: str):
    """前台窗口的进程是否在允许范围内。**别名精确/主体匹配**（非包含匹配）。"""
    p = (proc_name or "").lower().strip()
    if not p:
        return False
    for key in pc.APP_WHITELIST:
        for alias in APP_ALIASES.get(key, [key]):
            if _proc_matches_alias(p, alias.lower()):
                return True
    return False


# ============ 步骤数据结构 ============
class Step:
    """一个待执行步骤。

    action:
      click       点击 (x, y)
      dblclick    双击
      rclick      右击
      type        输入文本
      key         按键（必须是安全键）
      hotkey      组合键（需确认）
      scroll      滚动
      wait        等待
      launch      启动应用
      look        重新截图观察（视觉重试时用）
      stop        停在这里，等用户确认（不可逆动作前的闸）
    """
    __slots__ = ("action", "x", "y", "x2", "y2", "text", "keys", "amount",
                 "target_desc", "expect", "irreversible", "done", "result",
                 "target", "locate_as", "ocr_as", "window_kw")

    def __init__(self, action, x=None, y=None, text=None, keys=None,
                 amount=None, target_desc="", expect="", irreversible=False,
                 x2=None, y2=None, target=None, locate_as=None, ocr_as=None,
                 window_kw=None):
        self.action = action
        self.x, self.y = x, y
        self.x2, self.y2 = x2, y2
        self.text, self.keys, self.amount = text, keys, amount
        self.target_desc = target_desc
        self.expect = expect
        self.irreversible = irreversible
        # target:    不可逆动作的对象（收件人/文件名），用于白名单校验
        # locate_as: 坐标为空时用 UIA 按这个名字现取坐标
        # ocr_as:    UIA 取不到时用 OCR 按这个文字现取坐标
        self.target = target
        self.locate_as = locate_as
        self.ocr_as = ocr_as
        self.window_kw = window_kw   # 定位时用哪个窗口（子串）
        self.done = False
        self.result = ""

    def _pos(self):
        """坐标文本。坐标可能还没取（执行时才现取），所以不能直接 %d。"""
        if self.x is None or self.y is None:
            return "(定位中)"
        return "(%d,%d)" % (self.x, self.y)

    def brief(self):
        """一行描述，给用户看进度。"""
        a = self.action
        suf = "「%s」" % self.target_desc if self.target_desc else ""
        if a == "click":
            return "点击 %s%s" % (self._pos(), suf)
        if a == "dblclick":
            return "双击 %s%s" % (self._pos(), suf)
        if a == "rclick":
            return "右击 %s" % self._pos()
        if a == "drag":
            return "从 %s 拖到 %s" % (
                self._pos(),
                "(%d,%d)" % (self.x2, self.y2)
                if self.x2 is not None and self.y2 is not None
                else "(定位中)")
        if a == "type":
            return "输入「%s」" % ((self.text or "")[:30])
        if a == "key":
            return "按 %s" % (self.keys,)
        if a == "hotkey":
            return "组合键 %s" % (self.keys,)
        if a == "scroll":
            return "滚动 %d" % (self.amount or 0)
        if a == "launch":
            return "启动 %s" % (self.target_desc or self.text or "")
        if a == "wait":
            return "等待 %d 秒" % (self.amount or 1)
        if a == "look":
            return "重新观察屏幕"
        if a == "stop":
            return "⏸ 停下等你确认：%s" % (self.target_desc or "")
        return a

    def to_dict(self):
        return {"action": self.action, "x": self.x, "y": self.y,
                "text": self.text, "keys": self.keys, "amount": self.amount,
                "target_desc": self.target_desc, "expect": self.expect,
                "irreversible": self.irreversible, "done": self.done,
                "target": self.target, "locate_as": self.locate_as,
                "ocr_as": self.ocr_as, "window_kw": self.window_kw,
                "result": self.result}


# ============ 执行器 ============
class Executor:
    """按步骤列表执行，遇到不可逆动作停下。"""

    def __init__(self, steps, on_progress=None, max_look=3):
        self.steps = steps
        self.on_progress = on_progress        # 每步回调（用于飞书实时汇报）
        self.max_look = max_look
        self.stopped_at = None                # 停下的步骤序号
        self.stop_reason = ""
        self.rejected = False                 # True=被白名单拒绝（非暂停）
        self.look_count = 0
        self.fail_streak = 0
        self.log = []

    def _emit(self, idx, step, status):
        """汇报一步的结果。"""
        msg = {"i": idx + 1, "n": len(self.steps), "action": step.brief(),
               "status": status}
        if self.on_progress:
            try:
                self.on_progress(msg)
            except Exception:
                pass

    def run(self):
        """执行。返回 (是否完成, 汇报文本)。"""
        # ---- 第二道闸：目标白名单**预检**（在执行任何东西之前）----
        # ⚠️ 为什么必须预检而不是走到那一步再查：
        #   放在最后一步的话，前面任何一步失败（定位不到、窗口被抢）
        #   都会先熔断，**永远到不了不可逆那步**，白名单就成了摆设。
        #   **安全闸不能依赖"前面的步骤都成功"** —— 这是本轮实测抓到的缺陷。
        for i, st in enumerate(self.steps):
            if st.irreversible and not target_allowed(st.target):
                # 用 target_desc 而非 brief()：brief() 对 stop 步骤会输出
                # "⏸ 停下等你确认：…"，放在「已拒绝」里语义自相矛盾。
                self.stop_reason = (
                    "%s 的目标「%s」不在授权白名单里，我**不会**执行，"
                    "也不会发出去。"
                    % (st.target_desc or "这一步", st.target or "（没识别出目标）"))
                self.stopped_at = i
                self.rejected = True
                # status 用 "rejected: <理由>"，让回调能拿到正确语义，
                # 而不是靠下游过滤（过滤容易漏，且 action 文本本身是错的）
                self._emit(i, st, "rejected: " + self.stop_reason)
                return False, self.report()

        for i, st in enumerate(self.steps):
            if i >= pc.MAX_STEPS:
                self.stop_reason = "达到步数上限 %d，已停止" % pc.MAX_STEPS
                self.stopped_at = i
                break
            if st.irreversible:
                # ---- 已通过白名单预检 -> 停下确认 ----
                # 用户明确要求：日常使用一律停一下
                self.stopped_at = i
                self.stop_reason = ("%s 是不可逆动作，且目标是已授权的「%s」，"
                                    "等你确认。"
                                    % (st.target_desc or "这一步", st.target))
                self._emit(i, st, "stopped")
                break
            self._emit(i, st, "doing")
            try:
                ok, detail = self._exec_one(st)
            except TaskAborted as e:
                # 不可恢复的中止（视觉重试超限等）——立即停止整个任务，
                # 不当作"这一步失败"继续往下走。
                self.stopped_at = i
                self.stop_reason = str(e)
                st.done = False
                st.result = str(e)
                self._emit(i, st, "stopped")
                break
            st.done = ok
            st.result = detail
            self.log.append({"i": i + 1, "action": st.brief(),
                             "ok": ok, "detail": detail})
            if ok:
                self.fail_streak = 0
                self._emit(i, st, "ok")
            else:
                self.fail_streak += 1
                self._emit(i, st, "fail: " + detail)
                if self.fail_streak >= 2:
                    self.stop_reason = "「%s」连续失败 2 次，停下来汇报（可能陷入循环）" % st.brief()
                    self.stopped_at = i
                    break
        return self.stopped_at is None, self.report()

    def _resolve_pos(self, st, window_kw):
        """用定位层按名字/文字现取坐标。返回 (x, y) 或 None。"""
        try:
            import pc_locate
        except ImportError:
            return None
        if not window_kw:
            return None
        try:
            r = pc_locate.locate(window_kw,
                                 st.locate_as or [],
                                 ocr_texts=st.ocr_as or [st.ocr_as] if st.ocr_as else None)
            return r.center
        except Exception:
            return None

    def _exec_one(self, st):
        """执行单个步骤。返回 (成功?, 详情)。"""
        a = st.action
        try:
            # ---- 坐标现取：步骤没给坐标时，用定位层按名字/文字取 ----
            # 为什么在执行时才取而不是规划时预取：
            #   ① 规划到执行之间窗口可能移动/滚动，预取的坐标会过期
            #   ② OCR 一次 1~2 秒，只有真的要点的步骤才值得付这个时间
            if a in ("click", "dblclick", "rclick", "drag", "stop") \
                    and (st.x is None or st.y is None) \
                    and (st.locate_as or st.ocr_as):
                got = self._resolve_pos(st, st.window_kw or st.expect)
                if got is None:
                    return False, "定位不到「%s」" % (st.locate_as or st.ocr_as)
                st.x, st.y = got[0], got[1]
                if a == "drag" and st.x2 is None:
                    st.x2, st.y2 = got[0] + 120, got[1] + 90

            if a == "launch":
                key = st.text
                ok, detail = open_app(key or "")
                return ok, detail

            if a == "click":
                pc.click(st.x, st.y, step=self._stepno(), label=st.target_desc)
                time.sleep(0.4)
                return True, ""
            if a == "dblclick":
                pc.click(st.x, st.y, clicks=2, step=self._stepno(), label=st.target_desc)
                time.sleep(0.4)
                return True, ""
            if a == "rclick":
                pc.click(st.x, st.y, button="right", step=self._stepno(), label=st.target_desc)
                time.sleep(0.4)
                return True, ""
            if a == "drag":
                pc.drag(st.x, st.y, st.x2, st.y2, step=self._stepno(),
                        label=st.target_desc)
                time.sleep(0.4)
                return True, ""
            if a == "type":
                pc.type_text(st.text or "", step=self._stepno())
                time.sleep(0.2)
                return True, ""
            if a == "key":
                pc.tap(st.keys, step=self._stepno())
                time.sleep(0.2)
                return True, ""
            if a == "hotkey":
                # 组合键永远要确认。不可逆的已经在上层拦了，
                # 这里的组合键（如 Ctrl+V）虽不不可逆但也不自动做。
                return False, "组合键需要你确认，我不会自动执行"
            if a == "scroll":
                pc.scroll(st.amount or 0, step=self._stepno())
                time.sleep(0.2)
                return True, ""
            if a == "wait":
                time.sleep(min(int(st.amount or 1), 10))
                return True, ""
            if a == "look":
                # 先检查再计数。顺序反了会多跑一轮（原来的 bug：
                # 5 次 look 全部"成功"，max_look=3 完全没拦住）。
                if self.look_count >= self.max_look:
                    raise TaskAborted(
                        "已经重新观察 %d 次仍然无法定位，停下来汇报"
                        "（继续看屏幕只会越看越乱，需要你告诉我哪里不对）"
                        % self.look_count)
                self.look_count += 1
                # ⚠️ 别只说"已观察"却不说看到什么。
                # 2026-10-04 用户测试「那你定位啊」时，这一步什么都没报，
                # 用户完全不知道我到底看没看到。定位类任务必须给**具体位置**。
                win_kw = st.window_kw or st.target_desc or ""
                if win_kw:
                    info = describe_window(win_kw)
                    if info:
                        return True, info
                return True, "已重新观察（第 %d 次）" % self.look_count
        except pc.SafetyError as e:
            return False, "安全闸拒绝：%s" % str(e)[:80]
        except Exception as e:
            return False, "异常：%s" % str(e)[:80]
        return False, "未知动作 %s" % a

    def _stepno(self):
        return sum(1 for x in self.log if True) + 1

    def report(self):
        """生成执行汇报。"""
        if self.rejected:
            # 拒绝场景：没有任何动作被执行，不要显示空的「执行结果」标题
            lines = ["🚫 **已拒绝**"]
        else:
            lines = ["📋 **执行结果**"]
        if not self.log and not self.rejected:
            lines.append("")
            lines.append("（无已执行的步骤）")
        for e in self.log:
            mark = "✅" if e["ok"] else "❌"
            lines.append("%s 第%d步 %s%s"
                         % (mark, e["i"], e["action"],
                            ("  —— " + e["detail"]) if e["detail"] else ""))
        if self.stopped_at is not None:
            lines.append("")
            if self.rejected:
                lines.append(self.stop_reason)
                allow = target_list()
                lines.append("")
                if allow:
                    lines.append("已授权的目标：%s"
                                 % "、".join(a["name"] for a in allow))
                else:
                    lines.append("当前**没有任何**已授权的目标，"
                                 "所以所有发送类任务都会被拒绝。")
                lines.append("要授权可以发 `/target add <名字>`。")
            else:
                lines.append("⏸ **已停下**")
                lines.append(self.stop_reason)
                st = self.steps[self.stopped_at]
                if st.target_desc:
                    lines.append("")
                    lines.append("请确认这一步：%s" % st.target_desc)
                    lines.append("确认无误回复「继续」，我接着做；"
                                 "不确认就停在这里，不会替你点。")
        else:
            done = sum(1 for e in self.log if e["ok"])
            lines.append("")
            lines.append("全部完成：%d/%d 步成功。" % (done, len(self.steps)))
        return "\n".join(lines)
