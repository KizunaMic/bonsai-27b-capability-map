"""
launch.py —— 直接拉起三项服务

为什么不直接双击 start_kvmem_all.bat：
该 bat 内部调用了 PowerShell（monitor / warmup / 端口查询）。
在某些受限环境里 PowerShell 被策略拦截，而 cmd 会把这种拦截显示成
「命令语法不正确」——看起来像 bat 语法错误，极难定位。
本脚本用纯 Python 实现同样的功能，没有这个歧义。

用法:
    python launch.py all          引擎 + 代理 + 分流（等待就绪）
    python launch.py engine       只起引擎（18200）
    python launch.py status       查看状态（单次查询）
    python launch.py killport 18200
    python launch.py stop

路径配置（全部可选，默认从脚本位置推导）:
    KVME_ROOT     项目根目录
    KVME_ENGINE   引擎可执行文件
    KVME_MODEL    主模型权重
    KVME_MMPROJ   多模态投影权重（若与模型不同目录则必须指定）
    KVME_PY       主 Python 解释器
    KVME_QWEN_PY  意图分流服务的 Python 解释器（独立 venv）
"""

import os
import subprocess
import sys
import time
import urllib.request

# 路径一律**从脚本位置推导**，不写死绝对路径。两个理由：
#   1. 写死路径会把本机用户名带进仓库
#   2. 别人克隆后路径不同，写死就完全跑不起来
ROOT = os.environ.get(
    "KVME_ROOT",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENGINE_EXE = os.environ.get(
    "KVME_ENGINE",
    os.path.join(ROOT, "engine", "bin", "llama-kvmem-server.exe"))
MODEL = os.environ.get(
    "KVME_MODEL",
    os.path.join(ROOT, "models", "Ternary-Bonsai-2-27B-PTQ1_0.gguf"))
# mmproj 常与主权重不在同一目录，单独用环境变量指定
MMPROJ = os.environ.get("KVME_MMPROJ", "")
PY_MAIN = os.environ.get("KVME_PY", sys.executable)
PY_QWEN = os.environ.get(
    "KVME_QWEN_PY",
    os.path.join(ROOT, "qwen", ".venv", "Scripts", "python.exe"))

PORTS = {"engine": 18200, "proxy": 18201, "router": 18500}

# 与 start_kvmem_all.bat 完全一致（除行尾）
ENGINE_ARGS = [
    ENGINE_EXE, "-m", MODEL,
    "--host", "127.0.0.1", "--port", "18200",
    "-c", "131072", "-ngl", "99",
    "--kv-dtype", "q8_0",
    "-ub", "64", "-b", "256",
    "--mmproj", MMPROJ, "--no-mmproj-offload",
    "--kvmem-block-tokens", "128",
    "--kvmem-sink-tokens", "4096",
    "--kvmem-budget", "16384",
    "--kvmem-gen-reserve", "16384",
    "--kvmem-cpu-gb", "6",
    "--kvmem-method", "retrieval",
    "--kvmem-trace",
    "--reasoning-budget", "4096",
    "--presence-penalty", "1.0",
    "--spec-type", "none",
]


def listening(port):
    """检查端口是否监听。

    ⚠️ netstat 输出是 GBK，capture_output 用 text=True 会按 utf-8 解码并炸。
    必须指定 errors='replace' 或直接用 bytes。
    """
    out = subprocess.run(["netstat", "-ano"], capture_output=True,
                         timeout=20).stdout.decode("gbk", "replace")
    for line in out.splitlines():
        if ":%d " % port in line and "LISTENING" in line:
            return True
    return False


def http_ok(url, timeout=6):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return None


def start_engine():
    if listening(18200):
        print("引擎已在运行，跳过")
        return None
    required = [("引擎", ENGINE_EXE), ("模型", MODEL)]
    if MMPROJ:
        required.append(("多模态投影", MMPROJ))
    else:
        print("提示: 未设置 KVME_MMPROJ，将跳过多模态投影（视觉功能不可用）")
    missing = [(n, p) for n, p in required if not os.path.exists(p)]
    if missing:
        for n, p in missing:
            print("✗ 缺少%s: %s" % (n, p))
        print("\n用环境变量指定路径，例如：")
        print('  set KVME_MODEL=D:\\models\\your-model.gguf')
        print('  set KVME_MMPROJ=D:\\models\\your-mmproj.gguf')
        return None
    args = [a for a in ENGINE_ARGS if a]
    if not MMPROJ:
        # 去掉 --mmproj <path> 与 --no-mmproj-offload
        out, skip = [], False
        for a in args:
            if skip:
                skip = False
                continue
            if a == "--mmproj":
                skip = True
                continue
            if a == "--no-mmproj-offload":
                continue
            out.append(a)
        args = out
    log = open(os.path.join(ROOT, "engine.log"), "ab")
    p = subprocess.Popen(args, cwd=ROOT, stdout=log,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         creationflags=0x00000008)   # DETACHED_PROCESS
    print("引擎已启动 pid=%d  （27B 加载需要 1-3 分钟）" % p.pid)
    return p.pid


def start_proxy():
    if listening(18201):
        print("代理已在运行，跳过")
        return
    f = os.path.join(ROOT, "controller", "proxy_server.py")
    if not os.path.exists(f):
        print("✗ 找不到 proxy_server.py")
        return
    log = open(os.path.join(ROOT, "proxy.log"), "ab")
    p = subprocess.Popen([PY_MAIN, f], cwd=ROOT, stdout=log,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         creationflags=0x00000008)
    print("代理已启动 pid=%d :18201" % p.pid)


def start_router():
    if listening(18500):
        print("分流已在运行，跳过")
        return
    f = os.path.join(ROOT, "qwen", "qwen_service.py")
    if not os.path.exists(f):
        print("✗ 找不到 qwen_service.py")
        return
    log = open(os.path.join(ROOT, "qwen.log"), "ab")
    env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_VERBOSITY="error")
    p = subprocess.Popen([PY_QWEN, f], cwd=ROOT, stdout=log,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         env=env, creationflags=0x00000008)
    print("分流已启动 pid=%d :18500（加载 0.8B 约 10-20 秒）" % p.pid)


def wait_ready(timeout=240):
    t0 = time.time()
    while time.time() - t0 < timeout:
        h = http_ok("http://127.0.0.1:18200/health", timeout=5)
        if h:
            return True
        el = int(time.time() - t0)
        print("  等待引擎 %d 秒…" % el)
        time.sleep(10)
    return False


def kill_port(port):
    """杀掉监听指定端口的进程。纯 netstat + taskkill，不用 PowerShell。

    为什么不用 PowerShell：它在部分沙箱里被策略拦截，而 cmd 会把这种拦截
    显示成「命令语法不正确」，看起来像 bat 语法问题，极难定位。
    """
    out = subprocess.run(["netstat", "-ano"], capture_output=True,
                         timeout=20).stdout.decode("gbk", "replace")
    pids = set()
    for line in out.splitlines():
        if "LISTENING" not in line or (":%d " % port) not in line:
            continue
        parts = line.split()
        if parts:
            p = parts[-1]
            if p.isdigit() and p != "0":
                pids.add(p)
    if not pids:
        print("  :%d 无监听进程" % port)
        return
    for p in pids:
        r = subprocess.run(["taskkill", "/F", "/PID", p],
                           capture_output=True)
        msg = r.stdout.decode("gbk", "replace").strip().splitlines()
        print("  :%d -> pid %s  %s"
              % (port, p, msg[-1] if msg else ""))


def status_once():
    """打印三个服务的状态。单次查询，不进入等待循环。"""
    print()
    print("  KVMem service status")
    print("  " + "-" * 46)
    for name, port in PORTS.items():
        on = listening(port)
        print("   %-7s :%-6d %s" % (name, port,
                                   "RUNNING" if on else "DOWN"))
    h = http_ok("http://127.0.0.1:18200/health")
    if h:
        print("   engine health: %s" % h[:80])
    return 0


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd == "status":
        return status_once()
    if cmd == "killport":
        if len(sys.argv) < 3:
            print("usage: launch.py killport <port>")
            return 1
        kill_port(int(sys.argv[2]))
        return 0
    if cmd == "stop":
        out = subprocess.run(["taskkill", "/F", "/IM",
                              "llama-kvmem-server.exe"],
                             capture_output=True).stdout.decode("gbk", "replace")
        print(out.strip()[:200])
        return 0
    if cmd in ("engine", "all"):
        start_engine()
    if cmd == "all":
        start_proxy()
        start_router()
        print("\n等待引擎加载（27B 模型）…")
        if not wait_ready():
            print("引擎在 240 秒内未就绪，看 engine.log 尾部")
            return 1
        print("\n引擎就绪")
    for name, port in PORTS.items():
        print("  %-8s :%d  %s" % (name, port,
                                  "监听中" if listening(port) else "未监听"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
