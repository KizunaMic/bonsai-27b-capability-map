#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
本地网页聊天界面 + 数据监测面板（KVMem / Bonsai 27B）
- 浏览器只与本服务通信（同源，无跨域问题）
- 本服务把请求转发给模型服务 llama-kvmem-server（默认 127.0.0.1:18200）
- /api/chat  : 流式转发 /v1/chat/completions（SSE）
- /api/props : 读取引擎配置（引擎状态监测）
- /api/log   : 读取 server.log 末尾 N 行（KVMem trace 实时日志）
仅依赖 Python 标准库。
"""
import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = "http://127.0.0.1:18200"          # 监测面板（/api/props、/api/log）固定走模型服务
CHAT_UPSTREAM = os.environ.get("CHAT_UPSTREAM", "http://127.0.0.1:18201")  # /api/chat 默认走减肥代理(带护栏)；需直连引擎设 CHAT_UPSTREAM=http://127.0.0.1:18200
LOG_PATH = r"E:\ninfer-kvmem\server.log"
LISTEN_PORT = 8787
MODEL_NAME = "Ternary-Bonsai-2-27B-PTQ1_0.gguf"


def _proxy(method, path, body=None, base=UPSTREAM):
    url = base + path
    data = body.encode("utf-8") if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    return urllib.request.urlopen(req, timeout=600)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, payload, ctype="application/json; charset=utf-8"):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path in ("/", "/index", "/index.html"):
            try:
                with open(os.path.join(os.path.dirname(__file__), "webui.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:  # noqa
                self._send(500, json.dumps({"error": str(e)}))
        elif self.path.startswith("/api/props"):
            try:
                r = _proxy("GET", "/props")
                self._send(200, r.read())
            except Exception as e:  # noqa
                self._send(502, json.dumps({"error": "模型服务未启动或不可达: " + str(e)}))
        elif self.path.startswith("/api/log"):
            try:
                n = 60
                if "n=" in self.path:
                    try:
                        n = int(self.path.split("n=")[1].split("&")[0])
                    except Exception:  # noqa
                        pass
                lines = []
                if os.path.exists(LOG_PATH):
                    with open(LOG_PATH, "r", errors="replace") as f:
                        lines = f.readlines()
                self._send(200, json.dumps({"log": "".join(lines[-n:])}, ensure_ascii=False))
            except Exception as e:  # noqa
                self._send(500, json.dumps({"error": str(e)}))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if self.path.startswith("/api/chat"):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw)
            except Exception:  # noqa
                payload = {}
            payload["model"] = MODEL_NAME
            payload["stream"] = True
            body = json.dumps(payload).encode("utf-8")
            try:
                r = _proxy("POST", "/v1/chat/completions", body=body, base=CHAT_UPSTREAM)
            except Exception as e:  # noqa
                try:
                    self._send(502, json.dumps({"error": "模型服务未启动或不可达: " + str(e)}))
                except Exception:  # noqa
                    pass
                return
            # 流式转发：不声明 Content-Length / chunked，
            # 用 Connection: close + 连接关闭标记响应结束
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                while True:
                    chunk = r.read(512)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except Exception:  # noqa
                pass
            finally:
                try:
                    r.close()
                except Exception:  # noqa
                    pass
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def log_message(self, *args):  # noqa
        pass


if __name__ == "__main__":
    print("KVMem 本地网页已启动: http://127.0.0.1:%d" % LISTEN_PORT)
    print("聊天上游(走护栏): %s  | 监测面板(直连引擎): %s  | 模型: %s" % (CHAT_UPSTREAM, UPSTREAM, MODEL_NAME))
    print("请先确保 llama-kvmem-server 已在 18200 端口运行、减肥代理 18201 已运行。Ctrl+C 退出。")
    httpd = ThreadingHTTPServer(("127.0.0.1", LISTEN_PORT), Handler)
    httpd.serve_forever()
