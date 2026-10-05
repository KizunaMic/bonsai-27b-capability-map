"""
probe_control.py —— 控制实验：区分「视觉定位」与「算术转换」

背景：2026-10-05 的一轮实验出现了看似完美的 12/12 命中，
但 prompt 里写了「approximately 11% of the image width」，
而 11% × 1024 = 112.6，正是该目标点的真值 X。
**我在提问里告诉了它答案。** 那轮测的是算术，不是视觉。

本脚本做三组对照，判定模型到底用了什么信息：

  A 组 不给位置      「红色圆点的中心坐标」        → 真定位能力
  B 组 给正确比例    「约在宽度 11% 处」          → 算术 + 定位（混合）
  C 组 给错误比例    「约在宽度 50% 处」（实际不是）→ 纯算术？

**关键判据：**
  若 C 组也精确命中在 50% 对应位置 → 模型**根本没看图**，只做算术
  若 C 组命中真值附近             → 模型有定位能力，且会忽略错误提示

这是本项目至今最重要的一个控制实验。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_core as pc
import run_probe as rp

W, H = 1024, 683
R = 25
COLORS = [(255, 0, 0), (0, 0, 255), (0, 128, 0)]

# 三个真值分散的目标
TARGETS = [
    ("T1", 0.11, 0.13),
    ("T2", 0.43, 0.41),
    ("T3", 0.72, 0.34),
]


def build():
    buf = pc._make_rgb(W, H)
    truth = {}
    for i, (name, fx, fy) in enumerate(TARGETS):
        cx, cy = int(W * fx), int(H * fy)
        pc._disc(buf, W, H, cx, cy, R, COLORS[i])
        truth[name] = (cx, cy, fx, fy)
    return pc.png_bytes(W, H, buf), truth


P_A = ("This image contains solid circles on a plain white background. "
       "Locate the circle. Output ONLY its center coordinates as x,y. "
       "Image size is {w}x{h}. Do not explain.")

P_B = ("This image contains solid circles on a plain white background. "
       "The circle is at approximately {fx:.0%} of the image width and "
       "{fy:.0%} of the image height. "
       "Output ONLY its center coordinates as x,y. "
       "Image size is {w}x{h}. Do not explain.")

P_C = ("This image contains solid circles on a plain white background. "
       "The circle is at approximately 50% of the image width and "
       "50% of the image height. "
       "Output ONLY its center coordinates as x,y. "
       "Image size is {w}x{h}. Do not explain.")


def run(png, truth, group, prompt_tpl, fake=None):
    out = []
    for name, _fx, _fy in TARGETS:
        tx, ty, fx, fy = truth[name]
        pfx, pfy = (fake if fake else (fx, fy))
        prompt = prompt_tpl.format(w=W, h=H, fx=pfx, fy=pfy)
        try:
            txt, el, _u = rp.ask_vision(png, prompt, think=False)
        except Exception as e:
            print("    %s ERROR %s" % (name, repr(e)[:60]))
            continue
        a = pc.audit(txt, (tx, ty), W, H, 40)
        # 也算一下「离提示位置」的距离
        hint = (int(W * pfx), int(H * pfy))
        dh = pc.distance(a["loose"], hint) if a["loose"] else None
        a.update({"label": name, "truth": [tx, ty], "elapsed": round(el, 1),
                  "asked_pct": [pfx, pfy], "hint_px": list(hint),
                  "d_hint": round(dh, 1) if dh is not None else None})
        out.append(a)
        print("    %s 真值(%d,%d) 提示%s 输出%-12s 距真值%4.0fpx 距提示%4.0fpx %.0fs"
              % (name, tx, ty, "(%d,%d)" % hint, str(a["loose"] or "None"),
                 a["d_loose"] if a["d_loose"] is not None else -1,
                 a["d_hint"] if a["d_hint"] is not None else -1, el))
        time.sleep(0.4)
    return {"group": group, "case": "control_" + group, "w": W, "h": H,
            "results": out}


def main():
    print("=" * 70)
    print("控制实验：模型用的是眼睛还是算术？")
    print("=" * 70)
    png, truth = build()
    pc.save_case("control3_1024x683", png,
                 {k: (v[0], v[1]) for k, v in truth.items()}, W, H)
    for name, tx, ty, fx, fy in [(t[0], truth[t[0]][0], truth[t[0]][1],
                                  truth[t[0]][2], truth[t[0]][3])
                                 for t in TARGETS]:
        print("  %s 真值 (%d,%d) = 宽%.0f%% 高%.0f%%"
              % (name, tx, ty, fx * 100, fy * 100))
    print()

    print("【A 组】不给位置 —— 纯视觉定位")
    ga = run(png, truth, "ctrlA", P_A)
    print()
    print("【B 组】给正确比例 —— 算术 + 视觉（混合）")
    gb = run(png, truth, "ctrlB", P_B)
    print()
    print("【C 组】统一给 50%,50%（错误提示）—— 纯算术 or 抗提示？")
    gc = run(png, truth, "ctrlC", P_C, fake=(0.50, 0.50))

    print()
    print("=" * 70)
    print("判读")
    print("=" * 70)

    def stat(g):
        ok = [r for r in g["results"] if r.get("loose")]
        if not ok:
            return None
        d_true = [r["d_loose"] for r in ok if r["d_loose"] is not None]
        d_hint = [r["d_hint"] for r in ok if r["d_hint"] is not None]
        return {
            "n": len(ok),
            "hit_true": sum(1 for r in ok if r["loose_hit"]),
            "hit_hint": sum(1 for r in ok
                            if r["d_hint"] is not None and r["d_hint"] <= 40),
            "d_true": sum(d_true) / len(d_true) if d_true else None,
            "d_hint": sum(d_hint) / len(d_hint) if d_hint else None,
        }

    sa, sb, sc = stat(ga), stat(gb), stat(gc)
    for tag, s in [("A 不给位置", sa), ("B 给正确比例", sb),
                   ("C 给错误比例 50%/50%", sc)]:
        if not s:
            print("  %-22s 无数据" % tag)
            continue
        print("  %-22s 命中真值 %d/%d (均距 %.0fpx) | 命中提示 %d/%d (均距 %.0fpx)"
              % (tag, s["hit_true"], s["n"], s["d_true"] or -1,
                 s["hit_hint"], s["n"], s["d_hint"] or -1))

    print()
    if sc and sa:
        print("  >>> 核心判据（C 组）")
        if sc["hit_hint"] >= sc["n"] - 1 and sc["hit_true"] <= 1:
            print("      ✗✗ 模型**没有在看图**。给错误比例它照算，")
            print("         给正确比例它也算对 —— 它只在做百分比→像素换算。")
            print("         → 视觉定位能力实测为 0；上一轮 12/12 是算术的结果。")
        elif sc["hit_true"] >= sc["n"] - 1:
            print("      ✓ 模型**能抗错误提示**，命中真值而非提示位置。")
            print("         → 说明它确实在看图，定位能力存在。")
        else:
            print("      ? 结果混合：真值 %d/%d，提示 %d/%d —— 需更多样本"
                  % (sc["hit_true"], sc["n"], sc["hit_hint"], sc["n"]))
    if sa and sb:
        print()
        print("  >>> A vs B")
        if sb["d_true"] and sa["d_true"] and sb["d_true"] < sa["d_true"] / 5:
            print("      B 组误差比 A 组小 5 倍以上 → 给位置提示确实有帮助，")
            print("      但这不能区分「看得见」和「会算」—— C 组才是判据。")

    results = [ga, gb, gc]
    path = pc.save_run("control3_%s" % time.strftime("%Y%m%d_%H%M%S"), {
        "group": "control", "w": W, "h": H, "hit_radius": 40,
        "A_no_hint": sa, "B_correct_hint": sb, "C_wrong_hint": sc,
        "results": results,
        "note": "C 组给 50%,50% 但真值不在那；"
                "若 C 命中提示位置则证明模型未使用视觉信息",
    })
    print("\n已保存: %s" % os.path.basename(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
