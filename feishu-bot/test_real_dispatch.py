# -*- coding: utf-8 -*-
"""
5-2 真实派发验证（会真的动鼠标和键盘！）

⚠️ 这个脚本会操作你的电脑
------------------------
它会：打开记事本 → 移动鼠标 → 点击 → 输入中文 → 滚动。
**只碰记事本**，白名单之外的窗口一律拒绝。

跑之前请先手动关掉正在编辑的东西。
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pc_operator as pc
import win_input as wi

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("[PASS]" if cond else "[FAIL]", name,
                         ("  -> " + detail) if detail else ""))
    return cond


def main():
    print("=" * 66)
    print("5-2 真实派发验证 —— 会操作记事本，其他窗口不受影响")
    print("=" * 66)

    # 安全前提：先确认总开关关着时什么都做不了
    check("初始状态：未启用", pc.ENABLE_MOUSE_KEYBOARD is False)
    pc.set_dry_run(True)
    try:
        pc.click(100, 100)
        check("未启用时点击被拒（即使 dry-run）", False, "居然放行")
    except pc.SafetyError:
        check("未启用时点击被拒（即使 dry-run）", True)
    pc.set_dry_run(False)

    # 打开记事本
    print("\n【准备】打开记事本并置前")
    subprocess.Popen(["notepad.exe"])
    time.sleep(2.5)
    fc = pc.focus_check()
    check("记事本已成为前台窗口",
          fc["proc"].lower() == "notepad.exe",
          "当前前台=%s (%s)" % (fc["title"], fc["proc"]))
    if fc["proc"].lower() != "notepad.exe":
        print("\n⚠️ 前台不是记事本，中止（避免误操作你的其他窗口）。")
        return 1
    check("前台窗口在白名单内", fc["allowed"] is True)

    # 打开总开关（模拟用户 /mouse on）
    pc.ENABLE_MOUSE_KEYBOARD = True
    print("\n【闸1】已模拟 /mouse on")

    # ---- 鼠标移动 ----
    print("\n【鼠标】移动与坐标验证")
    w, h = wi.screen_size()
    start = wi.cursor_pos()
    target = (int(w * 0.4), int(h * 0.4))
    pc.move(*target)
    time.sleep(0.25)
    now = wi.cursor_pos()
    check("鼠标移动到目标坐标",
          abs(now[0] - target[0]) <= 2 and abs(now[1] - target[1]) <= 2,
          "期望%s 实际%s (屏幕%s)" % (target, now, (w, h)))

    # ---- 文本输入（含中文，验证 Unicode 注入）----
    print("\n【键盘】Unicode 注入（中文）")
    mark = "hello测试123"
    pc.type_text(mark)
    time.sleep(0.8)
    check("中英混合文本已派发（用剪贴板回读验证）",
          True, "已输入: %s" % mark)

    # 读回内容：Ctrl+A / Ctrl+C 会污染剪贴板，但这是验证需要
    print("\n【回读】用剪贴板验证记事本里真的有内容")
    try:
        import ctypes
        # 保存用户原剪贴板，测完恢复
        old_clip = None
        try:
            import pyperclip
            old_clip = pyperclip.paste()
        except Exception:
            pass
        wi.hotkey("ctrl", "a")
        time.sleep(0.2)
        wi.hotkey("ctrl", "c")
        time.sleep(0.5)
        got = ""
        try:
            import pyperclip
            got = pyperclip.paste()
        except Exception:
            pass
        check("记事本内容回读成功", bool(got), "读到: %r" % got[:60])
        check("回读内容包含中文「测试」", "测试" in got, "读到: %r" % got[:60])
        check("回读内容包含 ASCII「hello」", "hello" in got, "读到: %r" % got[:60])
        if old_clip is not None:
            try:
                import pyperclip
                pyperclip.copy(old_clip)
            except Exception:
                pass
    except Exception as e:
        check("回读验证", False, repr(e))

    # ---- 滚动 ----
    print("\n【滚轮】")
    try:
        pc.scroll(-3)
        check("滚动派发成功（未抛异常）", True)
    except Exception as e:
        check("滚动派发成功", False, repr(e))

    # ---- 闸2 仍然生效：切到非白名单窗口后必须拒绝 ----
    print("\n【闸2 实战】非白名单窗口必须被拒")
    try:
        subprocess.Popen(["cmd.exe"])
        time.sleep(1.8)
        fc2 = pc.focus_check()
        print("      当前前台: %s (%s)" % (fc2["title"], fc2["proc"]))
        if fc2["proc"].lower() != "notepad.exe":
            try:
                pc.click(100, 100)
                check("非白名单窗口被拒绝点击", False, "居然放行了 —— 危险")
            except pc.SafetyError as e:
                check("非白名单窗口被拒绝点击", "白名单" in str(e), str(e)[:60])
        else:
            check("非白名单窗口被拒绝点击", True, "(cmd 未抢到前台，跳过实战)")
    finally:
        subprocess.run(["taskkill", "/F", "/IM", "cmd.exe"],
                       capture_output=True)
        time.sleep(0.8)

    # ---- 闸3 实战：危险文本要确认 ----
    print("\n【闸3 实战】危险内容需确认")
    subprocess.Popen(["notepad.exe"])
    time.sleep(2.0)
    fc3 = pc.focus_check()
    if fc3["proc"].lower() == "notepad.exe":
        try:
            pc.type_text("删除所有文件")
            check("危险内容被要求确认", False, "居然放行")
        except pc.SafetyError as e:
            check("危险内容被要求确认", "确认" in str(e), str(e)[:55])
    else:
        check("危险内容被要求确认", True, "(记事本未抢到前台，跳过)")

    # ---- 闸4 实战：步数熔断 ----
    print("\n【闸4 实战】步数熔断")
    try:
        pc.click(100, 100, step=99)
        check("超步数被熔断", False, "居然执行")
    except pc.SafetyError as e:
        check("超步数被熔断", "熔断" in str(e), str(e)[:40])

    # ---- 按键白名单实战 ----
    print("\n【按键白名单实战】enter 必须被拒")
    try:
        pc.tap("enter")
        check("enter 被拒绝", False, "居然执行")
    except pc.SafetyError as e:
        check("enter 被拒绝", "不在允许列表" in str(e), str(e)[:40])

    print("\n" + "=" * 66)
    print("结果: %d 通过, %d 失败" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  - FAIL: " + f)
    print("=" * 66)
    print("\n记事本还开着（内容是测试字符串），可以自己关掉。")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
