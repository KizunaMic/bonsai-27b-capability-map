#!/usr/bin/env python3
# controller.py - 上下文减肥控制器（Phase A，仅标准库）
# 职责：管理会话状态、滚动窗口、每日/增量摘要、产物外移、上下文装配、上游调用。
import os, json, time, datetime, re, urllib.request, urllib.error, threading

# 会话状态读写锁：保证流式轻量收尾与后台摘要线程不会并发写坏 session.json
_SESSION_LOCK = threading.Lock()

BASE_DIR = os.environ.get("CONTROLLER_DIR", os.path.dirname(os.path.abspath(__file__)))
SESSION_FILE = os.path.join(BASE_DIR, "session.json")
HISTORY_DIR = os.path.join(BASE_DIR, "history")
SUMMARY_DIR = os.path.join(BASE_DIR, "summaries")
ARTIFACT_DIR = os.path.join(BASE_DIR, "artifacts")
# 长期记忆库：与 history/summaries 同级。存"跨会话的事实"（用户偏好、设备、项目路径等），
# 不随 /session/reset 清空——这正是"隔几天回来还认得你"的依据。
MEMORY_DIR = os.path.join(BASE_DIR, "memory")
MEMORY_FILE = os.path.join(MEMORY_DIR, "long_term_memory.json")
# 记忆条目上限与注入长度上限（都按 token 粗算：1 汉字≈1 token，控制在数百 token 内）
MEMORY_MAX_ITEMS = 60
MEMORY_INJECT_CHARS = 700
# 每 N 轮做一次「记忆抽取 + 摘要滚动更新」的后台维护
MEMORY_EVERY_N_TURNS = 5

MODEL_ENDPOINT = "http://127.0.0.1:18200/v1/chat/completions"
MODEL_NAME = "Ternary-Bonsai-2-27B-PTQ1_0.gguf"

# 代理 -> 引擎 的单次请求超时（秒）。原先 1800 太长：模型陷入超长思考时，
# 引擎 slot 久久不释放、代理连接长期 ESTABLISHED 空烧算力。180s 足够正常问答
# （实测最慢的"你是谁"约 30s），超时后主动断开让引擎释放 slot。
# 这是连接层兜底，不违反 04 文档"别用 max_tokens 截思考"（§9 禁令①）。
UPSTREAM_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "180"))

ROLLING_WINDOW = 6           # 保留最近 N 轮（1 轮 = user+assistant = 2 条消息）
ARTIFACT_THRESHOLD = 800     # 助手回复超过此字符数 -> 存为产物文件，上下文只留引用
SUMMARY_MAX_CHARS = 500      # 摘要目标长度上限
CAP = ROLLING_WINDOW * 2     # turns 消息条数上限

# 字符闸：turns 里 user+assistant 正文合计超过此值就触发压缩。
# 为什么需要它（实测教训）：飞书 bot 只有一个会话窗口、无法开新对话，历史会单调增长。
# 之前只看 CAP(条数) 不够——飞书里助手长回复常有几百上千字，几轮就能把提示词
# 撑到 6000+ token，于是每轮都要重新 prefill 整段历史（实测 n_prompt=6723），
# 预填就要 25~30s，客户端等不及先超时断开，引擎却仍在续写该轮生成 ⇒ 空转烧算力。
# 加上字符闸后，历史体量被真正限制住，"发一条消息"不会再拖着几千 token 的历史。
TURN_CHARS_CAP = int(os.environ.get("TURN_CHARS_CAP", "1800"))

SYSTEM_PROMPT = "你是一个本地部署的 Bonsai 27B 办公助手（基于 Qwen3.5 架构）。回答简洁、准确、可执行。"

# 工具模式下保留最近 N 条消息（含工具往返），更早的压进历史摘要。
# 关键：整段透传会让上下文膨胀到 9k~15k token、KV 前缀失效、prefill 飙到 70~140s；
# 这里改为「窗口 + 摘要」压缩，前缀稳定，KV 复用恢复，速度回到 ~21t/s。
TOOL_RECENT_WINDOW = 24

# ---------- 防退化护栏（依据 04-卡死与循环的防治.md F2） ----------
# 文本态判据（机器可判、阈值显式）：
#   EMPTY      正文近空（真实 bug：思考吃光预算致正文 0 字；纯空才触发，避免误伤正常短答）
#   TRUNCATED  代码围栏未闭合（三反引号计数为奇数）或 <svg> 无 </svg>
#   FIXED_POINT 与上一轮正文去空白后逐字相同
#   SAME_PLAN  计划行（OPT:/NEXT:/下一步：）与上轮完全相同
# 结构态（工具模式）：
#   SHELL_LOOP 纯工具轮空壳：assistant 仅 tool_calls、text 为空，且同一工具连续出现 ≥SHELL_LOOP_MIN 次
#              （04 文档 §1 第6条 / 判据表；内测实证：连续 5 轮只调 conversation_read、不输出正文、撑爆上下文）
# 命中即在「同一条响应」内附纠正指令重生成/截断，上限 GUARD_MAX_RETRY 次；仍失败记 undelivered_final。
# 关闸：设环境变量 KVMEM_GUARD=0 即逐字回到未加护栏。
GUARD_ON = os.environ.get("KVMEM_GUARD", "1") != "0"
GUARD_MAX_RETRY = 2
GUARD_EMPTY_CHARS = 1            # 仅正文长度 < 1（即纯空/空白）才判 EMPTY，零误判
SHELL_LOOP_MIN = 3               # 同一工具连续纯工具轮达到此数即判 SHELL_LOOP（文档判据表）


def now_date():
    return datetime.date.today().isoformat()


def _open_upstream(req, timeout=None):
    """打开引擎请求；HTTP 错误时读取并附带引擎的错误 body，不再吞掉 400 的真实原因。

    超时说明：原先固定 1800s（30 分钟），导致模型陷入超长思考时引擎 slot 长时间不释放、
    代理侧连接长期 ESTABLISHED 空烧算力（客户端早已拿到回复）。现改为 UPSTREAM_TIMEOUT
    （默认 180s），超时后主动关闭与引擎的连接，让引擎能尽早收束/释放 slot。
    注意：这是**连接层**超时兜底，不是用 max_tokens 截断思考（04 文档 §9 禁令①），
    思考长度仍由引擎侧 --reasoning-budget 体面收束。
    """
    try:
        return urllib.request.urlopen(req, timeout=(timeout or UPSTREAM_TIMEOUT))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        # 落盘真实请求体，便于定位 400 根因（哪个字段是 null / 何种 shape）
        try:
            bd = getattr(req, "data", None)
            if bd:
                dp = os.path.join(BASE_DIR, "debug_last_payload.json")
                with open(dp, "w", encoding="utf-8") as f:
                    f.write(bd.decode("utf-8", "replace"))
        except Exception:
            pass
        raise RuntimeError("引擎返回 HTTP %s：%s" % (e.code, detail or "(无详情)")) from None


def _msg_text(m):
    """从一条 OpenAI 消息中取纯文本（兼容字符串 / 多模态数组）。"""
    c = m.get("content")
    if isinstance(c, list):
        return "".join(p.get("text", "") for p in c if isinstance(p, dict))
    return c or ""


def _is_tool_call_msg(m):
    return m.get("role") == "assistant" and bool(m.get("tool_calls"))


def _extract_plan(text):
    """抽取计划行（OPT:/NEXT:/下一步：），用于 SAME_PLAN 判定。"""
    out = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if re.match(r"^(OPT:|NEXT:|下一步[:：])", s):
            out.append(s)
    return out


def _last_assistant(state):
    """返回最近一条 assistant 正文与计划行（FIXED_POINT/SAME_PLAN 需要上一轮答案）。"""
    for t in reversed(state.get("turns", [])):
        if t.get("role") == "assistant":
            c = t.get("content") or ""
            return c, _extract_plan(c)
    return "", []


def _is_tool_only(m):
    """纯工具轮空壳判定：assistant 消息有 tool_calls 但无正文（content 空/None/空白）。"""
    if not isinstance(m, dict) or m.get("role") != "assistant":
        return False
    if not m.get("tool_calls"):
        return False
    c = m.get("content")
    if isinstance(c, list):
        c = "".join(p.get("text", "") for p in c if isinstance(p, dict))
    return not (c and str(c).strip())


def _shell_loop_tool(conv, min_n=SHELL_LOOP_MIN):
    """在会话尾部找「连续 ≥min_n 个纯工具轮、且同一工具名连续出现」的空转循环。
    返回触发循环的工具名，否则 None。依据 04 文档 §1 第6条 / 判据表 SHELL_LOOP。"""
    # 从尾部收集连续的纯工具轮（assistant 且 tool_only）
    streak = []
    for m in reversed(conv or []):
        if _is_tool_only(m):
            streak.append(m)
        else:
            break
    if len(streak) < min_n:
        return None
    # streak 最新在前；转成时间序（旧->新）取每轮首个工具名
    names_old_to_new = []
    for m in reversed(streak):
        tcs = m.get("tool_calls") or []
        nm = (tcs[0].get("function", {}) or {}).get("name", "") if tcs else ""
        names_old_to_new.append(nm)
    run = 1
    for i in range(1, len(names_old_to_new)):
        if names_old_to_new[i] and names_old_to_new[i] == names_old_to_new[i - 1]:
            run += 1
            if run >= min_n:
                return names_old_to_new[i]
        else:
            run = 1
    return None


def _corrective_shell(tool):
    return ("【护栏·SHELL_LOOP】你已连续多次调用工具「%s」却未输出任何正文，这是空转循环。"
            "现在停止调用任何工具，仅基于已有全部信息直接给出最终答案或结论。" % tool)


def _judge_state(content, prev_answer, prev_plan):
    """返回退化态名或 None；仅在确属退化时触发，避免正常短答误判。"""
    if not isinstance(content, str):
        return None
    cstrip = content.strip()
    # EMPTY：纯空/空白（真实 bug：思考吃光预算致正文 0 字）；正常短答不误判
    if len(cstrip) < GUARD_EMPTY_CHARS:
        return "EMPTY"
    # TRUNCATED：代码围栏未闭合，或 <svg> 无 </svg>
    if cstrip.count("```") % 2 == 1:
        return "TRUNCATED"
    if "<svg" in cstrip and "</svg>" not in cstrip:
        return "TRUNCATED"
    # FIXED_POINT：与上一轮逐字相同（去空白后）
    if prev_answer and re.sub(r"\s+", "", content) == re.sub(r"\s+", "", prev_answer):
        return "FIXED_POINT"
    # SAME_PLAN：计划行与上轮完全相同
    cur_plan = _extract_plan(content)
    if prev_plan and cur_plan and cur_plan == prev_plan:
        return "SAME_PLAN"
    return None


def _corrective_prompt(state, n):
    return ("【交付检查未通过 · 第 %d 次重试 · 状态 %s】现在只做一件事：立刻输出"
            "完整且闭合的产物；空间不够就先精简但必须闭合；不许只写计划、不许写「同上」、"
            "不许重复上一轮的措辞。若上一轮方案已完成，就换一个不同的点。" % (n, state))


def _norm_msg(m):
    """规范化单条消息发给引擎前的形态：
    - content 为 null（OpenAI 标准允许，常见于 assistant(tool_calls) 消息）
      -> 引擎 json 解析要求字符串，改为空串，否则 HTTP 400。
    """
    if isinstance(m, dict) and m.get("content") is None:
        m = dict(m)
        m["content"] = ""
    return m


def _sanitize_messages(msgs):
    """递归强净化：把所有「应当是字符串」的字段里的 null 清掉，避免引擎 400。
    OpenAI 标准允许 content/name/arguments 等为 null，但 kvmem/llama.cpp 解析要求字符串。
    覆盖：content、name、tool_call_id、tool_calls[].function.arguments，以及多模态数组里的 null text。
    """
    out = []
    for m in (msgs or []):
        if not isinstance(m, dict):
            continue
        nm = dict(m)
        c = nm.get("content")
        if c is None:
            nm["content"] = ""
        elif isinstance(c, list):  # 多模态数组
            parts = []
            for p in c:
                if isinstance(p, dict):
                    pp = dict(p)
                    if pp.get("type") == "text" and pp.get("text") is None:
                        pp["text"] = ""
                    parts.append(pp)
                elif p is not None:
                    parts.append(p)
            nm["content"] = parts
        # 可选字符串字段为 null 时直接剔除（引擎 schema 要求有值或省略）
        for k in ("name", "tool_call_id"):
            if k in nm and nm[k] is None:
                del nm[k]
        tcs = nm.get("tool_calls")
        if isinstance(tcs, list):
            clean = []
            for tc in tcs:
                if not isinstance(tc, dict):
                    continue
                ntc = dict(tc)
                fn = ntc.get("function")
                if isinstance(fn, dict):
                    nfn = dict(fn)
                    if nfn.get("arguments") is None:
                        nfn["arguments"] = "{}"
                    ntc["function"] = nfn
                clean.append(ntc)
            nm["tool_calls"] = clean
        out.append(nm)
    return out


# ==================================================================
# 长期记忆库（跨会话持久）
# 设计要点（对照豆包/DeepSeek 的做法）：
#   - 摘要保存"脉络"（聊过什么），但会随压缩越来越粗；
#   - 记忆保存"事实"（你是谁、你的偏好、你的设备/路径），跨会话不丢；
#   - 每次装配请求时把记忆注入 system 段 —— 这就是"隔几天回来还认得你"的来源。
# 不依赖向量库（本地单机、无嵌入模型），用「模型抽取 + JSON 合并 + 全量注入」。
# ==================================================================

def _mem_load():
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("items", [])
            return data
    except Exception:
        pass
    return {"items": []}


def _mem_save(data):
    os.makedirs(MEMORY_DIR, exist_ok=True)
    tmp = MEMORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MEMORY_FILE)  # 原子替换，避免写坏


def _mem_normalize(items):
    """规整为 [{'text':..., 'tags':[...], 'ts':...}] 并按 text 去重、限长。

    兼容模型可能用的多种字段名（text / fact / content / memory）——实测 Bonsai
    抽出来的是 "fact"，早期只认 "text" 会导致抽取结果全部丢失。
    """
    out, seen = [], set()
    for it in items or []:
        if isinstance(it, str):
            it = {"text": it}
        if not isinstance(it, dict):
            continue
        text = ""
        for k in ("text", "fact", "content", "memory", "summary"):
            v = it.get(k)
            if isinstance(v, str) and v.strip():
                text = v.strip()
                break
        if not text or len(text) > 120:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        tags = it.get("tags") or it.get("tag") or it.get("category") or it.get("type") or []
        if isinstance(tags, str):
            tags = [tags]
        out.append({
            "text": text,
            "tags": [str(t) for t in tags][:4],
            "ts": it.get("ts") or time.time(),
        })
    out.sort(key=lambda x: x.get("ts", 0))
    return out[-MEMORY_MAX_ITEMS:]


def mem_inject_block():
    """生成注入 system 段的记忆文本（没有记忆时返回空串）。"""
    items = _mem_normalize(_mem_load().get("items"))
    if not items:
        return ""
    lines = ["【长期记忆（跨会话，供参考，可能已过期）】"]
    for it in items:
        tag = ("[%s]" % "/".join(it["tags"])) if it.get("tags") else ""
        lines.append("- %s%s" % (it["text"], tag))
    block = "\n".join(lines)
    if len(block) > MEMORY_INJECT_CHARS:
        block = block[:MEMORY_INJECT_CHARS] + "…"
    return block


# --- 规则抽取（默认，零延迟、不占引擎） ---
# 为什么默认不用模型抽取：本引擎 -np 1 单路，记忆抽取若要调模型，就必须和用户
# 对话抢同一个 slot，实测会被排队卡住（后台线程迟迟跑不完）。所以默认走规则，
# 只在 MEMORY_USE_MODEL=1 时才启用模型抽取（更聪明但更慢、且占 slot）。
_MEM_RULES = [
    # (正则, 模板, 标签)  —— 从用户发言里抓"值得跨会话记住"的事实
    # 名字：只认"我叫X / 我的名字叫X / 记住我叫X"；不要用裸"我是X"，
    # 否则"我是做PS后期调色的"会把职业当成名字（实测踩过）。
    (r"我(?:的名字)?叫\s*([^\s，。,.!！?？]{1,12})", "用户自称{name}。", ["身份/称呼"]),
    (r"记住我(?:的名字)?是\s*([^\s，。,.!！?？]{1,12})", "用户自称{name}。", ["身份/称呼"]),
    (r"我的名字(?:是|叫)\s*([^\s，。,.!！?？]{1,12})", "用户自称{name}。", ["身份/称呼"]),
    (r"我是(?:个|一名)?\s*([^，。,.!！?？]{2,20}?)(?:[，。,.!！?？]|$|工作|工程师|设计师|老师|学生|医生|律师|开发)",
     "用户的职业/身份是{value}。", ["职业/身份"]),
    (r"我(?:主要)?(?:做|干|从事)(?:的是)?\s*([^，。,.!！?？]{2,20})", "用户的工作领域是{value}。", ["职业/领域"]),
    (r"我(?:比较)?喜欢\s*([^，。,.!！?？]{1,20})", "用户偏好{value}。", ["偏好"]),
    (r"我不喜欢\s*([^，。,.!！?？]{1,20})", "用户不喜欢{value}。", ["偏好"]),
    (r"以后(?:请)?(?:回答|回复|输出)(?:要)?(?:简洁|简短|短一点)", "用户希望回答简洁。", ["偏好"]),
    (r"(?:我)?(?:在|用|使用)\s*(飞书|微信|QQ|Photoshop|PS|剪映|Pr|AE|DaVinci|Unity)\s*(?:聊天|办公|做|工作)?",
     "用户常用{value}。", ["工具/环境"]),
    (r"(?:我的?)?(?:工作)?目录(?:是|在)\s*([A-Za-z]:\\[^\s，。]{1,30})", "用户工作目录是{value}。", ["环境/路径"]),
    (r"我(?:的)?(?:电脑|机器|显卡)(?:是|配置)?\s*([^，。,.!！?？]{2,25})", "用户的设备是{value}。", ["设备"]),
]


def mem_extract_rules(recent_text):
    """用正则从对话里抽事实：零延迟、不占引擎 slot。返回 items 列表。"""
    out = []
    for line in recent_text.splitlines():
        if not line.startswith("用户"):
            continue
        # 疑问句/反问句不是陈述事实，跳过。
        # 实测踩坑：用户问"我叫什么名字？"曾被抽成"用户自称什么名字。"（把疑问当陈述）。
        body = line[2:].strip()
        if any(c in body for c in "？?") or body.startswith(("谁", "什么", "哪", "吗", "呢", "怎么", "为什么")):
            continue
        for pat, tpl, tags in _MEM_RULES:
            m = re.search(pat, line)
            if not m:
                continue
            groups = [g for g in m.groups() if g]
            if not groups:
                continue
            text = tpl
            if "{name}" in tpl:
                text = tpl.format(name=groups[0])
            elif "{value}" in tpl:
                text = tpl.format(value=groups[0])
            # 抽出来的"名字"若本身就是疑问词，说明是疑问句被误匹配，丢弃
            if any(c in text for c in "？?什么谁哪"):
                continue
            out.append({"text": text, "tags": tags, "ts": time.time()})
    return _mem_normalize(out)


def mem_extract_model(recent_text):
    """让模型从最近对话里抽取值得长期记住的事实（更聪明，但慢且占引擎 slot）。"""
    prompt = (
        "你是记忆抽取器。从下面的对话中，抽取**值得跨会话长期记住的事实**。\n"
        "只抽取这些类型：用户的身份/称呼、职业与领域、长期偏好（喜欢的风格、习惯）、"
        "设备与环境（电脑配置、常用软件、路径）、长期在做的项目与目标、重要约束。\n"
        "每条用一句话陈述，≤40 字。不要抽取闲聊、临时任务、一次性的具体数字。\n"
        "最多 6 条。若没有值得记的，返回空数组 []。\n"
        "只输出 JSON 数组，不要任何解释文字。元素必须形如："
        '{"text":"事实陈述","tags":["类别"]}（字段名用 text 与 tags）'
    )
    user = prompt + "\n\n【对话】\n" + recent_text[-3000:]
    messages = [{"role": "system", "content": "你只做记忆抽取，只输出 JSON 数组。"},
                {"role": "user", "content": user}]
    try:
        with _open_upstream(urllib.request.Request(
                MODEL_ENDPOINT,
                data=json.dumps({"model": MODEL_NAME, "messages": messages,
                                 "stream": False}).encode("utf-8"),
                headers={"Content-Type": "application/json"})) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
        raw = obj["choices"][0]["message"].get("content") or ""
    except Exception:
        return []
    m = re.search(r"\[.*\]", raw, re.S)   # 容错：可能带 ```json 包裹或前后有话
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return []
    return _mem_normalize(arr) if isinstance(arr, list) else []


def mem_extract(recent_text):
    """记忆抽取总入口。默认规则抽取（快、不占 slot）；MEMORY_USE_MODEL=1 时叠加模型抽取。"""
    items = mem_extract_rules(recent_text)
    if os.environ.get("MEMORY_USE_MODEL", "0") == "1":
        items = _mem_normalize(items + mem_extract_model(recent_text))
    return items


def mem_merge(new_items):
    """把新抽取的记忆并入长期记忆库（按 text 去重）。返回合并后的总条数。"""
    if not new_items:
        return len(_mem_load().get("items", []))
    with _SESSION_LOCK:
        data = _mem_load()
        merged = _mem_normalize(list(data.get("items", [])) + list(new_items))
        data["items"] = merged
        _mem_save(data)
    return len(merged)


class Session:
    def __init__(self):
        for d in (HISTORY_DIR, SUMMARY_DIR, ARTIFACT_DIR, MEMORY_DIR):
            os.makedirs(d, exist_ok=True)
        self.state = self._load()
        if self.state.get("last_date") and self.state["last_date"] != now_date():
            self.summarize(force=True)  # 跨日：把昨日活跃轮次并入摘要

    def reset(self):
        """清空当前会话历史与摘要，等价于「开一个新对话」。

        飞书 bot 只有一个会话窗口、无法像网页端那样点「新对话」，所以提供这个
        方法供 bot 侧指令调用（见 feishu-bot/bot.py 的 /new 会话命令）。
        只清 state，history/ 与 artifacts/ 里的落盘记录保留，方便回看。
        """
        with _SESSION_LOCK:
            self.state["turns"] = []
            self.state["summary"] = ""
            self.state["summary_date"] = ""
            self.save()
        return True

    def _load(self):
        if os.path.exists(SESSION_FILE):
            try:
                with open(SESSION_FILE, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"turns": [], "summary": "", "summary_date": "", "artifacts": [],
                "last_date": now_date(), "artifact_seq": 0,
                "guard_fired": 0, "rescued_by_retry": 0, "undelivered_final": 0,
                "shell_loop_breaks": 0}

    def save(self):
        self.state["last_date"] = now_date()
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)

    def _archive(self, role, text):
        path = os.path.join(HISTORY_DIR, now_date() + ".jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "role": role, "text": text}, ensure_ascii=False) + "\n")

    # ---------- 摘要 ----------
    def _summarize_text(self, base_text, existing=""):
        prompt = ("你是上下文压缩器。请把下面的对话压缩为不超过 %d 字的中文摘要，"
                  "保留：用户的目标与偏好、已完成的任务及产物位置（若提到 artifacts/ 文件请保留引用）、"
                  "待办事项、关键结论、对后续重要的约束。不要编造未提及的内容。只输出摘要正文。"
                  % SUMMARY_MAX_CHARS)
        user = prompt + "\n\n【已有摘要】\n" + (existing or "(无)") + "\n\n【待压缩内容】\n" + base_text
        messages = [{"role": "system", "content": "你只做压缩，不回答原问题。"},
                    {"role": "user", "content": user}]
        try:
            return self._call_upstream_text(messages).strip()
        except Exception as e:
            return existing or ("[摘要生成失败: %s]" % e)

    def summarize(self, force=False):
        if not self.state["turns"] and not self.state["summary"]:
            return self.state["summary"]
        conv = "\n".join("%s: %s" % ("用户" if t["role"] == "user" else "助手", t["content"])
                         for t in self.state["turns"])
        new_summary = self._summarize_text(conv, self.state["summary"])
        self.state["summary"] = new_summary
        self.state["summary_date"] = now_date()
        self._write_summary_file(new_summary)
        if len(self.state["turns"]) > 2:
            self.state["turns"] = self.state["turns"][-2:]
        self.save()
        return new_summary

    def _write_summary_file(self, summary):
        with open(os.path.join(SUMMARY_DIR, now_date() + ".md"), "w", encoding="utf-8") as f:
            f.write("# 对话摘要 %s\n\n%s\n" % (now_date(), summary))

    # ---------- 产物外移 ----------
    def _maybe_artifact(self, text):
        if len(text) <= ARTIFACT_THRESHOLD:
            return text
        self.state["artifact_seq"] += 1
        aid = "task%d" % self.state["artifact_seq"]
        path = os.path.join(ARTIFACT_DIR, aid + ".md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        first = (text.strip().splitlines()[0] if text.strip() else "")
        ref = "[产物已存 %s（约 %d 字；首行：%s）]" % (
            os.path.relpath(path, BASE_DIR), len(text), first[:40])
        self.state["artifacts"].append({"id": aid, "path": path, "desc": first[:60]})
        return ref

    # ---------- 装配瘦身消息 ----------
    def _turns_chars(self, turns):
        return sum(len(t.get("content", "")) for t in turns)

    def _trim_turns_for_prompt(self):
        """组装前按「条数 + 字符数」双重闸裁剪历史，从源头压住 prompt 体量。

        与 CAP/字符闸的压缩线程是「双保险」：压缩是异步的、可能滞后或失败，
        而这里在每次组装请求前同步裁一次，保证引擎永远收不到超长提示词。
        裁掉的部分已在 _finalize 的压缩线程里并入 summary；这里的兜底最多丢掉
        尚未被摘要覆盖的少量近期内容，对飞书这种连续闲聊影响很小。
        """
        turns = self.state["turns"]
        if len(turns) <= CAP and self._turns_chars(turns) <= TURN_CHARS_CAP:
            return turns
        # 从最新往回保留，直到同时满足条数与字符上限；至少留 1 轮
        kept, total = [], 0
        for t in reversed(turns):
            c = len(t.get("content", ""))
            if kept and (len(kept) >= CAP or total + c > TURN_CHARS_CAP):
                break
            kept.append(t)
            total += c
        kept.reverse()
        if kept and kept is not turns:
            self.state["turns"] = kept
            self.save()
        return kept

    def assemble(self):
        # 注意：多数 chat 模板只接受一条 system 消息，故把系统提示与历史摘要合并成一条
        sys_parts = [SYSTEM_PROMPT]
        # 长期记忆放在 system 段最前（紧随系统提示），让模型每次都能看到"你记得我什么"。
        # 它跨会话持久、不随 /session/reset 清空 —— 这是"隔几天回来还接得上"的关键。
        mem = mem_inject_block()
        if mem:
            sys_parts.append(mem)
        if self.state["summary"]:
            sys_parts.append("【历史摘要】\n" + self.state["summary"])
        msgs = [{"role": "system", "content": "\n\n".join(sys_parts)}]
        for t in self._trim_turns_for_prompt():
            msgs.append({"role": t["role"], "content": t["content"]})
        return msgs

    # ---------- 上游调用 ----------
    def _call_upstream_text(self, messages, **params):
        messages = _sanitize_messages(messages)
        body = {"model": MODEL_NAME, "messages": messages, "stream": False}
        body.update(params)
        # 思考模式开关（2026-10-04）：默认关闭，飞书 /think on 按请求打开。
        # 引擎 help 明确写 "request can override"，故无需重启引擎。
        try:
            import think_mode
            think_mode.apply_to_params(body)
        except Exception:
            pass
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(MODEL_ENDPOINT, data=data,
                                     headers={"Content-Type": "application/json"})
        with _open_upstream(req) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
        return obj["choices"][0]["message"]["content"]

    def _call_upstream_stream(self, messages, **params):
        messages = _sanitize_messages(messages)
        body = {"model": MODEL_NAME, "messages": messages, "stream": True}
        body.update(params)
        try:
            import think_mode
            think_mode.apply_to_params(body)
        except Exception:
            pass
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(MODEL_ENDPOINT, data=data,
                                     headers={"Content-Type": "application/json"})
        with _open_upstream(req) as resp:
            for raw in resp:
                line = raw.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except Exception:
                    continue
                # 引擎在流结束前会发一个 "choices": [] 的空块，必须防御
                choices = obj.get("choices") or [{}]
                delta = (choices[0] if choices else {}).get("delta", {}) or {}
                # 思考模式：reasoning_content 是思考内容，content 才是正文
                rc = delta.get("reasoning_content")
                if rc:
                    yield ("reasoning", rc)
                c = delta.get("content")
                if c:
                    yield ("content", c)
                # 工具模式：引擎可能以增量形式回传 tool_calls
                tcs = delta.get("tool_calls")
                if tcs:
                    for tc in tcs:
                        yield ("tool_calls", tc)

    def _call_upstream_json(self, messages, **params):
        """非流式调用，返回引擎的完整 assistant 消息（含 content 与 tool_calls）及 finish_reason。"""
        messages = _sanitize_messages(messages)
        body = {"model": MODEL_NAME, "messages": messages, "stream": False}
        body.update(params)
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(MODEL_ENDPOINT, data=data,
                                     headers={"Content-Type": "application/json"})
        with _open_upstream(req) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
        ch = (obj.get("choices") or [{}])[0]
        msg = ch.get("message", {}) or {}
        return msg, ch.get("finish_reason")

    # ---------- 工具模式（computer use / Chatbox 自带搜索 铺路） ----------
    def chat_tool(self, client_messages, stream=False, **params):
        # F4：工具模式 presence 设为 0（文档 L3：高 presence 会与工具调用的结构化 XML 打架，
        #     导致工具块泄漏成正文）。客户端显式带值仍以客户端为准。
        params.setdefault("presence_penalty", 0.0)
        # 以客户端发来的 messages 为权威上下文，做「窗口 + 摘要」压缩，保留 tool_call↔tool 往返。
        client_messages = client_messages or []
        conv = list(client_messages)
        client_sys = ""
        if conv and conv[0].get("role") == "system":
            client_sys = _msg_text(conv[0])
            conv = conv[1:]

        old_text, recent, new_artifacts = self._compact_tool(conv)

        sys_parts = [SYSTEM_PROMPT]
        if self.state.get("summary"):
            sys_parts.append("【历史摘要】\n" + self.state["summary"])
        if client_sys:
            sys_parts.append(client_sys)
        eng_msgs = [{"role": "system", "content": "\n\n".join(sys_parts)}] + recent
        eng_msgs = _sanitize_messages(eng_msgs)  # 清掉任何 null 字符串字段，防引擎 400

        if old_text:
            # 旧上下文并入历史摘要（锁外调模型，不阻塞输出），保留 long-term 记忆
            threading.Thread(target=self._merge_summary_bg, args=(old_text,), daemon=True).start()
        if new_artifacts:
            with _SESSION_LOCK:
                self.state.setdefault("artifacts", []).extend(new_artifacts)
                self.save()

        if stream:
            return self._stream_tool(eng_msgs, params, conv)
        msg, finish = self._call_upstream_json(eng_msgs, **params)
        # ---- F2 SHELL_LOOP 护栏：纯工具轮空壳 + 同一工具连调 ≥SHELL_LOOP_MIN → 中止工具循环 ----
        if GUARD_ON and _is_tool_only(msg):
            tool = _shell_loop_tool(conv, SHELL_LOOP_MIN)
            if tool:
                with _SESSION_LOCK:
                    self.state["guard_fired"] = self.state.get("guard_fired", 0) + 1
                # 同轮纠正：强制停止调工具、直接给结论，最多 1 次重试
                regen, rfinish = self._call_upstream_json(
                    eng_msgs + [{"role": "user", "content": _corrective_shell(tool)}], **params)
                if regen and not _is_tool_only(regen):
                    msg, finish = regen, rfinish
                    with _SESSION_LOCK:
                        self.state["rescued_by_retry"] = self.state.get("rescued_by_retry", 0) + 1
                else:
                    # 仍空转 → 合成一个带结论的停止消息截断循环（不再把 tool_calls 交还给客户端）
                    with _SESSION_LOCK:
                        self.state["shell_loop_breaks"] = self.state.get("shell_loop_breaks", 0) + 1
                    msg = {"role": "assistant",
                           "content": ("[护栏·SHELL_LOOP] 检测到纯工具轮空壳循环：工具「%s」连续调用 %d 次但未产生任何正文，"
                                       "已强制中止工具循环。请根据已有信息直接给出最终结论。" % (tool, SHELL_LOOP_MIN)),
                           "finish_reason": "stop"}
                    finish = "stop"
        self._finalize_tool()
        return (msg, finish)

    def _compact_tool(self, conv):
        """把工具会话压缩成 [系统 + 工具定义 + 最近窗口]；更早的转成摘要文本。"""
        artifacts = []
        if len(conv) <= TOOL_RECENT_WINDOW:
            return "", [self._ext_tool_msg(m, artifacts) for m in conv], artifacts
        old = conv[:-TOOL_RECENT_WINDOW]
        recent_raw = conv[-TOOL_RECENT_WINDOW:]
        # 边界保护：若窗口起点正好落在工具往返中间（recent 首条是 tool 结果），
        # 则把对应的 assistant(tool_calls) 整段并入 recent，绝不切断工具图。
        if recent_raw and recent_raw[0].get("role") == "tool":
            i = len(old) - 1
            while i >= 0 and not _is_tool_call_msg(old[i]):
                i -= 1
            if i >= 0:
                recent_raw = old[i:] + recent_raw
                old = old[:i]
        old_text = self._old_to_text(old)
        recent = [self._ext_tool_msg(m, artifacts) for m in recent_raw]
        return old_text, recent, artifacts

    def _ext_tool_msg(self, m, artifacts):
        """工具结果 / 过长的助手正文 -> 产物外移，上下文只留引用，避免 KV 被大块撑爆。"""
        role = m.get("role")
        if role == "tool":
            c = _msg_text(m)
            if len(c) > ARTIFACT_THRESHOLD:
                path = self._write_artifact_file(c, artifacts, "tool:" + str(m.get("name", "")))
                return _norm_msg({"role": "tool", "tool_call_id": m.get("tool_call_id"), "name": m.get("name"),
                        "content": "[工具 %s 结果已存 %s（约 %d 字）]" %
                        (m.get("name", ""), os.path.relpath(path, BASE_DIR), len(c))})
            return _norm_msg(m)
        if role == "assistant":
            if m.get("tool_calls"):
                return _norm_msg(m)  # 工具调用本身必须原样保留（content null -> ""）
            c = _msg_text(m)
            if len(c) > ARTIFACT_THRESHOLD:
                path = self._write_artifact_file(c, artifacts, "assistant")
                return _norm_msg({"role": "assistant",
                        "content": "[助手回复已存 %s（约 %d 字；首行：%s）]" %
                        (os.path.relpath(path, BASE_DIR), len(c),
                         (c.strip().splitlines()[0] if c.strip() else "")[:40])})
            return _norm_msg(m)
        return _norm_msg(m)

    def _write_artifact_file(self, text, artifacts, tag):
        with _SESSION_LOCK:
            self.state["artifact_seq"] += 1
            aid = "tool%d" % self.state["artifact_seq"]
            path = os.path.join(ARTIFACT_DIR, aid + ".md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        artifacts.append({"id": aid, "path": path, "desc": str(tag)[:60]})
        return path

    def _old_to_text(self, old):
        """把被滑出窗口的旧消息转成摘要文本——工具 JSON 只记「发生了什么」，不塞原文，避免摘要被污染。"""
        parts = []
        for m in old:
            role = m.get("role")
            if role == "tool":
                parts.append("（工具 %s 已执行并返回结果，原文已省略）" % m.get("name", ""))
            elif _is_tool_call_msg(m):
                names = ", ".join(tc.get("function", {}).get("name", "") for tc in m["tool_calls"])
                parts.append("助手调用了工具：%s" % names)
            else:
                parts.append("%s: %s" % ("用户" if role == "user" else "助手", _msg_text(m)))
        return "\n".join(parts)

    def _merge_summary_bg(self, text):
        """后台线程：把旧上下文并入历史摘要（锁外调模型），锁内落盘，不阻塞输出。"""
        with _SESSION_LOCK:
            old_summary = self._load().get("summary", "")
        new_summary = self._summarize_text(text, old_summary)
        with _SESSION_LOCK:
            self.state = self._load()
            self.state["summary"] = new_summary
            self.state["summary_date"] = now_date()
            self._write_summary_file(new_summary)
            self.save()

    def _stream_tool(self, eng_msgs, params, conv=None):
        saw_content = False
        saw_tool = False
        for kind, piece in self._call_upstream_stream(eng_msgs, **params):
            if kind == "tool_calls":
                saw_tool = True
                yield ("tool_calls", piece)
            elif kind == "reasoning":
                yield ("reasoning", piece)
            else:
                saw_content = True
                yield ("content", piece)
        # ---- F2 SHELL_LOOP 护栏（流式，best-effort）：空转循环时向用户提示 ----
        # 流式路径已把 tool_calls 实时交给客户端，无法中途截断循环；故仅发可见警告，
        # 真正截断由非流式 chat_tool 路径负责（agent 工具循环通常用非流式调用工具步）。
        if GUARD_ON and saw_tool and not saw_content and conv is not None:
            tool = _shell_loop_tool(conv, SHELL_LOOP_MIN)
            if tool:
                with _SESSION_LOCK:
                    self.state["guard_fired"] = self.state.get("guard_fired", 0) + 1
                    self.state["shell_loop_breaks"] = self.state.get("shell_loop_breaks", 0) + 1
                yield ("content", "\n\n[护栏·SHELL_LOOP] 检测到工具「%s」连续空转循环，已记录；非流式路径会自动截断。"
                       % tool)
        self._finalize_tool()

    def _finalize_tool(self):
        # 工具模式：上下文权威在客户端，我们仅持久化摘要 / 产物（已在 chat_tool 同步落盘），
        # 此处无需再向 session.json 追加 turns，避免与普通模式的历史相互污染。
        pass

    # ---------- 对外对话 ----------
    def chat(self, user_content, stream=False, **params):
        # F4：非工具模式默认补 presence 惩罚（思考通道官方默认 0.0 不抑制重复）；
        #     客户端若显式带 presence_penalty 则以客户端为准（setdefault 不覆盖）。
        params.setdefault("presence_penalty", 1.0)
        # user_content 可以是纯文本(str)，也可以是多模态 content 数组(含 image_url 等)
        is_multimodal = not isinstance(user_content, str)
        user_text = _msg_text({"content": user_content}) if is_multimodal else user_content
        self._archive("user", user_text)
        # 防客户端重复发送：若最后一条已是相同 user 且中间无 assistant，则跳过追加
        if not (self.state["turns"] and self.state["turns"][-1]["role"] == "user"
                and self.state["turns"][-1]["content"] == user_text):
            # 持久化只存文本：避免图片数组随每轮请求被反复 prefill，撑爆上下文
            self.state["turns"].append({"role": "user", "content": user_text})
        msgs = self.assemble()
        prev_answer, prev_plan = _last_assistant(self.state)
        # 本次生成用完整多模态 content（含图片），让模型看到图；历史里只留文本
        if is_multimodal:
            msgs[-1] = {"role": "user", "content": user_content}
        if stream:
            return self._stream(msgs, params, prev_answer, prev_plan)
        text = self._call_upstream_text(msgs, **params)
        if not isinstance(text, str) or not text.strip():
            text = "[注意]模型本次未输出正文（可能思考耗尽了生成预算）。请重试或把问题拆小。"
        # ---- F2 护栏：非流式，判 + 同轮纠正重试 ----
        if GUARD_ON:
            state = _judge_state(text, prev_answer, prev_plan)
            tries = 0
            while state and tries < GUARD_MAX_RETRY:
                tries += 1
                with _SESSION_LOCK:
                    self.state["guard_fired"] = self.state.get("guard_fired", 0) + 1
                regen = self._call_upstream_text(
                    msgs + [{"role": "user", "content": _corrective_prompt(state, tries)}], **params)
                if isinstance(regen, str) and regen.strip():
                    newstate = _judge_state(regen, prev_answer, prev_plan)
                    if not newstate:
                        text = regen
                        with _SESSION_LOCK:
                            self.state["rescued_by_retry"] = self.state.get("rescued_by_retry", 0) + 1
                        state = None
                    else:
                        state = newstate
                else:
                    state = "EMPTY"
            if state:
                with _SESSION_LOCK:
                    self.state["undelivered_final"] = self.state.get("undelivered_final", 0) + 1
                text = text + "\n\n[护栏：经 %d 次纠正重试仍未通过 %s 判据，以上为最后一次输出。]" % (GUARD_MAX_RETRY, state)
        self._finalize(text)
        return text

    def _stream(self, msgs, params, prev_answer="", prev_plan=None):
        full, reasoning = [], []
        for kind, piece in self._call_upstream_stream(msgs, **params):
            if kind == "reasoning":
                reasoning.append(piece)
            else:
                full.append(piece)
            yield (kind, piece)
        content = "".join(full)
        if not content.strip() and reasoning:
            content = ("[注意]模型只输出了思考（约 %d 字）没有正文，通常是生成预算被思考耗尽。"
                       "请重试，或把问题拆小。" % len("".join(reasoning)))
        final_content = content
        # ---- F2 护栏：流式，判不过则附纠正指令在同一条响应里重生成 ----
        if GUARD_ON:
            state = _judge_state(content, prev_answer, prev_plan or [])
            tries = 0
            while state and tries < GUARD_MAX_RETRY:
                tries += 1
                with _SESSION_LOCK:
                    self.state["guard_fired"] = self.state.get("guard_fired", 0) + 1
                yield ("content", "\n\n[护栏·%s] 正在纠正重试（第 %d 次）…\n" % (state, tries))
                rfull, rreason = [], []
                for kind, piece in self._call_upstream_stream(
                        msgs + [{"role": "user", "content": _corrective_prompt(state, tries)}], **params):
                    if kind == "reasoning":
                        rreason.append(piece)
                    elif kind == "content":
                        rfull.append(piece)
                        yield (kind, piece)
                rcontent = "".join(rfull)
                if not rcontent.strip() and rreason:
                    rcontent = ("[注意]模型只输出了思考（约 %d 字）没有正文。" % len("".join(rreason)))
                newstate = _judge_state(rcontent, prev_answer, prev_plan or [])
                if not newstate:
                    with _SESSION_LOCK:
                        self.state["rescued_by_retry"] = self.state.get("rescued_by_retry", 0) + 1
                    final_content = rcontent
                    state = None
                else:
                    state = newstate
            if state:
                with _SESSION_LOCK:
                    self.state["undelivered_final"] = self.state.get("undelivered_final", 0) + 1
                final_content = final_content + "\n\n[护栏：经 %d 次纠正重试仍未通过 %s 判据。]" % (GUARD_MAX_RETRY, state)
        self._finalize(final_content)

    def _finalize(self, content):
        # 轻量、同步收尾：立刻把本轮回复落盘（毫秒级文件操作），保证下一轮请求能读到，
        # 不阻塞流式响应关闭 —— 解决"正文已出完却还在转圈"的问题。
        with _SESSION_LOCK:
            stored = self._maybe_artifact(content)
            self.state["turns"].append({"role": "assistant", "content": stored})
            self._archive("assistant", content)
            self.save()
        # 重活（超长历史裁剪 + 摘要，会再向模型发一次推理）放到后台线程，不拖慢单次输出。
        # 条数或字符数任一超限都触发（字符闸是防 prompt 膨胀的关键，见 TURN_CHARS_CAP 注释）。
        if len(self.state["turns"]) > CAP or self._turns_chars(self.state["turns"]) > TURN_CHARS_CAP:
            threading.Thread(target=self._summarize_bg, daemon=True).start()
        # 长期记忆维护（抽取事实 + 摘要滚动更新），放后台。
        # 触发条件：①每 N 轮做一次摘要滚动更新；②**只要本轮用户发言里出现了
        # 可抽取的事实就立即抽取**（规则抽取是 0.002s 的纯正则，无需等轮次门槛）。
        # 实测踩坑：原先只看 turns%5==0，导致用户刚说完"我叫X"立刻查询时记忆还没入库。
        if len(self.state["turns"]) % MEMORY_EVERY_N_TURNS == 0:
            try:
                with open(os.path.join(MEMORY_DIR, "maintenance.log"), "a", encoding="utf-8") as lf:
                    lf.write("[%s] 触发记忆维护 turns=%d\n" % (now_date(), len(self.state["turns"])))
            except Exception:
                pass
            threading.Thread(target=self._memory_maintain_bg, daemon=True).start()
        else:
            self._extract_memory_now()

    def _extract_memory_now(self):
        """轻量即时抽取：只跑规则抽取（0.002s，不调模型、不占 slot），命中才写库。"""
        try:
            with _SESSION_LOCK:
                turns = list(self.state["turns"])
            recent = "\n".join(
                "%s: %s" % ("用户" if t["role"] == "user" else "助手", t["content"])
                for t in turns)
            new_items = mem_extract_rules(recent)
            if new_items:
                mem_merge(new_items)
        except Exception:
            pass
        except Exception:
            pass

    def _memory_maintain_bg(self):
        """后台维护长期记忆：
        1) 摘要**滚动更新** —— 把超出窗口的最近几轮并进已有摘要（不丢弃脉络），
           这样即使明细被裁掉，长期脉络仍然完整（这是豆包那类系统的做法）。
        2) 记忆抽取 —— 让模型从最近对话里提炼跨会话该记住的事实并入库。
        两者都是额外推理，放后台不阻塞本次回复。
        """
        def _log(msg):
            try:
                with open(os.path.join(MEMORY_DIR, "maintenance.log"), "a", encoding="utf-8") as lf:
                    lf.write("  %s\n" % msg)
            except Exception:
                pass
        try:
            _log("开始维护 turns=%d" % len(self.state.get("turns", [])))
            with _SESSION_LOCK:
                turns = list(self.state["turns"])
            _log("取到 %d 条 turns, CAP=%d" % (len(turns), CAP))
            # --- 1) 滚动更新摘要：只压缩"超出窗口"的部分，并入已有摘要 ---
            overflow = turns[:-CAP] if len(turns) > CAP else []
            _log("overflow=%d 条" % len(overflow))
            if overflow:
                conv = "\n".join(
                    "%s: %s" % ("用户" if t["role"] == "user" else "助手", t["content"])
                    for t in overflow)
                old_summary = self.state.get("summary", "")
                new_summary = self._summarize_text(conv, old_summary)
                with _SESSION_LOCK:
                    self.state["summary"] = new_summary
                    self.state["summary_date"] = now_date()
                    self._write_summary_file(new_summary)
                _log("摘要已滚动更新(%d 字)" % len(new_summary))
            # --- 2) 记忆抽取：扫**全部** turns（规则抽取零成本，不限窗口），
            #     只看最近几条会漏掉更早说过的事实（实测漏掉"我叫阿泽"）。
            recent = "\n".join(
                "%s: %s" % ("用户" if t["role"] == "user" else "助手", t["content"])
                for t in turns)
            _log("准备抽取, recent 长度=%d" % len(recent))
            new_items = mem_extract(recent)
            _log("抽取到 %d 条记忆" % len(new_items))
            if new_items:
                total = mem_merge(new_items)
                _log("记忆库现有 %d 条" % total)
            _log("维护完成")
        except Exception as e:
            _log("维护异常: %r" % (e,))
        except Exception:
            pass  # 记忆维护失败不影响主流程

    def _summarize_bg(self):
        """后台线程：先（锁外）调模型生成摘要，再（加锁）裁剪历史并落盘，避免阻塞流式响应。"""
        with _SESSION_LOCK:
            snapshot = self._load()
            turns = snapshot.get("turns", [])
            if len(turns) <= CAP and self._turns_chars(turns) <= TURN_CHARS_CAP:
                return
            # 保留满足双重闸的最近若干条，其余并入摘要
            keep, total = [], 0
            for t in reversed(turns):
                c = len(t.get("content", ""))
                if keep and (len(keep) >= CAP or total + c > TURN_CHARS_CAP):
                    break
                keep.append(t)
                total += c
            keep.reverse()
            overflow = turns[:len(turns) - len(keep)]
            old_summary = snapshot.get("summary", "")
        if not overflow:
            return
        # 锁外：调模型生成摘要（耗时操作不挡其他请求）
        conv = "\n".join("%s: %s" % ("用户" if t["role"] == "user" else "助手", t["content"])
                         for t in overflow)
        new_summary = self._summarize_text(conv, old_summary)
        # 加锁应用：重新读最新状态后裁剪并写入摘要，避免并发覆盖
        with _SESSION_LOCK:
            self.state = self._load()
            if len(self.state["turns"]) > len(keep):
                self.state["turns"] = self.state["turns"][len(self.state["turns"]) - len(keep):]
                self.state["summary"] = new_summary
                self.state["summary_date"] = now_date()
                self._write_summary_file(new_summary)
                self.save()

    def _increment_summary(self, overflow_turns):
        conv = "\n".join("%s: %s" % ("用户" if t["role"] == "user" else "助手", t["content"])
                         for t in overflow_turns)
        new_summary = self._summarize_text(conv, self.state["summary"])
        self.state["summary"] = new_summary
        self.state["summary_date"] = now_date()
        self._write_summary_file(new_summary)

    # ---------- 显式存产物 ----------
    def save_artifact(self, content, desc=""):
        self.state["artifact_seq"] += 1
        aid = "task%d" % self.state["artifact_seq"]
        path = os.path.join(ARTIFACT_DIR, aid + ".md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        self.state["artifacts"].append({"id": aid, "path": path, "desc": desc[:60]})
        self.save()
        return path

    def status(self):
        mem = _mem_normalize(_mem_load().get("items"))
        return {
            "active_turns": len(self.state["turns"]),
            "summary_len": len(self.state["summary"]),
            "summary_date": self.state["summary_date"],
            "artifacts": len(self.state["artifacts"]),
            "est_context_chars": sum(len(t["content"]) for t in self.state["turns"]) + len(self.state["summary"]),
            "guard_on": GUARD_ON,
            "guard_fired": self.state.get("guard_fired", 0),
            "rescued_by_retry": self.state.get("rescued_by_retry", 0),
            "undelivered_final": self.state.get("undelivered_final", 0),
            "shell_loop_breaks": self.state.get("shell_loop_breaks", 0),
            "memory_items": len(mem),
        }


if __name__ == "__main__":
    import sys
    s = Session()
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        print(json.dumps(s.status(), ensure_ascii=False, indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "summarize":
        print(s.summarize())
    else:
        print("usage: controller.py [status|summarize]")
