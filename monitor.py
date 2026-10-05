"""
monitor.py —— 等价于原 monitor.ps1，纯 Python 实现

为什么改写：bat 里原本 `powershell -File monitor.ps1`。PowerShell 在某些环境被
策略拦截，而 cmd 会把这种拦截显示成「命令语法不正确」——极难定位。

⚠️ 重要：**热态下不要 reset 会话。** 早期版本的 monitor 会周期性清空上下文，
这会破坏跨会话记忆（用户发过的图、历史事实），且会让长会话退��实验失真。
现在改为纯只读监测。
"""

import sys
import time
import urllib.request

PORTS = [("engine", 18200, "/health"),
         ("proxy", 18201, "/session/status"),
         ("qwen", 18500, "/status")]


def probe(port, path):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path),
                                    timeout=4) as r:
            return r.read().decode("utf-8", "replace")[:120]
    except Exception as e:
        return "DOWN (%s)" % type(e).__name__


def main():
    print("=" * 60)
    print("  KVMem Monitor   (read-only -- Ctrl+C to close)")
    print("=" * 60)
    try:
        while True:
            print()
            print("  [%s]" % time.strftime("%H:%M:%S"))
            for name, port, path in PORTS:
                print("   %-7s :%-6d %s" % (name, port, probe(port, path)))
            time.sleep(10)
    except KeyboardInterrupt:
        print("\nmonitor closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
