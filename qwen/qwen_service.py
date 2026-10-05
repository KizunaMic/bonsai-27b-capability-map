# -*- coding: utf-8 -*-
"""
qwen_service.py —— Qwen 意图决策层常驻服务（替�� laya_service.py）

端口 18500（原来 Laya 用 18400，改开新端口避免混淆/残留）。
bot 通过 HTTP 调 /route，任何异常都必须降级为 abstain，绝不阻断对话。

接口：
  GET  /ping    -> {"ok": true, "state": "就绪"}
  GET  /status  -> 详细状态
  POST /route   {"text": "..."} -> {"intent","confidence","abstain","probs","via"}
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

LISTEN_PORT = 18500
_router = None
_lock = threading.Lock()


def log(*a):
    print("[%s]" % time.strftime("%H:%M:%S"), *a, flush=True)


def get_router():
    global _router
    if _router is None:
        with _lock:
            if _router is None:
                from qwen_router import get_router as gr
                _router = gr()
    return _router


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path == "/ping":
            r = get_router()
            self._json(200, {"ok": True, "state": r.state})
        elif path == "/status":
            r = get_router()
            self._json(200, r.status())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if path != "/route":
            self._json(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            text = data.get("text", "")
        except Exception as e:
            self._json(400, {"error": "bad request: %s" % e})
            return
        try:
            r = get_router()
            t0 = time.time()
            res = r.route(text)
            res["ms"] = int((time.time() - t0) * 1000)
            self._json(200, res)
        except Exception as e:
            # 服务端异常也降级为 abstain，绝不让 bot 挂
            self._json(200, {"intent": None, "confidence": 0.0, "abstain": True,
                             "probs": {}, "via": "ERROR", "reason": str(e)[:80]})

    def log_message(self, *a):      # 静音默认访问日志
        pass


def main():
    log("Qwen 决策层启动中（端口 %d，加载模型约需 5-15 秒）…" % LISTEN_PORT)
    r = get_router()
    ok = r._ensure_loaded()
    log("模型加载%s：%s" % ("成功" if ok else "失败", r.status()))
    if not ok:
        # 不放弃：启动时段与引擎抢资源容易失败，注册后台重试线程。
        # 成功后 bot 立刻就能用上分流；期间所有请求仍走 abstain（安全回退）。
        log("将启动后台重试（最多 5 次，间隔 20 秒）；期间 bot 自动退回纯聊天。")

        def retry():
            for i in range(5):
                time.sleep(20)
                if r._ensure_loaded():
                    log("后台重试成功（第 %d 次），分流已启用：%s" % (i + 1, r.status()))
                    return
                log("后台重试 %d/5 仍失败：%s" % (i + 1, r.last_error[:90]))
            log("后台重试全部失败，分流保持关闭（bot 仍可纯聊天，功能不受影响）。")

        threading.Thread(target=retry, daemon=True).start()

    srv = ThreadingHTTPServer(("127.0.0.1", LISTEN_PORT), Handler)
    log("Qwen 决策层已就绪：http://127.0.0.1:%d/route" % LISTEN_PORT)
    log("模型: %s  设备: %s  弃权阈值: %.2f"
        % (r.status()["model"], r.status()["device"], r.status()["min_confidence"]))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("收到中断，退出")
        srv.shutdown()


if __name__ == "__main__":
    main()
