# -*- coding: utf-8 -*-
"""
bot 集成测试：/mouse 指令 + 动作句式解析（dry-run，不动真实桌面）
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
    print("bot 集成测试：/mouse 指令 + 动作解析（dry-run）")
    print("=" * 66)

    import bot
    import pc_operator as pc

    # dry-run：不真的动桌面
    pc.set_dry_run(True)
    pc.ENABLE_MOUSE_KEYBOARD = False

    # ---- 1. 总开关关闭时，动作句式必须被拒 ----
    print("\n【1】总开关关闭时拒绝一切动作")
    for t in ["点击 800,600", "双击 100,100", "移动鼠标到 400,300",
              "上滚 3", "下滚 2", "输入：你好", "右击 50,60"]:
        r = bot.try_pc_action(t)
        check("拒绝「%s」" % t, r is not None and "🛡️" in r, str(r)[:50])

    # ---- 2. 非指令文本不该被误解析 ----
    print("\n【2】普通聊天不该被误判成操作")
    for t in ["你好", "讲个笑话", "今天天气怎么样", "你在干嘛",
              "帮我看看这张图", "把那个窗口关掉", "1", "随便说点什么",
              "我刚才吃了三个包子", "打开PS", "把文件都删掉",
              "你会写代码吗", "12345", "点外卖", "向下滚动一点",
              "帮我点一下那个按钮"]:
        r = bot.try_pc_action(t)
        check("不误触「%s」" % t, r is None, "误判为: %s" % str(r)[:50])

    # 注意：「输入密码」这类**确实是想让我打字**的句子，
    # 命中 type 规则是正确的（"输入"是明确动词）。
    # 安全性由确认闸保证（内容含"密码"不在确认词里，但白名单只允许记事本，
    # 且默认总开关关闭），而不是靠"不解析"。
    r = bot.try_pc_action("输入密码")
    check("「输入密码」识别为输入动作（合理）", r is not None, str(r)[:50])

    # ---- 3. /mouse dry 开启演练 ----
    print("\n【3】开关切换")
    r = bot.cmd_mouse_toggle("/mouse dry")
    check("/mouse dry 进入演练", "演练" in r, r[:40])
    check("dry_run 已开", pc.DRY_RUN is True)
    check("总开关已开", pc.ENABLE_MOUSE_KEYBOARD is True)

    # ---- 4. 演练模式下动作应被记录（含各种语序变体）----
    print("\n【4】演练模式：解析并记录，不执行")
    real_fg = pc.wi.foreground_window
    pc.wi.foreground_window = lambda: (999, "无标题 - 记事本", "notepad.exe")
    try:
        cases = [
            # 语序变体：动词在前 / 名词在前 都必须命中
            ("点击 800,600", "已点击", "click"),
            ("点 800,600", "已点击", "click"),
            ("单击800,600", "已点击", "click"),
            ("双击 100,100", "双击", "click"),
            ("右击 50,60", "右击", "click"),
            ("移动鼠标到 400,300", "已移到", "move"),
            ("鼠标移动到400,300", "已移到", "move"),      # 名词在前
            ("把鼠标移到 400,300", "已移到", "move"),
            ("鼠标移至400，300", "已移到", "move"),        # 全角逗号
            ("上滚 3", "向上滚动", "scroll"),
            ("下滚 2", "向下滚动", "scroll"),
            ("上滚3", "向上滚动", "scroll"),
            ("输入：你好世界", "已输入", "type_text"),
            ("输入你好世界", "已输入", "type_text"),
            ("在记事本里输入：测试中文", "已输入", "type_text"),
        ]
        for t, want, wantact in cases:
            before = len(pc.dry_log())
            r = bot.try_pc_action(t)
            after = len(pc.dry_log())
            ok = r is not None and want in r and after == before + 1
            if ok:
                ok = pc.dry_log()[-1]["action"] == wantact
            check("「%s」" % t, ok,
                  "ret=%s log+%d" % (str(r)[:45], after - before))
    finally:
        pc.wi.foreground_window = real_fg

    # 验证 dry 日志内容正确
    log = pc.dry_log()
    check("dry 日志记录了 click 800,600",
          any(e["action"] == "click" and e["args"][:2] == [800, 600] for e in log))
    check("dry 日志记录了 type_text 中文",
          any(e["action"] == "type_text" and e["args"][0] == "你好世界"
              for e in log))
    check("dry 日志记录了 dblclick(clicks=2)",
          any(e["action"] == "click" and e["kwargs"].get("clicks") == 2
              for e in log))
    check("dry 日志记录了 scroll 负值(下滚)",
          any(e["action"] == "scroll" and e["args"][0] == -2 for e in log))

    # ---- 5. 白名单外的窗口仍被拒 ----
    print("\n【5】白名单外窗口仍被拒（即使总开关开着）")
    pc.wi.foreground_window = lambda: (1, "WorkBuddy", "WorkBuddy.exe")
    try:
        r = bot.try_pc_action("点击 500,500")
        check("WorkBuddy 前台时点击被拒",
              r is not None and "🛡️" in r and "白名单" in r, str(r)[:70])
    finally:
        pc.wi.foreground_window = real_fg

    # ---- 6. 危险内容走确认闸 ----
    print("\n【6】危险内容需确认")
    pc.wi.foreground_window = lambda: (999, "记事本", "notepad.exe")
    try:
        r = bot.try_pc_action("输入：删除所有文件")
        check("危险内容被要求确认",
              r is not None and "🛡️" in r and "确认" in r, str(r)[:70])
    finally:
        pc.wi.foreground_window = real_fg

    # ---- 7. /mouse status 内容完整 ----
    print("\n【7】/mouse status")
    r = bot.cmd_mouse_status()
    for kw in ["演练模式", "屏幕", "前台窗口", "白名单", "允许按键", "步数上限"]:
        check("status 含「%s」" % kw, kw in r, r[:60] if kw not in r else "")
    r2 = bot.cmd_mouse_toggle("/mouse off")
    check("/mouse off 关闭", "关闭" in r2, r2[:40])
    check("关闭后总开关为 False", pc.ENABLE_MOUSE_KEYBOARD is False)
    r3 = bot.cmd_mouse_toggle("/mouse on")
    check("/mouse on 开启且警告", "真实模式" in r3 and "白名单" in r3, r3[:50])

    # ---- 8. 恢复安全默认 ----
    print("\n【8】收尾：恢复关闭状态")
    bot.cmd_mouse_toggle("/mouse off")
    check("最终状态为关闭", pc.ENABLE_MOUSE_KEYBOARD is False)
    check("最终 dry_run 也关", pc.DRY_RUN is False)
    st = pc.status()
    check("白名单未被测试改动（4 个：记事本/资源管理器/微信/画图）",
          set(st["app_whitelist"]) ==
          {"notepad.exe", "explorer.exe", "wechat", "mspaint"},
          str(st["app_whitelist"]))

    print("\n" + "=" * 66)
    print("结果: %d 通过, %d 失败" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  - FAIL: " + f)
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
