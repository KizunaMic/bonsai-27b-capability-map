"""
test_probe.py —— 探针管道自测（不调模型）

重点验证：**三解析器能区分「答错」和「格式错」**。
这是课题 #1 的命门 —— 如果区分不了，整个实验没有意义。

另有跨进程验证：所有 run 记录通过 subprocess 读取，
模拟「实验数据落盘后由另一个进程分析」的真实流程。
"""

import json
import os
import shutil
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

PASS = 0
FAIL = 0


STASH = os.path.join(BASE, "_selftest_stash")


def _stash(path):
    """把自测产物挪到同盘暂存目录。

    ⚠️ 不用 tempfile.gettempdir()——它在 C 盘，项目在 E 盘，
    跨盘 os.replace 会 WinError 17（已踩过）。
    """
    if not os.path.exists(path):
        return
    if not os.path.isdir(STASH):
        os.makedirs(STASH, exist_ok=True)
    shutil.move(path, os.path.join(STASH, os.path.basename(path)))


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s" % label)
    else:
        FAIL += 1
        print("  - FAIL %s   %s" % (label, str(detail)[:90]))


def main():
    sys.path.insert(0, BASE)
    import probe_core as pc

    print("=" * 62)
    print("探针管道自测")
    print("=" * 62)

    # ---------- 1. 合成图 ----------
    print("\n【1】合成测试图")
    data, truth = pc.build_dot_image(1024, 683)
    check("PNG 有内容", len(data) > 1000, "%d 字节" % len(data))
    check("PNG 魔数正确", data[:8] == b"\x89PNG\r\n\x1a\n")
    check("4 个目标点", len(truth) == 4, str(list(truth)))
    xs = [v[0] for v in truth.values()]
    ys = [v[1] for v in truth.values()]
    check("X 跨度 >500（破坏中心猜测）", max(xs) - min(xs) > 500,
          max(xs) - min(xs))
    check("Y 跨度 >300", max(ys) - min(ys) > 300, max(ys) - min(ys))
    check("所有点在图内", all(0 < x < 1024 and 0 < y < 683
                             for x, y in truth.values()))

    g, gt = pc.build_grid_image(1024, 683)
    check("网格图有内容", len(g) > 1000, "%d 字节" % len(g))
    gx, gy = gt["7,3"]
    check("7行3列中心在图内", 0 < gx < 1024 and 0 < gy < 683, (gx, gy))
    # 中心算法自检：第3列第7行 = (2.5*102.4, 6.5*68.3)
    check("7行3列中心计算正确",
          abs(gx - int(1024 / 10 * 2.5)) <= 2 and abs(gy - int(683 / 10 * 6.5)) <= 2,
          "(%d,%d) 期望约(%d,%d)" % (gx, gy, int(1024 / 10 * 2.5),
                                    int(683 / 10 * 6.5)))

    # ---------- 2. 严格解析器 ----------
    print("\n【2】严格解析器（项目原实现）")
    t = (153, 136)
    for txt, exp in [("153,136", (153, 136)),
                     ("153，136", (153, 136)),
                     ("x=153, y=136", (153, 136)),
                     ("X: 153 Y: 136", (153, 136)),
                     ("  153 , 136  ", (153, 136)),
                     ("(153,136)", (153, 136)),
                     ("153,136。", (153, 136)),
                     ("红色圆点大约在 (153,136) 附近", None),
                     ("153 136", None),
                     ("x=153 y=136", (153, 136)),
                     ("", None),
                     (None, None)]:
        got = pc.parse_strict(txt)
        check("strict(%r)" % (txt if txt else ""), got == exp,
              "得到 %s 期望 %s" % (got, exp))

    # ---------- 3. 宽松解析器 ----------
    print("\n【3】宽松解析器")
    for txt, exp in [("153,136", (153, 136)),
                     ("红色圆点大约在 (153,136) 附近。", (153, 136)),
                     ("x=153, y=136", (153, 136)),
                     ("[123,106,183,166]", (153, 136)),
                     ("答案：153, 136", (153, 136)),
                     ("抱歉，我无法确定", None),
                     ("", None)]:
        got = pc.parse_loose(txt)
        check("loose(%r)" % (txt if txt else ""), got == exp,
              "得到 %s 期望 %s" % (got, exp))

    # ---------- 4. 核心区分能力（课题 #1 命门）----------
    print("\n【4】核心：区分「答错」与「格式错」")
    truth_xy = (153, 136)

    # 场景 A：位置正确但带解释 → 必须是格式损失
    a = pc.audit("红色圆点大约在 (153,136) 附近。", truth_xy, 1024, 683)
    check("A: 严格失败宽松命中 → format_loss=True",
          (not a["strict_hit"]) and a["loose_hit"] and a["format_loss"],
          "strict_hit=%s loose_hit=%s" % (a["strict_hit"], a["loose_hit"]))
    check("A: 不被误判为严格命中", not a["strict_hit"])

    # 场景 B：位置错误 → 必须是真正的能力失败
    b = pc.audit("120,180", truth_xy, 1024, 683)
    check("B: 位置错 → format_loss=False", not b["format_loss"])
    check("B: 宽松也不命中", not b["loose_hit"])
    check("B: 严格判失败", b["strict"] is not None and not b["strict_hit"])

    # 场景 C：bbox 格式 → 中心命中
    c = pc.audit("[123,106,183,166]", truth_xy, 1024, 683)
    check("C: bbox 中心命中", c["loose_hit"], c["loose"])
    check("C: 标为格式损失（严格不接受 bbox）", c["format_loss"])

    # 场景 D：拒答 → 不得产生幻觉坐标
    d = pc.audit("抱歉，我无法确定。", truth_xy, 1024, 683)
    check("D: 拒答不产生坐标", d["loose"] is None and d["strict"] is None)
    check("D: 拒答不算格式损失", not d["format_loss"])

    # 场景 E：中心猜测基线
    e = pc.audit("", truth_xy, 1024, 683)
    check("E: 中心猜测距离 >100px（否则测不出真本事）",
          e["d_center"] > 100, e["d_center"])

    # 场景 F：五个格式变体都要被识别为格式损失
    print("\n【5】格式变体覆盖（这是真实模型输出的常见形态）")
    variants = [
        "153,136",
        "The red circle is at 153,136",
        "红色圆点大约在 (153,136) 附近。",
        "x=153, y=136",
        "X: 153 Y: 136",
        "[123,106,183,166]",
        "答案：153, 136",
        "(153, 136)",
        "位于坐标(153,136)处的红色圆点",
    ]
    n_strict_ok = 0
    n_loose_ok = 0
    n_loss = 0
    for v in variants:
        r = pc.audit(v, truth_xy, 1024, 683)
        if r["strict_hit"]:
            n_strict_ok += 1
        if r["loose_hit"]:
            n_loose_ok += 1
        if r["format_loss"]:
            n_loss += 1
    print("     9 种正确表达：严格接受 %d/9，宽松接受 %d/9，格式损失 %d/9"
          % (n_strict_ok, n_loose_ok, n_loss))
    check("严格口径确实比宽松严（否则审计无意义）",
          n_strict_ok < n_loose_ok, "%d vs %d" % (n_strict_ok, n_loose_ok))
    check("宽松接受全部 9 种正确表达", n_loose_ok == 9, n_loose_ok)
    check("格式损失 = 宽松接受 - 严格接受", n_loss == n_loose_ok - n_strict_ok,
          "%d vs %d" % (n_loss, n_loose_ok - n_strict_ok))
    print("     → 若真实模型用严格不认的 %d 种格式回答，"
          % (n_loose_ok - n_strict_ok))
    print("       原口径会全部记为「能力失败」—— 这就是 C3 审计的意义")
    print("     ⚠️ 数字取决于真实生产代码（pc_locator），不是复刻版")

    # ---------- 6. 落盘与跨进程读取 ----------
    print("\n【6】落盘 + 跨进程读取")
    run_id = "selftest_probe"
    pc.save_run(run_id, {"group": "selftest", "results": [
        {"label": "A", "strict_hit": True, "loose_hit": True,
         "format_loss": False, "raw": "153,136", "d_loose": 0.0}]})

    code = (
        "import sys, json, os;"
        "sys.path.insert(0, %r);"
        "import probe_core as pc;"
        "r = pc.load_runs();"
        "hit = [x for x in r if x.get('run_id') == %r];"
        "print(json.dumps({'n': len(hit),"
        " 'raw': hit[0]['results'][0]['raw'] if hit else None},"
        " ensure_ascii=False))"
    ) % (BASE, run_id)
    out = subprocess.run([PY, "-c", code], capture_output=True, text=True,
                         timeout=60)
    try:
        res = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:
        res = {"n": -1, "raw": None}
    check("子进程能读到运行记录", res["n"] == 1, out.stdout[-120:] + out.stderr[-120:])
    check("原始输出文本完整保留", res["raw"] == "153,136", res["raw"])

    # ---------- 7. 损坏文件不阻塞 ----------
    print("\n【7】鲁棒性")
    bad = os.path.join(pc.RUNS, "_corrupt_selftest.json")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("{ this is not valid json ")
    runs = pc.load_runs()
    check("含损坏文件时仍能读取其他记录", len(runs) >= 1, "%d 条" % len(runs))
    _stash(bad)

    # ---------- 8. 清理 ----------
    _stash(os.path.join(pc.RUNS, run_id + ".json"))

    print("\n" + "=" * 62)
    print("结果: %d 通过, %d 失败" % (PASS, FAIL))
    print("=" * 62)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
