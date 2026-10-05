# -*- coding: utf-8 -*-
"""坐标解析测试（不调模型，纯解析逻辑）"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pc_locator as pl

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("[PASS]" if cond else "[FAIL]", name,
                         ("  -> " + detail) if detail else ""))
    return cond


def main():
    print("=" * 64)
    print("坐标解析测试（模型输出容错 + 缩放映射）")
    print("=" * 64)

    print("\n【1】输出格式容错（模型不保证格式完美）")
    for t, exp in [("120,340", 120), ("120，340", 120),
                   ("x=120, y=340", 120), ("(120, 340)", 120),
                   ("  120 , 340  ", 120), ("120,340。", 120),
                   ("X: 120 Y: 340", 120), ("(120，340)", 120)]:
        r = pl.parse_coords(t, 1024, 640)
        check("解析「%s」" % t, r[0] == exp, "得到 %s 期望 x=%s" % (str(r), exp))

    print("\n【1b】带解释文字的回复一律拒绝（严格化，防幻觉坐标）")
    # 这些用宽松正则会抠出错误坐标。宁可丢掉这次结果，
    # 也不能凭空点一个地方——误点的代价是真实的。
    for t in ["坐标是 (120,340)", "120,340 这是发送按钮",
              "发送按钮大约在 800,340 附近", "宽度800 高度600",
              "第1个和第2个", "1到2之间", "1.5倍", "前5 后7"]:
        r = pl.parse_coords(t, 1024, 640)
        check("拒绝「%s」" % t, r == (None, None), "得到 %s" % str(r))

    print("\n【2】找不到 / 幻觉值必须拒绝")
    for t in ["NONE", "找不到", "图中没有这个元素", "99999,99999",
              "-5,10", "-1,-1", "", "abc", "无", "不确定"]:
        r = pl.parse_coords(t, 1024, 640)
        check("拒绝「%s」" % t, r == (None, None), "得到 %s" % str(r))

    print("\n【3】边界值")
    check("(0,0) 合法", pl.parse_coords("0,0", 1024, 640) == (0, 0))
    check("(1024,640) 边缘合法", pl.parse_coords("1024,640", 1024, 640) ==
          (1024, 640))
    check("略超界但<1.5倍被裁剪", pl.parse_coords("1100,650", 1024, 640) ==
          (1024, 640), str(pl.parse_coords("1100,650", 1024, 640)))

    print("\n【4】缩放映射（模型图 -> 真实屏幕）")
    # 1024 宽的图，屏幕 1707 宽
    r = pl.parse_coords("512,300", 1024, 640, 1707, 1067)
    check("中心点映射正确", r == (853, 500), str(r))
    r2 = pl.parse_coords("0,0", 1024, 640, 1707, 1067)
    check("原点映射为 (0,0)", r2 == (0, 0), str(r2))
    # 比例一致性
    a = pl.parse_coords("256,160", 1024, 640, 1707, 1067)
    b = pl.parse_coords("512,320", 1024, 640, 1707, 1067)
    check("缩放是线性的（2倍输入=2倍输出）",
          abs((b[0] - a[0]) - (a[0] - 0)) <= 2 and abs((b[1] - a[1]) - (a[1] - 0)) <= 2,
          "%s vs %s" % (str(a), str(b)))

    print("\n【5】真实截图尺寸下的映射")
    # 实际屏幕 1707x1067，压缩到 1024 → 实际是 1024x640
    r = pl.parse_coords("1000,600", 1024, 640, 1707, 1067)
    check("右下角映射到屏幕右下区域",
          1600 < r[0] <= 1707 and 950 < r[1] <= 1067, str(r))

    print("\n" + "=" * 64)
    print("结果: %d 通过, %d 失败" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  - FAIL: " + f)
    print("=" * 64)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
