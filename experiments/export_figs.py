"""
export_figs.py —— 从报告 HTML 里抽出各个 SVG 图，存成独立文件

为什么要抽出来：正文 FINDINGS.md / FINDINGS.en.md 用相对路径引用图片，
但报告是单文件（自带内嵌 SVG）。抽出后两者共用同一份图，
避免"正文一套图、报告另一套图"的不一致。

⚠️ 识别方式用 **data-fig / data-lang** 属性，不靠标题文本。
原因：图表标题现在是双语的，用中文标题去匹配会漏掉英文那套。

⚠️ 导出的 SVG 必须把 width='100%' 换成固定宽度。
GitHub 的 Markdown 渲染器不会按 viewBox 缩放，width='100%' 会让
图显示得极小（已踩）。

产出：
    docs/figs/fig0_control.svg      docs/figs/fig0_control_en.svg
    ...（每张图中英各一份）
"""
import os
import re

BASE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(BASE, "analysis", "report.html")
FIGS = os.path.join(os.path.dirname(BASE), "docs", "figs")

NAMES = {
    "fig0": "fig0_control",
    "fig1": "fig1_criteria",
    "fig2": "fig2_format_loss",
    "fig3": "fig3_latency",
    "fig4": "fig4_discreteness",
    "fig5": "fig5_dissociation",
    "fig6": "fig6_wording",
}


def main():
    if not os.path.exists(HTML):
        print("找不到报告：%s" % HTML)
        print("先跑 analyze.py charts")
        return 1
    with open(HTML, encoding="utf-8") as f:
        html = f.read()

    if not os.path.isdir(FIGS):
        os.makedirs(FIGS, exist_ok=True)

    blocks = re.findall(r"<svg\b.*?</svg>", html, re.S)
    print("报告内含 %d 张 SVG 图" % len(blocks))

    made = []
    for b in blocks:
        fid = re.search(r"data-fig='([^']*)'", b)
        lng = re.search(r"data-lang='([^']*)'", b)
        vb = re.search(r"viewBox='0 0 (\d+) (\d+)'", b)
        if not fid or not vb:
            continue
        figid = fid.group(1)
        lang = lng.group(1) if lng else "zh"
        w, h = int(vb.group(1)), int(vb.group(2))
        key = NAMES.get(figid, figid)
        if lang == "en":
            key += "_en"

        out = b
        # 去掉 HTML 内联属性中不合法的部分（SVG 1.1 无 role/aria，保留无害；
        # 但 width='100%' 必须换掉）
        out = out.replace("width='100%'",
                          "width='%d' style='max-width:100%%;height:auto'" % w, 1)
        if "xmlns=" not in out.split(">", 1)[0]:
            out = out.replace("<svg ", '<svg xmlns="http://www.w3.org/2000/svg" ', 1)

        path = os.path.join(FIGS, key + ".svg")
        with open(path, "w", encoding="utf-8") as f:
            f.write(out)
        made.append((key, lang, w, h, os.path.getsize(path)))

    for key, lang, w, h, size in made:
        print("  %-26s %-3s %4dx%-4d %6d 字节"
              % (key + ".svg", lang, w, h, size))
    print("\n共导出 %d 张，目录：%s" % (len(made), FIGS))

    # 清理历史遗留：早期按中文标题命名、且没有 _en 的旧图
    stale = []
    for f in os.listdir(FIGS):
        if not f.endswith(".svg"):
            continue
        stem = f[:-4]
        if stem.endswith("_en"):
            continue
        if stem not in NAMES.values() and stem not in [m[0] for m in made]:
            stale.append(f)
    if stale:
        print("\n以下文件已不被引用（可能是重命名遗留）：%s" % ", ".join(stale))

    print("\n正文引用语法：")
    for key, lang, _, _, _ in made:
        print("  ![%s](figs/%s.svg)" % (key, key))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
