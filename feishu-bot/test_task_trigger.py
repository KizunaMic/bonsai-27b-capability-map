# -*- coding: utf-8 -*-
"""
5-3 bot 触发逻辑测试（dry-run）
重点：**不误触**。把"打开微信"当执行指令是危险的误判，
而把真的执行请求当聊天更让人恼火。两头都要测。
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
    print("5-3 触发逻辑测试（不执行任何动作）")
    print("=" * 68)
    import bot
    import pc_operator as pc
    pc.set_dry_run(True)
    pc.wi.foreground_window = lambda: (999, "无标题 - 记事本", "notepad.exe")
    bot.api_client = None          # 阻止真实推送

    # ---------- 1. 必须是「任务」，不能是「提问」 ----------
    print("\n【1】提问类不该触发执行（防误操作）")
    questions = [
        "微信怎么发文件", "画图怎么画圆", "打开微信", # 单独"打开微信"是真指令，见下
        "你会操作电脑吗", "怎么用画图画三角形",
        "打开微信是什么意思", "记事本能干嘛",
        "你能不能帮我画个圆", "发送文件要怎么弄",
        "请问微信在哪里下载", "保存文件怎么保存",
    ]
    for q in questions:
        # "打开微信" 单独是明确指令，这里单独排除
        if q == "打开微信":
            continue
        r = bot.try_natural_task(q)
        check("不触发「%s」" % q[:14], r is None, "触发了: %s" % str(r)[:40])

    # ---------- 2. 明确的执行指令应该被识别 ----------
    print("\n【2】明确执行指令应被识别")
    # 操作层关闭时应该明确说"没开"而不是静默
    pc.ENABLE_MOUSE_KEYBOARD = False
    r = bot.try_natural_task("打开微信")
    check("「打开微信」被识别（但因操作层关闭而提示开）",
          r is not None and "/mouse on" in r, str(r)[:50])

    pc.ENABLE_MOUSE_KEYBOARD = True
    r = bot.try_natural_task("打开浏览器")
    check("「打开浏览器」被识别但因不在白名单而拒绝",
          r is not None and "白名单" in r, str(r)[:60])

    # ---------- 3. 普通闲聊不触发 ----------
    print("\n【3】闲聊不触发")
    for c in ["你好", "讲个笑话", "今天天气不错", "你是谁", "帮我查下资料",
              "这段代码什么意思", "谢谢你", "在吗"]:
        r = bot.try_natural_task(c)
        check("不触发「%s」" % c, r is None, str(r)[:40])

    # ---------- 4. 不可逆任务的完整流程（不真正执行）----------
    print("\n【4】发文件流程：必须停在确认闸")
    bot._pending_task.update(steps=None, executor=None, stop_idx=0, ts=0.0, desc="")
    r = bot.try_natural_task("打开微信把我桌面上那个方案发给张三")
    check("发文件被识别为任务", r is not None)
    if r:
        # 白名单默认为空 -> 未授权收件人应被**直接拒绝**（不是暂停）
        check("未授权收件人被直接拒绝", "已拒绝" in r, r[-90:])
        check("报告里出现收件人", "张三" in r, r[-90:])
        check("报告里没有「已发送」", "已发送" not in r)
        check("告知可授权白名单", "/target add" in r, r[-90:])
    check("未授权不产生待确认任务（不是暂停）",
          bot._pending_task["steps"] is None,
          "未授权不该进入待确认队列")

    # ---- 已授权场景：应停下等确认，而不是直接执行 ----
    # 注意：白名单要在【5】取消测试之后才清理，
    # 否则「取消」时没有待确认任务可清，测的就不是取消逻辑了。
    import pc_task as _pt
    _pt.ALLOWED_TARGETS.clear()
    _pt.add_target("文件传输助手", reason="测试")
    try:
        bot._pending_task.update(steps=None, executor=None, stop_idx=0,
                                 ts=0.0, desc="")
        r2 = bot.try_natural_task("打开微信把那个方案发给文件传输助手")
        check("已授权收件人被接受", r2 is not None)
        if r2:
            check("已授权 -> 停下等确认", "已停下" in r2 or "等你确认" in r2,
                  r2[-90:])
            check("已授权 -> 仍不自动发送", "已发送" not in r2)
        check("已授权产生待确认任务",
              bot._pending_task["steps"] is not None)
        if bot._pending_task["steps"]:
            desc = bot._pending_task["desc"]
            check("确认文案含收件人", "文件传输助手" in desc, desc)
            check("确认文案不含文件名污染", desc.count("文件传输助手") == 1,
                  desc)

        # ---------- 5. 取消流程（在待确认任务存在时测）----------
        print("\n【5】取消")
        r3 = bot.try_natural_task("取消")
        check("「取消」清空待确认", r3 is not None and "取消" in r3, str(r3)[:40])
        check("取消后无待确认", bot._pending_task["steps"] is None)
    finally:
        _pt.ALLOWED_TARGETS.clear()
        bot._pending_task.update(steps=None, executor=None, stop_idx=0,
                                 ts=0.0, desc="")

    # ---------- 6. 待确认过期 ----------
    print("\n【6】待确认过期处理")
    r = bot.try_natural_task("继续")
    check("无任务时「继续」不报错（返回 None 交正常流程）",
          r is None or "没有待确认" in r, str(r)[:40])

    # ---------- 7. 画图任务（可逆，不需确认）----------
    print("\n【7】画图任务不需要确认")
    bot._pending_task.update(steps=None, executor=None, desc="")
    r = bot.try_natural_task("打开画图给我画个圆")
    check("画图任务被识别", r is not None, str(r)[:50])
    if r:
        check("画图不产生待确认（可逆）",
              bot._pending_task["steps"] is None,
              "不该有待确认")
        check("画图报告不含「不可逆」", "不可逆" not in r, r[-80:])

    # ---------- 7b2. 多轮追问（2026-10-04 用户真机测试暴露）----------
    # 「打开微信」失败后追问「那你定位啊」，bot 完全没听懂 ——
    # 根因：try_natural_task 无状态，每次只看当前这一句。
    print("\n【7b2】多轮追问：省略宾语的口语")
    bot._remember_task("打开微信", {"kind": "open_app", "app": "微信"})
    for f in ["那你定位啊", "定位", "找一下", "点击", "执行"]:
        check("「%s」识别为追问" % f, bot._is_followup(f))
    for f in ["快点", "你倒是找啊", "在吗", "1", "讲个笑话",
              "今天天气怎么样", "微信怎么发文件", "帮我把文件发给张三"]:
        check("「%s」不该是追问" % f, not bot._is_followup(f))

    mg = bot._resolve_followup("那你定位啊")
    check("追问能补全出应用名", bool(mg) and "微信" in mg, str(mg))
    if mg:
        import pc_planner as _pp2
        _st, _mm = _pp2.plan(mg)
        check("补全后能被规划（kind != unknown）", _mm["kind"] != "unknown",
              "%s -> %s" % (mg, _mm["kind"]))
    # 上下文过期后不该再补全
    bot._LAST_TASK["ts"] = 0.0
    check("上下文过期后不再补全", bot._resolve_followup("那你定位啊") is None)
    bot._LAST_TASK.update(text="", kind="", app="", recipient="",
                          ts=0.0, steps=None)

    # ---------- 8. 收尾 ----------
    print("\n【8】收尾：恢复安全默认")
    pc.ENABLE_MOUSE_KEYBOARD = False
    pc.set_dry_run(False)
    bot._pending_task.update(steps=None, executor=None, desc="")
    check("总开关关闭", pc.ENABLE_MOUSE_KEYBOARD is False)
    check("dry-run 关闭", pc.DRY_RUN is False)
    check("无遗留待确认", bot._pending_task["steps"] is None)

    print("\n" + "=" * 68)
    print("结果: %d 通过, %d 失败" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  - FAIL: " + f)
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
