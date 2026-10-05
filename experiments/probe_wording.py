"""
probe_wording.py —— 缺口 1：排除「问法因素」

为什么要做：
现有 37 个样本的定位结论都来自**单一措辞**的 prompt。如果换种问法结果就变，
说明模型不是"不会定位"，而是"不懂我这种问法"。
那结论就完全不成立。

设计：同一张图，同一批目标点，用 N 种语义等价的措辞各问一遍。
  - 若各种措辞的误差都很大 → 结论稳健，能力确实不可用
  - 若某种措辞明显更好 → 之前的 prompt 选得不理想，需要重测

措辞类别（刻意覆盖不同风格）：
  1. 直白型     "Locate the red circle. Output x,y"
  2. 冗长型     长描述 + 格式要求 + 否定指令
  3. 简短型     "Where is the red circle? x,y"
  4. 格式强调   重复强调 "ONLY two numbers, nothing else"
  5. 中文       用中文问（模型是中文向的）
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_core as pc
import run_probe as rp

W, H = 1024, 683
R = 28
COLORS = [(255, 0, 0), (0, 0, 255), (0, 128, 0), (200, 0, 200)]
COLOR_NAME = {0: "red", 1: "blue", 2: "green", 3: "magenta"}
COLOR_CN = {0: "红色", 1: "蓝色", 2: "绿色", 3: "品红色"}

# 4 个目标点，沿用 probe_density 的不规则位置
POINTS = [
    ("Q1", 0.13, 0.19),
    ("Q2", 0.29, 0.52),
    ("Q3", 0.58, 0.31),
    ("Q4", 0.81, 0.74),
]

WORDINGS = {
    "plain": (
        "This image contains solid circles on a plain white background. "
        "Locate the {c} circle. Output ONLY its center coordinates as x,y. "
        "Image size is {w}x{h}. Do not explain."
    ),
    "verbose": (
        "You are given a {w}x{h} image. It is mostly pure white, with "
        "several small solid-colored circles drawn on it, and nothing else. "
        "Your task is to find the one that is {c}. Report the pixel "
        "coordinates of the CENTER of that circle. Respond with exactly two "
        "integers separated by a comma, in the form x,y, and output nothing "
        "else. Do not describe the image, do not explain your reasoning, and "
        "do not add units or labels."
    ),
    "terse": (
        "Where is the {c} circle? Give only x,y. Image is {w}x{h}."
    ),
    "format_emph": (
        "Locate the {c} circle in this {w}x{h} image.\n"
        "OUTPUT FORMAT: only \"x,y\" -- two numbers, one comma, nothing else.\n"
        "Any word, letter, space, or punctuation in your answer makes the "
        "answer invalid."
    ),
    "chinese": (
        "这张 {w}x{h} 的图片上有几个纯色圆点。请找出那个{c}的圆点，"
        "只输出它的中心坐标，格式为 x,y，不要解释。"
    ),
}


def build():
    buf = pc._make_rgb(W, H)
    truth = {}
    for i, (name, fx, fy) in enumerate(POINTS):
        cx, cy = int(W * fx), int(H * fy)
        pc._disc(buf, W, H, cx, cy, R, COLORS[i])
        truth[name] = (cx, cy, i)
    return pc.png_bytes(W, H, buf), truth


def main():
    print("=" * 74)
    print("缺口 1：多措辞对照 —— 排除「问法因素」")
    print("=" * 74)
    png, truth = build()
    pc.save_case("wording_1024x683", png,
                 {k: (v[0], v[1]) for k, v in truth.items()}, W, H)
    print("图 1024x683，4 个目标点，5 种措辞，共 20 次推理")
    for name, _, _ in POINTS:
        print("  %s 真值 (%d,%d)" % (name, truth[name][0], truth[name][1]))
    print()

    all_rows = []
    for wname, tpl in WORDINGS.items():
        print("【%s】" % wname)
        rows = []
        for name, _, _ in POINTS:
            tx, ty, ci = truth[name]
            cn = COLOR_CN[ci] if wname == "chinese" else COLOR_NAME[ci]
            prompt = tpl.format(c=cn, w=W, h=H)
            try:
                txt, el, _u = rp.ask_vision(png, prompt, think=False)
            except Exception as e:
                print("    %s ERROR %s" % (name, repr(e)[:60]))
                continue
            a = pc.audit(txt, (tx, ty), W, H, 40)
            a.update({"label": name, "wording": wname, "truth": [tx, ty],
                      "elapsed": round(el, 1), "raw_len": len(txt)})
            rows.append(a)
            print("    %s 真值(%d,%d) → %-12s 距%4.0fpx %s %.0fs"
                  % (name, tx, ty, str(a["loose"] or "None"),
                     a["d_loose"] if a["d_loose"] is not None else -1,
                     "命中" if a["loose_hit"] else "", el))
            time.sleep(0.3)
        ok = [r for r in rows if r.get("loose") is not None]
        if ok:
            hit = sum(1 for r in ok if r["loose_hit"])
            d = sorted(r["d_loose"] for r in ok if r["d_loose"] is not None)
            print("    小计: 宽松命中 %d/%d  中位距 %.0fpx"
                  % (hit, len(ok), d[len(d) // 2] if d else -1))
        all_rows.extend(rows)
        print()

    print("=" * 74)
    print("汇总：措辞是否是关键因素？")
    print("=" * 74)
    print("  %-14s %6s %10s %12s" % ("措辞", "命中", "中位距(px)", "平均耗时"))
    summary = []
    for wname in WORDINGS:
        rows = [r for r in all_rows if r.get("wording") == wname
                and r.get("loose") is not None]
        if not rows:
            continue
        hit = sum(1 for r in rows if r["loose_hit"])
        d = sorted(r["d_loose"] for r in rows if r["d_loose"] is not None)
        mid = d[len(d) // 2] if d else -1
        el = sum(r["elapsed"] for r in rows) / len(rows)
        summary.append((wname, hit, len(rows), mid, el))
        print("  %-14s %2d/%-3d %10.0f %11.1fs"
              % (wname, hit, len(rows), mid, el))

    if summary:
        best = max(summary, key=lambda x: x[1] / x[2])
        worst = min(summary, key=lambda x: x[1] / x[2])
        dc = pc.distance((W // 2, H // 2), (POINTS[0][1] and
             (int(W * POINTS[0][1]), int(H * POINTS[0][2]))))
        print("\n  最佳措辞: %s (%.0f%% 命中)"
              % (best[0], 100.0 * best[1] / best[2]))
        print("  最差措辞: %s (%.0f%% 命中)"
              % (worst[0], 100.0 * worst[1] / worst[2]))
        print()
        if best[1] / best[2] <= 0.25:
            print("  ✓ 结论稳健：所有措辞命中率都 ≤25%%，")
            print("    说明不是「问法不对」，是能力确实不可用。")
        else:
            print("  ✗ 存在明显更优的措辞 —— 之前的 prompt 选得不理想，")
            print("    需要用更好的措辞重测才能下结论。")

    path = pc.save_run("wording5_%s" % time.strftime("%Y%m%d_%H%M%S"), {
        "group": "wording", "w": W, "h": H, "hit_radius": 40,
        "n_wordings": len(WORDINGS), "n_points": len(POINTS),
        "summary": [{"wording": s[0], "hit": s[1], "n": s[2],
                     "median_px": s[3], "avg_s": s[4]} for s in summary],
        "results": [{"label": "wording", "results": all_rows}],
    })
    print("\n已保存: %s" % os.path.basename(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
