"""
analyze.py —— 汇总实验数据并出图（课题 #1 #2 #3 #6）

⚠️ 实现约束：**HTML 模板一律用占位符替换，不用 % 格式化**。
原因：模板里含 CSS 的 `width:100%`，用 % 格式化会抛
`unsupported format character`。这是本项目第三次栽在
「把非格式化内容塞进 % 格式化模板」上（前两次：Windows bat
的 %变量、experiment_design 的 heredoc 转义）。

用法:
    python analyze.py summary     文本汇总
    python analyze.py charts      生成 HTML 图表 + Markdown
    python analyze.py all
"""

import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(BASE, "runs")
OUT = os.path.join(BASE, "analysis")

COLORS = {
    "ok": "#1D9E75", "strict": "#185FA5", "loss": "#EF9F27",
    "miss": "#E24B4A", "lat": "#D85A30", "pal": ["#1D9E75", "#185FA5",
                                                  "#BA7517", "#534AB7"],
}

# ---------------------------------------------------------------------------
# 双语文案
#
# 设计：报告是**单文件**，中英两套内容都在同一个 HTML 里，
# 用 <body data-lang="zh|en"> + CSS 显隐切换（无网络请求、无重排）。
# 图表也必须双语——SVG 里的标题是文本节点，不切就会中英混杂。
# ---------------------------------------------------------------------------
LANGS = ("zh", "en")

L = {
    "zh": {
        "doc_title": "三元量化 27B 能力探针 · 实验结果",
        "h1": "三元量化 27B 能力探针 · 实验结果",
        "meta": "模型 <code>Ternary-Bonsai-2-27B-PTQ1</code>｜探针：纯色圆点定位，"
                "命中半径 40px｜采样锁定 <code>temperature=0</code>",
        "meta2": "<b>主指标仅统计「无位置提示」的定位样本</b>；"
                 "对照组与无效组已单列，见下方说明。",
        "kpi": ["样本数", "严格命中", "宽松命中", "格式损失",
                "真能力缺失", "格式损失率"],
        "verdict": "判读",
        "verdict_txt": "严格与宽松之差 = <code>格式损失</code>，"
                       "即模型答对了位置但被判定口径记为失败的数量。"
                       "这一项直接决定「grounding 能力缺失」这一已有结论是否需要修正。",
        "dissoc": "能力解离",
        "dissoc_txt": "语义类视觉维度 <code>%s</code> 通过，坐标定位 <code>%s</code>。"
                      "多措辞对照最佳 <code>%s</code>。",
        "excl_head": "未计入主指标的数据",
        "excl_of": "（共 %d 条，占全部 %d 条）",
        "excl_note": "混入这些样本会把命中率从 <b>%.0f%%</b> 虚高到 %.0f%%。",
        "kind": {"grounding": "定位", "control_hint": "对照",
                 "invalid": "无效", "dimension": "维度"},
        "excl_reason": {
            "control_hint": "有意给出位置提示的对照组，B 组设计上就应命中",
            "invalid": "实验设计有缺陷（prompt 泄露答案），结论不可用",
            "dimension": "语义维度探针，不测坐标",
        },
        "raw_h2": "原始输出",
        "raw_sub": "完整保存不截断，用于人工复核「语义是否指向正确位置」。",
        "th": ["目标", "真值", "类型", "严格", "宽松", "格式损失", "耗时",
               "模型原始输出"],
        "toggle": ["中文", "EN"],
        "switch_to": "切换到英文",
        "no_data": "无数据",
    },
    "en": {
        "doc_title": "Ternary-Quantized 27B Capability Probe · Results",
        "h1": "Ternary-Quantized 27B Capability Probe · Results",
        "meta": "Model <code>Ternary-Bonsai-2-27B-PTQ1</code>｜probe: solid-dot "
                "localization, hit radius 40px｜sampling locked at "
                "<code>temperature=0</code>",
        "meta2": "<b>The primary metric counts only no-hint localization "
                 "samples</b>; control and invalid runs are listed separately "
                 "below.",
        "kpi": ["Samples", "Strict hits", "Loose hits", "Format loss",
                "True miss", "Format-loss rate"],
        "verdict": "Verdict",
        "verdict_txt": "Strict − loose = <code>format loss</code>, i.e. answers "
                       "that hit the right location but were recorded as "
                       "failures by the parsing criterion. This directly "
                       "determines whether the existing "
                       "&ldquo;grounding is missing&rdquo; conclusion needs "
                       "revising.",
        "dissoc": "Dissociation",
        "dissoc_txt": "Semantic vision dimensions <code>%s</code> passed; "
                      "coordinate localization <code>%s</code>. "
                      "Best wording variant <code>%s</code>.",
        "excl_head": "Data excluded from the primary metric",
        "excl_of": " (%d of %d samples in total)",
        "excl_note": "Folding these in would inflate the hit rate from "
                     "<b>%.0f%%</b> to %.0f%%.",
        "kind": {"grounding": "grounding", "control_hint": "control",
                 "invalid": "invalid", "dimension": "dimension"},
        "excl_reason": {
            "control_hint": "Control group given a location hint on purpose; "
                            "group B is designed to hit",
            "invalid": "Flawed experimental design (prompt leaked the answer); "
                       "result unusable",
            "dimension": "Semantic-dimension probe; does not measure coordinates",
        },
        "raw_h2": "Raw model outputs",
        "raw_sub": "Stored in full, not truncated — for manual review of whether "
                   "the answer refers to the right location.",
        "th": ["Target", "Truth", "Kind", "Strict", "Loose", "Format loss",
               "Time", "Raw model output"],
        "toggle": ["中文", "EN"],
        "switch_to": "Switch to Chinese",
        "no_data": "no data",
    },
}

# 图表文案（标题/注释/行标签）——同样必须双语
CHART = {
    "zh": {
        "fig0": ("图 0　控制实验：模型用的是眼睛还是算术？",
                 "若「距提示」远小于「距真值」，说明模型只按 prompt 算术换算，未真正看图",
                 "%s 距真值", "%s 距提示", "组"),
        "fig1": ("图 1　三种判定口径的命中数（仅无提示定位，n=%d）",
                 "严格与宽松之差即格式损失；本口径已排除对照组与无效组",
                 ["严格口径（项目现用）", "宽松口径（含格式容错）",
                  "└ 因格式被误判", "真能力缺失"], " 例"),
        "fig2": ("图 2　各组格式损失率（宽松命中% − 严格命中%）",
                 "正值说明该组存在格式口径造成的误判", "%"),
        "fig3": ("图 3　单次视觉推理耗时（按实验组平均）",
                 "首次调用含视觉塔预热会显著偏高；terse 短问法比 verbose 快约 2 倍",
                 "s", "全局：最小 %.0fs · 中位 %.0fs · 最大 %.0fs（最大值为首次调用，含视觉塔预热）"),
        "fig4": ("图 4　坐标离散度：模型输出有多少个不同值",
                 "若输出值远少于目标点数，说明模型在猜网格而非定位",
                 ["不同 X 值", "不同 Y 值", "目标点总数"], " 个"),
        "fig5": ("图 5　能力解离：语义类维度全过，唯独坐标定位失败",
                 "若语义维度全过而定位全败，则不是整体视觉能力缺失",
                 "★ 坐标定位（输出 x,y）", "%s（%s）", ""),
        "fig6": ("图 6　多措辞对照：换 5 种问法，命中率如何",
                 "若各种措辞命中率都低，说明结论稳健，不依赖问法", "%"),
        # 维度探针里的名字/问法是中文原值，英文版需要映射
        "dim_map": {}, "ask_map": {}, "show_ask": True,
    },
    "en": {
        "fig0": ("Fig 0 · Control experiment: eyes or arithmetic?",
                 "If distance-to-hint ≪ distance-to-truth, the model is only "
                 "converting prompt numbers, not looking at the image",
                 "%s vs truth", "%s vs hint", "grp"),
        "fig1": ("Fig 1 · Hits under three parsing criteria (no-hint only, n=%d)",
                 "Strict − loose = format loss; control and invalid runs are "
                 "excluded from this count",
                 ["Strict (production)", "Loose (format-tolerant)",
                  "└ misjudged by format", "True capability miss"], " cases"),
        "fig2": ("Fig 2 · Format-loss rate per run (loose% − strict%)",
                 "A positive value means the run contains misjudgements caused by "
                 "the parsing criterion", "%"),
        "fig3": ("Fig 3 · Vision inference latency per call (run mean)",
                 "The first call includes vision-tower warm-up and is much higher; "
                 "terse prompts are ~2× faster than verbose ones",
                 "s", "Overall: min %.0fs · median %.0fs · max %.0fs "
                      "(max is the first call, incl. warm-up)"),
        "fig4": ("Fig 4 · Coordinate discreteness: how many distinct values",
                 "Far fewer distinct outputs than targets means the model is "
                 "guessing a grid, not localizing",
                 ["distinct X values", "distinct Y values", "total targets"],
                 " values"),
        "fig5": ("Fig 5 · Dissociation: all semantic dims pass, localization fails",
                 "All semantic dimensions passing while localization fails "
                 "entirely is not a general vision deficit",
                 "★ Localization (x,y)", "%s (%s)", ""),
        "fig6": ("Fig 6 · Wording robustness: hit rate across 5 phrasings",
                 "Low hit rate across all phrasings means the conclusion is "
                 "robust and does not depend on phrasing", "%"),
        # 英文的「维度名 + 问法」拼起来太长（>240px），超出标签列宽，
        # 所以英文只显示维度名
        "show_ask": False,
        "dim_map": {"内容理解": "Content understanding",
                    "左半区计数": "Left-half count",
                    "颜色计数": "Colour count",
                    "圆点计数": "Object count"},
        "ask_map": {"说出图里有什么": "list contents",
                    "左半有几个（答案 2）": "how many on the left (2)",
                    "几种颜色（答案 2）": "how many colours (2)",
                    "几个圆点（答案 4）": "how many dots (4)"},
    },
}


def load_all():
    rows = []
    if not os.path.isdir(RUNS):
        return rows
    for fn in sorted(os.listdir(RUNS)):
        if not fn.endswith(".json"):
            continue
        with open(os.path.join(RUNS, fn), encoding="utf-8") as f:
            try:
                rows.append((fn, json.load(f)))
            except json.JSONDecodeError:
                pass
    return rows


def flatten(payload):
    out = []
    for grp in (payload.get("results") or []):
        if not isinstance(grp, dict):
            continue
        meta = {"case": grp.get("case"), "w": grp.get("w"),
                "h": grp.get("h"), "think": grp.get("think"),
                "unique_x": grp.get("unique_x"),
                "unique_y": grp.get("unique_y")}
        for r in (grp.get("results") or []):
            if not isinstance(r, dict) or r.get("error"):
                continue
            m = dict(meta)
            m.update({
                "label": r.get("label"),
                "raw": r.get("raw", ""),
                "strict_hit": r.get("strict_hit"),
                "loose_hit": r.get("loose_hit"),
                "format_loss": r.get("format_loss"),
                "d_loose": r.get("d_loose"),
                "d_center": r.get("d_center"),
                "elapsed": r.get("elapsed"),
                "truth": r.get("truth"),
                "d_hint": r.get("d_hint"),
                "hint_px": r.get("hint_px"),
            })
            out.append(m)
    return out


def classify_run(fn, payload):
    """给每个 run 分类。**这是防止"把无效数据算进结论"的关键。**

    三类：
      grounding   测量真实定位能力（无位置提示）→ 计入主指标
      control_hint 有意给出位置提示的对照组 → **不计入主指标**
      invalid     实验设计有缺陷，结论不可用 → **不计入任何指标**

    背景（2026-10-05 实际踩的坑）：
    第一版汇总把三类样本混在一起算，得出「命中 17/57 = 30%」。
    但其中 12 个来自一次 prompt 泄露答案的实验（我告诉它位置它照抄），
    另有 3 个来自 control 组的 B 组（有意给正确位置提示，设计上就该命中）。
    真实的无提示定位命中率是 2/39 ≈ 5%。

    **把无效/对照数据混入主指标，是一个会直接毁掉报告可信度的错误。**
    """
    grp = payload.get("group")
    # prompt 泄露答案的那一次：文件名带时间戳，用「prompt 是否含位置」标记
    if grp == "density":
        # 修正后的版本改为序号指代（rank_x），泄露版带 asked_fx
        for g in (payload.get("results") or []):
            for r in (g.get("results") or []):
                if r.get("asked_fx") is not None:
                    return "invalid"
        return "grounding"
    if grp == "control":
        return "control_hint"
    if grp in ("fmt", "wording"):
        return "grounding"
    if grp == "dimensions":
        return "dimension"
    return "other"


def gather():
    samples = []
    for fn, p in load_all():
        kind = classify_run(fn, p)
        for s in flatten(p):
            s["run"] = fn
            s["kind"] = kind
            samples.append(s)
    return samples


VERDICTS = {
    "zh": ("需修正已有结论", "存在但非主因", "能力缺失为主因（口径无误）"),
    "en": ("existing conclusion needs revision",
           "present but not the main cause",
           "capability miss is the main cause (criterion is sound)"),
}


def verdict_of(loss, n, lang="zh"):
    v = VERDICTS.get(lang, VERDICTS["zh"])
    rate = 100.0 * loss / max(n, 1)
    if rate > 30:
        return v[0], rate
    if loss:
        return v[1], rate
    return v[2], rate


def summary():
    rows = load_all()
    samples = gather()
    print("=" * 68)
    print("实验数据汇总   样本 %d 条，来自 %d 个 run"
          % (len(samples), len(rows)))
    print("=" * 68)
    if not samples:
        print("尚无数据。先跑 run_probe.py fmt")
        return

    # ⚠️ 主指标**只统计 grounding 类**样本。
    # control_hint 是有意给提示的对照组，invalid 是设计有缺陷的那次，
    # 两者混进来会虚高命中率（实测混算得 30%，真实 5%）。
    ground = [s for s in samples if s["kind"] == "grounding"]
    other = [s for s in samples if s["kind"] != "grounding"]

    if ground:
        n = len(ground)
        s_hit = sum(1 for s in ground if s["strict_hit"])
        l_hit = sum(1 for s in ground if s["loose_hit"])
        loss = sum(1 for s in ground if s["format_loss"])
        miss = n - l_hit
        v, rate = verdict_of(loss, n)
        print("\n【主指标】无位置提示的定位（kind=grounding，n=%d）" % n)
        print("  严格命中      %d/%d  (%.0f%%)" % (s_hit, n, 100.0 * s_hit / n))
        print("  宽松命中      %d/%d  (%.0f%%)" % (l_hit, n, 100.0 * l_hit / n))
        print("  格式损失      %d/%d  (%.0f%%)" % (loss, n, rate))
        print("  真能力缺失    %d/%d  (%.0f%%)" % (miss, n, 100.0 * miss / n))
        print("\n  >>> 判读：%s" % v)
    else:
        n = s_hit = l_hit = loss = miss = 0
        rate = 0
        v = "无 grounding 样本"

    if other:
        print("\n【未计入主指标】")
        for k in sorted(set(s["kind"] for s in other)):
            ss = [s for s in other if s["kind"] == k]
            h = sum(1 for s in ss if s["loose_hit"])
            reason = {"control_hint": "有意给位置提示的对照组"
                      + "（B 组设计上就该命中）",
                      "invalid": "实验设计有缺陷（prompt 泄露答案）",
                      "dimension": "语义维度探针，不测坐标",
                      "other": ""}.get(k, "")
            print("  %-13s n=%-3d 命中=%-3d  %s" % (k, len(ss), h, reason))

    print("\n【按 run】")
    groups = {}
    for s in samples:
        groups.setdefault(s["run"], []).append(s)
    print("  %-28s %5s %10s %10s %s"
          % ("run", "n", "宽松命中", "格式损失", "类型"))
    for fn, ss in sorted(groups.items()):
        a = sum(1 for x in ss if x["loose_hit"])
        c = sum(1 for x in ss if x["format_loss"])
        print("  %-28s %5d %10s %10s %s"
              % (fn[:28], len(ss),
                 "%d (%.0f%%)" % (a, 100.0 * a / len(ss)),
                 "%d (%.0f%%)" % (c, 100.0 * c / len(ss)),
                 ss[0]["kind"]))
        ux = next((x.get("unique_x") for x in ss
                   if x.get("unique_x") is not None), None)
        uy = next((x.get("unique_y") for x in ss
                   if x.get("unique_y") is not None), None)
        if ux is not None:
            print("       坐标离散度  X %d/%d  Y %s/%d"
                  % (ux, len(ss), uy, len(ss)))

    els = [s["elapsed"] for s in samples if s.get("elapsed")]
    if els:
        es = sorted(els)
        print("\n【延迟】单次视觉推理")
        print("  最小 %.0fs | 中位 %.0fs | 最大 %.0fs"
              % (es[0], es[len(es) // 2], es[-1]))
        print("  ⚠️ 首次调用含预热（视觉塔加载），会显著偏高")

    dc = [s["d_center"] for s in samples if s.get("d_center")]
    if dc:
        print("\n【基线】「往中心猜」平均距离 %.0f px" % (sum(dc) / len(dc)))
        print("  模型若只会猜中心，误差就是这个量级")

    print("\n【原始输出（人工复核用）】")
    for s in samples[:8]:
        raw = (s["raw"] or "").replace("\n", " ")[:60]
        print("  [%-3s] %s 宽松%s 格式损失%s  %s"
              % (s["label"], s["kind"][:8],
                 "✓" if s["loose_hit"] else "✗",
                 "⚠️" if s["format_loss"] else "-", raw))


# ==================== 图表（内嵌 SVG，无外部依赖）====================

def svg_bars(title, rows, unit="", note="", hunit="px", figid="", lang="zh"):
    if not rows:
        return "<p class='empty'>%s</p>" % L[lang]["no_data"]
    w = 660
    # 标签列宽度：英文标签比中文长得多（"Loose (format-tolerant)" ≈ 160px，
    # 而中文同类标签 ≈ 80px）。原来的 136px 会让英文字压到柱子上 ——
    # 这是双语版才暴露出来的问题，已按最长的英文标签留出 200px。
    LAB_X, LAB_W = 24, 200
    BAR_X, BAR_MAX = 232, 362
    h = 118 + len(rows) * 46
    mx = max([abs(v) for _, v, _ in rows] + [1])
    # data-fig / data-lang 供 export_figs.py 稳定识别（不靠标题文本，
    # 因为标题现在是双语的，中英各一份）
    o = ["<svg data-fig='%s' data-lang='%s' viewBox='0 0 %d %d' width='100%%' "
         "role='img' aria-label='%s'>" % (figid, lang, w, h, title)]
    o.append("<title>%s</title><desc>%s</desc>" % (title, note or title))
    o.append("<text x='24' y='28' font-family='sans-serif' font-size='15' "
             "font-weight='500' fill='#2C2C2A'>%s</text>" % title)
    y = 46
    for lab, val, col in rows:
        bw = int(BAR_MAX * abs(val) / mx)
        o.append("<text x='%d' y='%d' font-family='sans-serif' "
                 "font-size='13' fill='#5F5E5A' "
                 "dominant-baseline='central'>%s</text>" % (LAB_X, y + 16, lab))
        o.append("<rect x='%d' y='%d' width='%d' height='26' rx='4' "
                 "fill='%s'/>" % (BAR_X, y + 3, max(bw, 2), col))
        o.append("<text x='%d' y='%d' font-family='sans-serif' "
                 "font-size='13' fill='#2C2C2A' "
                 "dominant-baseline='central'>%s%s</text>"
                 % (BAR_X + 8 + max(bw, 2), y + 16, _num(val), unit))
        y += 46
    o.append("</svg>")
    return "".join(o)


def _num(v):
    if isinstance(v, float):
        return ("%.0f" % v) if abs(v) >= 10 else ("%.1f" % v)
    return str(v)


def dims_section(lang="zh"):
    """能力解离证据表。

    这是课题 #1 的核心——如果语义类维度全过、唯独定位失败，
    「能力解离」就成立，而不是「模型整体视觉能力差」。
    """
    C = CHART[lang]["fig5"]
    dims = []
    for fn, p in load_all():
        if p.get("group") == "dimensions" and p.get("dims"):
            dims = p["dims"]          # 取最新一次
    if not dims:
        return "", "n/a"
    n_pass = sum(1 for d in dims if d["passed"])
    n = len(dims)
    dm = CHART[lang].get("dim_map") or {}
    am = CHART[lang].get("ask_map") or {}
    show_ask = CHART[lang].get("show_ask", True)
    bars = []
    for d in dims:
        name = dm.get(d["dim"], d["dim"])
        ask = am.get(d["ask"], d["ask"])
        if lang == "zh":
            # 问法里带的「（答案 2）」对图无信息量，且会把标签撑过列宽
            for sep in ("（答案", "(答案"):
                if sep in ask:
                    ask = ask.split(sep)[0]
            ask = ask[:8]
        lab = (C[3] % (name, ask)) if show_ask else name
        bars.append((lab,
                     100 if d["passed"] else 0,
                     "#1D9E75" if d["passed"] else "#E24B4A"))
    # 定位维度用红色标出（来自其他 run 的实测）
    bars.insert(0, (C[2], 0, "#E24B4A"))
    c = svg_bars(C[0], bars, unit=C[4], note=C[1], figid="fig5", lang=lang)
    return c, "%d/%d" % (n_pass, n)


def wording_section(lang="zh"):
    """多措辞对照：确认结论不是「问法不对」造成的。"""
    C = CHART[lang]["fig6"]
    summ = []
    for fn, p in load_all():
        if p.get("group") == "wording" and p.get("summary"):
            summ = p["summary"]
    if not summ:
        return "", "n/a"
    rows = []
    for s in summ:
        rate = 100.0 * s["hit"] / max(s["n"], 1)
        rows.append((s["wording"], rate,
                     "#E24B4A" if rate >= 50 else "#EF9F27"))
    c = svg_bars(C[0], rows, unit=C[2], note=C[1],
                 figid="fig6", lang=lang)
    best = max(summ, key=lambda x: x["hit"] / max(x["n"], 1))
    # 只返回「plain 25%」这样的值 —— 前缀由模板提供，
    # 否则中英两边都会出现「最佳 最佳 …」/「Best wording variant best …」
    return c, "%s %.0f%%" % (best["wording"],
                             100.0 * best["hit"] / max(best["n"], 1))


def build_lang(lang):
    """生成某一语言下的全部指标、图表与表格片段。

    为什么按语言生成两遍：报告是**单文件双语**，而 SVG 的标题与行标签
    都是文本节点 —— 不生成两份，切到英文时图里仍是中文。
    代价是 HTML 体积翻倍（约 +30KB），换来零请求、离线可切。
    """
    C = CHART[lang]
    T = L[lang]
    samples = gather()
    # ⚠️ 图表也只用 grounding 类样本 —— 与 summary() 口径一致。
    # 混入 control_hint / invalid 会虚高命中率（实测混算 30% vs 真实 6%）。
    ground = [s for s in samples if s["kind"] == "grounding"]
    n = len(ground)
    s_hit = sum(1 for s in ground if s["strict_hit"])
    l_hit = sum(1 for s in ground if s["loose_hit"])
    loss = sum(1 for s in ground if s["format_loss"])
    miss = n - l_hit
    hit_rate = 100.0 * l_hit / max(n, 1)
    v, rate = verdict_of(loss, n, lang)

    # ---- 图 1：三种判定口径的命中数 ----
    f = C["fig1"]
    c1 = svg_bars(f[0] % n,
                  [(f[2][0], s_hit, COLORS["strict"]),
                   (f[2][1], l_hit, COLORS["ok"]),
                   (f[2][2], loss, COLORS["loss"]),
                   (f[2][3], miss, COLORS["miss"])],
                  unit=f[3], note=f[1], figid="fig1", lang=lang)

    # ---- 图 2：各组格式损失率 ----
    f = C["fig2"]
    groups = {}
    for s in samples:
        groups.setdefault(s["run"], []).append(s)
    grows = []
    for i, (fn, ss) in enumerate(sorted(groups.items())):
        a = 100.0 * sum(1 for x in ss if x["strict_hit"]) / len(ss)
        b = 100.0 * sum(1 for x in ss if x["loose_hit"]) / len(ss)
        grows.append((fn.replace(".json", "")[:20], b - a,
                      COLORS["pal"][i % len(COLORS["pal"])]))
    c2 = svg_bars(f[0], grows, unit=f[2], note=f[1], figid="fig2", lang=lang)

    # ---- 图 3：延迟（按 run 平均）----
    # 逐样本画会产生 57 根条、SVG 高近 3000px，插进文档要拉好几屏，
    # 且单次波动（预热、冷启动）会淹没真正的分布。故按 run 聚合。
    f = C["fig3"]
    lat = {}
    for s in samples:
        if s.get("elapsed"):
            lat.setdefault(s["run"], []).append(s["elapsed"])
    lrows = [(fn.replace(".json", "")[:20], sum(vv) / len(vv), COLORS["lat"])
             for fn, vv in sorted(lat.items())]
    c3 = svg_bars(f[0], lrows, unit=f[2], note=f[1], figid="fig3", lang=lang)
    _all = sorted(x for vv in lat.values() for x in vv)
    if _all:
        c3 += ("<p class='note'>%s</p>"
               % (f[3] % (_all[0], _all[len(_all) // 2], _all[-1])))

    # ---- 图 0：控制实验（A/B/C 三组）----
    #
    # ⚠️ 2026-10-05 修正：原实现按 **run** 分组，而 A/B/C 三组在同一个
    #    json 文件里，于是三组被平均成两根条（≈193px / ≈103px），
    #    把判决性的「B 组距真值 0px」「C 组距提示 1px」全抹平了 ——
    #    图上完全看不出 C 组证明了什么。必须按样本的 case 分组。
    f = C["fig0"]
    ctrl = [s for s in samples if s.get("d_hint") is not None]
    if ctrl:
        gg = {}
        for s in ctrl:
            cse = (s.get("case") or "?").split("_")[-1]   # ctrlA / ctrlB / ctrlC
            gg.setdefault(cse, []).append(s)
        pal2 = {"ctrlA": "#E24B4A", "ctrlB": "#1D9E75", "ctrlC": "#534AB7"}
        crows = []
        for cse, ss in sorted(gg.items()):
            lbl = cse.replace("ctrl", "")
            col = pal2.get(cse, "#888780")
            dt = [x["d_loose"] for x in ss if x["d_loose"] is not None]
            dh = [x["d_hint"] for x in ss if x["d_hint"] is not None]
            if dt:
                crows.append((f[2] % lbl, sum(dt) / len(dt), col))
            if dh:
                crows.append((f[3] % lbl, sum(dh) / len(dh), col))
        c0 = svg_bars(f[0], crows, unit=" px", note=f[1],
                      figid="fig0", lang=lang)
    else:
        c0 = ""

    # ---- 图 4：坐标离散度 ----
    f = C["fig4"]
    ux = next((s.get("unique_x") for s in samples
               if s.get("unique_x") is not None), None)
    uy = next((s.get("unique_y") for s in samples
               if s.get("unique_y") is not None), None)
    if ux:
        nd = max(s.get("n_valid") or 0 for s in samples) or len(samples)
        c4 = svg_bars(f[0],
                      [(f[2][0], ux, COLORS["miss"]),
                       (f[2][1], uy or 0, COLORS["lat"]),
                       (f[2][2], nd, COLORS["strict"])],
                      unit=f[3], note=f[1], figid="fig4", lang=lang)
    else:
        c4 = ""

    c5, dims_stat = dims_section(lang)
    c6, wording_stat = wording_section(lang)

    # ---- 原始输出表 ----
    rows_html = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
        "<td>%.0fs</td><td class='raw'>%s</td></tr>"
        % (s["label"], "%d,%d" % tuple(s["truth"]) if s.get("truth") else "-",
           T["kind"].get(s["kind"], s["kind"]),
           "✓" if s["strict_hit"] else "✗",
           "✓" if s["loose_hit"] else "✗",
           "⚠️" if s["format_loss"] else "-",
           s.get("elapsed") or 0,
           (s["raw"] or "").replace("<", "&lt;").replace("\n", " ")[:100])
        for s in samples)

    # ---- 被排除样本必须显式列出，不能悄悄丢掉 ----
    excl = [s for s in samples if s["kind"] != "grounding"]
    excl_html = ""
    if excl:
        items = []
        for k in ("control_hint", "invalid", "dimension"):
            ss = [s for s in excl if s["kind"] == k]
            if ss:
                items.append("<li>%s: <b>%d</b> — %s</li>"
                             % (T["kind"].get(k, k), len(ss),
                                T["excl_reason"].get(k, "")))
        excl_html = ("<div class='card'><div class='excl'><b>%s</b>%s"
                     "<ul>%s</ul><span class='note'>%s</span></div></div>"
                     % (T["excl_head"],
                        T["excl_of"] % (len(excl), len(samples)),
                        "".join(items),
                        T["excl_note"]
                        % (hit_rate,
                           100.0 * sum(1 for s in samples if s["loose_hit"])
                           / max(len(samples), 1))))

    kpi = "".join(
        "<div class='k'><div class='lab'>%s</div><div class='val'>%s</div></div>"
        % (lab, val)
        for lab, val in zip(T["kpi"],
                            [n, s_hit, l_hit, loss, miss, "%.0f%%" % rate]))

    cards = "".join("<div class='card'>%s</div>" % c
                    for c in (c0, c5, c6, c1, c4, c2, c3) if c)

    out = {"n": n, "cards": cards, "rows": rows_html,
           "th": "".join("<th>%s</th>" % h for h in T["th"]),
           "head": "<h1>%s</h1>\n<p class=\"sub\">%s<br>%s</p>"
                   % (T["h1"], T["meta"], T["meta2"]),
           "kpi": kpi,
           "verdict": "<div class=\"verdict\"><strong>%s: %s</strong><br>%s</div>"
                      % (T["verdict"], v, T["verdict_txt"]),
           "dissoc": "<div class=\"verdict2\"><strong>%s:</strong> %s</div>"
                     % (T["dissoc"],
                        T["dissoc_txt"] % (dims_stat, "%.0f%%" % hit_rate,
                                           wording_stat)),
           "excl": excl_html,
           "raw_h2": T["raw_h2"], "raw_sub": T["raw_sub"],
           # 供 charts() 里的 Markdown 副产物与自检使用
           "s_hit": s_hit, "l_hit": l_hit, "loss": loss, "miss": miss,
           "rate": rate, "hit_rate": hit_rate, "v": v,
           "dims": dims_stat, "wording": wording_stat}
    return out


def charts():
    bodies = {}
    for lang in LANGS:
        d = build_lang(lang)
        if lang == "zh" and not d["n"]:
            print("无数据，无法出图")
            return
        h = _BODY
        for k, v in (("{{HEAD}}", d["head"]), ("{{KPI}}", d["kpi"]),
                     ("{{VERDICT}}", d["verdict"]), ("{{EXCL}}", d["excl"]),
                     ("{{DISSOC}}", d["dissoc"]), ("{{CARDS}}", d["cards"]),
                     ("{{RAW_H2}}", d["raw_h2"]), ("{{RAW_SUB}}", d["raw_sub"]),
                     ("{{TH}}", d["th"]), ("{{ROWS}}", d["rows"])):
            h = h.replace(k, str(v))
        bodies[lang] = h

    html = _TEMPLATE.replace("{{BODY_ZH}}", bodies["zh"])
    html = html.replace("{{BODY_EN}}", bodies["en"])

    if not os.path.isdir(OUT):
        os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, "report.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print("HTML 报告（中英双语）: %s" % path)

    # Markdown 版保持中文（文档层面的中英切换由 README/FINDINGS 的
    # 独立副本承担，报告 md 只是体检用的副产物）
    zh = build_lang("zh")
    md = os.path.join(OUT, "report.md")
    with open(md, "w", encoding="utf-8") as f:
        f.write("# 能力探针实验结果\n\n")
        f.write("模型 `Ternary-Bonsai-2-27B-PTQ1`｜探针：纯色圆点定位，"
                "命中半径 40px｜采样锁定 `temperature=0`\n\n")
        f.write("| 指标 | 值 |\n|---|---|\n")
        f.write("| 样本数 | %d |\n" % zh["n"])
        f.write("| 严格口径命中 | %d |\n" % zh["s_hit"])
        f.write("| 宽松口径命中 | %d |\n" % zh["l_hit"])
        f.write("| **格式损失** | **%d (%.0f%%)** |\n" % (zh["loss"], zh["rate"]))
        f.write("| 真能力缺失 | %d |\n\n" % zh["miss"])
        f.write("**判读：%s**\n\n" % zh["v"])
        f.write("## 原始输出\n\n| 目标 | 真值 | 严格 | 宽松 | 格式损失 | 耗时 | 输出 |\n")
        f.write("|---|---|---|---|---|---|---|\n")
        for s in gather():
            t = ("%d,%d" % tuple(s["truth"])) if s.get("truth") else "-"
            raw = (s["raw"] or "").replace("\n", " ").replace("|", "\\|")
            f.write("| %s | %s | %s | %s | %s | %.0fs | `%s` |\n"
                    % (s["label"], t,
                       "✓" if s["strict_hit"] else "✗",
                       "✓" if s["loose_hit"] else "✗",
                       "⚠️" if s["format_loss"] else "-",
                       s.get("elapsed") or 0, raw[:90]))
    print("Markdown: %s" % md)


# 每语言一份的主体模板（用占位符替换，不用 % 格式化 —— 模板里有 CSS 的
# width:100% 之类写法，% 格式化会直接抛 unsupported format character）
_BODY = """{{HEAD}}

<div class="kpi">{{KPI}}</div>

{{VERDICT}}

{{EXCL}}

{{DISSOC}}

{{CARDS}}

<h2 style="font-size:15px;font-weight:500;margin-top:32px">{{RAW_H2}}</h2>
<p class="sub">{{RAW_SUB}}</p>
<table><thead><tr>{{TH}}</tr></thead><tbody>
{{ROWS}}
</tbody></table>
"""


_TEMPLATE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ternary-Quantized 27B Capability Probe · 能力探针实验结果</title>
<style>
 body{font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;
      max-width:780px;margin:0 auto;padding:32px 24px;line-height:1.7;
      color:#2C2C2A;background:#fff}
 h1{font-size:19px;font-weight:500;margin:0 0 6px}
 h2{font-size:15px;font-weight:500}
 .sub{color:#888780;font-size:13px;margin-bottom:28px}
 .card{border:1px solid rgba(0,0,0,.12);border-radius:12px;
       padding:18px 20px;margin:18px 0}
 .kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(104px,1fr));
      gap:12px;margin:16px 0}
 .k{background:#F6F5F2;border-radius:8px;padding:12px 14px}
 .k .lab{font-size:12px;color:#5F5E5A}
 .k .val{font-size:22px;font-weight:500;margin-top:2px}
 .verdict{border-left:3px solid #EF9F27;background:#FAEEDA;
          padding:14px 18px;border-radius:0 8px 8px 0;margin:18px 0;
          font-size:13px}
 table{border-collapse:collapse;width:100%;font-size:13px;margin:12px 0}
 th,td{text-align:left;padding:7px 10px;
       border-bottom:1px solid rgba(0,0,0,.07)}
 th{color:#5F5E5A;font-weight:500}
 code{background:#F1EFE8;padding:1px 5px;border-radius:4px;font-size:12px}
 .verdict2{border-left:3px solid #1D9E75;background:#E1F5EE;
          padding:14px 18px;border-radius:0 8px 8px 0;margin:18px 0;
          font-size:13px}
 .note{color:#888780;font-size:12px;margin:8px 0 0}
 .excl{background:#FCEBEB;border-left:3px solid #E24B4A;padding:12px 16px;
       border-radius:0 8px 8px 0;font-size:13px}
 .excl ul{margin:6px 0 4px;padding-left:20px}
 .excl li{margin:2px 0}
 .empty{color:#888780;font-size:13px}
 .raw{font-family:ui-monospace,Menlo,monospace;font-size:11.5px;
      color:#5F5E5A;word-break:break-all}
 /* ---- 语言切换：同一份 HTML 里两套内容，只显示当前语言 ---- */
 body[data-lang="zh"] .en,body[data-lang="en"] .zh{display:none}
 .langbar{display:flex;gap:6px;justify-content:flex-end;margin:0 0 18px}
 .langbar button{font:inherit;font-size:12px;line-height:1;padding:6px 14px;
   border-radius:14px;border:1px solid rgba(0,0,0,.15);background:#fff;
   color:#5F5E5A;cursor:pointer}
 .langbar button.on{background:#2C2C2A;color:#fff;border-color:#2C2C2A}
</style></head><body data-lang="zh">
<div class="langbar">
 <button id="btn-zh" type="button" onclick="setLang('zh')">中文</button>
 <button id="btn-en" type="button" onclick="setLang('en')">EN</button>
</div>
<div class="zh">{{BODY_ZH}}</div>
<div class="en">{{BODY_EN}}</div>
<script>
(function(){
  function setLang(l){
    document.body.dataset.lang = l;
    document.documentElement.lang = (l === 'zh') ? 'zh' : 'en';
    try { localStorage.setItem('probe_lang', l); } catch (e) {}
    var zh = document.getElementById('btn-zh'), en = document.getElementById('btn-en');
    if (zh) zh.className = (l === 'zh') ? 'on' : '';
    if (en) en.className = (l === 'en') ? 'on' : '';
  }
  window.setLang = setLang;
  var saved = null;
  try { saved = localStorage.getItem('probe_lang'); } catch (e) {}
  var nav = (navigator.language || 'zh').toLowerCase();
  // 默认跟随浏览器语言：中文环境看中文，其他看英文
  setLang(saved || (nav.indexOf('zh') === 0 ? 'zh' : 'en'));
})();
</script>
</body></html>"""


if __name__ == "__main__":
    c = sys.argv[1] if len(sys.argv) > 1 else "all"
    if c in ("summary", "all"):
        summary()
        print()
    if c in ("charts", "all"):
        charts()
