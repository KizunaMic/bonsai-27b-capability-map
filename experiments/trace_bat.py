"""
trace_bat.py —— 定位 bat 里真正触发「命令语法不正确」的行

方法：在每一行非注释行前插 @echo on，看执行到哪一行开始崩。
之前二分法失败是因为 @echo on 改变了时序，这次逐行插桩后能精确定位。
"""
import os
import subprocess
import sys

SRC = r"E:\ninfer-kvmem\start_kvmem_all.bat"
TMP = r"E:\ninfer-kvmem\_trace2.bat"
ERRS = ("命令语法不正确", "命令语法错误", "The syntax of the command is")


def run(lines):
    body = "\r\n".join(lines) + "\r\n"
    with open(TMP, "w", encoding="ascii", newline="") as f:
        f.write(body)
    try:
        r = subprocess.run([r"C:\Windows\System32\cmd.exe", "/c", "_trace2.bat"],
                           cwd=r"E:\ninfer-kvmem", capture_output=True,
                           timeout=25)
    except subprocess.TimeoutExpired:
        return "", -1
    return (r.stdout + r.stderr).decode("gbk", "replace"), r.returncode


def main():
    with open(SRC, encoding="ascii") as f:
        src = f.read().replace("\r\n", "\n")
    lines = src.split("\n")

    base_out, base_rc = run(lines)
    if not any(e in base_out for e in ERRS):
        print("原样执行未复现该错误。exit=%s" % base_rc)
        print(base_out[:400])
        return 0
    print("原样执行复现了错误。开始逐行插桩定位。\n")

    # 找到第一个触发错误的行
    hit = None
    for n in range(1, len(lines) + 1):
        pre = []
        for l in lines[:n]:
            s = l.strip()
            if s.upper().startswith("REM") or s.startswith("@") or not s:
                pre.append(l)
            else:
                pre.append("@echo on")
                pre.append(l)
        out, rc = run(pre)
        if any(e in out for e in ERRS):
            hit = n
            break

    if hit:
        print(">>> 首个触发行: L%d" % hit)
        for i in range(max(0, hit - 5), min(len(lines), hit + 1)):
            mark = ">>>" if i + 1 == hit else "   "
            print("%s L%d: %s" % (mark, i + 1, lines[i][:120]))
    else:
        print("逐行插桩未复现 —— 说明是整体交互时序问题，非单行语法。")
        print("尝试分段定位...")
        for n in range(1, len(lines) + 1, 5):
            out, rc = run(lines[:n])
            if any(e in out for e in ERRS):
                print(">>> 前 %d 行内即触发，末行为: %s" % (n, lines[n - 1][:110]))
                break

    if os.path.exists(TMP):
        os.rename(TMP, os.path.join(os.path.dirname(TMP), "_trace2_done.txt"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
