#!/usr/bin/env python3
# tool_probe.py - 直连引擎(:18200)验证"模型能否消费 tool 结果"
# 不经过 Chatbox / 代理，用最小、干净、规范格式，确认 computer use 的前置能力。
# 运行: 在 Windows 机器上执行  python tool_probe.py   （引擎需已在 :18200 运行）
import json, sys, time, urllib.request, urllib.error

ENGINE = "http://127.0.0.1:18200/v1/chat/completions"
MODEL = "Ternary-Bonsai-2-27B-PTQ1_0.gguf"
TIMEOUT = 600


def chat(messages, tools=None, stream=False, max_tokens=2048):
    body = {"model": MODEL, "messages": messages, "stream": stream,
            "max_tokens": max_tokens}
    if tools:
        body["tools"] = tools
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(ENGINE, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            if stream:
                content = reasoning = ""
                tool_calls = []
                for raw in resp:
                    line = raw.decode("utf-8").strip()
                    if not line.startswith("data:"):
                        continue
                    p = line[5:].strip()
                    if not p or p == "[DONE]":
                        continue
                    try:
                        o = json.loads(p)
                    except Exception:
                        continue
                    ch = (o.get("choices") or [{}])[0]
                    d = ch.get("delta", {}) or {}
                    if d.get("reasoning_content"):
                        reasoning += d["reasoning_content"]
                    if d.get("content"):
                        content += d["content"]
                    if d.get("tool_calls"):
                        tool_calls.append(d["tool_calls"])
                return {"content": content, "reasoning": reasoning,
                        "tool_calls": tool_calls}
            obj = json.loads(resp.read().decode("utf-8"))
            msg = obj["choices"][0]["message"]
            return {"content": msg.get("content", ""),
                    "reasoning": msg.get("reasoning_content", ""),
                    "tool_calls": msg.get("tool_calls"),
                    "finish": obj["choices"][0].get("finish_reason"),
                    "usage": obj.get("usage")}
    except urllib.error.URLError as e:
        print("❌ 无法连接引擎 %s: %s" % (ENGINE, e))
        print("   请确认引擎(:18200)正在运行，且本脚本在你的 Windows 机器上执行（不是远程沙箱）。")
        sys.exit(2)


CALC = [{"type": "function", "function": {
    "name": "calculator",
    "description": "计算两个整数相乘",
    "parameters": {"type": "object",
                   "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                   "required": ["a", "b"]}}}]

EXPECT = "56088"  # 123 * 456

print("=" * 64)
print("TEST A: 注入 assistant tool_call + tool 结果, 看模型是否消费")
print("=" * 64)
msgs_A = [
    {"role": "user", "content": "用 calculator 算 123*456，最后只给数字结果"},
    {"role": "assistant", "content": "",
     "tool_calls": [{"id": "call_001", "type": "function",
                     "function": {"name": "calculator",
                                  "arguments": json.dumps({"a": 123, "b": 456})}}]},
    {"role": "tool", "tool_call_id": "call_001", "content": EXPECT},
    {"role": "user", "content": "结果是？直接告诉我那个数字。"},
]
t0 = time.time()
rA = chat(msgs_A, tools=CALC)
print("耗时 %.1fs" % (time.time() - t0))
print("content   :", repr(rA.get("content")))
print("reasoning :", repr((rA.get("reasoning") or "")[:160]))
print("tool_calls:", rA.get("tool_calls"))
print("finish    :", rA.get("finish"))
print("usage     :", rA.get("usage"))
verdict_A = EXPECT in (rA.get("content") or "")
print(">> 判定 A:", "PASS ✅ 模型识别并使用了工具结果 56088"
      if verdict_A else "FAIL/UNCLEAR ❓ 回答里没出现 56088")

print()
print("=" * 64)
print("TEST B: 完整循环 - 让模型自己发 tool_calls, 再喂回结果")
print("=" * 64)
msgs_B = [{"role": "user", "content": "用 calculator 算 123*456，最后只给数字"}]
rB1 = chat(msgs_B, tools=CALC)
print("Round1 finish:", rB1.get("finish"), "| tool_calls:", rB1.get("tool_calls"))
if rB1.get("tool_calls"):
    tc = rB1["tool_calls"][0]
    tc_id = tc.get("id") or "call_auto"
    assistant_msg = {"role": "assistant",
                     "content": rB1.get("content") or "",
                     "tool_calls": rB1["tool_calls"]}
    tool_msg = {"role": "tool", "tool_call_id": tc_id, "content": EXPECT}
    msgs_B2 = msgs_B + [assistant_msg, tool_msg,
                        {"role": "user", "content": "结果是？"}]
    t1 = time.time()
    rB2 = chat(msgs_B2, tools=CALC)
    print("Round2 耗时 %.1fs" % (time.time() - t1))
    print("Round2 content:", repr(rB2.get("content")))
    print("Round2 tool_calls:", rB2.get("tool_calls"))
    print("Round2 finish:", rB2.get("finish"))
    verdict_B = EXPECT in (rB2.get("content") or "")
    print(">> 判定 B:", "PASS ✅ 完整工具循环跑通"
          if verdict_B else "FAIL/UNCLEAR ❓ 回合并未基于 56088 作答")
else:
    verdict_B = None
    print(">> 判定 B: 模型未发出 tool_calls（可能不支持/未触发），无法测完整循环")

print()
print("=" * 64)
print("总结")
print("=" * 64)
if verdict_A:
    print("引擎【能消费工具结果】。computer use 的'结果喂回'这一关通过；")
    print("之前 Chatbox 联网失败是 Chatbox/上下文问题，不是模型缺陷。")
elif verdict_B:
    print("直接注入未识别，但完整循环(模型自产调用)可用——需进一步排查注入格式。")
else:
    print("引擎【未能识别工具结果】。落地 computer use 前必须先解决此问题")
    print("(检查引擎 chat template 对 tool 角色的支持，或换/重训 GGUF)。")
