#!/usr/bin/env python3
# proxy_server.py - OpenAI 兼容代理，:18201 -> :18200，并对上下文减肥
# 客户端（ChatBox / OpenWebUI / 8787 网页）把 Base URL 指向 http://127.0.0.1:18201/v1 即可。
import json, os, sys, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import controller

PORT = int(os.environ.get("PROXY_PORT", "18201"))  # 可用环境变量覆盖，便于多实例调试


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, code, obj):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(payload)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError, OSError):
            pass  # 客户端已断开，忽略
        self.close_connection = True

    def _send_sse(self, gen):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            for chunk in gen:
                self.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError):
            pass  # 客户端提前断开，属正常
        except Exception as e:
            import traceback, sys as _sys
            traceback.print_exc(file=_sys.stderr)  # 其他异常记入 proxy.log
            # 兜底：给客户端吐一条可读的错误消息，绝不让气泡空白
            try:
                err = {"id": "chatcmpl-local", "object": "chat.completion.chunk",
                       "model": controller.MODEL_NAME,
                       "choices": [{"index": 0,
                                    "delta": {"content": "[代理错误] 本次上游请求失败：%s（详见 proxy.log）" % e},
                                    "finish_reason": None}]}
                self.wfile.write(("data: " + json.dumps(err, ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def do_GET(self):
        # 客户端（如 Chatbox）会主动 GET /v1/models 拉模型列表
        path = self.path.split("?")[0].rstrip("/")
        if path in ("/v1/models", "/models"):
            self._send_json(200, {"object": "list",
                                  "data": [{"id": controller.MODEL_NAME, "object": "model", "owned_by": "local"}]})
        elif path == "/session/status":
            # 供飞书 bot 查询会话体量（诊断上下文膨胀用）
            self._send_json(200, controller.Session().status())
        elif path == "/think/status":
            # 思考模式开关（2026-10-04）。GET 查，POST 改。
            import think_mode
            self._send_json(200, {"on": think_mode.is_on(),
                                  "desc": think_mode.describe()})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if path in ("/v1/chat/completions",):
            self.handle_chat()
        elif path in ("/v1/models", "/models"):
            self._send_json(200, {"object": "list",
                                  "data": [{"id": controller.MODEL_NAME, "object": "model", "owned_by": "local"}]})
        elif path == "/session/reset":
            # 飞书 bot 没有「新对话」按钮，用这个接口清空会话历史
            controller.Session().reset()
            self._send_json(200, {"ok": True, "message": "会话已清空，等同于新对话。"})
        elif path == "/think":
            # 飞书 "/think on|off" -> 这里改开关。引擎支持 per-request 覆盖，
            # 所以**不需要重启引擎**，下一次请求立即生效。
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = {}
            if length:
                try:
                    raw = json.loads(self.rfile.read(length).decode("utf-8"))
                except Exception:
                    raw = {}
            want = str(raw.get("mode", "")).strip().lower()
            if want not in ("on", "off"):
                self._send_json(400, {"ok": False, "error": "mode 必须是 on 或 off"})
                return
            import think_mode
            ok = think_mode.set_mode(want == "on")
            self._send_json(200, {"ok": ok, "on": think_mode.is_on(),
                                  "desc": think_mode.describe()})
        else:
            self._send_json(404, {"error": "not found"})

    def handle_chat(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8"))

        # ---------- 工具类型前置过滤（修复空气泡 bug） ----------
        # 引擎只认 type=="function" 的工具定义；Chatbox「联网」等客户端会发
        # OpenAI 内置工具类型（如 {"type":"web_search"}），引擎直接 400 拒绝，
        # 流式已在途 -> 客户端空气泡。这里剔除不支持的类型；
        # 剔除后没有任何 function 工具时，整体回落到普通模式（快且稳）。
        raw_tools = body.get("tools")
        if isinstance(raw_tools, list) and raw_tools:
            fn_tools = [t for t in raw_tools if isinstance(t, dict) and t.get("type") == "function"]
            bad = [str(t.get("type") if isinstance(t, dict) else t) for t in raw_tools
                   if not (isinstance(t, dict) and t.get("type") == "function")]
            if bad:
                print("[proxy] dropped unsupported builtin tool type(s): %s" % ", ".join(bad),
                      file=sys.stderr)
            if fn_tools:
                body["tools"] = fn_tools
            else:
                body.pop("tools", None)
                body.pop("tool_choice", None)
        # tool_choice 指向被剔除的工具时一并去掉，防引擎 400
        tc = body.get("tool_choice")
        if isinstance(tc, dict):
            fname = (tc.get("function") or {}).get("name")
            kept = {t["function"].get("name") for t in body.get("tools", []) if isinstance(t, dict)}
            if fname and fname not in kept:
                body.pop("tool_choice", None)

        # ---------- 工具模式：请求携带工具定义 -> 走控制器做「窗口+摘要」压缩 ----------
        # 为后续 computer use / Chatbox 自带搜索等铺路：保留 tools / functions /
        # function_call / tool_choice 交给引擎；上下文由控制器压缩并保留 tool 往返，
        # 避免整段透传导致 KV 前缀失效、prefill 飙到 70~140s、速度掉到 8t/s。
        if body.get("tools") or body.get("functions") or body.get("function_call"):
            messages = body.get("messages", [])
            stream = body.get("stream", False)
            # 转发安全生成参数；继续丢弃 max_tokens（交给引擎侧 reasoning-budget 控制），
            # 避免客户端小上限再次导致正文为空。
            _DROP = ("messages", "stream", "model", "max_tokens")
            params = {k: v for k, v in body.items() if k not in _DROP}
            sess = controller.Session()
            if stream:
                def gen():
                    first = True
                    for kind, piece in sess.chat_tool(messages, stream=True, **params):
                        d = {"role": "assistant"} if first else {}
                        first = False
                        if kind == "reasoning":
                            d["reasoning_content"] = piece
                        elif kind == "content":
                            d["content"] = piece
                        elif kind == "tool_calls":
                            d["tool_calls"] = [piece]
                        yield {"id": "chatcmpl-local", "object": "chat.completion.chunk",
                               "model": controller.MODEL_NAME,
                               "choices": [{"index": 0, "delta": d, "finish_reason": None}]}
                    yield {"id": "chatcmpl-local", "object": "chat.completion.chunk",
                           "model": controller.MODEL_NAME,
                           "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                self._send_sse(gen())
            else:
                msg, finish = sess.chat_tool(messages, stream=False, **params)
                self._send_json(200, {
                    "id": "chatcmpl-local", "object": "chat.completion", "model": controller.MODEL_NAME,
                    "choices": [{"index": 0, "message": msg, "finish_reason": finish or "stop"}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})
            return

        # ---------- 普通模式：走减肥控制器（KV 复用 + 滚动窗口 + 摘要） ----------
        # 只取客户端发来的最后一条 user 消息作为新输入；
        # 客户端的完整历史由控制器自己的 session 维护，避免重复 prefill。
        messages = body.get("messages", [])
        last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                # 保留完整 content：多模态图片数组原样传给控制器，
                # 让模型在走本代理时也能看到客户端附带的图片（此前只抽文本导致图被丢弃）。
                last_user = m.get("content", "")
                break
        stream = body.get("stream", False)
        # 注意：不转发客户端的 max_tokens —— 思考模式下模型先输出 reasoning，
        # 客户端默认的小 max_tokens（如 1024/4096）会被思考耗尽，导致正文为空。
        # 生成上限交给引擎侧控制（kvmem-gen-reserve + reasoning-budget）。
        # 同时剥离 tools/tool_choice：客户端（agent 类）常随请求携带工具定义，
        # 模型会改输出 tool_calls 而非正文，客户端不渲染工具调用就表现为空气泡。
        _STRIP = ("messages", "stream", "model", "max_tokens",
                  "tools", "tool_choice", "functions", "function_call",
                  "response_format")
        params = {k: v for k, v in body.items() if k not in _STRIP}
        sess = controller.Session()
        if stream:
            def gen():
                first = True
                for kind, piece in sess.chat(last_user, stream=True, **params):
                    d = {"role": "assistant"} if first else {}
                    first = False
                    if kind == "reasoning":
                        d["reasoning_content"] = piece
                    else:
                        d["content"] = piece
                    yield {"id": "chatcmpl-local", "object": "chat.completion.chunk",
                           "model": controller.MODEL_NAME,
                           "choices": [{"index": 0, "delta": d, "finish_reason": None}]}
                yield {"id": "chatcmpl-local", "object": "chat.completion.chunk",
                       "model": controller.MODEL_NAME,
                       "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            self._send_sse(gen())
        else:
            # 非流式：上游超时/断连时不刷 traceback，回一条可读提示（04 文档 CLIENT_ABORT）
            try:
                out = sess.chat(last_user, stream=False, **params)
            except Exception as e:
                import traceback, sys as _sys
                traceback.print_exc(file=_sys.stderr)  # 详情进 proxy.log
                out = ("[代理] 上游引擎超时或连接中断（%s）。"
                       "这类问题多因模型思考过长，可稍后重试。" % e)
            self._send_json(200, {
                "id": "chatcmpl-local", "object": "chat.completion", "model": controller.MODEL_NAME,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": out}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print("上下文减肥代理已启动: http://127.0.0.1:%d  ->  :18200" % PORT)
    print("把任意 OpenAI 客户端的 Base URL 改成 http://127.0.0.1:%d/v1 即可。" % PORT)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
