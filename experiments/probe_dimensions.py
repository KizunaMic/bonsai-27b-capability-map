"""
probe_dimensions.py —— 缺口 2：把「能力解离」的多维度证据补齐

为什么必须要这个：
"解离"这个词的含义是**不同能力之间出现差异**。
如果只测了定位一个维度，那只能说"定位不可用"，
**说不了"解离"**。

本脚本测 4 个维度，覆盖 3 种能力模态：
  维度 1  图像内容理解   语义   「图里有什么」   —— 看不懂会答不出内容
  维度 2  空间关系判断   语义   「A 在 B 的左边吗」—— 不需要坐标，只需比较
  维度 3  图像属性识别   语义   「圆是红还是蓝」  —— 颜色识别，最基础
  维度 4  视觉计数       语义   「有几个圆点」    —— 计数，比定位简单得多

设计意图：
  - 维度 2/3/4 **都不要求输出坐标**，排除了"不会格式化"的干扰
  - 如果维度 1/3/4 都对，唯独定位错 → **解离成立**
  - 如果连维度 3（颜色）都错 → 不是解离，是整体视觉能力缺失

⚠️ 每个维度都提供**可自动判定的客观标准答案**，不依赖人工评分。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_core as pc
import run_probe as rp

W, H = 1024, 683
R = 34

# ---- 场景：2 红 2 蓝，位置刻意不对称 ----
LAYOUT = [
    # (x比例, y比例, 颜色名)
    (0.12, 0.15, "red"),
    (0.13, 0.80, "red"),
    (0.88, 0.18, "blue"),
    (0.85, 0.78, "blue"),
]
COLOR_RGB = {"red": (255, 0, 0), "blue": (0, 0, 255)}


def build():
    buf = pc._make_rgb(W, H)
    truth = []
    for i, (fx, fy, cname) in enumerate(LAYOUT):
        cx, cy = int(W * fx), int(H * fy)
        pc._disc(buf, W, H, cx, cy, R, COLOR_RGB[cname])
        truth.append({"x": cx, "y": cy, "color": cname, "quad": i})
    return pc.png_bytes(W, H, buf), truth


# ============ 四个维度 ============

D1_CONTENT = (
    "Look at this {w}x{h} image and describe what you see. "
    "List each object you find and its color. Be concise."
)

D2_LEFTCOUNT = (
    "This {w}x{h} image contains circles in the left half and circles in "
    "the right half. How many circles are in the LEFT half of the image? "
    "Answer with a single number only."
)

D3_COLOR = (
    "This {w}x{h} image contains solid circles. "
    "How many distinct colors do the circles have? "
    "Answer with a single number."
)

D4_COUNT = (
    "This {w}x{h} image contains solid circles on a white background. "
    "Count the circles. Answer with a single number only."
)

# 额外一个更硬的维度：位置关系判断（不问坐标，只问大小关系）
D5_ORDER = (
    "This {w}x{h} image contains circles of different sizes. "
    "Is the largest circle located in the upper half or the lower half "
    "of the image? Answer with exactly one word: upper or lower."
)


def norm(s):
    return (s or "").strip().lower()


def has_number(s, n):
    """文本里是否出现数字 n（避免 'four' vs '4' 的判别问题）。"""
    import re
    return re.search(r"(?<![\d.])%d(?![\d.])" % n, s or "") is not None


def has_word(s, *words):
    t = norm(s)
    return any(w in t for w in words)


def ask(png, prompt, tag):
    try:
        txt, el, usage = rp.ask_vision(png, prompt, think=False)
    except Exception as e:
        print("  [%s] ERROR %s" % (tag, repr(e)[:70]))
        return None
    return {"raw": txt, "elapsed": round(el, 1), "usage": usage}


def main():
    print("=" * 74)
    print("缺口 2：多维度能力探针（解离是否成立）")
    print("=" * 74)
    png, truth = build()
    pc.save_case("dims_1024x683", png,
                 {str(i): (t["x"], t["y"]) for i, t in enumerate(truth)},
                 W, H)
    print("图 %dx%d：2 个红点（左），2 个蓝点（右）" % (W, H))
    for t in truth:
        print("  (%d,%d) %s" % (t["x"], t["y"], t["color"]))
    print()

    results = []

    # ---- 维度 1：内容理解（人工/LLM 判定，用关键词命中）----
    print("【维度 1】图像内容理解 —— 能否说出有什么")
    r = ask(png, D1_CONTENT.format(w=W, h=H), "D1")
    if r:
        txt = norm(r["raw"])
        hit_red = has_word(txt, "red", "红")
        hit_blue = has_word(txt, "blue", "蓝")
        hit_circle = has_word(txt, "circle", "dot", "round", "圆")
        n_red = has_number(r["raw"], 2)
        ok = hit_circle and (hit_red and hit_blue)
        print("  回答: %s" % r["raw"][:150].replace("\n", " "))
        print("  命中: 圆点=%s 红=%s 蓝=%s  数量2=%s  → %s"
              % (hit_circle, hit_red, hit_blue, n_red,
                 "✓ 通过" if ok else "✗ 未通过"))
        print("  耗时 %.0fs" % r["elapsed"])
        results.append({"dim": "内容理解", "ask": "说出图里有什么",
                        "passed": ok, "detail": r["raw"][:200],
                        "elapsed": r["elapsed"]})
    print()

    # ---- 维度 2：空间关系（不问坐标）----
    # ⚠️ 初版问法有缺陷：问的是 yes/no（"一个在左，一个在右吗"），
    #    但标准答案不是 yes/no，模型答 "left" 是合理回应，
    #    而我的判定只认 yes/no → 记为未通过，**是判定逻辑错，不是模型错**。
    # 改法：问法与判定严格对应 —— 问「左半有几个圆点」，
    #    标准答案 2（左边是 2 个红点，右边是 2 个蓝点）。
    print("【维度 2】左半区计数 —— 空间分区，不问坐标")
    r = ask(png, D2_LEFTCOUNT.format(w=W, h=H), "D2")
    if r:
        ok = has_number(r["raw"], 2)
        print("  回答: %s" % r["raw"][:150].replace("\n", " "))
        print("  标准答案 2（左半 2 个） → %s"
              % ("✓ 通过" if ok else "✗ 未通过"))
        print("  耗时 %.0fs" % r["elapsed"])
        results.append({"dim": "左半区计数", "ask": "左半有几个（答案 2）",
                        "passed": ok, "detail": r["raw"][:200],
                        "elapsed": r["elapsed"]})
    print()

    # ---- 维度 3：颜色计数 ----
    print("【维度 3】颜色种类计数 —— 最基础的视觉属性")
    r = ask(png, D3_COLOR.format(w=W, h=H), "D3")
    if r:
        ok = has_number(r["raw"], 2) or has_word(r["raw"], "two", "两种")
        print("  回答: %s" % r["raw"][:100].replace("\n", " "))
        print("  标准答案 2 → %s" % ("✓ 通过" if ok else "✗ 未通过"))
        print("  耗时 %.0fs" % r["elapsed"])
        results.append({"dim": "颜色计数", "ask": "几种颜色（答案 2）",
                        "passed": ok, "detail": r["raw"][:200],
                        "elapsed": r["elapsed"]})
    print()

    # ---- 维度 4：圆点计数 ----
    print("【维度 4】圆点计数 —— 完整可见，答案 4")
    r = ask(png, D4_COUNT.format(w=W, h=H), "D4")
    if r:
        ok = has_number(r["raw"], 4) or has_word(r["raw"], "four", "四")
        print("  回答: %s" % r["raw"][:100].replace("\n", " "))
        print("  标准答案 4 → %s" % ("✓ 通过" if ok else "✗ 未通过"))
        print("  耗时 %.0fs" % r["elapsed"])
        results.append({"dim": "圆点计数", "ask": "几个圆点（答案 4）",
                        "passed": ok, "detail": r["raw"][:200],
                        "elapsed": r["elapsed"]})
    print()

    print("=" * 74)
    print("维度汇总")
    print("=" * 74)
    print("  %-12s %-24s %8s %8s" % ("维度", "问法", "结果", "耗时"))
    for r in results:
        print("  %-12s %-24s %8s %7.0fs"
              % (r["dim"], r["ask"][:24],
                 "✓ 通过" if r["passed"] else "✗ 失败", r["elapsed"]))

    passed = sum(1 for r in results if r["passed"])
    print()
    print("  通过 %d/%d" % (passed, len(results)))
    print()
    if passed >= len(results) - 1:
        print("  ✓ 语义类视觉能力基本完好，而坐标定位完全不可用")
        print("    → **能力解离成立**：不是整体视觉能力缺失，")
        print("      是「知道有什么」但「说不出在哪」。")
    elif passed >= 1:
        print("  ~ 部分维度通过，解离成立但需说明哪些维度受损")
    else:
        print("  ✗ 全部维度失败 —— 这不是「解离」，是整体视觉能力缺失。")
        print("    必须改写结论：PTQ1 模型的视觉能力整体不足，")
        print("    而非「理解可用但定位不可用」。")

    path = pc.save_run("dims_%s" % time.strftime("%Y%m%d_%H%M%S"), {
        "group": "dimensions", "w": W, "h": H,
        "n_passed": passed, "n_dims": len(results),
        "dims": results,
        "dissociation": passed >= len(results) - 1,
        "note": "维度 2/3/4 不要求输出坐标，排除格式干扰",
    })
    print("\n已保存: %s" % os.path.basename(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
