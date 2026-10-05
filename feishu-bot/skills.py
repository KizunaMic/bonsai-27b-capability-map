# -*- coding: utf-8 -*-
"""
skills.py —— 技能注册表（skill 插座）

作用：让"装技能"变成「拷目录 + 加一行注册」，而不是改 bot 逻辑。

一个技能 = skills/<name>/ 目录（里面通常有 SKILL.md）+ 下面注册表里的一条记录。

注册项字段：
    name        技能名（目录同名）
    triggers    触发词列表（任一命中即激活；留空表示只能被 /<name> 显式调用）
    sensitive   是否敏感技能（True 则命中后先过确认闸，永不自动执行）
    desc        一句话说明（用于列表和日志）

激活后做什么：
    把该目录的 SKILL.md 作为「参考指令」注入给 Bonsai，让它按技能里的方法做事。
    技能文本以 <skill name="..."> 包裹，放在 system 段之后、首条消息之前——
    这样它既能起作用，又不会像 04 文档第 13 条警告的那样"宣称有工具却调不到"
    （我们只在技能文本里给出**可执行的动作说明**，不引用任何不存在的工具名）。
"""
import os

SKILLS_DIR = os.environ.get(
    "SKILLS_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills"),
)

# ---- 注册表：装新技能就往这里加一行 ----
REGISTRY = [
    {
        "name": "grill-me",
        "triggers": ["grill me", "grill-me", "盘问我", "拷问我", "追问到底", "stress-test"],
        "sensitive": False,
        "desc": "一次问一个问题、逐个击破你的方案疑点，直到达成共识",
    },
]


def _skill_path(name):
    return os.path.join(SKILLS_DIR, name, "SKILL.md")


def list_skills():
    """返回可用技能列表（目录里确有 SKILL.md 才算）。"""
    out = []
    for s in REGISTRY:
        if os.path.exists(_skill_path(s["name"])):
            out.append(s)
    return out


def match(text):
    """按触发词匹配技能。返回 (skill, skill_text) 或 (None, None)。"""
    t = (text or "").strip().lower()
    for s in list_skills():
        for kw in s.get("triggers", []):
            if kw.lower() in t:
                try:
                    with open(_skill_path(s["name"]), "r", encoding="utf-8") as f:
                        return s, f.read().strip()
                except Exception:
                    return s, ""
    return None, None


def match_by_command(text):
    """显式命令形式 /<name> 激活（即使没有触发词）。"""
    t = (text or "").strip()
    if not t.startswith("/"):
        return None, None
    name = t[1:].split()[0].lower() if len(t) > 1 else ""
    for s in list_skills():
        if s["name"].lower() == name:
            try:
                with open(_skill_path(s["name"]), "r", encoding="utf-8") as f:
                    return s, f.read().strip()
            except Exception:
                return s, ""
    return None, None


def build_skill_block(skill, skill_text):
    """把技能文本包成可注入的指令块。

    关键：明确告诉模型这是"行为准则"而不是"可用工具"——因为我们这套没有任何
    内置工具，模型若以为能调用工具就会反复尝试不存在的函数（04 文档第13条）。
    """
    if not skill_text:
        return ""
    return (
        "【当前启用的技能：%s】\n"
        "%s\n"
        "（以上是本轮需要遵守的行为准则。回复请用自然语言，不要声称调用了任何"
        "外部工具——本助手没有内置工具，所有操作都通过对话完成。）"
        % (skill.get("name", "?"), skill_text)
    )


def status():
    return {
        "skills_dir": SKILLS_DIR,
        "registered": len(REGISTRY),
        "available": [s["name"] for s in list_skills()],
    }


if __name__ == "__main__":
    import json
    print(json.dumps(status(), ensure_ascii=False, indent=2))
