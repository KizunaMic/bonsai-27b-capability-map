"""
warmup.py —— 等价于原 warmup.ps1，纯 Python 实现

为什么改写：bat 里原本 `powershell -File warmup.ps1`，在禁用 PowerShell 的
环境里 cmd 会报「命令语法不正确」——看起来像 bat 的语法问题，其实是策略拦截。
用 Python 实现后，任何环境都能跑，且行为可预期。

作用：等引擎就绪后发一条极短的预热请求，吸收首轮 prefill 的冷启动开销。
"""

import json
import sys
import time
import urllib.error
import urllib.request

ENGINE = "http://127.0.0.1:18200"
PROXY = "http://127.0.0.1:18201/v1/chat/completions"
MODEL = "Ternary-Bonsai-2-27B-PTQ1_0.gguf"
WAIT_TIMEOUT = 240
WARMUP_TIMEOUT = 120


def wait_engine(quiet_after=8):
    t0 = time.time()
    last = 0.0
    while time.time() - t0 < WAIT_TIMEOUT:
        try:
            with urllib.request.urlopen(ENGINE + "/health", timeout=5) as r:
                if b"ok" in r.read():
                    return True
        except Exception:
            pass
        el = int(time.time() - t0)
        if el - last >= 10:
            sys.stdout.write("  waiting for engine... %ds\r" % el)
            sys.stdout.flush()
            last = el
        time.sleep(3)
    return False


def warmup():
    """发一条极短的请求，吸收冷启动 prefill。"""
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
        "max_tokens": 4,
        "temperature": 0,
    }
    t0 = time.time()
    req = urllib.request.Request(
        PROXY, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=WARMUP_TIMEOUT) as r:
        json.loads(r.read().decode("utf-8"))
    return time.time() - t0


def main():
    print("warmup: waiting for engine ...")
    if not wait_engine():
        print("warmup: engine not ready within %ds, skipped" % WAIT_TIMEOUT)
        return 1
    print("warmup: engine ready")
    try:
        el = warmup()
        print("warmup: done in %.1fs (cold-start prefill absorbed)" % el)
    except (urllib.error.URLError, OSError) as e:
        # 预热失败不是致命错误：服务本身已就绪，只是首轮会慢一点
        print("warmup: request failed (%s) -- non-fatal, first reply will "
              "just be slower" % repr(e)[:80])
    return 0


if __name__ == "__main__":
    sys.exit(main())
