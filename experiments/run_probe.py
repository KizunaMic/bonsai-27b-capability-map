"""
run_probe.py —— 能力探针执行器（课题 #1 #2 #6 #7）

用法:
    python run_probe.py <实验组> [选项]

实验组:
    fmt        C3 格式审计（最高优先级，可能推翻已有结论）
    kv         G5 核心对照：KV 在 RAM vs 显存
    res        G1–G4 分辨率梯度
    all        全部（不含长会话）

⚠️ 硬约束（见《04-卡死与循环的防治.md》§9 禁令①）：
   不使用 -n / 客户端 max_tokens 硬截思考。
   思考长度由引擎侧 --reasoning-budget 控制。
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_core as pc

ENGINE = "http://127.0.0.1:18200"
PROXY = "http://127.0.0.1:18201"
MODEL = "Ternary-Bonsai-2-27B-PTQ1_0.gguf"

HIT_RADIUS = 40          # 命中半径（px）
TIMEOUT = 300            # 单次推理超时（视觉推理 40–70s，留足余量）


def post(url, payload, timeout=TIMEOUT):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        obj = json.loads(r.read().decode("utf-8"))
    return obj, time.time() - t0


def ask_text(prompt, system=None, think=False):
    """纯文本询问。返回 (内容, 耗时, usage)。"""
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    payload = {
        "model": MODEL,
        "messages": msgs,
        "stream": False,
        "temperature": 0,          # 采样锁死，实验可复现
        "top_p": 1,
        "presence_penalty": 0,
    }
    # 思考模式通过 chat_template_kwargs 走引擎侧预算，不做客户端截断
    payload["chat_template_kwargs"] = {"enable_thinking": bool(think)}
    obj, el = post(PROXY + "/v1/chat/completions", payload)
    ch = (obj.get("choices") or [{}])[0]
    return (ch.get("message", {}).get("content") or ""), el, \
        obj.get("usage") or {}


def ask_vision(png, prompt, think=False):
    """多模态询问。png 为 bytes。返回 (内容, 耗时, usage)。"""
    b64 = base64.b64encode(png).decode("ascii")
    payload = {
        "model": MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64," + b64}},
            ],
        }],
        "stream": False,
        "temperature": 0,
        "top_p": 1,
        "presence_penalty": 0,
        "chat_template_kwargs": {"enable_thinking": bool(think)},
    }
    obj, el = post(PROXY + "/v1/chat/completions", payload)
    ch = (obj.get("choices") or [{}])[0]
    return (ch.get("message", {}).get("content") or ""), el, \
        obj.get("usage") or {}


# 提问模板：措辞固定，避免 prompt 本身成为变量
PROMPT_DOT = (
    "This image contains solid circles on a plain white background. "
    "Locate the {color} circle labeled {label}. "
    "Output ONLY its center coordinates as x,y. "
    "Image size is {w}x{h}. Do not explain."
)
PROMPT_GRID = (
    "This image is a {rows}x{cols} grid where each cell is labeled with its "
    "row,column number in the top-left corner. Find the cell labeled "
    "\"{r},{c}\" and output ONLY the center coordinates of that cell as x,y. "
    "Image size is {w}x{h}. Do not explain."
)
COLOR_WORD = {"A": "red", "B": "blue", "C": "green", "D": "red"}


def run_locate_case(case_name, width, height, label, group, think=False):
    """跑一个定位探针：4 个目标点各问一次。"""
    png, truth = pc.build_dot_image(width, height)
    pc.save_case(case_name, png, truth, width, height)
    out = []
    for lab in ("A", "B", "C", "D"):
        prompt = PROMPT_DOT.format(color=COLOR_WORD[lab], label=lab,
                                   w=width, h=height)
        try:
            txt, el, usage = ask_vision(png, prompt, think=think)
        except (urllib.error.URLError, OSError) as e:
            out.append({"label": lab, "error": repr(e)[:200]})
            print("    %s ERROR %s" % (lab, repr(e)[:80]))
            continue
        a = pc.audit(txt, truth[lab], width, height, HIT_RADIUS)
        a["label"] = lab
        a["elapsed"] = round(el, 1)
        a["usage"] = usage
        a["truth"] = list(truth[lab])
        out.append(a)
        print("    %s 真值(%d,%d) 严格%s 宽松%s 距%.0fpx %s %.0fs"
              % (lab, truth[lab][0], truth[lab][1],
                 "✓" if a["strict_hit"] else "✗",
                 "✓" if a["loose_hit"] else "✗",
                 a["d_loose"] if a["d_loose"] is not None else -1,
                 "格式损失" if a["format_loss"] else "", el))
        time.sleep(0.5)
    return {
        "group": group, "case": case_name, "w": width, "h": height,
        "think": think, "hit_radius": HIT_RADIUS, "results": out,
    }


def run_grid_case(width=1024, height=683, think=False):
    """10×10 网格：问 7行3列。"""
    png, truth = pc.build_grid_image(width, height)
    pc.save_case("grid_10x10_%dx%d" % (width, height), png, truth,
                 width, height)
    tx, ty = truth["7,3"]
    prompt = PROMPT_GRID.format(rows=10, cols=10, r=7, c=3,
                               w=width, h=height)
    txt, el, usage = ask_vision(png, prompt, think=think)
    a = pc.audit(txt, (tx, ty), width, height, HIT_RADIUS)
    print("    网格 7,3 真值(%d,%d) 严格%s 宽松%s 距%.0fpx %s %.0fs"
          % (tx, ty, "✓" if a["strict_hit"] else "✗",
             "✓" if a["loose_hit"] else "✗",
             a["d_loose"] if a["d_loose"] is not None else -1,
             "格式损失" if a["format_loss"] else "", el))
    return {
        "group": "res", "case": "grid_10x10_%dx%d" % (width, height),
        "w": width, "h": height, "think": think, "results": [a],
        "elapsed": round(el, 1), "usage": usage, "truth": [tx, ty],
    }


# ==================== 实验组 ====================

def exp_fmt(think=False):
    """C3 格式审计 —— 最高优先级。

    只跑一个分辨率，4 个点。目的不是测准确率，
    而是测「严格失败但语义正确」的比例。
    """
    print("\n=== C3 格式审计 ===")
    print("目的：判定 grounding 失败是能力问题还是格式问题")
    r = run_locate_case("fmt_audit_1024x683", 1024, 683, None,
                        "fmt", think=think)
    ok = [x for x in r["results"] if not x.get("error")]
    if not ok:
        return r
    loss = sum(1 for x in ok if x["format_loss"])
    s_hit = sum(1 for x in ok if x["strict_hit"])
    l_hit = sum(1 for x in ok if x["loose_hit"])
    print("\n  严格命中 %d/%d | 宽松命中 %d/%d | 格式损失 %d/%d (%.0f%%)"
          % (s_hit, len(ok), l_hit, len(ok), loss, len(ok),
             100.0 * loss / len(ok)))
    print("  判读：格式损失 >30%% → 已有「grounding 能力缺失」结论需修正")
    return r


def exp_res(think=False):
    """G1–G4 分辨率梯度 + 网格。"""
    print("\n=== G1–G4 分辨率梯度 ===")
    allr = []
    for w, h in [(1024, 683), (1536, 1024), (2560, 1600)]:
        print("\n  [%dx%d]" % (w, h))
        allr.append(run_locate_case("dots_%dx%d" % (w, h), w, h, None,
                                    "res", think=think))
    print("\n  [网格对照]")
    allr.append(run_grid_case(1024, 683, think=think))
    return allr


def exp_kv(think=False):
    """G5 核心对照。**需要重启引擎切换 --kvmem-cpu-gb**，所以这里只跑当前配置。"""
    print("\n=== G5 KV 位置对照（当前配置）===")
    r = run_locate_case("kv_current_%d" % int(time.time()), 1024, 683,
                        None, "kv", think=think)
    ok = [x for x in r["results"] if not x.get("error")]
    if ok:
        print("\n  严格命中 %d/%d | 格式损失 %d/%d"
              % (sum(1 for x in ok if x["strict_hit"]), len(ok),
                 sum(1 for x in ok if x["format_loss"]), len(ok)))
    print("  提示：切到 --kvmem-cpu-gb 0 后重跑，run_id 会带时间戳可区分")
    return r


def exp_think(think=True):
    """思考模式对照 —— 延迟账本（课题 #3）的核心数据。"""
    print("\n=== 思考模式对照 ===")
    r = run_locate_case("think_on_%d" % int(time.time()), 1024, 683,
                        None, "think", think=think)
    return r


GROUPS = {"fmt": exp_fmt, "res": exp_res, "kv": exp_kv, "think": exp_think}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("group", choices=list(GROUPS.keys()) + ["all"])
    ap.add_argument("--think", action="store_true", help="开启思考模式")
    ap.add_argument("--tag", default="", help="自定义 run_id 前缀")
    a = ap.parse_args()

    # 先探活，避免跑一半才发现服务没起
    try:
        with urllib.request.urlopen(ENGINE + "/health", timeout=8) as r:
            h = r.read().decode("utf-8", "replace")
        print("引擎: %s" % h[:160])
    except Exception as e:
        print("✗ 引擎不可用 (%s)" % repr(e)[:120])
        print("  请先双击 start_kvmem_all.bat 启动引擎 + 代理")
        return 1

    stamp = time.strftime("%Y%m%d_%H%M%S")
    results = []
    names = list(GROUPS) if a.group == "all" else [a.group]
    for n in names:
        results.append(GROUPS[n](think=a.think))
        time.sleep(1)

    prefix = a.tag or a.group
    run_id = "%s_%s" % (prefix, stamp)
    path = pc.save_run(run_id, {
        "group": a.group, "think": a.think, "model": MODEL,
        "hit_radius": HIT_RADIUS, "results": results,
        "notes": "温度锁 0；思考由引擎侧 budget 控制，未做客户端截断",
    })
    print("\n已保存: %s" % os.path.basename(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
