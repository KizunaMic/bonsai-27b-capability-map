# -*- coding: utf-8 -*-
"""
qwen_bench.py —— Qwen3.5-0.8B 意图分流 134 条基准测试

方法：logits 零样本分类（只前向 1 个 token，取 A/B/C/D 的 logits  softmax 得概率）
对比对象：Laya multilingual 322M（原始准确率 43%）
复用测试集：bench_cases.py（与 Laya 完全相同的 134 条，保证横向可比）
"""
import json
import os
import sys
import time

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ["HF_HUB_OFFLINE"] = "1"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import torch
from transformers import AutoTokenizer, AutoModelForCausalLM

from bench_cases import TESTS, SENSITIVE_KEYWORDS
import qwen_router as QR

MODEL_DIR = r"E:/ninfer-kvmem/qwen/model"


def main():
    t0 = time.time()
    # 走 router 的真实加载路径（含前缀 KV 预计算），测出的速度即线上速度
    r = QR.get_router()
    if not r._ensure_loaded():
        print("加载失败:", r.last_error)
        return
    model = r.model
    print("模型加载: %.1fs  (Qwen3.5-0.8B, %.0fM, CPU fp32, 前缀KV %d token 已缓存)"
          % (time.time() - t0,
             sum(p.numel() for p in model.parameters()) / 1e6,
             getattr(r, "prefix_len", 0)))
    print("测试样本: %d 条" % len(TESTS))
    print("=" * 78)

    rows = []
    t_start = time.time()
    for text, expect, tag in TESTS:
        s = time.time()
        res = r.route(text)
        dt = (time.time() - s) * 1000
        rows.append({"text": text, "expect": expect, "tag": tag,
                     "raw": res.get("intent"), "conf": res.get("confidence", 0.0),
                     "probs": res.get("probs", {}), "final": res.get("intent"),
                     "abstain": res.get("abstain", True),
                     "via": res.get("via", "?"), "kw": None, "ms": dt})

    total = time.time() - t_start

    def pct(n, d):
        return (n * 100.0 / d) if d else 0.0

    raw_ok = sum(1 for r in rows if r["raw"] == r["expect"])
    decided = [r for r in rows if not r["abstain"]]
    dec_ok = sum(1 for r in decided if r["final"] == r["expect"])
    danger = [r for r in rows if r["expect"] == "sensitive" and r["final"] != "sensitive"]
    fp = [r for r in rows if r["expect"] == "chat" and r["final"] == "sensitive"]

    print("\n【总体】(Laya 对比：原始 43% / 弃权 57% / 危险漏判 3 / 误伤 8)")
    print("  原始准确率   (全量)      : %d/%d = %.0f%%" % (raw_ok, len(rows), pct(raw_ok, len(rows))))
    print("  决策准确率   (排除弃权)  : %d/%d = %.0f%%" % (dec_ok, len(decided), pct(dec_ok, len(decided))))
    print("  弃权率                   : %d/%d = %.0f%%" % (len(rows) - len(decided), len(rows), pct(len(rows) - len(decided), len(rows))))
    print("  ⚠危险漏判(sensitive 漏过): %d 条" % len(danger))
    print("  ⚠关键词误伤(chat->sens)  : %d 条" % len(fp))
    print("  响应速度 : 平均 %.0fms  最快 %.0fms  最慢 %.0fms  (Laya 103ms)"
          % (sum(r["ms"] for r in rows) / len(rows), min(r["ms"] for r in rows), max(r["ms"] for r in rows)))
    print("  总耗时   : %.1fs" % total)

    print("\n【按期望类别】")
    for cls in ["chat", "control_pc", "query_status", "sensitive"]:
        g = [r for r in rows if r["expect"] == cls]
        ok = sum(1 for r in g if r["final"] == cls)
        rawc = sum(1 for r in g if r["raw"] == cls)
        dk = [r for r in g if not r["abstain"]]
        dok = sum(1 for r in dk if r["final"] == cls)
        print("  %-13s %2d条 | 原始 %2d (%.0f%%) | 最终 %2d (%.0f%%) | 决策 %2d 其中对 %2d"
              % (cls, len(g), rawc, pct(rawc, len(g)), ok, pct(ok, len(g)), len(dk), dok))

    print("\n【按细分标签】")
    tags = {}
    for r in rows:
        tags.setdefault(r["tag"], []).append(r)
    for tag in sorted(tags):
        g = tags[tag]
        ok = sum(1 for r in g if r["final"] == r["expect"])
        print("  %-10s %2d条 | 命中 %2d (%.0f%%)" % (tag, len(g), ok, pct(ok, len(g))))

    bad = [r for r in rows if r["final"] != r["expect"]]
    print("\n【错判明细】%d 条" % len(bad))
    for r in bad:
        kind = "误伤" if (r["expect"] == "chat" and r["final"] == "sensitive") else \
               ("漏判" if r["abstain"] else "错判")
        pr = {k: round(v, 2) for k, v in (r["probs"] or {}).items()}
        print("  [%s][%s] %-26s 期望=%-12s 得=%-11s conf=%.2f via=%s %s"
              % (kind, r["tag"], r["text"][:24], r["expect"], r["final"] or "弃权", r["conf"], r["via"], pr))

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_qwen_bench_result.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"summary": {"n": len(rows), "raw_ok": raw_ok, "dec_ok": dec_ok,
                               "decided": len(decided), "danger": len(danger), "fp": len(fp),
                               "avg_ms": sum(r["ms"] for r in rows) / len(rows)}, "rows": rows},
                  f, ensure_ascii=False, indent=1)
    print("\n明细:", out)


if __name__ == "__main__":
    main()
