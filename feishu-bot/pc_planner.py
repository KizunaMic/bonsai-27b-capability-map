# -*- coding: utf-8 -*-
"""
pc_planner.py —— 把一句自然语言变成可执行步骤

核心设计：规则优先，模型只做它擅长的事
--------------------------------------
为什么不直接让模型输出 JSON 步骤：
  1) Bonsai-2-27B 在坐标精度上不可靠（它会给"大概在右上角"这种描述）
  2) JSON 格式遵循率不稳定，04 文档记录的 FIXED_POINT/EMPTY 病会复发
  3) 出错时无法定位是"理解错"还是"格式错"

所以拆成两类工作：

  【规则做】意图 → 步骤骨架
    "打开微信"        → launch wechat
    "把文件发给张三"   → stop(等确认) + 说明
    "画个圆"          → launch mspaint → 选择画笔 → 画圆
    这些有固定模式，正则可靠、零延迟、可测试。

  【模型做】只有一件事：**在截图上定位坐标**
    "桌面上那个叫方案的图标在哪" → 模型给 (x, y)
    这是视觉定位，规则做不了，必须靠模型。而这个任务
    比"生成 JSON 步骤"简单得多，精度也更高（我们只要一个点）。

调用方负责：先截图 → 调模型定位 → 拿到坐标 → 填进规则生成的骨架。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pc_task as pt

# ============ 常见应用 ============
APP_WORDS = [
    # (正则, app_key, 显示名)
    (r"微信|wechat|weixin", "wechat", "微信"),
    (r"画图|画板|绘图|paint|mspaint", "mspaint", "画图"),
    (r"记事本|notepad", "notepad", "记事本"),
    (r"浏览器|chrome|谷歌", "chrome", "Chrome"),
    (r"qq|企鹅", "qq", "QQ"),
    (r"资源管理器|文件夹|我的电脑|explorer", "explorer", "资源管理器"),
]


def _find_app(text):
    for pat, key, name in APP_WORDS:
        if re.search(pat, text, re.I):
            return key, name
    return None, None


# ============ 任务意图识别 ============
# 每个意图返回一个"步骤骨架"（可能不含坐标，需要视觉补全）
def _is_paint_task(t):
    """画图类任务。"""
    return bool(re.search(r"画|绘制|画个|画一", t)) and \
        bool(re.search(r"圆|方|三角|线|星|字|涂|色", t))


def _is_send_task(t):
    """发送/传输类任务（不可逆）。

    ⚠️ 动词要覆盖「发出去」「发过来」「转发」这些变体。
    最初只写 `发给|发到|发送|传给`，结果「把文件发出去」不匹配，
    掉到了 open_app 分支——**用户说"发出去"却只打开了应用**，很糟。
    """
    # ⚠️ 动词要覆盖大量口语变体。**今天已在同一个坑里栽了三次**
    # （截图指令、鼠标移动语序、这里的"发一句"）。
    #
    # 「发文件给王五」这种"发+宾语+给+人"的结构里，"发给"不连写，
    # 所以除了固定搭配，还要有 `发.{0,6}给` 这种带容错的模式。
    return bool(re.search(
        r"发给|发到|发送|传给|发出去|发过来|发我|发个|发一句|发条|留言|"
        r"分享给|share\s*to|send\s*to|forward", t, re.I)) or \
        bool(re.search(r"发[^，。]{0,6}给", t)) or \
        bool(re.search(r"发[^，。]{0,4}(?:话|消息|内容|信息|东西|字)", t))


def _is_locate_task(t):
    """定位/查找类：把窗口切到前台并报告位置（只读 + 激活，无不可逆风险）。

    2026-10-04 用户真机测试新增：用户说「打开微信」失败后追问
    「那你定位啊」，补全后是「微信 定位」——之前完全没有这个意图。
    """
    return bool(re.search(r"定位|找一下|找找|找到|搜索一下|看下窗口|"
                          r"window\s*locate|locate", t, re.I))


def _is_open_app(t):
    """打开应用。"""
    return bool(re.match(r"^\s*(打开|开启|启动|运行|点开|open)\s*", t, re.I))


def _has_irreversible(t):
    return bool(re.search(r"|".join(pt.IRREVERSIBLE[:24]), t, re.I))


# ============ 图形绘制库 ============
SHAPES = {
    "圆":  ("ellipse",  (700, 500), (900, 700)),
    "正方": ("rect",     (700, 500), (900, 700)),
    "方":   ("rect",     (700, 500), (900, 700)),
    "三角": ("triangle", (700, 500), (900, 700)),
    "直线": ("line",     (700, 500), (1000, 700)),
    "横线": ("line",     (700, 500), (1000, 700)),
    "竖线": ("vline",    (700, 500), (700, 800)),
    "星":   ("star",     (700, 500), (900, 700)),
    "方块": ("rect",     (700, 500), (900, 700)),
}


def _shape_of(t):
    for k in SHAPES:
        if k in t:
            return k, SHAPES[k]
    return None, None


# ============ 主入口：生成计划 ============
def plan(user_text, coords=None):
    """把一句自然语言变成执行计划。

    coords: 视觉定位结果，形如
        {"icon_方案": (x, y), "btn_发送": (x, y), ...}
        没有视觉定位时传 None，坐标类步骤会被标为 pending。

    返回 (steps, meta)
      steps: [Step, ...]
      meta:  {"kind": ..., "app": ..., "need_confirm": bool, "note": str}
    """
    t = (user_text or "").strip()
    coords = coords or {}
    app_key, app_name = _find_app(t)
    steps = []

    # ---------- 画图任务 ----------
    shape_k, shape_info = _shape_of(t)
    if _is_paint_task(t) or (app_key == "mspaint" and shape_k):
        if not app_key:
            app_key, app_name = "mspaint", "画图"
        steps.append(pt.Step("launch", target_desc=app_name, text=app_key,
                             expect="窗口打开"))
        if shape_k:
            kind, (x1, y1), (x2, y2) = shape_info
            # 画笔选择：需要视觉定位
            bx, by = coords.get("brush", (60, 200))
            steps.append(pt.Step("click", x=bx, y=by,
                                 target_desc="画笔工具", expect="画笔被选中"))
            # 颜色
            cx, cy = coords.get("color", (120, 260))
            steps.append(pt.Step("click", x=cx, y=cy,
                                 target_desc="颜色", expect="颜色被选中"))
            # 画
            steps.append(pt.Step("drag", x=x1, y=y1, target_desc="起点"))
            steps[-1].keys = (x2, y2)
            steps.append(pt.Step("look", target_desc="确认画出来了"))
        return steps, {"kind": "paint", "app": app_name,
                       "need_confirm": False,
                       "note": "画%s" % (shape_k or "图")}

    # ---------- 发送类任务（不可逆） ----------
    if _is_send_task(t):
        who = _extract_recipient(t)      # 收件人（白名单校验对象）
        # ⚠️ 不可逆闸必须放在**第一步**，不能放在最后。
        # 原因：如果放在最后，前面任何一步失败（定位不到、窗口被抢）
        # 都会先熔断，**永远到不了确认那一步** → 用户永远不会看到
        # 「要发给谁」的确认，等于把不可逆动作藏起来了。
        # 先把「我要发什么给谁」摆出来让你确认，再做前面的准备动作。
        # target_desc 单独存确认文案；Step.brief() 用于进度汇报，
        # 不能带"⏸ 停下等你确认"——拒绝场景下会出现语义矛盾
        # （🚫 已拒绝：「⏸ 停下等你确认…」）。
        steps.append(pt.Step(
            "stop", target_desc=_describe_send(t),
            irreversible=True, target=who,     # ← 白名单校验对象
            window_kw=app_name,
            locate_as=coords.get("submit_name") or "发送",
            ocr_as="发送"))
        # 确认之后才做的准备动作
        if app_key:
            steps.append(pt.Step("launch", target_desc=app_name, text=app_key))
        if coords.get("file") or "文件" in t or "附件" in t:
            steps.append(pt.Step("click", target_desc="目标文件",
                                 window_kw=app_name,
                                 locate_as=coords.get("file_name"),
                                 ocr_as=None))
        if who:
            steps.append(pt.Step("click", target_desc="收件人「%s」" % who,
                                 window_kw=app_name,
                                 locate_as=who, ocr_as=who))
        steps.append(pt.Step("look", target_desc="确认界面已就绪"))
        return steps, {"kind": "send", "app": app_name or "目标应用",
                       "need_confirm": True, "recipient": who,
                       "note": "发送是不可逆动作，需你确认",
                       "confirm_desc": _describe_send(t)}

    # ---------- 打开应用 ----------
    # ---------- 定位类：把窗口切前台并报告位置 ----------
    # 「那你定位啊」这类追问补全后会变成「微信 定位」。
    # 2026-10-04 用户真机测试：之前没有这个分支，bot 完全听不懂，
    # 回了一句「你好」。定位本身是**只读 + 激活**，没有不可逆风险。
    if _is_locate_task(t):
        if not app_key:
            return [pt.Step("stop", target_desc="没认出要定位哪个应用：" + t,
                            irreversible=False)], \
                   {"kind": "unknown_app", "need_confirm": False,
                    "note": "无法识别应用名"}
        steps.append(pt.Step("launch", target_desc=app_name, text=app_key))
        steps.append(pt.Step("look", target_desc=app_name, window_kw=app_name))
        return steps, {"kind": "locate", "app": app_name,
                       "need_confirm": False, "note": "定位" + app_name}

    if _is_open_app(t):
        if not app_key:
            # "打开xxx" 但认不出是什么应用 → 需要视觉定位桌面图标
            return [pt.Step("stop", target_desc="没认出是哪个应用：" + t,
                            irreversible=False)], \
                   {"kind": "unknown_app", "need_confirm": False,
                    "note": "无法识别应用名"}
        steps.append(pt.Step("launch", target_desc=app_name, text=app_key))
        return steps, {"kind": "open_app", "app": app_name,
                       "need_confirm": False, "note": "打开" + app_name}

    # ---------- 兜底：有不可逆词 → 先停下 ----------
    if _has_irreversible(t):
        steps.append(pt.Step("stop", target_desc=t[:60], irreversible=True))
        return steps, {"kind": "risky", "need_confirm": True,
                       "note": "含高危关键词"}

    return steps, {"kind": "unknown", "need_confirm": False, "note": "未识别"}


def _extract_recipient(t):
    """从用户原话里提取**收件人**（白名单校验对象）。

    返回空字符串表示没识别出收件人 —— 会被执行器判为"未授权"而拒绝，
    因为空名单 = 全部拒绝。**不猜**是这里的关键。

    ⚠️ 中文里"发"有两种完全不同的用法，必须区分：
        A. 发给某人   「把方案**发给**张三」   -> 动词后是收件人
        B. 发一段内容 「给文件助手**发一句**你好」-> 动词后是**内容**
    误判后果：把内容当收件人 -> 白名单拿"你好"去比对 -> 永远"未授权"
    -> 拒绝。安全上没错，但用户永远发不出去。

    两种结构的判据：
        「发给/发到/传给/发给到」= 必带对象 -> 其后是收件人
        「发一句/发个/发条」     = 内容动词 -> 其后**不是**收件人
        纯「给X」              -> X 是收件人（"微信发个消息给X"）
    """
    # 先找明确带方向的动词「发给X」「发到X」「传给X」
    # ⚠️ 字符类里必须有 `+`（量词），否则 `[^...]` 只吃**一个**字符：
    #    「发给张三」会变成收件人="张"。这是本轮第三次栽在正则细节上。
    m = None
    for mm in re.finditer(
            r"(?:发给|发到|传给|发过来|share\s*to|send\s*to)\s*"
            r"([^\s，,。的地得和跟了是要]+)", t, re.I):
        m = mm
    if m:
        return m.group(1).strip()

    # 再找「…给X」（内容动词在前、收件人在"给"后）
    # ⚠️ 收件人后面可能还跟着谓语：「给文件传输助手**发一句你好**」
    #    所以要在下一个动词/助词处截断，不能一路吃到句尾。
    g = re.search(r"给\s*([^\s，,。的地得和跟了是要]+)", t)
    if g:
        w = g.group(1)
        # 截断到第一个动词/助词（「发」「说」「写」「告诉」等）
        cut = re.search(r"(发|说|写|告诉|问|回|聊|讲|提醒)", w)
        if cut and cut.start() > 0:
            w = w[:cut.start()]
        return w.strip()

    return ""


def _describe_send(t):
    """生成「发什么 → 发给谁」的确认文案。

    ⚠️ 这个文案是用户做发送前**唯一的确认依据**，提取错 = 确认机制失效。
    所以必须复用 `_extract_recipient` 的解析结果——之前这里自己写了一套
    独立的正则，两边不一致，导致文案说"没说要发给谁"而白名单其实放行了
    （「点开微信给文件传输助手发一句你好」就是这个 bug）。
    """
    who = _extract_recipient(t) or "（没说要发给谁）"

    # 把收件人那部分整段切掉，剩下的才是内容/文件名
    rest = t
    for pat in (r"给\s*([^\s，,。的地得和跟了是要]+)",
                r"(?:发给|发到|传给|发过来)\s*([^\s，,。的地得和跟了是要]+)"):
        g = re.search(pat, t, re.I)
        if g:
            rest = t[:g.start()] + t[g.end():]
            break

    # 剥掉开头的操作指令（"点开微信"、"打开微信"），它们不是要发的东西
    rest = re.sub(r"^\s*(?:请|帮我|帮忙)?\s*"
                  r"(?:打开|开启|启动|运行|点开)\s*"
                  r"(?:微信|wechat|weixin|qq|钉钉|画图|记事本)?\s*",
                  "", rest, flags=re.I)
    # 剥掉应用名残留
    rest = re.sub(r"(?:微信|wechat|weixin|qq|钉钉)\s*", "", rest, flags=re.I)

    what = re.sub(r"^\s*(?:把|将|从)?\s*", "", rest)
    what = re.sub(r"[\s，,。]+$", "", what).strip()
    what = re.sub(r"^(?:桌面上|我的电脑|电脑上|本地)?\s*的?\s*", "", what).strip()
    # 切掉收件人后可能残留动词尾巴：「把方案发|给张三」-> 「那个方案发」
    what = re.sub(r"[\s，,。]*(?:发|给|传|送|说|写)\s*$", "", what).strip()

    # 只发一句话内容时，what 应该是那句话本身
    if not what:
        return "准备发送：（未指明内容）→ %s" % who
    return "准备发送：「%s」→ %s" % (what[:40], who)
