# -*- coding: utf-8 -*-
"""
think_mode.py —— 思考模式开关（供 bot 与 controller 共享）

背景（2026-10-04 实测）：引擎开启 thinking 后，思考占全部输出的 76%
（思考 438 字符 vs 正文 135 字符），"那好吧"三个字要等 86 秒；而 04 文档
记录思考档的收益"未经验证"，且复读退化率不低。故**默认关闭**。

实现：引擎 `--enable-thinking` 默认 off，且其 help 明确写了
"request can override"，因此**按请求开关是零成本的** —— 只要在请求体里
带上对应字段即可，不需要重启引擎。

开关状态存于 controller/think_mode.txt（简单文本，内容 "on"/"off"），
bot 写、proxy 读，无需数据库。
"""
import os

FLAG_FILE = os.path.join(
    os.environ.get("CONTROLLER_DIR", r"E:\ninfer-kvmem\controller"),
    "think_mode.txt")

DEFAULT_OFF = True          # 默认关闭思考
VALID = ("on", "off")


def _read_raw():
    try:
        with open(FLAG_FILE, encoding="utf-8") as f:
            return f.read().strip().lower()
    except Exception:
        return None


def is_on() -> bool:
    """当前是否开启思考。文件缺失/损坏 -> 按默认（关）。"""
    v = _read_raw()
    if v in ("on", "1", "true", "yes"):
        return True
    if v in ("off", "0", "false", "no"):
        return False
    return not DEFAULT_OFF


def set_mode(on: bool) -> bool:
    """写入开关状态，返回是否成功。"""
    try:
        os.makedirs(os.path.dirname(FLAG_FILE), exist_ok=True)
        with open(FLAG_FILE, "w", encoding="utf-8") as f:
            f.write("on" if on else "off")
        return True
    except Exception:
        return False


def describe() -> str:
    return "已开启（回答会先思考，单轮可能 40-90 秒）" if is_on() else \
           "已关闭（回答更快，约 5-15 秒）"


# ---------- 供 proxy 注入到请求体的字段 ----------
def apply_to_params(params: dict) -> dict:
    """按当前开关给请求体加上思考相关字段。

    llama.cpp / KVMem 系的 OpenAI 兼容层支持两种写法，都带上以确保生效：
      - "reasoning_effort": "none" | "low" | "medium" | "high"  （模板级）
      - "enable_thinking": false | true                            （Qwen 特有）
    引擎 help 原文：--reasoning-effort LEVEL，none disables thinking。
    """
    if is_on():
        params["reasoning_effort"] = "medium"
        params["enable_thinking"] = True
    else:
        params["reasoning_effort"] = "none"
        params["enable_thinking"] = False
    return params


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] in VALID:
        set_mode(sys.argv[1] == "on")
        print("思考模式 ->", sys.argv[1])
    else:
        print("当前: %s (%s)" % ("on" if is_on() else "off", FLAG_FILE))
        print("用法: python think_mode.py on|off")
