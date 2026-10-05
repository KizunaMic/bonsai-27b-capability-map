# -*- coding: utf-8 -*-
"""
image_index.py —— 跨会话图片记忆（2026-10-04）

解决什么问题
------------
记忆库（long_term_memory.json）只存**文本事实**，图片只在内存留 15 分钟。
所以隔天/隔几小时问「我早上吃的那张菜单上有什么」必然答不上。

怎么解决
--------
**存图时顺手存一句「图内文字摘要」**——那一次视觉推理本来就要跑，
多要一句摘要的边际成本≈0。有了摘要，绝大多数追问**不必重跑视觉**。

两级应答：
  ① 摘要能答（「我早上吃了什么」「一共几个馅」）→ 读索引秒答，约 1-2 秒
  ② 摘要答不了（「哪个馅我没吃」——图上有但没说）→ 才重跑视觉，约 40 秒

数据格式：image_index.jsonl，每行一条
  {"ts": 1759507200.0, "time": "09:20", "date": "2026-10-04",
   "path": "E:/ninfer-kvmem/shots/img_092000.jpg",
   "source": "user_sent" | "screen_capture",
   "said": "我吃的包子",              # 用户当时配文（可能为空）
   "summary": "图内文字摘要",           # 模型看图时顺手产出
   "bytes": 48520}

设计取舍
--------
- **jsonl 而非 json**：追加写入快、损坏不影响其他行、便于增量读。
- **摘要限长 200 字**：索引要小；太长反而降低关键词命中率。
- **保留原图**：摘要答不了时要用原图重跑，所以原图不能删。
- **不主动清理**：图片很小（压缩后 ~50KB），按每天几张算一年也才几十 MB。
"""
import json
import os
import time

BASE = os.path.dirname(os.path.abspath(__file__))
MEM_DIR = os.path.join(BASE, "memory")
INDEX = os.path.join(MEM_DIR, "image_index.jsonl")
SHOTS = os.path.abspath(os.path.join(BASE, "..", "shots"))

# 原图归档目录：**独立于 shots/**。
# 为什么必须分开：shots/ 是"临时截图工作区"（feishu_*.png、_exp/），
# 任何人清理垃圾时第一个想到的就是它。而 image_index.jsonl 里存的 path
# 指向原图，shots/ 一旦被清，索引就变成一堆死路径，跨会话回忆直接失效
# （recall 里 alive=False 只降权到 0.2，等于半失效）。
# 放进 memory/archive/YYYY-MM-DD/ 后，语义上就是"要留的东西"，
# 和 memory/long_term_memory.json 同级，属于长期记忆的一部分。
ARCHIVE_ROOT = os.path.join(MEM_DIR, "archive")

MAX_SUMMARY_CHARS = 200
MAX_RECENT = 500      # 检索时最多看最近多少条（控制读取量）


def _ensure_dir():
    if not os.path.isdir(MEM_DIR):
        os.makedirs(MEM_DIR, exist_ok=True)


def archive_path(prefix: str = "img", ext: str = ".jpg", when: float = None) -> str:
    """在归档区按日期分目录生成一个不存在的文件路径。

    目录形如 memory/archive/2026-10-04/img_183045.jpg
    按天分目录是为了避免单目录堆几千个文件（Windows 上大目录会明显变慢）。
    """
    ts = when if when is not None else time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(ts))
    d = os.path.join(ARCHIVE_ROOT, day)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "%s_%s%s" % (prefix, time.strftime("%H%M%S", time.localtime(ts)), ext))


def add(path: str, source: str, said: str = "", summary: str = ""):
    """把一张图写进索引。失败不影响主流程（返回 False 即可）。"""
    try:
        _ensure_dir()
        if not path or not os.path.exists(path):
            return False
        ts = time.time()
        rec = {
            "ts": ts,
            "time": time.strftime("%H:%M", time.localtime(ts)),
            "date": time.strftime("%Y-%m-%d", time.localtime(ts)),
            "path": os.path.abspath(path),
            "source": source,                     # user_sent / screen_capture
            "said": (said or "")[:120],
            "summary": (summary or "").strip()[:MAX_SUMMARY_CHARS],
            "bytes": os.path.getsize(path),
        }
        with open(INDEX, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def load_recent(n: int = MAX_RECENT):
    """读最近 n 条（倒序返回，最新在前）。容忍损坏行。"""
    if not os.path.exists(INDEX):
        return []
    out = []
    try:
        with open(INDEX, encoding="utf-8") as f:
            lines = f.readlines()
        for line in reversed(lines[-n:]):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue          # 跳过损坏行，不影响其他
    except Exception:
        return []
    return out


def update_summary(target_ts: float, summary: str):
    """给某条记录补写摘要（视觉推理完成后调用）。"""
    try:
        if not os.path.exists(INDEX):
            return False
        with open(INDEX, encoding="utf-8") as f:
            lines = f.readlines()
        hit = False
        for i, line in enumerate(lines):
            line_s = line.strip()
            if not line_s:
                continue
            try:
                rec = json.loads(line_s)
            except Exception:
                continue
            # ts 有 0.001 误差，做容差匹配
            if abs(float(rec.get("ts", 0)) - target_ts) < 2.0:
                rec["summary"] = (summary or "").strip()[:MAX_SUMMARY_CHARS]
                lines[i] = json.dumps(rec, ensure_ascii=False) + "\n"
                hit = True
                break
        if hit:
            with open(INDEX, "w", encoding="utf-8") as f:
                f.writelines(lines)
        return hit
    except Exception:
        return False


def stats():
    rows = load_recent(MAX_RECENT)
    with_sum = sum(1 for r in rows if r.get("summary"))
    return {
        "total": len(rows),
        "with_summary": with_sum,
        "index_path": INDEX,
        "shots_dir": SHOTS,
    }


def health():
    """跨会话图片记忆的**体检报告**。

    这个函数存在的理由：之前"跨会话召回"只在同一进程内验证过，
    而 recall 对死路径只降权不报错，**看起来能用，实际半失效**，
    从外面完全看不出来（我就是因为这个误判过一次）。
    所以必须有一个能主动告诉你"现在到底行不行"的入口。

    返回：
      total          索引总条数
      with_summary   其中有摘要的条数（没有摘要的只能靠原图重跑，慢 40 秒）
      alive          原图文件确实还在的条数  ← 关键指标
      dead           原图丢失的条数（索引变死路径的数量）
      alive_bytes    还占着多少磁盘
      span           索引覆盖的时间范围
      last           最近一条记录（date/time/summary 摘要）
    """
    rows = load_recent(MAX_RECENT)
    alive_rows = [r for r in rows
                  if r.get("path") and os.path.exists(r["path"])]
    dead = len(rows) - len(alive_rows)
    total_bytes = 0
    for r in alive_rows:
        try:
            total_bytes += os.path.getsize(r["path"])
        except OSError:
            pass
    out = {
        "total": len(rows),
        "with_summary": sum(1 for r in rows if r.get("summary")),
        "alive": len(alive_rows),
        "dead": dead,
        "alive_bytes": total_bytes,
        "alive_mb": round(total_bytes / 1048576.0, 2),
        "index_path": INDEX,
        "archive_root": ARCHIVE_ROOT,
        "ok": bool(alive_rows),
    }
    if rows:
        out["span"] = "%s %s ~ %s %s" % (
            rows[-1].get("date", "?"), rows[-1].get("time", "?"),
            rows[0].get("date", "?"), rows[0].get("time", "?"))
        last = rows[0]
        out["last"] = {
            "date": last.get("date"), "time": last.get("time"),
            "source": last.get("source"),
            "summary": (last.get("summary") or "")[:60],
        }
        # 按来源分布——用来确认"截图到底有没有进索引"
        by_src = {}
        for r in rows:
            k = r.get("source", "?")
            by_src[k] = by_src.get(k, 0) + 1
        out["by_source"] = by_src
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "add" and len(sys.argv) >= 3:
        ok = add(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "user_sent",
                 sys.argv[4] if len(sys.argv) > 4 else "",
                 sys.argv[5] if len(sys.argv) > 5 else "")
        print("已记录" if ok else "记录失败")
    elif len(sys.argv) > 1 and sys.argv[1] == "health":
        h = health()
        print("图片记忆体检")
        print("  索引条数   : %d" % h["total"])
        print("  原图还在   : %d   <-- 关键指标" % h["alive"])
        print("  原图丢失   : %d   <-- 非 0 说明索引已半失效" % h["dead"])
        print("  有摘要     : %d" % h["with_summary"])
        print("  占用空间   : %.2f MB" % h["alive_mb"])
        if "span" in h:
            print("  覆盖时间   : %s" % h["span"])
            print("  来源分布   : %s" % h.get("by_source"))
        print("  结论       : %s" % ("可用" if h["ok"] else "不可用（无存活原图）"))
    else:
        s = stats()
        print("索引条数: %d（其中有摘要 %d）" % (s["total"], s["with_summary"]))
        print("索引文件:", s["index_path"])
        for r in load_recent(10):
            print("  %s %s [%s] %s | said=%s | %s"
                  % (r["date"], r["time"], r["source"], os.path.basename(r["path"]),
                     r.get("said", "")[:20], r.get("summary", "")[:40]))
