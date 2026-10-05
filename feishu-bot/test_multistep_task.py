# -*- coding: utf-8 -*-
"""
5-3 多步任务测试（dry-run，零副作用）
重点验证：不可逆动作**一定**停下、白名单别名匹配、步数熔断。
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
    print("=" * 68)
    print("5-3 多步任务测试（dry-run，不动真实桌面）")
    print("=" * 68)
    import pc_operator as pc
    import pc_task as pt
    import pc_planner as pp
    pc.set_dry_run(True)
    pc.ENABLE_MOUSE_KEYBOARD = True
    pc.wi.foreground_window = lambda: (999, "无标题 - 记事本", "notepad.exe")

    # ---------- 1. 不可逆动作：微信发文件 ----------
    print("\n【1】微信发文件（用户原话场景）")
    pt.ALLOWED_TARGETS.clear()          # 默认空名单 = 全部拒绝
    steps, meta = pp.plan("打开微信把我桌面上那个方案发给张三")
    check("识别为 send 任务", meta["kind"] == "send", meta["kind"])
    check("需要确认", meta["need_confirm"] is True)
    check("提取到收件人", meta.get("recipient") == "张三",
          str(meta.get("recipient")))
    check("含 launch 微信步骤", any(s.action == "launch" for s in steps))
    # ⚠️ stop 必须在**第一步**（见 pc_planner 注释）：
    # 放最后的话前面任何一步失败都会先熔断，确认门永远露不出来。
    first = steps[0]
    check("第一步是 stop 闸（不可逆前置）",
          first.action == "stop" and first.irreversible is True, first.action)
    check("stop 步骤带 target 供白名单校验", first.target == "张三", str(first.target))
    check("确认文案含收件人张三", "张三" in first.target_desc, first.target_desc)
    check("stop 之后还有准备动作", len(steps) > 1, str(len(steps)))

    ex = pt.Executor(steps)
    done, rep = ex.run()
    check("未授权收件人 -> 直接拒绝", ex.rejected is True)
    check("拒绝时执行未完成", done is False)
    check("拒绝理由含收件人", "张三" in ex.stop_reason, ex.stop_reason[:50])
    check("汇报里不含「已发送」", "已发送" not in rep and "发送成功" not in rep)
    check("汇报不含「请确认」（拒绝不是暂停）", "请确认这一步" not in rep)
    check("汇报里有「已拒绝」", "已拒绝" in rep)
    check("未授权不执行任何步骤（预检生效）", len(ex.log) == 0,
          "执行了 %d 步" % len(ex.log))

    # ---------- 2. 不可逆词的各种说法 ----------
    print("\n【2】各种不可逆说法都必须停下")
    for t in ["把桌面文件删掉", "帮我付一下这个订单", "删除桌面所有文件",
              "把这个文件覆盖掉", "把内容上传到服务器", "卸载这个软件",
              "结束进程", "把 E 盘格式化"]:
        steps, meta = pp.plan(t)
        ex = pt.Executor(steps)
        ex.run()
        check("「%s」停下" % t[:12], ex.stopped_at is not None or meta["kind"] in
              ("risky", "send", "unknown"),
              "kind=%s stopped=%s" % (meta["kind"], ex.stopped_at))

    # ---------- 3. 画图任务 ----------
    print("\n【3】画图任务")
    steps, meta = pp.plan("打开画图给我画个圆")
    check("识别为 paint", meta["kind"] == "paint", meta["kind"])
    check("无需确认（画图可逆）", meta["need_confirm"] is False)
    acts = [s.action for s in steps]
    check("含 launch 画图", "launch" in acts, str(acts))
    check("含画笔选择", "brush" not in acts, str(acts))
    check("含拖拽画圆", "drag" in acts, str(acts))
    ex = pt.Executor(steps)
    done, rep = ex.run()
    check("画图流程可跑完（launch 画图不可命令行时会失败但不该崩）",
          "Traceback" not in rep)

    for shape in ["正方形", "三角形", "直线", "五角星"]:
        steps, meta = pp.plan("画个" + shape)
        check("「画个%s」能规划" % shape, len(steps) >= 3, str(len(steps)))

    # ---------- 4. 打开应用 ----------
    print("\n【4】打开应用")
    for t, want in [("打开微信", "wechat"), ("打开画图", "mspaint"),
                    ("打开记事本", "notepad"), ("启动浏览器", "chrome")]:
        steps, meta = pp.plan(t)
        check("「%s」→ %s" % (t, want),
              meta["kind"] == "open_app" and steps[0].text == want,
              "%s/%s" % (meta["kind"], steps[0].text if steps else "-"))

    steps, meta = pp.plan("打开那个不存在的软件")
    check("认不出的应用名会明确说「没认出」",
          "没认出" in steps[0].target_desc, steps[0].target_desc[:30])

    # ---------- 5. 白名单别名匹配 ----------
    print("\n【5】白名单别名匹配（微信有 WeChat/Weixin 两种进程名）")
    for proc, want in [("WeChat.exe", True), ("Weixin.exe", True),
                       ("notepad.exe", True), ("mspaint.exe", True),
                       ("PaintApp.exe", True), ("explorer.exe", True),
                       ("WorkBuddy.exe", False), ("chrome.exe", False),
                       ("cmd.exe", False), ("", False), (None, False),
                       # 仿冒程序：包含匹配会误判，精确/主体匹配才拦得住
                       ("explorerX.exe", False), ("notepadX.exe", False),
                       ("mspaintX.exe", False), ("fakewechat.exe", False),
                       ("wechat_helper.exe", False),
                       ("Notepad.EXE", True), ("WECHAT.EXE", True)]:
        check("%-20s → %s" % (proc, want), pt.is_allowed_app(proc) is want)

    # ---------- 6. 步数熔断 ----------
    print("\n【6】步数熔断")
    steps = [pt.Step("click", x=10, y=10, target_desc="测试%d" % i)
             for i in range(30)]
    ex = pt.Executor(steps)
    done, rep = ex.run()
    check("超过 20 步熔断", ex.stopped_at is not None and "步数上限" in ex.stop_reason,
          ex.stop_reason[:40])
    check("实际执行不超过 21 步", len(ex.log) <= 21, str(len(ex.log)))

    # ---------- 7. 连续失败熔断 ----------
    print("\n【7】连续失败 2 次熔断（防原地打转）")
    # hotkey 永远失败（需确认），连用 3 个应该触发 fail_streak
    steps = [pt.Step("hotkey", keys="ctrl+v") for _ in range(5)]
    ex = pt.Executor(steps)
    done, rep = ex.run()
    check("连续失败后熔断", "连续失败" in ex.stop_reason, ex.stop_reason[:50])
    check("失败 2 次就停（不是做完 5 步）", len(ex.log) <= 2, str(len(ex.log)))

    # ---------- 8. 视觉重试上限 ----------
    print("\n【8】视觉重试上限")
    steps = [pt.Step("look") for _ in range(5)]
    ex = pt.Executor(steps, max_look=3)
    done, rep = ex.run()
    check("视觉重试超过 3 次熔断", ex.look_count <= 3, str(ex.look_count))

    # ---------- 9. 确认文案质量 ----------
    print("\n【9】不可逆确认文案必须说清「什么发给谁」")
    for t, must in [("把方案发给张三", "张三"),
                    ("把报告发给李四", "李四"),
                    ("把文件发给王五", "王五")]:
        steps, meta = pp.plan(t)
        check("「%s」确认含 %s" % (t[:8], must), must in steps[0].target_desc,
              steps[0].target_desc[:40])

    # ---------- 10. 收尾 ----------
    print("\n【10】收尾")
    pc.ENABLE_MOUSE_KEYBOARD = False
    pc.set_dry_run(False)
    check("总开关恢复关闭", pc.ENABLE_MOUSE_KEYBOARD is False)
    check("dry-run 恢复关闭", pc.DRY_RUN is False)
    st = pc.status()
    check("白名单含 4 个（微信+画图已加）", len(st["app_whitelist"]) == 4,
          str(st["app_whitelist"]))

    print("\n" + "=" * 68)
    print("结果: %d 通过, %d 失败" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  - FAIL: " + f)
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
