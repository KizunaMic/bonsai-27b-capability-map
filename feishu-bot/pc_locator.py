# -*- coding: utf-8 -*-
"""
pc_locator.py —— 让模型在截图上找坐标（5-3 的"眼睛"）

为什么单独一个文件
------------------
多步任务里，**只有一件事必须靠模型**：在截图上定位目标。
"打开微信"能靠规则，"点那个写着发送的按钮"不能。
把这件唯一需要模型的事隔离出来，好处是：
  - 模型的输出可以要求得极简（只要一个点），精度远高于"输出完整 JSON 步骤"
  - 出问题时能立刻分辨是"定位错了"还是"规则错了"
  - 可以独立测试

输出格式（刻意做到极简）
------------------------
只要一行：`x,y` 或 `NONE`。
不给模型 JSON 任务、不给坐标网格、不要求它解释——
04 文档记录的 FIXED_POINT / EMPTY 病，本质都是"要求太多导致的"。

⚠️ 坐标必须换算
---------------
模型看到的是**压缩后的图**（1024px），屏幕是 1707px。
模型给的坐标必须在压缩图的坐标系里，再按比例映射回真实屏幕。
漏了这步 = 全部点偏，而且偏移比例固定，很容易被误认为"模型不准"。
"""
import base64
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 定位精度档：与图片理解共用同一套分档逻辑
SIDE_FAST = 1024
SIDE_DETAIL = 1536
QUALITY = 75


def _shrink(data: bytes, max_side, quality=QUALITY):
    """按最长边压缩。返回 (bytes, 实际宽, 实际高, 缩放比)。"""
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    im = im.convert("RGB")
    w, h = im.size
    scale = 1.0
    if max(w, h) > max_side:
        scale = max_side / float(max(w, h))
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                       Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return buf.getvalue(), im.size[0], im.size[1], scale


def locate(image_bytes: bytes, target: str, llm_url: str, model: str,
           timeout=120, side=None):
    """在截图里找 target 的中心坐标。

    返回 (x, y) 真实屏幕坐标；找不到返回 (None, None)。
    失败不抛异常——定位失败是正常情况，不是错误。
    """
    import json as _json
    import urllib.error
    import urllib.request

    sd = side or SIDE_DETAIL      # 定位需要精度，用精细档
    try:
        small, iw, ih, _scale = _shrink(image_bytes, sd)
    except Exception:
        return None, None

    b64 = base64.b64encode(small).decode("ascii")
    prompt = (
        "在图里找到「%s」。\n"
        "只回答该元素**中心**的坐标，格式严格为 x,y（两个整数，像素）。\n"
        "找不到就只回答 NONE。\n"
        "不要解释，不要多余的话。\n"
        "注意：图像尺寸是 %d×%d，坐标以这个尺寸为准，不要用原图尺寸。"
        % (target, iw, ih)
    )
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": "data:image/jpeg;base64,%s" % b64}},
        ]}],
        "stream": False,
    }
    try:
        req = urllib.request.Request(
            llm_url, data=_json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        obj = _json.loads(raw)
        content = (obj["choices"][0]["message"].get("content") or "").strip()
    except Exception:
        return None, None

    return parse_coords(content, iw, ih)


def parse_coords(text, img_w, img_h, screen_w=None, screen_h=None):
    """从模型回复里解析坐标，并映射到真实屏幕。

    解析要**容错**：模型可能回 "x=120, y=340"、"(120, 340)"、"120,340"
    甚至带一句说明。不能因为格式不完美就丢掉一次 40-70 秒的推理。
    """
    if not text:
        return None, None
    if re.search(r"\bNONE\b|找不到|没有|不存在", text, re.I):
        return None, None

    # ---- 格式判定：只接受「纯坐标」，带解释的一律拒绝 ----
    #
    # 为什么严格：提示词要求"只回答 x,y"，所以合法回复**非常短**。
    # 如果模型开始解释（"发送按钮大约在 x=800, y=340 附近"），
    # 说明它没在按格式输出——这时候**宁可判失败，也不能从解释里抠数字**。
    #
    # 宽松分隔符实测的误匹配（全部是幻觉坐标）：
    #   "第1个和第2个" → (1,2)    "1到2之间" → (1,2)
    #   "宽度800 高度600" → (800,600)  "1.5倍" → (1,5)
    # 这些数字会被当真实坐标派发出去，点到无关位置。
    # 严格化的代价是偶尔丢掉一次本可用的结果，
    # 换来的是"绝不会凭空点一个地方"——这笔交易明显值。
    t = (text or "").strip()
    # 剥掉坐标标签：x=120, y=340 / X:120 Y:340 / (120,340) / 120,340。
    # 逐项剥离而不是一个大正则——大正则总会有漏掉的组合。
    stripped = t
    stripped = re.sub(r"^[xX]\s*[=：:]\s*", "", stripped)      # 前导 x=
    stripped = re.sub(r"^[\(（]\s*", "", stripped)              # 前导 (
    stripped = re.sub(r"[,，、]\s*[yY]\s*[=：:]\s*", ",", stripped)   # , y=
    stripped = re.sub(r"[,，、]?\s*[xX]\s*[yY]\s*[=：:]\s*", ",", stripped)   # x y= / X Y:
    stripped = re.sub(r"[,，、]\s*[xX]\s*[=：:]\s*", ",", stripped)    # , x=
    stripped = re.sub(r"\s+[yY]\s*[=：:]\s*", ",", stripped)          # 空格 + y:
    stripped = re.sub(r"[\)）]\s*[.。]?$", "", stripped)        # 尾部 ). 。
    stripped = re.sub(r"[.。]$", "", stripped)                   # 尾部单个句号
    stripped = stripped.strip()
    # 严格：整体必须像 "123,456" 或 "123，456"
    if not re.fullmatch(r"\d{1,5}\s*[,，、]\s*\d{1,5}", stripped):
        return None, None

    m = re.fullmatch(r"(\d{1,5})\s*[,，、]\s*(\d{1,5})", stripped)
    if not m:
        return None, None
    if not m:
        return None, None
    try:
        x = int(m.group(1))
        y = int(m.group(2))
    except ValueError:
        return None, None

    # 负坐标直接拒。⚠️ 正则里的 `-?` 是为了能匹配 "-5,10" 这种输入，
    # 但负数本身一定是幻觉（屏幕坐标从 0 起）——不加这个判断，
    # "-5" 会被当作分隔符前的噪声而匹配到 "5"，得到错误的正坐标。
    if x < 0 or y < 0:
        return None, None

    # 越界保护：模型偶尔会给出超出图像范围的数字（幻觉）
    if not (0 <= x <= img_w and 0 <= y <= img_h):
        # 容差放宽到 1.5 倍，但仍拒绝明显离谱的值
        if x > img_w * 1.5 or y > img_h * 1.5 or x < 0 or y < 0:
            return None, None
        x = min(x, img_w)
        y = min(y, img_h)

    # 映射回真实屏幕
    if screen_w and screen_h:
        x = int(x / float(img_w) * screen_w)
        y = int(y / float(img_h) * screen_h)
    return x, y


def locate_many(image_bytes, targets, llm_url, model, timeout=120,
                side=None, max_targets=4):
    """一次定位多个目标。**每个目标单独问一次**（模型一次看多目标会串）。

    max_targets 限制数量：每个目标一次 40-70s，
    问 10 个就是 10 分钟，不可接受。所以宁可少定位几个。
    """
    out = {}
    for t in targets[:max_targets]:
        x, y = locate(image_bytes, t, llm_url, model, timeout, side)
        if x is not None:
            out[t] = (x, y)
    return out
