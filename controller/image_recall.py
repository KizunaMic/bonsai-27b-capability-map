# -*- coding: utf-8 -*-
"""
image_recall.py —— 找出"用户说的那张图"是索引里的哪一张

场景
----
早上 09:20 发了张菜单照片，聊了一整天（上下文被压缩掉），晚上问：
  「我早上吃了什么」  「那张菜单上一共几个馅」  「昨天那张图写了啥」

做法
----
**时间锚点 + 关键词双通道打分**，纯规则、零推理、毫秒级返回。

时间锚点（把"早上/昨天/前天"翻译成时间窗）
    刚刚/刚才/刚      -> 0 ~ 30 分钟
    今天/今晚/上午…   -> 今天 00:00 至今
    早上/早晨/清晨     -> 今天(或昨天) 05:00-11:00
    中午              -> 11:00-14:00
    下午              -> 14:00-18:00
    晚上/傍晚         -> 18:00-23:59
    昨天/昨日         -> 昨天 00:00-23:59
    前天              -> 前天
    上周/上周         -> 7~14 天前
    具体日期(M/D、昨天上午) -> 尽力解析

关键词通道
    从用户问题里抽出实词，与记录里的 said + summary 做重合度。

为什么先做纯规则而不是用 Laya
-----------------------------
Laya/Qwen 在中文意图分类上已实测一般（134 条 91%）。而这里是**结构化打分**
（时间距离 + 词重合），规则比模型更准更快，且零延迟零不确定性。
只有当候选图 >3 张、需要"选哪张"时，才值得让模型参与。
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import image_index

# ---------------- 时间锚点表 ----------------
# (正则, 说明) —— 用于识别用户话里的时间指向
TIME_MARKERS = [
    (r"刚才|刚刚|刚发|刚截|刚拍", "just_now"),
    (r"早上|早晨|清晨|一大早", "morning"),
    (r"中午|午时", "noon"),
    (r"下午|午后", "afternoon"),
    (r"晚上|傍晚|夜里|今晚", "evening"),
    (r"昨天|昨日", "yesterday"),
    (r"前天", "day_before"),
    (r"上周|上星期", "last_week"),
    (r"今天|今日|今晚|刚才那张", "today"),
    (r"那天|那次|以前|之前|上次", "any_past"),
]
COMPILED_MARKERS = [(re.compile(p), k) for p, k in TIME_MARKERS]

# 一天中的时段 -> 小时区间
DAYPART_HOURS = {
    "morning": (5, 11),
    "noon": (11, 14),
    "afternoon": (14, 18),
    "evening": (18, 23.99),
}

STOPWORDS = set(
    "我 你 他 她 它 的 了 是 吗 呢 吧 啊 啊 什么 怎么 哪个 哪些 那个 这个 些 有 没有 "
    "在 呢 和 与 跟 把 被 让 请 帮 我要 想知道 记得 告诉 现在 还 到底 又 再 一 那 就 都 "
    "什么 情况 时候 东西 样".split())


def extract_time_hint(text: str):
    """从用户问题里识别时间指向，返回 hint 字符串。"""
    t = (text or "").strip()
    for rx, key in COMPILED_MARKERS:
        if rx.search(t):
            return key
    # 具体日期：M月D日 / M-D / M/D
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?", t)
    if m:
        return "date:%s-%s" % (int(m.group(1)), int(m.group(2)))
    m = re.search(r"(?<!\d)(\d{1,2})[/-](\d{1,2})(?!\d)", t)
    if m and 1 <= int(m.group(1)) <= 12 and 1 <= int(m.group(2)) <= 31:
        return "date:%s-%s" % (int(m.group(1)), int(m.group(2)))
    return None


def extract_keywords(text: str):
    """粗提取中文关键词。

    中文没有空格分词，所以**不能按 split() 切**（会把整句当一个词，导致
    关键词与摘要永远匹配不上）。这里用 n-gram 做法：取所有 2-4 字的连续
    中文片段，再用停用词表过滤。这样「我早上吃了什么」能切出
    「早上/吃了/什么」这类可匹配片段。
    """
    t = re.sub(r"[，。！？、,.!?;:：；\s]+", "", (text or ""))
    if not t:
        return []
    grams = set()
    # 2~4 字 n-gram（中文常用词长在这个区间）
    for n in (2, 3, 4):
        for i in range(len(t) - n + 1):
            g = t[i:i + n]
            if g in STOPWORDS:
                continue
            if re.fullmatch(r"[\dWw]+", g):
                continue
            grams.add(g)
    # 英文/数字整词也保留
    for w in re.findall(r"[A-Za-z][A-Za-z0-9_]{1,20}", text or ""):
        if w.lower() not in STOPWORDS:
            grams.add(w)
    return sorted(grams)


def _time_score(rec_ts, hint, now):
    """按时间提示给分：越高越像。返回 0~1。"""
    if not hint:
        return 0.5           # 无时间线索 -> 中性分
    lt = time.localtime(rec_ts)
    day = lt.tm_mday
    hour = lt.tm_hour

    if hint == "just_now":
        d = now - rec_ts
        return 1.0 if d <= 1800 else (0.6 if d <= 6 * 3600 else 0.1)
    if hint == "today":
        return 1.0 if day == time.localtime(now).tm_mday else 0.05
    if hint == "yesterday":
        y = time.localtime(now - 86400)
        return 1.0 if day == y.tm_mday else 0.05
    if hint == "day_before":
        y = time.localtime(now - 2 * 86400)
        return 1.0 if day == y.tm_mday else 0.03
    if hint in DAYPART_HOURS:
        lo, hi = DAYPART_HOURS[hint]
        if day == time.localtime(now).tm_mday and lo <= hour < hi:
            return 1.0
        # 非今天但时段对（前一天同时段）
        y = time.localtime(now - 86400)
        if day == y.tm_mday and lo <= hour < hi:
            return 0.85
        return 0.15
    if hint == "last_week":
        d = (now - rec_ts) / 86400.0
        return 0.9 if 5 <= d <= 14 else 0.1
    if hint == "any_past":
        return 0.6
    if hint and hint.startswith("date:"):
        mm, dd = [int(x) for x in hint.split(":")[1].split("-")]
        return 1.0 if (day == dd and lt.tm_mon == mm) else 0.02
    return 0.5


def _kw_score(rec, keywords):
    """关键词重合度 0~1。

    注意 n-gram 会产生很多片段，直接用 hit/总数 会让长句天然占优
    （片段多、总分母大、随便都能命中几个）。这里改为**按片段长度加权**：
    长片段（如「早上吃了」）比短片段（如「吃了」）更能代表意图，权重更高；
    再除以"文本总长"做归一化，避免长句虚高。
    """
    if not keywords:
        return 0.5
    blob = "%s %s" % (rec.get("said", ""), rec.get("summary", ""))
    if not blob.strip():
        return 0.0
    hit_w = 0.0
    for k in keywords:
        if k in blob:
            hit_w += len(k) ** 1.5      # 长片段权重更高
    total_w = sum(len(k) ** 1.5 for k in keywords) or 1.0
    # 再用"命中片段覆盖了多少查询文本"做一次上限约束
    cov = min(1.0, hit_w / total_w)
    # 摘要/配文越短，越可能恰好被命中 -> 略微降权，避免短摘要被"白撞"
    return cov * (0.6 + 0.4 * min(1.0, len(blob) / 120.0))


def recall(text: str, limit: int = 3):
    """检索最可能的若干张图。

    返回 [{"rec":..., "score":..., "why":...}, ...]，按 score 降序。
    """
    now = time.time()
    hint = extract_time_hint(text)
    kws = extract_keywords(text)
    rows = image_index.load_recent()
    if not rows:
        return []

    scored = []
    for r in rows:
        ts = float(r.get("ts", 0))
        alive = os.path.exists(r.get("path", ""))
        ts_score = _time_score(ts, hint, now)
        kw_score = _kw_score(r, kws)
        # 时间权重高于关键词：用户说"早上"通常比"关键词碰巧重合"更有指向性。
        # 无时间线索时两者等价；有线索时用 0.7/0.3。
        if hint:
            score = 0.70 * ts_score + 0.30 * kw_score
        else:
            score = 0.45 * ts_score + 0.55 * kw_score
        if not alive:
            score *= 0.2       # 图没了，轻微降权但仍可作线索
        why = []
        if hint:
            why.append("时间=%s(%.2f)" % (hint, ts_score))
        if kws:
            why.append("关键词=%s(%.2f)" % ("/".join(kws[:3]), kw_score))
        if not alive:
            why.append("原图已删除")
        scored.append({"rec": r, "score": round(score, 4),
                       "alive": alive, "why": ", ".join(why)})

    scored.sort(key=lambda x: -x["score"])
    return scored[:limit]


def format_candidates(cands):
    """把候选列表格式化成可读文本，喂给模型做二次判断。"""
    if not cands:
        return ""
    lines = ["我按你的描述在历史图片里找到这些候选："]
    for i, c in enumerate(cands, 1):
        r = c["rec"]
        lines.append("%d) %s %s（%s）" % (i, r.get("date"), r.get("time"),
                                          r.get("summary", "")[:50] or "无摘要"))
        if r.get("said"):
            lines.append("   你当时说：%s" % r["said"][:50])
    return "\n".join(lines)


if __name__ == "__main__":
    s = image_index.stats()
    print("索引: %d 条（有摘要 %d）" % (s["total"], s["with_summary"]))
    print("=" * 60)
    for q in sys.argv[1:] or ["我早上吃了什么", "昨天那张图写了啥", "那张菜单"]:
        print("\n【问】%s" % q)
        print("  时间线索:", extract_time_hint(q), "| 关键词:", extract_keywords(q))
        cands = recall(q)
        if not cands:
            print("  → 没有找到任何历史图片")
        for c in cands:
            r = c["rec"]
            print("  → %s %s 得分%.2f | %s"
                  % (r["date"], r["time"], c["score"],
                     (r.get("summary") or r.get("said") or "无摘要")[:50]))
