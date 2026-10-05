# -*- coding: utf-8 -*-
"""
uia_probe.py —— UI Automation 控件树探针（阶段 5-4 立项依据）

背景
----
两个模型都**没有 grounding 能力**（实测：纯色圆点偏 1400px），
「看截图 → 模型报坐标」这条路死了。

但坐标**不一定需要模型给**。Windows 的 UI Automation 能把窗口里的
控件树直接读出来——控件名 + 精确坐标都是系统给的，零推理、毫秒级。

本脚本**只读不写**，回答一个问题：
**你机器上的目标应用能暴露出多少可定位的控件？**

为什么用 uiautomation 这个库
---------------------------
纯 ctypes 调 COM 走 QI 极其脆弱：
- `CoCreateInstance(CLSID, ..., IID_IUIAutomation)` → E_NOINTERFACE（0x80004002）
  因为 IUIAutomation 的 IID 猜不对
- `win32com.client.Dispatch` → 「不支持此接口」（late binding 拿不到方法表）
- `comtypes` 在本机是**残包**（import 成功但 `__file__` 为 None）
- `LoadTypeLibEx` 能读类型库，但 GetLibAttr 会 access violation

`uiautomation` 是纯 Python + ctypes 的成熟封装，上述问题全绕开。
之前在 COM 上花了 20 分钟是**方向性错误**——该早点想到用库。
"""
import sys
import time

import uiautomation as auto

# 只探测白名单内的应用
TARGETS = [
    ("画图", "画图"),
    ("记事本", "记事本"),
]


def probe(title_kw, max_depth=6, limit=500):
    """探测一个窗口的控件树，返回统计。"""
    t0 = time.time()
    win = auto.WindowControl(searchDepth=1, SubName=title_kw)
    if not win.Exists(0.2):
        return None
    win.SetActive()
    time.sleep(0.3)

    stats = {"total": 0, "named": 0, "buttons": [], "edits": [],
             "canvases": [], "by_type": {}, "rows": []}

    def walk(ctrl, depth):
        if depth > max_depth or stats["total"] >= limit:
            return
        try:
            n = ctrl.GetChildren()
        except Exception:
            return
        for c in n:
            stats["total"] += 1
            try:
                ct = c.ControlTypeName
                nm = c.Name or ""
                stats["by_type"][ct] = stats["by_type"].get(ct, 0) + 1
                if nm:
                    stats["named"] += 1
                    r = c.BoundingRectangle
                    box = (r.left, r.top, r.right - r.left, r.bottom - r.top)
                    stats["rows"].append((ct, nm, box, depth))
                    if ct == "ButtonControl":
                        stats["buttons"].append((nm, box))
                    elif ct in ("EditControl", "DocumentControl"):
                        stats["edits"].append((nm, box))
                    elif ct in ("ImageControl", "CustomControl",
                                "PaneControl"):
                        stats["canvases"].append((nm, box))
            except Exception:
                pass
            walk(c, depth + 1)

    walk(win, 0)
    stats["elapsed"] = time.time() - t0
    stats["title"] = win.Name
    return stats


def main():
    print("=" * 76)
    print("UI Automation 控件树探针（只读，不做任何修改）")
    print("=" * 76)
    print()
    print("核心问题：既然模型不能给坐标，Windows 能不能直接告诉我们控件在哪？")
    print()

    summary = []
    for kw, label in TARGETS:
        print("=" * 76)
        print("【%s】" % label)
        print("=" * 76)
        st = probe(kw)
        if st is None:
            print("  未找到窗口（应用未运行？）\n")
            summary.append((label, None))
            continue

        print("窗口      : %s" % st["title"])
        print("耗时      : %.2f 秒" % st["elapsed"])
        print("控件总数  : %d" % st["total"])
        print("有名称    : %d" % st["named"])
        print("类型分布  : %s" % ", ".join(
            "%s×%d" % (k.replace("Control", ""), v)
            for k, v in sorted(st["by_type"].items(),
                               key=lambda x: -x[1])[:8]))
        print()
        if st["buttons"]:
            print("  ▸ 可按名字定位的按钮（%d 个）：" % len(st["buttons"]))
            for nm, (x, y, w, h) in st["buttons"][:18]:
                print("      「%s」 (%d,%d %dx%d)" % (nm[:28], x, y, w, h))
        if st["edits"]:
            print("  ▸ 编辑区：")
            for nm, (x, y, w, h) in st["edits"][:5]:
                print("      「%s」 (%d,%d %dx%d)" % (nm[:28], x, y, w, h))
        if not st["buttons"] and st["named"] < 5:
            print("  ⚠️ 几乎没有可定位的命名控件 → 该应用是自绘界面，UIA 基本无效")
        print()
        summary.append((label, st))

    print("=" * 76)
    print("汇总与判读")
    print("=" * 76)
    for label, st in summary:
        if st is None:
            print("  %-8s 未运行" % label)
            continue
        usable = len(st["buttons"])
        verdict = ("✅ UIA 可用" if usable >= 5 else
                   "⚠️ UIA 部分可用" if usable >= 1 else
                   "❌ UIA 无效（需 OCR）")
        print("  %-8s 控件%3d 命名%3d 按钮%3d  %s"
              % (label, st["total"], st["named"], usable, verdict))

    print()
    print("判读标准：")
    print("  按钮 >= 5  → 可做「按控件名定位」，模型只需说名字，不用给坐标")
    print("  按钮 1-4   → 部分可用，配合坐标兜底")
    print("  按钮 = 0   → 自绘界面，UIA 无效，必须用 Windows OCR 找文字")
    print()
    print("对比：模型定位 40-70 秒且偏 1400px  vs  UIA 定位 <0.1 秒且精确")
    print("=" * 76)
    return 0


if __name__ == "__main__":
    sys.exit(main())
