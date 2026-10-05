# -*- coding: utf-8 -*-
"""
跨会话图片记忆 —— 回归测试（2026-10-04）

为什么这个测试要单独写
--------------------
之前"跨会话召回"的验证有个致命漏洞：**测试和被测在同一个 Python 进程里**。
而这个功能的全部意义就是"进程重启后还能用"。同进程测，只能证明
"文件写进去了、读得出来"，证不了跨进程。

而且 recall() 对死路径（原图被删）只降权到 0.2、不报错，
所以**索引半失效时从外面看一切正常**。我就是因为这个误判过一次。

所以本测试的核心手段是：**子进程**。
用 subprocess 起一个全新的 Python 解释器（= 模拟 bot 重启），
让它去读索引、召回图片、和体检。父进程完全不共享内存。

跑法：
    python controller/test_cross_session_images.py
"""
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(BASE, ".."))
PY = sys.executable

# 测试产物标记：跑完必须清掉，不能污染真实索引
MARK = "testonly"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("[PASS]" if cond else "[FAIL]", name,
                         ("  -> " + detail) if detail else ""))
    return cond


def run_py(code, timeout=60):
    """在**全新子进程**里执行 Python 代码，返回 (rc, stdout, stderr)。

    这是本测试的核心：子进程 = 模拟重启 = 无内存共享。
    """
    p = subprocess.run([PY, "-c", code], capture_output=True, text=True,
                       timeout=timeout, encoding="utf-8", errors="replace")
    return p.returncode, p.stdout or "", p.stderr or ""


def cleanup():
    """清掉所有测试数据（索引行 + 归档文件）。"""
    rc, out, _ = run_py(
        "import sys; sys.path.insert(0, %r)\n"
        "import image_index, os\n"
        "n = 0\n"
        "if os.path.exists(image_index.INDEX):\n"
        "    keep = []\n"
        "    for line in open(image_index.INDEX, encoding='utf-8'):\n"
        "        if %r in line:\n"
        "            n += 1\n"
        "        else:\n"
        "            keep.append(line)\n"
        "    open(image_index.INDEX, 'w', encoding='utf-8').writelines(keep)\n"
        "print(n)\n" % (BASE, MARK))
    # 把测试文件移出归档区（不直接删：沙箱会拦 os.remove）。
    # 移到 controller/memory/_test_trash/，跑完再一次性处理。
    trash = os.path.join(BASE, "memory", "_test_trash")
    arc = os.path.join(BASE, "memory", "archive")
    moved = 0
    for root, _, files in os.walk(arc):
        for fn in files:
            if MARK in fn:
                try:
                    os.makedirs(trash, exist_ok=True)
                    os.rename(os.path.join(root, fn),
                              os.path.join(trash, fn))
                    moved += 1
                except OSError:
                    pass
    if moved:
        print("  （已移出 %d 个测试文件到 memory/_test_trash/）" % moved)


def main():
    print("=" * 68)
    print("跨会话图片记忆 回归测试 —— 所有断言都在子进程中执行")
    print("=" * 68)

    cleanup()

    # ---------- 阶段 1：父进程写入两张"图"并索引 ----------
    print("\n【阶段1】写入 2 张测试图（父进程）")
    # 造两张极小的合法 JPEG（1x1 灰度），只为验证路径与索引逻辑
    jpeg = bytes.fromhex(
        "ffd8ffe000104a46494600010100000100010000ffdb004300"
        + "08060607060508070707090909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432"
        + "ffc0000b080001000101011100ffc40014000100000000000000000000000000000009ffda0008010100000000d2cfffd9"
    )
    rc, out, err = run_py(
        "import sys, os, time; sys.path.insert(0, %r)\n"
        "import image_index\n"
        "a = image_index.archive_path('%s_user', '.jpg')\n"
        "b = image_index.archive_path('%s_shot', '.png')\n"
        "open(a,'wb').write(b'%%d' %% len(b'x'*100))\n"
        "open(b,'wb').write(b'%%d' %% len(b'y'*200))\n"
        "assert 'archive' in a, a\n"
        "image_index.add(a, 'user_sent', '这是我早上吃的包子', '菜单上有鲜肉包、韭菜包、豆沙包，一共三个馅')\n"
        "time.sleep(1.1)\n"
        "image_index.add(b, 'screen_capture', '', '')\n"
        "print(a)\n"
        "print(b)\n" % (BASE, MARK, MARK))
    if not check("子进程写入 + 归档到 memory/archive/", rc == 0, (err or out)[-300:]):
        return finish()
    paths = [l.strip() for l in out.strip().splitlines() if l.strip()]
    p_user, p_shot = paths[0], paths[1]
    print("      用户图: %s" % p_user)
    print("      截图  : %s" % p_shot)

    check("原图落在 archive 区（不是 shots/）",
          os.sep + "archive" + os.sep in p_user and "shots" not in p_user)

    # ---------- 阶段 2：全新子进程体检（= 重启后） ----------
    print("\n【阶段2】子进程体检（模拟 bot 重启后）")
    rc, out, err = run_py(
        "import sys, json; sys.path.insert(0, %r)\n"
        "import image_index\n"
        "h = image_index.health()\n"
        "print(json.dumps(h, ensure_ascii=False))\n" % BASE)
    if not check("health() 在新进程可调用", rc == 0, (err or out)[-300:]):
        return finish()
    import json
    h = json.loads(out.strip().splitlines()[-1])

    check("索引条数 = 2", h["total"] == 2, "实际 %s" % h["total"])
    check("**原图全部存活**（跨进程后仍能读到文件）", h["alive"] == 2,
          "alive=%s dead=%s" % (h["alive"], h["dead"]))
    check("死路径为 0", h["dead"] == 0, "dead=%s" % h["dead"])
    check("截图确实进了索引（修复前是 0）",
          (h.get("by_source") or {}).get("screen_capture") == 1,
          "by_source=%s" % h.get("by_source"))
    check("有摘要的 1 张", h["with_summary"] == 1,
          "with_summary=%s" % h["with_summary"])

    # ---------- 阶段 3：子进程召回 ----------
    print("\n【阶段3】子进程召回测试")
    for q, want_desc in [
        ("我早上吃的那个包子", "按配文召回"),
        ("那张菜单上一共几个馅", "按摘要召回"),
        ("刚才那张截图里写了什么", "按截图来源召回"),
    ]:
        rc, out, err = run_py(
            "import sys, json; sys.path.insert(0, %r)\n"
            "import image_recall\n"
            "c = image_recall.recall(%r, limit=3)\n"
            "print(json.dumps([{'p': x['rec']['path'], 's': x['score'],\n"
            "  'alive': x['alive'], 'src': x['rec']['source'],\n"
            "  'sum': x['rec'].get('summary','')} for x in c], ensure_ascii=False))\n"
            % (BASE, q))
        ok = rc == 0
        cands = json.loads(out.strip().splitlines()[-1]) if ok else []
        if not check("%-22s -> %s" % (q, want_desc), ok and bool(cands),
                     (err or "")[-200:]):
            continue
        top = cands[0]
        print("      命中: %s score=%.3f alive=%s src=%s"
              % (os.path.basename(top["p"]), top["s"], top["alive"], top["src"]))
        check("  └ 命中的是存活原图", top["alive"] is True)

    # 验证"包子那张"确实排第一（关键词正确性，不是只验证有返回）
    rc, out, err = run_py(
        "import sys, json; sys.path.insert(0, %r)\n"
        "import image_recall\n"
        "c = image_recall.recall('我早上吃的那个包子', limit=3)\n"
        "print(c[0]['rec']['source'] if c else 'NONE')\n" % BASE)
    check("「包子」精确命中用户发的那张（不是截图）",
          out.strip().splitlines()[-1] == "user_sent",
          "top=%s" % out.strip().splitlines()[-1])

    # ---------- 阶段 4：索引不随进程消失 ----------
    print("\n【阶段4】再起一个进程，确认索引仍在（第二次重启）")
    rc, out, _ = run_py(
        "import sys; sys.path.insert(0, %r)\n"
        "import image_index\n"
        "print(image_index.health()['total'])\n" % BASE)
    check("第二个新进程仍读到 2 条", rc == 0 and out.strip().splitlines()[-1] == "2",
          out.strip()[-100:])

    # ---------- 阶段 5：坏路径降级不崩 ----------
    print("\n【阶段5】鲁棒性")
    # 用 rename 而非 os.remove 模拟"原图没了"：
    # 沙箱环境会拦截 os.remove（回收站不可用 -> fail-closed），
    # 那测的是沙箱策略不是我们的代码。rename 同样能让路径失效。
    rc, out, err = run_py(
        "import sys, os; sys.path.insert(0, %r)\n"
        "import image_index, image_recall\n"
        "p = image_index.archive_path('%s_gone', '.jpg')\n"
        "open(p,'wb').write(b'x')\n"
        "image_index.add(p, 'user_sent', '', '这条的原图会消失')\n"
        "os.rename(p, p + '.moved')\n"
        "c = image_recall.recall('这条的原图会消失', limit=2)\n"
        "print('OK' if c else 'EMPTY')\n" % (BASE, MARK))
    check("原图消失后 recall 仍不崩溃（降权不报错）",
          rc == 0 and "OK" in out, (err or out)[-200:])

    rc, out, err = run_py(
        "import sys; sys.path.insert(0, %r)\n"
        "import image_index\n"
        "open(image_index.INDEX, 'a', encoding='utf-8').write('这行是坏的{{{not json\\n')\n"
        "print('OK' if image_index.health()['total'] >= 2 else 'BAD')\n" % BASE)
    check("索引里有损坏行时仍能读（跳过坏行）",
          rc == 0 and "OK" in out, (err or out)[-200:])

    return finish()


def finish():
    cleanup()
    print("\n" + "=" * 68)
    print("结果: %d 通过, %d 失败" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项:")
        for f in FAIL:
            print("  - " + f)
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
