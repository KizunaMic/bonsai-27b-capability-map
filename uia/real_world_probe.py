# -*- coding: utf-8 -*-
"""
real_world_probe.py —— 在**你真实机器上已安装的应用**里实测 UIA 覆盖率

为什么做这个
------------
用户说「不止微信，还有别的很多莫名其妙的应用，我们要举一反三」。
而实测发现：**微信/QQ 根本没装在这台机器上**。
真正需要覆盖的是这里实际存在的应用 —— 包括一堆 Electron/CEF 自绘的。

目的：在真实、不可控的应用集合上，测出 UIA 到底能覆盖多少，
从而决定三层链路里哪几层是必需的。
"""
import os
import subprocess
import sys
import time

import uiautomation as auto

# 实际存在的、UI 技术栈各异的应用
# Electron/CEF 类：Qzone、bluestacks、chatbox、mod.io、bililive、qBittorrent?
# 原生类：Qodoli(fonger)、极速版
TARGETS = [
    ("Qzone",        "腾讯空间",      "CEF/Electron"),
    ("bluestacks",   "BlueStacks",    "自绘/Android容器"),
    ("chatbox",      "Chatbox",       "Electron"),
    ("qBittorrent",  "qBittorrent",   "Qt"),
    ("fonger",       "极速 PDF",       "Electron"),
    ("mod.io",       "mod.io",        "Electron"),
    ("bililive",     "哔哩哔哩直播",   "Electron"),
    ("XDown",        "XDown",         "Electron"),
    ("shiyi",        "格式工厂",      "原生"),
]


def scan(kw, depth=7, limit=800):
    """UIA 扫描。返回统计字典。"""
    w = auto.WindowControl(searchDepth=1, SubName=kw)
    if not w.Exists(0.25):
        # 试按类名找
        return None
    try:
        w.SetActive()
        time.sleep(0.3)
    except Exception:
        pass
    st = {"total": 0, "named": 0, "buttons": [], "types": {}, "depth_max": 0}

    def walk(c, d):
        if d > depth or st["total"] >= limit:
            return
        st["depth_max"] = max(st["depth_max"], d)
        try:
            kids = c.GetChildren()
        except Exception:
            return
        for k in kids:
            st["total"] += 1
            try:
                ct = k.ControlTypeName.replace("Control", "")
                st["types"][ct] = st["types"].get(ct, 0) + 1
                nm = k.Name or ""
                if nm:
                    st["named"] += 1
                    if "Button" in k.ControlTypeName:
                        st["buttons"].append(nm)
            except Exception:
                pass
            walk(k, d + 1)

    walk(w, 0)
    return st


def main():
    print("=" * 80)
    print("真实应用 UIA 覆盖率实测（只读）")
    print("=" * 80)
    print()
    print("背景：这台机器上微信/QQ 并未安装。真正要覆盖的是下列实际应用。")
    print("它们横跨 Electron / CEF / Qt / 自绘 / Android 容器等多种技术栈。")
    print()

    running = []
    print("当前已在运行的相关进程：")
    for kw, cn, kind in TARGETS:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s.exe" % kw],
                           capture_output=True)
        out = r.stdout.decode("gbk", errors="replace")
        if kw.lower() in out.lower():
            running.append((kw, cn, kind))
            print("  %-14s %s" % (kw, cn))
    if not running:
        print("  （无）")
        print()
        print("→ 没有目标应用在运行。请手动打开其中几个（尤其是 Qzone / BlueStacks），")
        print("  然后重跑本脚本。这是「举一反三」唯一靠得住的方式。")
        return 1

    print()
    print("=" * 80)
    results = []
    for kw, cn, kind in running:
        st = scan(kw)
        if not st:
            results.append((cn, kind, None))
            print("  [%-12s] 无可扫描窗口" % cn)
            continue
        nb = len(st["buttons"])
        v = ("✅ 完美" if nb >= 8 else "✅ 可用" if nb >= 3 else
             "⚠️ 部分" if nb >= 1 else "❌ 需 OCR")
        results.append((cn, kind, (st, v)))
        top = ", ".join("%s×%d" % (k, v2) for k, v2 in
                        sorted(st["types"].items(), key=lambda x: -x[1])[:5])
        print("  [%-12s %-16s] 控件%4d 命名%4d 按钮%3d 深度%d  %s"
              % (cn, kind, st["total"], st["named"], nb, st["depth_max"], v))
        print("       类型: %s" % top)
        if st["buttons"]:
            print("       按钮: %s" % " / ".join(st["buttons"][:8]))
        print()

    print("=" * 80)
    print("汇总")
    print("=" * 80)
    print("%-16s %-18s %6s %6s %6s  %s" %
          ("应用", "技术栈", "控件", "命名", "按钮", "UIA"))
    print("-" * 80)
    for cn, kind, r in results:
        if r is None:
            print("%-16s %-18s %6s %6s %6s  %s" % (cn, kind, "-", "-", "-", "无窗口"))
        else:
            st, v = r
            print("%-16s %-18s %6d %6d %6d  %s"
                  % (cn, kind, st["total"], st["named"], len(st["buttons"]), v))
    print()
    need = [cn for cn, _, r in results
            if r and (r[1].startswith("⚠️") or r[1].startswith("❌"))]
    ok = [cn for cn, _, r in results if r and r[1].startswith("✅")]
    print("UIA 够用：%s" % ("、".join(ok) or "无"))
    print("需要兜底：%s" % ("、".join(need) or "无"))
    print("=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(main())
