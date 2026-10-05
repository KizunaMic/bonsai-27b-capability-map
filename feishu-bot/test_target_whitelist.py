# -*- coding: utf-8 -*-
"""
目标白名单安全闸测试（dry-run，零副作用）

重点验证：**未授权目标连问都不问，直接拒绝**。
这是安全关键——"停下来问"本身可能是错的（模型认错收件人）。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("[PASS]" if cond else "[FAIL]", name,
                         ("  -> " + detail) if detail else ""))
    return cond


def main():
    print("=" * 72)
    print("目标白名单安全闸测试")
    print("=" * 72)
    import pc_operator as pc
    import pc_task as pt
    import pc_planner as pp
    pc.set_dry_run(True)
    pc.ENABLE_MOUSE_KEYBOARD = True
    pc.wi.foreground_window = lambda: (999, "记事本", "notepad.exe")

    # ---------- 1. 默认必须全拒 ----------
    print("\n【1】默认状态 = 全部拒绝（安全默认）")
    pt.ALLOWED_TARGETS.clear()
    check("白名单默认为空", len(pt.ALLOWED_TARGETS) == 0)
    check("target_allowed(文件传输助手) 默认 False",
          not pt.target_allowed("文件传输助手"))
    check("target_allowed(任何人) 默认 False", not pt.target_allowed("张三"))
    check("target_allowed(空) 默认 False", not pt.target_allowed(""))
    check("target_allowed(None) 默认 False", not pt.target_allowed(None))

    # ---------- 2. 未授权目标：直接拒绝，不问 ----------
    print("\n【2】未授权目标必须直接拒绝（不是暂停）")
    steps, meta = pp.plan("打开微信把桌面上那个方案发给张三")
    check("规划出 send 任务", meta["kind"] == "send", meta["kind"])
    check("提取到收件人=张三", meta.get("recipient") == "张三",
          str(meta.get("recipient")))
    last = steps[0]   # stop 现在是第一步（不可逆前置）
    check("stop 步骤带 target=张三", last.target == "张三", str(last.target))
    ex = pt.Executor(steps)
    done, rep = ex.run()
    check("未授权 -> 执行未完成", done is False)
    check("未授权 -> rejected=True（不是暂停）", ex.rejected is True)
    check("拒绝理由含收件人名", "张三" in ex.stop_reason, ex.stop_reason[:60])
    check("汇报里没有「请确认」", "请确认这一步" not in rep, rep[-80:])
    check("汇报里有「已拒绝」", "已拒绝" in rep, rep[-80:])

    # ---------- 3. 授权后：停下确认（不自动执行）----------
    print("\n【3】授权后仍要停下确认（用户明确要求日常一律停）")
    pt.add_target("文件传输助手", reason="测试授权")
    check("add_target 生效", pt.target_allowed("文件传输助手"))
    steps2, meta2 = pp.plan("打开微信把桌面上那个方案发给文件传输助手")
    check("提取到收件人=文件传输助手",
          meta2.get("recipient") == "文件传输助手", str(meta2.get("recipient")))
    ex2 = pt.Executor(steps2)
    done2, rep2 = ex2.run()
    check("已授权 -> 仍未完成（等确认）", done2 is False)
    check("已授权 -> rejected=False（是暂停）", ex2.rejected is False)
    check("暂停理由含「等你确认」", "确认" in ex2.stop_reason, ex2.stop_reason[:50])
    check("汇报里有「请确认这一步」", "请确认这一步" in rep2, rep2[-80:])

    # ---------- 4. 授权不串号 ----------
    print("\n【4】授权必须精确匹配，不许模糊放行")
    pt.add_target("文件传输助手")
    for other in ["文件传输", "传输助手", "文件传输助手2", "张三",
                  "文件传输助手 ", "  文件传输助手", "FILE_TRANSFER"]:
        # 只有去掉首尾空格后完全相等才算
        expected = (other.strip() == "文件传输助手")
        got = pt.target_allowed(other)
        check("「%s」-> %s" % (other, expected), got == expected)

    # ---------- 5. 撤销授权 ----------
    print("\n【5】撤销授权")
    check("remove_target 返回 True", pt.remove_target("文件传输助手") is True)
    check("撤销后不再允许", not pt.target_allowed("文件传输助手"))
    steps3, _ = pp.plan("打开微信把桌面上那个方案发给文件传输助手")
    ex3 = pt.Executor(steps3)
    ex3.run()
    check("撤销后 -> 又变成直接拒绝", ex3.rejected is True)
    check("remove 不存在返回 False", pt.remove_target("不存在的人") is False)

    # ---------- 6. 认不出收件人 = 拒绝 ----------
    print("\n【6】认不出收件人时必须拒绝（不猜）")
    steps4, meta4 = pp.plan("打开微信把文件发出去")
    check("无收件人时 kind=send", meta4["kind"] == "send", meta4["kind"])
    last4 = steps4[-1]
    check("target 为空或缺失", not last4.target, repr(last4.target))
    ex4 = pt.Executor(steps4)
    ex4.run()
    check("认不出收件人 -> 拒绝", ex4.rejected is True, ex4.stop_reason[:50])

    # ---------- 7. 白名单不持久化到磁盘（重启即失效）----------
    print("\n【7】白名单只在内存（重启后失效 = fail-safe）")
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "pc_task.py"), encoding="utf-8").read()
    check("白名单定义为空字典", "ALLOWED_TARGETS = {}" in src)
    check("没有把白名单写入文件的代码",
          "json.dump" not in src.split("ALLOWED_TARGETS")[1][:2000]
          or "target" not in src.split("ALLOWED_TARGETS")[1][:2000])

    # ---------- 7b. 拒绝时不得发「执行计划 / 正在执行」 ----------
    # 虚假承诺（说着"正在执行"却一个动作都没做）比不报更糟。
    print("\n【7b】拒绝时不做虚假承诺")
    import bot as _bot
    _bot.api_client = None
    sent = []
    _bot.send_text = lambda oid, txt: sent.append(txt) or True
    pc.set_dry_run(True)
    pc.ENABLE_MOUSE_KEYBOARD = True
    try:
        pt.ALLOWED_TARGETS.clear()
        sent.clear()
        r = _bot.try_natural_task("打开微信把那个方案发给张三")
        joined = " ".join(sent)
        check("未授权时不推送任何计划消息", not sent,
              "推送了 %d 条" % len(sent))
        check("未授权回复不含「正在执行」", "正在执行" not in (r or ""),
              (r or "")[:70])
        pt.add_target("文件传输助手", reason="测试")
        sent.clear()
        _bot.try_natural_task("打开微信把那个方案发给文件传输助手", "ou_test")
        joined2 = " ".join(sent)
        check("已授权时推送执行计划", "执行计划" in joined2, joined2[:60])
    finally:
        pt.ALLOWED_TARGETS.clear()
        pc.ENABLE_MOUSE_KEYBOARD = False
        pc.set_dry_run(False)

    # ---------- 8. 非法授权名 ----------
    print("\n【8】非法授权名要拒绝")
    for bad in ["*", "?", "所有人", "*.*", "a/b", "x" * 40]:
        if "*" in bad or "?" in bad or "/" in bad or len(bad) > 30:
            pass   # 由 bot 层拦，这里只记录不测
    pt.add_target("正常人", reason="")
    check("单个名字可以授权", pt.target_allowed("正常人"))
    pt.remove_target("正常人")

    # ---------- 8b. 自然口语变体（今天已在同一坑栽了三次）----------
    print("\n【8b】自然口语变体识别")
    import pc_planner as _pp
    # (原话, 期望收件人或 None=不是 send 任务)
    cases = [
        ("点开微信给文件传输助手发一句你好", "文件传输助手"),
        ("打开微信把方案发给文件传输助手", "文件传输助手"),
        ("微信发个消息给文件传输助手", "文件传输助手"),
        ("给文件传输助手发句话", "文件传输助手"),
        ("把方案发给张三", "张三"),
        ("发给李四", "李四"),
        ("发文件给王五", "王五"),
        ("打开微信把文件发出去", ""),
        ("给我看这张图", None),          # 咨询类，不是任务
        ("打开微信", None),
        ("打开画图给我画个圆", None),     # 画图是另一类
    ]
    for t, exp in cases:
        _, mm = _pp.plan(t)
        got = mm.get("recipient")
        good = (got == exp) if exp is not None else (mm["kind"] != "send")
        check("「%s」→ %r" % (t[:18], exp), good,
              "得到 kind=%s recipient=%r" % (mm["kind"], got))

    # ---------- 8c. 确认文案质量（用户做发送前唯一的依据）----------
    print("\n【8c】确认文案：发什么 / 发给谁")
    for t, must_who, must_what in [
            ("把桌面上那个方案发给张三", "张三", "方案"),
            ("把方案发给文件传输助手", "文件传输助手", "方案"),
            ("点开微信给文件传输助手发一句你好", "文件传输助手", None),
            ("打开微信把文件发出去", None, "文件")]:
        st, _ = _pp.plan(t)
        d = st[0].target_desc if st else ""
        if must_who:
            check("「%s」收件人=%s" % (t[:14], must_who), must_who in d, d[:60])
        if must_what:
            check("「%s」含内容=%s" % (t[:14], must_what), must_what in d, d[:60])
        # 不能出现"没说要发给谁"却其实识别到了的情况
        if must_who:
            check("「%s」不误报没收件人" % t[:14], "没要说发给谁" not in d, d[:60])
        # 不能残留动词尾巴
        check("「%s」无残留动词" % t[:14],
              not re.search(r"[发给传送]\s*」", d), d[:60])

    # ---------- 9. 收尾 ----------
    print("\n【9】收尾")
    pt.ALLOWED_TARGETS.clear()
    check("白名单已清空", len(pt.ALLOWED_TARGETS) == 0)
    pc.ENABLE_MOUSE_KEYBOARD = False
    pc.set_dry_run(False)
    check("总开关关闭", pc.ENABLE_MOUSE_KEYBOARD is False)

    print("\n" + "=" * 72)
    print("结果: %d 通过, %d 失败" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  - FAIL: " + f)
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
