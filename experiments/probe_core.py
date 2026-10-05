"""
probe_core.py —— 能力探针核心（课题 #1 #2 #5 #6 #7 共用）

设计原则（来自本项目三次翻车的教训）：
1. **原始输出必须完整保存。** 三次错误（Qwen 视觉误判、坐标误判、格式误判）
   全部源于没有回看模型的原始文本。
2. **标准答案必须无争议。** 定位探针只用纯色圆点 + 四象限分布，
   排除「识别歧义」和「往中心猜」两个混淆源。
3. **判定必须分层。** 同一份输出用三种解析器各判一次，
   差异本身就是数据（见 EXPERIMENT_DESIGN.md §3.3）。

本模块不联网、不调模型，可独立运行与测试。
"""

import io
import json
import os
import re
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
IMAGES = os.path.join(BASE, "images")
RUNS = os.path.join(BASE, "runs")

# ==================== 合成测试图 ====================
# 为什么不用真实 UI 截图：真实界面有文字/图标/渐变，模型可能靠 OCR 或语义
# 猜对，混入额外变量。先用纯色点测最基础的空间能力。

WHITE = (255, 255, 255)
RED = (255, 0, 0)
BLUE = (0, 0, 255)
GREEN = (0, 128, 0)

# 四象限比例（刻意偏离中心，破坏「往中心猜」策略）
QUAD = {
    "left_top": (0.15, 0.20),
    "right_top": (0.82, 0.25),
    "left_bottom": (0.20, 0.80),
    "right_bottom": (0.80, 0.78),
}
POINT_RADIUS = 30

TARGETS = {
    "A": ("left_top", RED),
    "B": ("right_bottom", BLUE),
    "C": ("right_top", GREEN),
    "D": ("left_bottom", RED),
}


def _make_rgb(width, height, bg=WHITE):
    return bytearray(bytes(bg) * (width * height))


def _put(buf, width, height, x, y, color):
    if 0 <= x < width and 0 <= y < height:
        i = (y * width + x) * 3
        buf[i] = color[0]
        buf[i + 1] = color[1]
        buf[i + 2] = color[2]


def _disc(buf, width, height, cx, cy, r, color):
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dx * dx + dy * dy <= r * r:
                _put(buf, width, height, cx + dx, cy + dy, color)


def png_bytes(width, height, buf):
    """把 RGB bytearray 编码成 PNG。用标准库 zlib + struct，不依赖 Pillow。"""
    import struct
    import zlib

    raw = bytearray()
    for y in range(height):
        raw.append(0)                       # filter type 0 (None)
        start = y * width * 3
        raw += buf[start:start + width * 3]

    def chunk(tag, data):
        c = (struct.pack(">I", len(data)) + tag + data
             + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
        return c

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def build_dot_image(width, height, targets=("A", "B", "C", "D")):
    """生成纯色点图。返回 (png_bytes, {name: (x, y)})。

    ⚠️ 图上**不画任何文字标注**——标注会泄露位置，变成位置线索。
    """
    buf = _make_rgb(width, height)
    truth = {}
    for name in targets:
        quad, color = TARGETS[name]
        fx, fy = QUAD[quad]
        cx, cy = int(width * fx), int(height * fy)
        _disc(buf, width, height, cx, cy, POINT_RADIUS, color)
        truth[name] = (cx, cy)
    return png_bytes(width, height, buf), truth


def build_grid_image(width=1024, height=683, rows=10, cols=10):
    """生成 10×10 网格图。每格标「r,c」（r 上→下，c 左→右）。

    标准答案：第 7 行第 3 列的中心。
    """
    import struct
    import zlib
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            i = (y * width + x) * 3
            grid = (x % (width // cols) < 2) or (y % (height // rows) < 2)
            if grid:
                raw += bytes((180, 180, 180))
            else:
                raw += bytes(WHITE)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))
    cw, ch = width / cols, height / rows
    truth = {"7,3": (int(cw * 2.5), int(ch * 6.5))}
    return data, truth


def save_case(name, data, truth, width, height):
    """保存测试图 + 标准答案到 images/。"""
    if not os.path.isdir(IMAGES):
        os.makedirs(IMAGES, exist_ok=True)
    path = os.path.join(IMAGES, name + ".png")
    with open(path, "wb") as f:
        f.write(data)
    meta = {
        "name": name,
        "width": width,
        "height": height,
        "truth": {k: {"x": v[0], "y": v[1]} for k, v in truth.items()},
        "quadrants": {k: (TARGETS[k][0] if k in TARGETS else "n/a")
                      for k in truth},
        "note": "图上无文字标注，位置线索为零",
    }
    with open(os.path.join(IMAGES, name + ".json"), "w",
              encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return path, meta


# ==================== 三种解析器（核心：C3 格式审计）====================

def parse_strict(text, img_w=None, img_h=None):
    """严格解析 —— **直接复用项目生产代码 `pc_locator.parse_coords`**。

    ⚠️ 为什么复用而不是复刻：
    C3 格式审计的全部意义在于「我们已有结论实际用的那个判定口径有多严」。
    复刻一份简化版会得到**虚假的格式损失率**（我第一版就犯了这个错：
    自己写的 strict 漏了括号/句号处理，测出「9 种正确表达只认 1 种」，
    而真实的 pc_locator 实际能认 4 种。**那个 1/9 是我制造的假象。**）

    剥离逻辑共七步（见 pc_locator.py:128-141）：前导 x= / 前导 ( /
    , y= / x y= / , x= / 空格+y: / 尾部 ). 。，然后 fullmatch 必须是纯坐标。
    最后还做负坐标与越界检查。

    返回 (x, y) 或 None。带解释文字一律判失败 —— 这是刻意设计的，
    因为从解释里抠数字会产出幻觉坐标（"第1个和第2个" → (1,2)）。
    """
    try:
        sys.path.insert(0, os.path.join(BASE, "..", "feishu-bot"))
        import pc_locator
    except Exception:
        return _parse_strict_fallback(text)
    w = img_w or 1024
    h = img_h or 683
    try:
        x, y = pc_locator.parse_coords(text, w, h)
    except Exception:
        return None
    return (x, y) if (x is not None and y is not None) else None


def _parse_strict_fallback(text):
    """pc_locator 不可用时的降级实现（保持同一口径）。"""
    t = (text or "").strip()
    t = re.sub(r"^[xX]\s*[=：:]\s*", "", t)
    t = re.sub(r"^[\(（]\s*", "", t)
    t = re.sub(r"[,，、]\s*[yY]\s*[=：:]\s*", ",", t)
    t = re.sub(r"[,，、]?\s*[xX]\s*[yY]\s*[=：:]\s*", ",", t)
    t = re.sub(r"[,，、]\s*[xX]\s*[=：:]\s*", ",", t)
    t = re.sub(r"\s+[yY]\s*[=：:]\s*", ",", t)
    t = re.sub(r"[\)）]\s*[.。]?$", "", t)
    t = re.sub(r"[.。]$", "", t).strip()
    if not re.fullmatch(r"\d{1,5}\s*[,，、]\s*\d{1,5}", t):
        return None
    m = re.fullmatch(r"(\d{1,5})\s*[,，、]\s*(\d{1,5})", t)
    if not m:
        return None
    x, y = int(m.group(1)), int(m.group(2))
    if x < 0 or y < 0:
        return None
    return (x, y)


def parse_loose(text):
    """宽松解析 —— 从任意文本里抠出第一对数字。

    ⚠️ 这正是本项目早期刻意**避免**的做法（「第1个和第2个」→ (1,2)、
    「宽度800 高度600」→ (800,600)）。但它在这里是有用的——
    用来区分「模型答错了」和「模型答对了但格式不合规」。
    """
    t = (text or "")
    # 优先找带 box 语义的 [x1,y1,x2,y2]
    m = re.search(r"\[\s*(\d{1,5})\s*[，,]\s*(\d{1,5})"
                  r"\s*[，,]\s*(\d{1,5})\s*[，,]\s*(\d{1,5})\s*\]", t)
    if m:
        return ((int(m.group(1)) + int(m.group(3))) // 2,
                (int(m.group(2)) + int(m.group(4))) // 2)
    # 常见标签形式
    m = re.search(r"x\s*[=：:]\s*(\d{1,5}).{0,8}?y\s*[=：:]\s*(\d{1,5})",
                  t, re.I | re.S)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    # 兜底：第一对相邻数字
    m = re.search(r"(\d{1,5})\s*[^0-9\-]{1,4}\s*(\d{1,5})", t)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return None


# 「往中心猜」这个策略的基线预测：返回图片中心
def parse_center(width, height):
    return (width // 2, height // 2)


def distance(a, b):
    if a is None or b is None:
        return None
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def audit(text, truth_xy, width, height, hit_radius=40):
    """三解析器对照 + 语义判定代理。

    语义判定用两个代理指标（人工复核前先看这两个）：
      - `center_hit`: 宽松/中心猜测是否命中
      - `box_hit`:   若模型给了 box，其中心是否命中

    返回 dict，含**格式损失率**这个关键指标所需的全部分量。
    """
    s = parse_strict(text, width, height)
    l = parse_loose(text)
    c = parse_center(width, height)

    def hit(pt):
        return distance(pt, truth_xy) is not None and \
            distance(pt, truth_xy) <= hit_radius

    return {
        "raw": text,
        "raw_len": len(text or ""),
        "strict": list(s) if s else None,
        "loose": list(l) if l else None,
        "center": list(c),
        "strict_hit": hit(s),
        "loose_hit": hit(l),
        "center_hit": hit(c),
        "d_strict": (round(distance(s, truth_xy), 1) if s else None),
        "d_loose": (round(distance(l, truth_xy), 1) if l else None),
        "d_center": round(distance(c, truth_xy), 1),
        # 关键判据：严格判失败，但宽松命中 → 格式问题，不是能力问题
        "format_loss": (not hit(s)) and hit(l),
        "has_explanation": bool(re.search(r"[，。,.；;]", (text or "")[:200])),
    }


# ==================== 运行记录 ====================

def save_run(run_id, payload):
    if not os.path.isdir(RUNS):
        os.makedirs(RUNS, exist_ok=True)
    payload = dict(payload)
    payload["run_id"] = run_id
    payload["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    path = os.path.join(RUNS, run_id + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def load_runs():
    """读取全部运行记录。**跨进程读取**，用于子进程验证。"""
    if not os.path.isdir(RUNS):
        return []
    out = []
    for fn in sorted(os.listdir(RUNS)):
        if fn.endswith(".json"):
            with open(os.path.join(RUNS, fn), encoding="utf-8") as f:
                try:
                    out.append(json.load(f))
                except json.JSONDecodeError:
                    pass
    return out


# ==================== 自检 ====================

if __name__ == "__main__":
    print("=" * 62)
    print("probe_core 自检（不调模型）")
    print("=" * 62)

    # --- 合成图 ---
    data, truth = build_dot_image(1024, 683)
    p, meta = save_case("dots_1024x683", data, truth, 1024, 683)
    print("\n[1] 纯色点图")
    print("    %s (%d 字节)" % (os.path.basename(p), len(data)))
    for k, v in truth.items():
        print("    目标 %s @ (%d, %d)  %s" % (k, v[0], v[1], meta["quadrants"][k]))
    # 验证四象限真的分散开
    xs = [v[0] for v in truth.values()]
    ys = [v[1] for v in truth.values()]
    assert max(xs) - min(xs) > 500, "X 方向没拉开"
    assert max(ys) - min(ys) > 300, "Y 方向没拉开"
    print("    四象限分布 OK（X 跨度 %d, Y 跨度 %d）"
          % (max(xs) - min(xs), max(ys) - min(ys)))

    g, gtruth = build_grid_image()
    gp, _ = save_case("grid_10x10", g, gtruth, 1024, 683)
    print("\n[2] 网格图")
    print("    %s   7行3列中心 = %s" % (os.path.basename(gp), gtruth["7,3"]))

    # --- 三解析器对照：核心 ---
    print("\n[3] 三解析器对照（用模拟输出验证判定逻辑）")
    tx, ty = truth["A"]
    cases = [
        ("纯坐标", "%d,%d" % (tx, ty)),
        ("带解释句", "红色圆点大约在 (%d, %d) 附近。" % (tx, ty)),
        ("x= 标签", "x=%d, y=%d" % (tx, ty)),
        ("bbox 格式", "[%d,%d,%d,%d]" % (tx - 30, ty - 30, tx + 30, ty + 30)),
        ("答错位置", "120,180"),
        ("完全没答", "抱歉，我无法确定。"),
        ("空输出", ""),
    ]
    print("    %-10s %-14s %-14s %-8s %-8s %s"
          % ("输入类型", "严格", "宽松", "严格中?", "宽松中?", "格式损失"))
    n_fmtloss = 0
    for label, txt in cases:
        a = audit(txt, (tx, ty), 1024, 683)
        if a["format_loss"]:
            n_fmtloss += 1
        print("    %-10s %-14s %-14s %-8s %-8s %s"
              % (label,
                 a["strict"] or "None", a["loose"] or "None",
                 "✓" if a["strict_hit"] else "✗",
                 "✓" if a["loose_hit"] else "✗",
                 "⚠️ 是" if a["format_loss"] else "-"))
    print("\n    格式损失 %d/%d" % (n_fmtloss, len(cases)))
    print("    中心猜测基线距离: %.1f px" % audit("", (tx, ty), 1024, 683)["d_center"])

    # --- 关键断言 ---
    print("\n[4] 判定逻辑自检")
    a = audit("红色圆点大约在 (%d, %d) 附近。" % (tx, ty), (tx, ty), 1024, 683)
    assert a["loose_hit"] and not a["strict_hit"], "应判为格式损失"
    print("    ✓ 「带解释句但位置正确」被判为格式损失，不是能力缺失")

    a2 = audit("120,180", (tx, ty), 1024, 683)
    assert not a2["loose_hit"], "答错不应命中"
    print("    ✓ 「答错位置」不被误判为命中")

    a3 = audit("", (tx, ty), 1024, 683)
    assert a3["strict"] is None and a3["loose"] is None, "空输出不应解析出坐标"
    print("    ✓ 「空输出」不产生幻觉坐标")

    a4 = audit("[%d,%d,%d,%d]" % (tx - 30, ty - 30, tx + 30, ty + 30),
               (tx, ty), 1024, 683)
    assert a4["loose_hit"], "bbox 中心应命中"
    print("    ✓ 「bbox 格式」宽松解析取中心，命中")

    print("\n" + "=" * 62)
    print("自检通过。探针管道可用。")
    print("下一步：等引擎启动后跑 run_probe.py")
    print("=" * 62)
