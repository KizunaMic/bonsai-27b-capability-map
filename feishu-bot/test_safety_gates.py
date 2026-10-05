# -*- coding: utf-8 -*-
"""
5-2 鼠标键盘操作层 —— 安全闸测试

⚠️ 本测试**绝不派发任何真实动作**
--------------------------------
派发动作意味着真的会动你的鼠标、真的往当前窗口敲键盘。
所以这里只测**闸门判定**（拒绝路径），不测"动作是否真的执行成功"。

这是刻意的取舍：
- 拒绝路径才是安全的关键（放行了不该放的 = 事故）
- 执行成功与否依赖真实窗口环境，由用户手动在记事本里验收

用 dry-run 模式跑真实派发路径：`_gate()` 全过之后用一个记录器
替代真实注入，既能验证"该放行的确实放行"，又不碰你的电脑。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("[PASS]" if cond else "[FAIL]", name,
                         ("  -> " + detail) if detail else ""))
    return cond


def main():
    print("=" * 66)
    print("5-2 操作层安全闸测试（不派发任何真实动作）")
    print("=" * 66)

    import pc_operator as pc

    # ⭐ 关键：整个测试跑在 dry-run 下。
    # 这样"验证该放行的确实放行"就不会真的动用户鼠标/键盘。
    # 拒绝路径本来就不派发；放行路径由 _dispatch 拦在 dry-run 里。
    pc.set_dry_run(True)
    print("（dry-run 已开：本测试不会真的动你的鼠标或键盘）\n")

    # ================= 闸1：总开关 =================
    print("\n【闸1】总开关 ENABLE_MOUSE_KEYBOARD")
    check("默认关闭（安全默认）", pc.ENABLE_MOUSE_KEYBOARD is False)
    for fn, args in [("click", (10, 10)), ("type_text", ("x",)),
                     ("scroll", (1,)), ("tap", ("up",)),
                     ("move", (1, 1)), ("drag", (1, 1, 2, 2))]:
        try:
            getattr(pc, fn)(*args)
            check("%s 在未启用时被拒绝" % fn, False, "居然执行了")
        except pc.SafetyError as e:
            check("%s 在未启用时被拒绝" % fn, "未启用" in str(e))
        except Exception as e:
            check("%s 在未启用时被拒绝" % fn, False,
                  "抛的不是 SafetyError: %r" % e)

    # ================= 闸2：白名单 =================
    print("\n【闸2】前台窗口白名单")
    check("notepad.exe 在白名单", pc.app_allowed("notepad.exe"))
    check("explorer.exe 在白名单", pc.app_allowed("explorer.exe"))
    check("Notepad.EXE 大小写不敏感", pc.app_allowed("Notepad.EXE"))
    for bad in ["WorkBuddy.exe", "cmd.exe", "", None,
                "powershell.exe", "explorerX.exe", "notepadX.exe",
                "mspaintX.exe", "fakewechat.exe"]:
        check("拒绝 %r" % bad, not pc.app_allowed(bad))

    fc = pc.focus_check()
    check("focus_check 能拿到前台窗口", fc.get("hwnd") is not None,
          "hwnd=%s" % fc.get("hwnd"))
    check("focus_check 报告是否允许（当前应为 false）",
          fc.get("allowed") in (True, False),
          "allowed=%s proc=%s" % (fc.get("allowed"), fc.get("proc")))

    # 白名单闸的实际拦截：模拟一个非白名单前台窗口
    print("\n【闸2b】非白名单窗口必须拦住（模拟前台=WorkBuddy）")
    real_fg = pc.wi.foreground_window
    pc.wi.foreground_window = lambda: (
        12345, "WorkBuddy", "WorkBuddy.exe")
    try:
        pc.ENABLE_MOUSE_KEYBOARD = True      # 只为测试闸2
        try:
            pc.click(100, 100)
            check("白名单外的窗口被拒绝点击", False, "居然放行了")
        except pc.SafetyError as e:
            check("白名单外的窗口被拒绝点击", "白名单" in str(e), str(e)[:60])
        # 模拟白名单内窗口 -> 应该放行到闸3
        pc.wi.foreground_window = lambda: (999, "无标题 - 记事本", "notepad.exe")
        try:
            pc.click(100, 100)
            check("白名单内的普通点击被放行", True)
        except pc.SafetyError as e:
            check("白名单内的普通点击被放行", False, "被误拦: %s" % str(e)[:60])
    finally:
        pc.wi.foreground_window = real_fg
        pc.ENABLE_MOUSE_KEYBOARD = False

    # ================= 闸3：确认闸 =================
    print("\n【闸3】危险动作确认闸")
    for kw in ["删除", "发送", "付款", "上传", "覆盖", "格式化"]:
        check("「%s」需要确认" % kw, pc.needs_confirm(kw))
    for kw in ["你好", "截图", "讲个笑话", "移动鼠标到 (100,200)", "滚动 3"]:
        check("「%s」不需要确认" % kw, not pc.needs_confirm(kw))

    real_fg = pc.wi.foreground_window
    pc.wi.foreground_window = lambda: (999, "无标题 - 记事本", "notepad.exe")
    try:
        pc.ENABLE_MOUSE_KEYBOARD = True
        # 危险内容走确认闸
        try:
            pc.type_text("删除所有文件", step=1)
            check("输入含危险词的文本被要求确认", False, "居然放行了")
        except pc.SafetyError as e:
            check("输入含危险词的文本被要求确认", "确认" in str(e), str(e)[:50])
        # confirmed=True 应该放行
        try:
            pc.type_text("删除所有文件", step=1, confirmed=True)
            check("用户确认后放行", True)
        except pc.SafetyError as e:
            check("用户确认后放行", False, str(e)[:60])
    finally:
        pc.wi.foreground_window = real_fg
        pc.ENABLE_MOUSE_KEYBOARD = False

    # ================= 闸4：步数熔断 =================
    print("\n【闸4】步数熔断")
    check("步数 %d 放行" % pc.MAX_STEPS,
          _step_ok(pc.MAX_STEPS))
    check("步数 %d 熔断" % (pc.MAX_STEPS + 1),
          not _step_ok(pc.MAX_STEPS + 1))
    check("步数 999 熔断", not _step_ok(999))
    real_fg = pc.wi.foreground_window
    pc.wi.foreground_window = lambda: (999, "记事本", "notepad.exe")
    try:
        pc.ENABLE_MOUSE_KEYBOARD = True
        try:
            pc.click(10, 10, step=pc.MAX_STEPS + 1)
            check("超步数的点击被熔断", False, "居然执行了")
        except pc.SafetyError as e:
            check("超步数的点击被熔断", "熔断" in str(e), str(e)[:40])
    finally:
        pc.wi.foreground_window = real_fg
        pc.ENABLE_MOUSE_KEYBOARD = False

    # ================= 按键白名单 =================
    print("\n【按键白名单】危险键必须被拒")
    for k in ["enter", "回车", "tab", "esc", "alt", "ctrl", "win", "f4"]:
        try:
            pc.tap(k)
            check("拒绝按键「%s」" % k, False, "居然允许")
        except pc.SafetyError as e:
            # 两种合法拒绝理由：不在白名单 / 总开关未启用
            check("拒绝按键「%s」" % k,
                  ("不在允许列表" in str(e)) or ("未启用" in str(e))
                  or ("白名单" in str(e)), str(e)[:40])
        except Exception as e:
            check("拒绝按键「%s」" % k, False, "异常类型错: %r" % e)
    check("ALLOWED_KEYS 不含 enter",
          "enter" not in pc.ALLOWED_KEYS and "return" not in pc.ALLOWED_KEYS)
    check("ALLOWED_KEYS 不含 ctrl/alt/win",
          not ({"ctrl", "alt", "win"} & pc.ALLOWED_KEYS))

    # ================= 组合键：默认必须拒绝 =================
    print("\n【组合键】默认必须拒绝（本轮修的真实漏洞）")
    real_fg = pc.wi.foreground_window
    pc.wi.foreground_window = lambda: (999, "记事本", "notepad.exe")
    try:
        pc.ENABLE_MOUSE_KEYBOARD = True
        for combo in [("ctrl", "a"), ("ctrl", "c"), ("alt", "f4")]:
            try:
                pc.hotkey(*combo)
                check("组合键 %s 在未确认时被拦" % "+".join(combo),
                      False, "居然执行了 —— 这是安全漏洞")
            except pc.SafetyError as e:
                check("组合键 %s 在未确认时被拦" % "+".join(combo),
                      "确认" in str(e), str(e)[:45])
        # 显式确认后应放行（dry-run 记录，不真执行）
        r = pc.hotkey("ctrl", "a", confirmed=True)
        check("组合键显式确认后放行", r.get("dry_run") is True, str(r)[:60])
    finally:
        pc.wi.foreground_window = real_fg
        pc.ENABLE_MOUSE_KEYBOARD = False

    # ================= 白名单外的动作类型 =================
    print("\n【动作覆盖】不该有裸的 press_enter 之类")
    for danger in ["press_enter", "exec", "run", "shell", "system"]:
        check("未暴露 %s" % danger, not hasattr(pc, danger))

    # ================= dry-run 自身有效性 =================
    print("\n【dry-run】必须真的拦住派发")
    real_fg = pc.wi.foreground_window
    pc.wi.foreground_window = lambda: (999, "无标题 - 记事本", "notepad.exe")
    try:
        pc.ENABLE_MOUSE_KEYBOARD = True
        before = len(pc.dry_log())
        r = pc.click(123, 456, step=1)
        check("放行的点击在 dry-run 下被记录而非执行",
              r.get("dry_run") is True and len(pc.dry_log()) == before + 1,
              "ret=%s" % r)
        last = pc.dry_log()[-1]
        check("dry 日志记下了正确的动作与坐标",
              last["action"] == "click" and last["args"][:2] == [123, 456],
              str(last))
        # 关掉 dry-run 再确认闸仍然生效（不实际执行，用异常验证）
        pc.set_dry_run(False)
        try:
            pc.click(1, 1, step=1, label="删除这个文件")
            check("dry-run 关闭后危险动作仍被确认闸拦住", False, "放行了")
        except pc.SafetyError as e:
            check("dry-run 关闭后危险动作仍被确认闸拦住", "确认" in str(e))
    finally:
        pc.set_dry_run(True)
        pc.wi.foreground_window = real_fg
        pc.ENABLE_MOUSE_KEYBOARD = False

    # ================= status 自述 =================
    print("\n【status】")
    st = pc.status()
    for k in ["mouse_keyboard_enabled", "app_whitelist", "confirm_keywords",
              "allowed_keys", "max_steps", "screen", "foreground"]:
        check("status 含 %s" % k, k in st)
    check("status 显示未启用", st["mouse_keyboard_enabled"] is False)
    # 5-3 起白名单按用户要求扩到 4 个（加了微信、画图）。
    # 断言的是"白名单是这 4 个且不含危险程序"，而不是"只有 2 个"。
    check("白名单 = 记事本/资源管理器/微信/画图",
          set(st["app_whitelist"]) ==
          {"notepad.exe", "explorer.exe", "wechat", "mspaint"},
          str(st["app_whitelist"]))
    check("白名单不含 cmd/powershell/explorer 之类",
          not ({"cmd.exe", "powershell.exe", "explorerx"} & set(st["app_whitelist"])))

    print("\n" + "=" * 66)
    print("结果: %d 通过, %d 失败" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项:")
        for f in FAIL:
            print("  - " + f)
    print("=" * 66)
    return 1 if FAIL else 0


def _step_ok(n):
    try:
        import pc_operator as pc
        pc.check_step(n)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(main())
