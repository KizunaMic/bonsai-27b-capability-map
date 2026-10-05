"""
probe_density.py —— 网格猜测假说的决定性验证

背景（2026-10-05 实测）：
4 个目标点的真值 X 是 153/204/819/839（四个不同值），
但模型输出的 X 只有 153 和 803 两个值。Y 只有 197/557/773 三个值。

假说：模型不是在"定位"，而是在"猜网格"——输出少数几个标准位置，
      目标点落在哪它都无所谓。

这个脚本用**加密网格**验证：
- 12 个目标点，位置刻意选在非标准比例上
- 若模型输出仍集中在少数值 → 假说成立，grounding 能力确实不存在
- 若模型能给出分散的坐标 → 假说被推翻，之前的观察另有原因

设计要点：
1. 目标点位置**避开**常见的 1/4, 1/2, 3/4 比例（那是"猜网格"会命中的地方）
2. 纯色点、无文字标注（位置线索为零）
3. 每个点单独问一次，采样锁定 temperature=0
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_core as pc
import run_probe as rp

W, H = 1024, 683
R = 25

# 刻意避开 0.25/0.5/0.75 这些"猜网格"容易命中的比例
# 全部落在 0.10~0.90 之间，但用不规则的间距
DENSE = [
    ("P1",  0.11, 0.13),
    ("P2",  0.17, 0.29),
    ("P3",  0.13, 0.47),
    ("P4",  0.19, 0.66),
    ("P5",  0.23, 0.83),
    ("P6",  0.34, 0.08),
    ("P7",  0.37, 0.22),
    ("P8",  0.43, 0.41),
    ("P9",  0.47, 0.59),
    ("P10", 0.41, 0.77),
    ("P11", 0.68, 0.11),
    ("P12", 0.72, 0.34),
]

COLORS = [(255, 0, 0), (0, 0, 255), (0, 128, 0), (200, 0, 200)]


def build_dense():
    """加密网格图：12 个点，位置互不规则。"""
    buf = pc._make_rgb(W, H)
    truth = {}
    for i, (name, fx, fy) in enumerate(DENSE):
        cx, cy = int(W * fx), int(H * fy)
        pc._disc(buf, W, H, cx, cy, R, COLORS[i % len(COLORS)])
        truth[name] = (cx, cy)
    return pc.png_bytes(W, H, buf), truth


# ⚠️ **2026-10-05 修正：原 prompt 泄露了标准答案**
#
# 原版写的是「at approximately {fx:.0%} of the image width」，
# 而 fx 正是该目标点的位置比例 —— 11% x 1024 = 112.6 = 真值 X。
# **我在提问里告诉了它答案。** 12/12 命中测的是百分比换算，不是视觉。
#
# 改为按颜色指认目标，不提任何位置信息。
# 已由 probe_control.py 的 C 组证明：模型会盲从 prompt 里的位置提示。
PROMPT = (
    "This image contains solid circles on a plain white background. "
    "Locate the circle that is {ordinal} from the left and {ordinal_y} from "
    "the top. Count all circles by their horizontal position. "
    "Output ONLY its center coordinates as x,y. "
    "Image size is {w}x{h}. Do not explain."
)


def main():
    print("=" * 68)
    print("网格猜测假说验证")
    print("=" * 68)
    png, truth = build_dense()
    pc.save_case("dense12_1024x683", png, truth, W, H)
    print("图 1024x683，12 个目标点")
    xs = sorted(v[0] for v in truth.values())
    ys = sorted(v[1] for v in truth.values())
    print("  真值 X: %s  (%d 个不同值)" % (xs, len(set(xs))))
    print("  真值 Y: %s  (%d 个不同值)" % (ys, len(set(ys))))
    print()

    # 按 X 排序，给出「第几个（从左数）」——不含任何坐标信息
    ordered = sorted(DENSE, key=lambda t: t[1])
    rank_x = {name: i + 1 for i, (name, _, _) in enumerate(ordered)}
    ys_sorted = sorted(DENSE, key=lambda t: t[2])
    rank_y = {name: i + 1 for i, (name, _, _) in enumerate(ys_sorted)}

    results = []
    for name, fx, fy in DENSE:
        tx, ty = truth[name]
        # 只说「从左数第 N 个」—— 这是可验证的指代，不泄露坐标
        prompt = PROMPT.format(ordinal=rank_x[name], ordinal_y=rank_y[name],
                               w=W, h=H)
        try:
            txt, el, usage = rp.ask_vision(png, prompt, think=False)
        except Exception as e:
            print("  %-4s ERROR %s" % (name, repr(e)[:70]))
            results.append({"label": name, "truth": [tx, ty],
                            "error": repr(e)[:150]})
            continue
        a = pc.audit(txt, (tx, ty), W, H, 40)
        a["label"] = name
        a["elapsed"] = round(el, 1)
        a["truth"] = [tx, ty]
        a["rank_x"] = rank_x[name]
        a["rank_y"] = rank_y[name]
        results.append(a)
        print("  %-4s 左数第%d/高数第%d 真值(%d,%d) → 输出 %-12s 距%4.0fpx %.0fs"
              % (name, rank_x[name], rank_y[name], tx, ty,
                 str(a["loose"] or "None"),
                 a["d_loose"] if a["d_loose"] is not None else -1, el))
        time.sleep(0.4)

    ok = [r for r in results if not r.get("error") and r.get("loose")]
    print()
    print("=" * 68)
    if not ok:
        print("无有效样本")
        return 1

    mx = sorted(r["loose"][0] for r in ok)
    my = sorted(r["loose"][1] for r in ok)
    ux, uy = len(set(mx)), len(set(my))
    print("离散度")
    print("  模型输出 X: %s" % mx)
    print("  模型输出 Y: %s" % my)
    print("  唯一值     X %d/%d  Y %d/%d" % (ux, len(ok), uy, len(ok)))
    print()

    # 判定
    s_hit = sum(1 for r in ok if r["strict_hit"])
    l_hit = sum(1 for r in ok if r["loose_hit"])
    loss = sum(1 for r in ok if r["format_loss"])
    print("命中")
    print("  严格 %d/%d | 宽松 %d/%d | 格式损失 %d/%d (%.0f%%)"
          % (s_hit, len(ok), l_hit, len(ok), loss, len(ok),
             100.0 * loss / max(len(ok), 1)))
    d = [r["d_loose"] for r in ok if r["d_loose"] is not None]
    if d:
        print("  距离 中位 %.0fpx  最小 %.0fpx  最大 %.0fpx"
              % (sorted(d)[len(d) // 2], min(d), max(d)))
    dc = [r["d_center"] for r in ok if r.get("d_center")]
    if dc:
        print("  「往中心猜」基线 平均 %.0fpx" % (sum(dc) / len(dc)))
    print()

    verdict = []
    if ux <= max(3, len(ok) // 3) or uy <= max(3, len(ok) // 3):
        verdict.append("✗ 网格猜测假说成立：%d 个不同目标只产生 %d 个不同 X 值"
                       % (len(ok), ux))
        verdict.append("  → 模型不定位，输出少数标准位置")
    else:
        verdict.append("✓ 网格猜测假说不成立：X 有 %d/%d 个不同值，"
                       "输出是分散的" % (ux, len(ok)))
    if l_hit / max(len(ok), 1) < 0.3:
        verdict.append("✗ 宽松口径命中率仅 %.0f%%，groundding 能力确实不可用"
                       % (100.0 * l_hit / max(len(ok), 1)))
    else:
        verdict.append("✓ 宽松命中 %.0f%%，能力部分可用"
                       % (100.0 * l_hit / max(len(ok), 1)))
    for v in verdict:
        print("  " + v)

    path = pc.save_run("dense12_%s" % time.strftime("%Y%m%d_%H%M%S"), {
        "group": "density",
        "w": W, "h": H, "hit_radius": 40,
        "n_targets": len(DENSE),
        "unique_x": ux, "unique_y": uy, "n_valid": len(ok),
        "strict_hit": s_hit, "loose_hit": l_hit, "format_loss": loss,
        "verdict": verdict,
        "results": [{"label": "dense", "results": results}],
    })
    print("\n已保存: %s" % os.path.basename(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
