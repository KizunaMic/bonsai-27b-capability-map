# -*- coding: utf-8 -*-
"""
pc_locate.py —— 三层定位层（阶段 5-4）

要解决的问题
------------
两个模型都**没有 grounding 能力**（实测：纯色圆点偏 1400px），
「看截图 → 模型报坐标」这条路走不通。

但坐标不一定需要模型给。本模块用三层确定性工具拿到坐标：

  第1层 UIA 控件树        0.04s，精确，语义明确    ← 主力
  第2层 Windows OCR       0.5~2s，认文字给坐标     ← 兜底
  第3层 模板匹配          图标类按钮                ← 最后手段

**模型只负责输出"要找什么"，绝不输出坐标。**

今天实测的覆盖率（决定优先级）
----------------------------
| 应用 | 技术栈 | UIA 按钮 | 结论 |
|---|---|---|---|
| 计算器 | UWP/WinUI | 36 | 完美 |
| 画图 | 标准 WinUI | 36 | 完美 |
| 文件资源管理器 | Shell | 27 | 完美 |
| 写字板 | 传统 Win32 | 14 | 完美 |
| QQ音乐 | TXGuiFoundation（腾讯自研） | 25 | 完美 |
| Bandizip | 压缩工具 | 3 | 可用 |
| **微信 4.0** | **Qt + MMUIRenderSubWindowHW** | **12（仅导航栏）** | **聊天区不透明** |

**微信的实测结论（关键）**：
- 窗口类 `Qt51514QWindowIcon`（Qt 框架，**不是 Chromium，所以 CDP 那条路无效**）
- UIA 树在 `MMUIRenderSubWindowHW`（1320x1088）处**断了**，
  导航栏（微信/通讯录/收藏/发现）能读，**聊天列表、联系人、输入框全在黑盒里**
- 但 **OCR 精确定位到了「文件传输助手」(1062, 308)** —— UIA 完全找不到它

**所以两层不是冗余，是互补**：左导航走 UIA，聊天区走 OCR。

中文字符合并（今天踩到并解决的坑）
------------------------------------
`winocr` 对中文**逐字返回 bbox**，6 个字是 6 个独立矩形，
不合并的话永远匹配不到「文件传输助手」。必须做相邻字符合并。
实测：微信窗口内 178 词 -> 合并后 44 词，「文件传输助手」精确命中。

⚠️ OCR 必须先裁剪到目标窗口
---------------------------
不裁剪的话会把整个屏幕（含 Agent 自己的终端输出）都读进来，
"命中文件传输助手"可能命中的是命令行里的字——**假阳性**。
今天实测踩到过：全屏 OCR 时「文件传输助手」命中的是我自己的终端回显。
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 匹配优先级：AutomationId 最稳，Name 次之
# 顺序参考已安装 skill 的 references/cef-fallbacks.md 第 3 节


class LocateError(RuntimeError):
    """三层都没找到。message 要能直接转达给用户。"""


class Located:
    """一个定位结果。"""
    __slots__ = ("x", "y", "w", "h", "source", "label", "rect")

    def __init__(self, x, y, w, h, source, label="", rect=None):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.source = source            # uia / ocr / template
        self.label = label              # 实际匹配到的控件名/文字
        self.rect = rect                # 原始矩形（若有）

    @property
    def center(self):
        return (self.x + self.w // 2, self.y + self.h // 2)

    def __repr__(self):
        return "Located(%d,%d %dx%d via %s '%s')" % (
            self.x, self.y, self.w, self.h, self.source, self.label)


# ---------------- 第1层：UIA ----------------
def find_uia(window_kw, control_name, timeout=0.4, activate=True):
    """按名字在**目标窗口内**找控件。

    ⚠️ **三个必须遵守的点**（都是今天实测踩到的）：

    1. **不要限定 ControlType**
       Ribbon 工具栏里「铅笔」的外层是 ListItemControl，
       限定 ButtonControl 会漏掉。必须用 auto.Control(SubName=...)。

    2. **必须从目标窗口往下搜，不能用全局 auto.Control()**
       ⚠️ 这是个**安全问题**：`auto.Control(searchDepth=12, SubName='搜索')`
       是从**桌面根**开始搜的，会命中任务栏上的搜索图标。
       实测：在微信窗口里找「搜索」，返回了 (1149,1564) —— 那是任务栏！
       后果是**点击会打到别的窗口上**。
       所以必须用 `win.Control(searchDepth=..., SubName=...)`，从窗口节点往下找。

    3. **必须校验命中结果在窗口矩形内**
       纵深防御：即使从窗口往下搜，也校验一次矩形包含关系。
    """
    try:
        import uiautomation as auto
    except ImportError:
        return None
    try:
        win = auto.WindowControl(searchDepth=1, SubName=window_kw)
        if not win.Exists(timeout):
            win = auto.WindowControl(searchDepth=1, ClassName=window_kw)
        if not win.Exists(0.15):
            return None
        if activate:
            try:
                win.SetActive()
                time.sleep(0.15)
            except Exception:
                pass
        wr = win.BoundingRectangle
        win_box = (wr.left, wr.top, wr.left + wr.width(), wr.top + wr.height())

        # ⚠️ 从窗口节点往下搜，不是全局 auto.Control()
        ctrl = win.Control(searchDepth=10, SubName=control_name)
        if not ctrl.Exists(timeout):
            return None
        r = ctrl.BoundingRectangle
        if r.width() <= 0 or r.height() <= 0:
            return None
        # 纵深防御：必须在窗口矩形内
        cx, cy = r.left + r.width() // 2, r.top + r.height() // 2
        if not (win_box[0] <= cx <= win_box[2] and win_box[1] <= cy <= win_box[3]):
            return None      # 命中窗口外 -> 视为未找到，交给 OCR
        return Located(r.left, r.top, r.width(), r.height(),
                       "uia", ctrl.Name or control_name, r)
    except Exception:
        return None


def find_uia_multi(window_kw, control_names, timeout=0.3):
    """依次尝试多个候选名，返回第一个命中的。"""
    for n in control_names:
        got = find_uia(window_kw, n, timeout)
        if got:
            return got
    return None


def dump_uia(window_kw, max_depth=6, limit=400):
    """导出控件树摘要（诊断用）。返回 (总数, 命名数, 按钮名列表, 最大深度)。"""
    import uiautomation as auto
    win = auto.WindowControl(searchDepth=1, SubName=window_kw)
    if not win.Exists(0.3):
        return None
    st = {"t": 0, "n": 0, "b": [], "d": 0}

    def walk(c, d):
        if d > max_depth or st["t"] >= limit:
            return
        st["d"] = max(st["d"], d)
        try:
            kids = c.GetChildren()
        except Exception:
            return
        for k in kids:
            st["t"] += 1
            try:
                nm = k.Name or ""
                if nm:
                    st["n"] += 1
                    if "Button" in k.ControlTypeName:
                        st["b"].append(nm)
            except Exception:
                pass
            walk(k, d + 1)

    walk(win, 0)
    return st


# ---------------- 第2层：Windows OCR ----------------
def merge_cjk_chars(words, y_tol=12, gap_ratio=0.9):
    """把同一行内水平相邻的单字合并成词。

    **这是中文 OCR 的必需后处理**，今天实测踩到：
    winocr 把「文件传输助手」拆成 6 个独立 bbox（文/件/传/输/助/手），
    不合并就永远匹配不到整个词。
    实测：微信窗口 178 词 -> 44 词，合并后精确命中。
    """
    if not words:
        return []
    rows = {}
    for t, r in words:
        rows.setdefault(r["y"] // y_tol, []).append((t, r))
    out = []
    for key in sorted(rows):
        items = sorted(rows[key], key=lambda x: x[1]["x"])
        cur_t, cur_r = items[0]
        for t, r in items[1:]:
            gap = r["x"] - (cur_r["x"] + cur_r["width"])
            close_x = gap <= max(cur_r["width"], r["width"]) * gap_ratio
            same_y = abs(r["y"] - cur_r["y"]) < y_tol
            if close_x and same_y:
                x0 = min(cur_r["x"], r["x"])
                y0 = min(cur_r["y"], r["y"])
                x1 = max(cur_r["x"] + cur_r["width"], r["x"] + r["width"])
                y1 = max(cur_r["y"] + cur_r["height"], r["y"] + r["height"])
                cur_t += t
                cur_r = {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}
            else:
                out.append((cur_t, cur_r))
                cur_t, cur_r = t, r
        out.append((cur_t, cur_r))
    return out


def ocr_window(window_kw=None, lang="zh-hans-cn", max_side=1600):
    """对目标窗口做 OCR，返回合并后的 [(文字, 矩形)]，坐标已换算成屏幕坐标。

    ⚠️ **必须传 window_kw 裁剪到窗口**。
    今天实测踩到：不裁剪会把整个屏幕（含 Agent 自己的终端输出）都读进来，
    "命中文件传输助手"可能命中的是命令行回显——假阳性。
    """
    try:
        import mss
        import winocr
        from PIL import Image
    except ImportError:
        return []

    region = None
    if window_kw:
        try:
            import uiautomation as auto
            win = auto.WindowControl(searchDepth=1, SubName=window_kw)
            if not win.Exists(0.25):
                win = auto.WindowControl(searchDepth=1, ClassName=window_kw)
            if win.Exists(0.2):
                r = win.BoundingRectangle
                region = {"left": r.left, "top": r.top,
                          "width": r.right - r.left,
                          "height": r.bottom - r.top}
        except Exception:
            region = None

    with mss.MSS() as sct:
        if region:
            shot = sct.grab(region)
        else:
            shot = sct.grab(sct.monitors[1])
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    if max(img.size) > max_side:
        ratio = float(max_side) / max(img.size)
        img = img.resize((int(img.size[0] * ratio), int(img.size[1] * ratio)),
                         Image.LANCZOS)
        scale = ratio
    else:
        scale = 1.0

    res = None
    for lg in (lang, "zh-CN", "en-US"):
        try:
            res = winocr.recognize_pil_sync(img, lg)
            break
        except Exception:
            continue
    if not res:
        return []

    raw = []
    for line in res.get("lines", []):
        for wd in line.get("words", []):
            t = wd.get("text", "")
            if t.strip():
                raw.append((t, wd["bounding_rect"]))
    merged = merge_cjk_chars(raw)

    off_x = region["left"] if region else 0
    off_y = region["top"] if region else 0
    inv = 1.0 / scale if scale else 1.0
    out = []
    for t, r in merged:
        out.append((t, {
            "x": int(r["x"] * inv) + off_x,
            "y": int(r["y"] * inv) + off_y,
            "width": int(r["width"] * inv),
            "height": int(r["height"] * inv),
        }))
    return out


def find_ocr(window_kw, text, lang="zh-hans-cn", exact=False):
    """在目标窗口里用 OCR 找文字。返回 Located 或 None。"""
    rows = ocr_window(window_kw, lang)
    if not rows:
        return None
    cands = []
    for t, r in rows:
        if (t == text) if exact else (text in t):
            cands.append((t, r))
    if not cands:
        return None
    # 优先选字形最接近的（宽高差最小的），避免匹配到奇怪的长句
    t, r = min(cands, key=lambda x: abs(x[1]["width"] - len(text) * 18))
    return Located(r["x"], r["y"], r["width"], r["height"], "ocr", t, r)


# ---------------- 统一入口 ----------------
def locate(window_kw, names, ocr_texts=None, allow_ocr=True,
           lang="zh-hans-cn"):
    """三层定位统一入口。

    names:     UIA 候选名列表（按优先级）
    ocr_texts: OCR 候选文字列表（UIA 失败后用）

    返回 Located（含 source 字段标明来自哪一层），全失败抛 LocateError。
    """
    if isinstance(names, str):
        names = [names]
    # 第1层 UIA
    got = find_uia_multi(window_kw, names)
    if got:
        return got
    # 第2层 OCR
    if allow_ocr and ocr_texts:
        if isinstance(ocr_texts, str):
            ocr_texts = [ocr_texts]
        for t in ocr_texts:
            g = find_ocr(window_kw, t, lang)
            if g:
                return g
    raise LocateError(
        "在「%s」里找不到 %s。\n"
        "UIA 控件树和 OCR 都没匹配到。\n"
        "可能原因：窗口没激活 / 名字不匹配 / 界面元素没有文字标签。\n"
        "可以先截个屏发给我，我看看界面长什么样。"
        % (window_kw, "、".join(names + (ocr_texts or []))))


if __name__ == "__main__":
    import json
    win = sys.argv[1] if len(sys.argv) > 1 else "画图"
    name = sys.argv[2] if len(sys.argv) > 2 else "铅笔"
    print("=" * 60)
    print("三层定位层自测")
    print("=" * 60)
    st = dump_uia(win)
    if st:
        print("UIA: 控件 %d 命名 %d 按钮 %d 深度 %d"
              % (st["t"], st["n"], len(st["b"]), st["d"]))
        print("     按钮: %s" % " / ".join(st["b"][:10]))
    try:
        r = locate(win, name, ocr_texts=[name])
        print("定位「%s」-> %r" % (name, r))
        print("中心坐标: %s" % str(r.center))
    except LocateError as e:
        print("定位失败:", e)
