"""
patch_bat.py —— 修 start_kvmem_all.bat 的两类问题

问题 1（真 bug，脚本本身）：
  - REM 注释里的裸 % 会被 cmd 当变量展开（L50 / L69）
  - REM 注释里的孤立单引号 Laya's（L70）会污染后续所有行的引号配对

问题 2（环境依赖，不是 bug）：
  - :status 与 :killport 都调用 PowerShell。在禁用 PowerShell 的沙箱里，
    cmd 会报「命令语法不正确」—— 看起来像语法错，其实是策略拦截。
  - 改成纯 netstat + taskkill，让脚本在受限环境里也能跑。

用法：python patch_bat.py
"""

import os

P = r"E:\ninfer-kvmem\start_kvmem_all.bat"
BAK = r"E:\ninfer-kvmem\start_kvmem_all.bat.bak_20261005"

OLD_KILL = (
    ':killport\r\n'
    'REM use PowerShell instead of taskkill.exe (taskkill fails 0xc0000142 on this machine)\r\n'
    'powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort %1 -State Listen '
    '-ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique '
    '| ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }"\r\n'
    'goto :eof'
)

NEW_KILL = (
    ':killport\r\n'
    'REM Delegate to launch.py stop-port. Why not inline for/f + taskkill:\r\n'
    'REM   percent signs must be doubled inside a .bat, and getting that\r\n'
    'REM   wrong yields "%%P was unexpected at this time" -- an error that\r\n'
    'REM   looks like a syntax bug but is really an escaping bug.\r\n'
    'REM   One Python entry point is easier to keep correct than four copies\r\n'
    'REM   of the same netstat parsing.\r\n'
    '"%PY%" "%LOGDIR%\\experiments\\launch.py" killport %1\r\n'
    'goto :eof'
)

OLD_STATUS = (
    ':status\r\n'
    'powershell -NoProfile -Command "$e=(Get-NetTCPConnection -LocalPort 18200 -State Listen '
    '-ErrorAction SilentlyContinue) -ne $null; $p=(Get-NetTCPConnection -LocalPort 18201 '
    '-State Listen -ErrorAction SilentlyContinue) -ne $null; $q=(Get-NetTCPConnection '
    '-LocalPort 18500 -State Listen -ErrorAction SilentlyContinue) -ne $null; '
    'Write-Output (\' engine:18200=\' + $(if($e){\'RUNNING\'}else{\'DOWN\'}) + '
    "'  proxy:18201=' + $(if($p){'RUNNING'}else{'DOWN'}) + "
    "'  qwen:18500=' + $(if($q){'RUNNING'}else{'DOWN'}))\"\r\n"
    'goto :loop'
)

NEW_STATUS = (
    ':status\r\n'
    '"%PY%" "%LOGDIR%\\experiments\\launch.py" status\r\n'
    'goto :loop'
)

OLD_MON = (
    'start "KVMem Monitor" powershell -NoProfile -ExecutionPolicy Bypass '
    '-File "%LOGDIR%\\monitor.ps1"'
)
NEW_MON = (
    'start "KVMem Monitor" "%PY%" "%LOGDIR%\\monitor.py"'
)

OLD_WARM = (
    'powershell -NoProfile -ExecutionPolicy Bypass -File "%LOGDIR%\\warmup.ps1"'
)
NEW_WARM = (
    '"%PY%" "%LOGDIR%\\warmup.py"'
)

FIX_LINES = [
    ("REM Why: measured on this engine, thinking was 76% of all output chars",
     "REM Why: measured on this engine, thinking was 76 pct of all output chars"),
    ("REM Replaced Laya on 2026-10-04: Qwen3.5-0.8B scores 91% decision accuracy",
     "REM Replaced Laya on 2026-10-04: Qwen3.5-0.8B scores 91 pct decision accuracy"),
    ("REM on our 134-case Chinese benchmark vs Laya's 57%, with 0 safety misses.",
     "REM on our 134-case Chinese benchmark vs Laya (57 pct), 0 safety misses."),
]


def main():
    if not os.path.exists(BAK):
        with open(P, "rb") as f:
            data = f.read()
        with open(BAK, "wb") as f:
            f.write(data)
        print("已备份 -> %s" % os.path.basename(BAK))

    with open(P, "rb") as f:
        raw = f.read().decode("ascii")

    n = 0
    for old, new in FIX_LINES:
        if old in raw:
            raw = raw.replace(old, new)
            n += 1
            print("fixed line: %s" % new[:64])
        else:
            print("SKIP (内容已变): %s" % old[:64])

    for tag, old, new in [("killport", OLD_KILL, NEW_KILL),
                          ("status", OLD_STATUS, NEW_STATUS),
                          ("monitor", OLD_MON, NEW_MON),
                          ("warmup", OLD_WARM, NEW_WARM)]:
        if old in raw:
            raw = raw.replace(old, new)
            n += 1
            print("rewrote :%s (PowerShell -> netstat)" % tag)
        else:
            print("SKIP :%s (已改过或格式不同)" % tag)

    # 全文件加固：REM 注释里任何裸 % 一律换掉
    # ⚠️ 必须跳过含 batch 变量的行（%%~2 / %1 / %%ENGINE%%）——
    #    上一版无差别替换，把 :isup 里的 "%%~2" 改成了 "pct ~2"，直接破坏变量。
    #    只替换**不与任何 batch 变量语法相邻**的裸 %。
    out = []
    for l in raw.split("\r\n"):
        s = l.lstrip()
        if s.upper().startswith("REM "):
            # 保护 %1 %~1 %%A %%P %%~2 %%ENGINE%% 等所有 batch 变量
            prot = []
            i = 0
            while i < len(l):
                if l[i] == "%":
                    j = i
                    while j < len(l) and l[j] == "%":
                        j += 1
                    # 取 % 后面紧跟的变量名或 ~n
                    k = j
                    if k < len(l) and l[k] == "~":
                        while k < len(l) and l[k] in "~0123456789":
                            k += 1
                        prot.append(l[i:k])
                        i = k
                        continue
                    while k < len(l) and (l[k].isalnum() or l[k] == "_"):
                        k += 1
                    if k > j:
                        prot.append(l[i:k])
                        i = k
                        continue
                    prot.append(l[i:j])
                    i = j
                    continue
                prot.append(l[i])
                i += 1
            # 只在非变量位置替换
            rebuilt = []
            i = 0
            while i < len(prot):
                seg = prot[i]
                if seg == "%" and (i == 0 or prot[i - 1] != "%"):
                    nxt = prot[i + 1] if i + 1 < len(prot) else ""
                    if not (nxt and (nxt[0].isalnum() or nxt[0] in "~_")):
                        rebuilt.append(" pct ")
                        i += 1
                        continue
                rebuilt.append(seg)
                i += 1
            l = "".join(rebuilt)
        out.append(l)
    raw = "\r\n".join(out)

    bad = [i + 1 for i, l in enumerate(raw.split("\r\n"))
           if l.count("'") % 2 != 0]
    with open(P, "wb") as f:
        f.write(raw.encode("ascii"))

    print("\n共 %d 处修改" % n)
    print("孤立单引号行: %s" % (bad or "无"))
    print("残留 PowerShell 调用: %s"
          % ("有" if "powershell" in raw.lower() else "无"))
    print("CRLF: %d  裸 LF: %d"
          % (raw.count("\r\n"), raw.count("\n") - raw.count("\r\n")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
