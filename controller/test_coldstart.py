#!/usr/bin/env python3
# test_coldstart.py - 测冷/热 prefill（首 token 延迟）
# 直连 :18200，验证 KVMem 在长前缀下的首 token 延迟，以及 NVMe 层是否让第二次明显变快。
import json, time, urllib.request

EP = "http://127.0.0.1:18200/v1/chat/completions"
prefix = ("请记住这句话：本地大模型部署要注意显存带宽，它是生成速度的物理上限，"
          "RTX 4060 8GB 只有 272GB/s，所以 27B 三元模型只能跑到约 22.6 t/s。"
          "上下文越长，prefill 越慢，因此要做上下文减肥，把历史压成摘要、滚动窗口、产物外移。"
          "这样可以把发给模型的 token 从几万降到几千，首 token 延迟随之坍缩。" * 120)
print("前缀长度约 %d 字符" % len(prefix))


def first_token():
    body = {"model": "Ternary-Bonsai-2-27B-PTQ1_0.gguf",
            "messages": [{"role": "system", "content": prefix},
                         {"role": "user", "content": "只回复两个字：好的。"}],
            "stream": True, "max_tokens": 8}
    data = json.dumps(body).encode()
    req = urllib.request.Request(EP, data=data, headers={"Content-Type": "application/json"})
    t0 = time.time(); first = None
    with urllib.request.urlopen(req, timeout=300) as r:
        for raw in r:
            line = raw.decode().strip()
            if not line.startswith("data:"):
                continue
            p = line[5:].strip()
            if p == "[DONE]":
                break
            try:
                o = json.loads(p)
            except Exception:
                continue
            d = o.get("choices", [{}])[0].get("delta", {})
            if "content" in d:
                first = time.time() - t0
                break
    return first


for i in range(2):
    t = first_token()
    tag = "(冷启动)" if i == 0 else "(热：若 NVMe/前缀缓存生效应明显变快)"
    print("run %d  首 token 延迟: %.2fs  %s" % (i + 1, t, tag))
