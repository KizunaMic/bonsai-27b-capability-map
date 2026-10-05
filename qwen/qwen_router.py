# -*- coding: utf-8 -*-
"""
qwen_router.py —— Qwen3-0.8B 意图决策层（替换 Laya）

为什么换掉 Laya（实测依据，勿删此段）：
  Laya multilingual 322M 在 134 条中文基准上**原始准确率仅 43%、弃权率 57%**，
  chat 类 45 条只判对 8 条；即使置信度 0.98 仍有 32% 错误（系统性"假自信"）。
  根因是它用 mmBERT 英文语料训练，中文语义锚点不足 —— 换 criteria / 改二分类
  都救不回来（级联二分类实测 0%）。故改用**中文原生**的 Qwen3-0.8B。

核心方法：**logits 零样本分类**（不是让它生成文字）
  1. 构造一段固定 prompt，末尾问「类别：」并给出 A/B/C/D 选项；
  2. **只前向 1 个 token**，取该位置上 "A"/"B"/"C"/"D" 的 logits；
  3. softmax 得到各意图的**真实概率分布** —— 这就是置信度。
  好处：① 快（只算 1 token）；② 置信度来自 logits 而非模型自报，可靠；
        ③ 输出恒为四选一，不会跑格式（避免 04 文档里"模型调不存在的工具"那类坑）。

安全设计：
  - **关键词硬规则优先级最高**：命中删/发/付/格式化等词直接判 sensitive，
    不询问模型。多走一次确认闸的代价 << 误删文件。
  - **疑问句保护**：含"什么是/怎么/如何/哪个"等疑问词时，关键词只降权不硬判，
    避免"帮我解释下什么叫删除操作"这类误伤。
  - **置信度弃权**：低于阈值一律 abstain，交回 Bonsai 自由处理。
  - **永不阻断**：任何异常（模型没装/加载失败/超时/服务未启动）都退回 abstain，
    bot 继续纯聊天。
"""
import os
import re
import threading

# ---- 配置 ----
# ---- 配置 ----
# 模型已下载到本地目录（用 requests 直连下载，见 _fetch2.py —— 工具的回收站机制
# 会删掉 huggingface_hub 的 tmp 文件导致下载失败）。用本地目录可完全离线。
MODEL_DIR = os.environ.get("QWEN_DIR", r"E:\ninfer-kvmem\qwen\model")
MODEL_ID = "Qwen/Qwen3.5-0.8B"   # 752M，多模态架构（Qwen3VL processor）
DEVICE = os.environ.get("QWEN_DEVICE", "cpu")     # Bonsai 占满显存 -> Qwen 走 CPU
# ⚠️ dtype 必须是 float32：实测 bf16 在 CPU 上无原生支持，反而慢 3 倍（5758ms vs 1933ms）。
# 弃权阈值。实测扫描（134 条）：
#   0.35 -> 决策 130 条(84%)  弃权  4
#   0.45 -> 决策 116 条(90%)  弃权 18   ← 当前取值（精度/覆盖最佳平衡）
#   0.55 -> 决策  96 条(93%)  弃权 38
#   0.60 -> 决策  85 条(92%)  弃权 49
# 取 0.45：弃权即回退 Bonsai（安全且不丢功能），而多覆盖 20 条比多 3 个点更有用。
MIN_CONFIDENCE = float(os.environ.get("QWEN_MIN_CONF", "0.45"))
MAX_TOKENS = 1024                                 # 前缀缓存长度上限

INTENTS = ["chat", "control_pc", "query_status", "sensitive"]
LABELS = {
    "chat": "闲聊",
    "control_pc": "操作电脑",
    "query_status": "查询状态",
    "sensitive": "敏感操作",
}
LETTER2INTENT = {"A": "chat", "B": "control_pc", "C": "query_status", "D": "sensitive"}

# ---- 提示词：Qwen3 chat template（关键！实测差 30 个百分点）----
# ⚠️ **必须用 Qwen3 的 im_start/im_end 模板**，这是本次实测最重要的发现：
#      同样 0-shot、同样内容，纯文本拼接只有 46%，用 chat template 直接 75%。
#      原因：Qwen3 系列是按 <|im_start|>role<|im_end|> 格式训练的，
#      手工拼 "用户：xx\n类别：" 它认不出来。**勿改回纯文本。**
#
# 系统提示词长度的影响（实测 36 条筛选集）：
#      SYS_A 精简 (111 tok) -> 69%
#      SYS_B 中等 (190 tok) -> 78%
#      SYS_C 详细 (278 tok) -> 83%   ← 当前采用
# 详细版多写了"易错提醒"（反问式算C / 轻动词也算B / 混合句看最终诉求），
# 这三条正好对应基准里的三类主要错判，值这个长度。
SYSTEM = (
    "你是严格的意图分类器，根据用户此刻的诉求只回答一个字母：\n"
    "A=闲聊寒暄、问知识、问你是谁、讲笑话、表达感谢（不涉及任何操作）\n"
    "B=操作电脑：截图、开关软件、窗口控制、点击/右击/双击、滚动、打字、整理文件\n"
    "C=查询状态：问服务/引擎/进程是否在跑、任务进度、内存显存占用（只读）\n"
    "D=敏感操作：删除、发送给他人、付款、格式化、卸载、强杀进程\n"
    "两条边界：① 只是提到相关词而没下指令（如『什么是剪贴板』）判A；"
    "② 有礼貌外壳但内含操作诉求（如『谢谢你，不过能帮我打开e盘吗』）判B。\n"
    "三条易错提醒：\n"
    "① 反问式确认属于C：『你还活着吗』『处理完了吗』『怎么还没好』都在问服务状态。\n"
    "② 轻动词也算B：『看一下我的屏幕』『帮我打几个字』『右击这个图标』都是要我操作。\n"
    "③ 混合句看最终诉求：前半句寒暄后半句要我做事，判后半句对应的类。"
)

# ⚠️ **不再使用少样本示例**（2026-10-04 实测后删除，勿加回）：
#   旧方案 = SYSTEM + 16 条 few-shot + 纯文本拼接  -> 134条 87% 但 prefix 271 tok / 2477ms
#   新方案 = SYSTEM + **Qwen3 chat template** + 0-shot -> 134条 83% 且 prefix 278 tok
#   看似少 4 个点，但**误伤从 7 条降到 0 条**，且 prompt 更短、结构更干净。
#   真正的加速来自 chat template（纯文本拼接白费了 Qwen3 的训练格式），
#   而不是少样本 —— 加少样本只会让 prefix 更长、更慢。
#
# 保留一个 FEWSHOT 变量仅为兼容旧代码引用，实际不参与 prompt 构造。
FEWSHOT = []


def build_prompt(text: str) -> str:
    """用 **Qwen3 官方 chat template** 拼 prompt，末尾停在「类别：」，
    下一个 token 即为 A/B/C/D。

    ⚠️ 格式关键：Qwen3 按 <|im_start|>role ... <|im_end|> 训练。
       实测同样 0-shot：纯文本拼接 46% vs chat template 75%（差 30 个点）。
       勿改回 "用户：xx\n类别：" 这种手工拼接。
    """
    return ("<|im_start|>system\n" + SYSTEM + "<|im_end|>\n"
            "<|im_start|>user\n" + (text or "").strip() + "<|im_end|>\n"
            "<|im_start|>assistant\n类别：")


# ---- 关键词硬规则（安全保底，不依赖模型）----
# 为什么必须有这一层：模型再准也可能漏判，而删除/发送/付款这类操作一旦漏判
# 就是真实损失。多走一次确认闸的代价 << 误删文件。
SENSITIVE_KEYWORDS = [
    "删除", "删掉", "删了", "清空", "清理掉", "移除", "彻底删",
    # 发送类：实测漏判 2 条（"帮我发个微信给文件传输助手" conf 0.38、
    # "发封邮件给他" conf 0.47 都被模型判成 control_pc），关键词层必须兜住。
    # 覆盖「发+对象」的各种变体：发微信/发邮件/发消息/转发/寄/抄送…
    "发送", "发给别人", "发微信", "发邮件", "发消息", "发文件", "发短信",
    "发个微信", "发封邮件", "转发", "抄送",
    "付款", "支付", "转账", "充值", "购买", "下单", "付钱", "打钱",
    "格式化", "重装", "卸载", "结束进程", "强制关闭", "强杀", "强制结束",
]
# 疑问词：出现这些说明是"问知识"而非"下指令"，关键词只降权不硬判。
# 解决实测发现的误伤："什么是剪贴板""帮我解释下什么叫删除操作""发邮件怎么实现"
# 都被含"删除/发送"字样的规则误判为敏感操作。
QUESTION_MARKERS = [
    "什么是", "是什么", "怎么", "如何", "哪个", "哪些", "为什么", "为啥",
    "?", "？", "吗", "呢", "什么意思", "解释", "介绍", "区别", "能不能",
]
# 祈使/动作标记：若句中有这些，说明即使带问号也是在下指令
ACTION_HINTS = ["帮我", "给我", "替我", "马上", "现在就去", "立刻", "执行", "去掉"]


def is_question(text: str) -> bool:
    t = (text or "").strip()
    if t.endswith("?") or t.endswith("？"):
        # 祈使句也常带问号（"帮我删掉行不行？"），若含明确动作动词仍算指令
        return not any(h in t for h in ACTION_HINTS)
    return any(q in t for q in QUESTION_MARKERS)


def hard_sensitive(text: str):
    """关键词硬规则。返回 (是否敏感, 命中的词)。疑问句只降权 -> 第二个返回值为 None。"""
    t = (text or "").strip()
    if not t:
        return False, None
    hits = [kw for kw in SENSITIVE_KEYWORDS if kw in t]
    if not hits:
        return False, None
    if is_question(t):
        # 疑问句 -> 不硬判（但记下命中词，供模型参考）
        return False, None
    return True, hits[0]


# 正则兜底：覆盖关键词表穷举不到的组合。
# 背景：实测 134 条里关键词表漏了 "帮我发个微信给文件传输助手" 和 "发封邮件给他"
#       两条（模型把它们判成 control_pc），说明**中文动词+量词+宾语的组合太发散**，
#       穷举关键词必然漏。用正则按"发送类动词 + 通信对象"的结构匹配更稳。
SENSITIVE_PATTERNS = [
    (r"发\s*(一?封|一?个|一?条|一?份|个)?\s*(微信|邮件|消息|短信|文件|图片|视频|语音)", "发送(通信)"),
    (r"(微信|邮件|短信|消息|文件)\s*(发|转|寄)\s*(给|到)", "发送(通信)"),
    (r"(转发|分享|发送|寄)\s*(给|到)\s*\S", "发送(通信)"),
    (r"(付|转|汇)\s*(钱|款|账)", "支付"),
    (r"(强杀|结束|干掉)\s*(进程|task|任务|服务)", "结束进程"),
]


def hard_sensitive_re(text: str):
    """正则版敏感检测（关键词表的补充）。命中返回 (True, 说明) 。"""
    t = (text or "").strip()
    if not t or is_question(t):
        return False, None
    for pat, name in SENSITIVE_PATTERNS:
        if re.search(pat, t):
            return True, name
    return False, None


class QwenRouter:
    def __init__(self):
        self.tok = None
        self.model = None
        self.ids = {}          # "A"/"B"/"C"/"D" -> token id
        self.state = "未加载"
        self.last_error = ""
        self._lock = threading.Lock()

    # ---------- 加载 ----------
    def _ensure_loaded(self):
        if self.model is not None:
            return True
        with self._lock:
            if self.model is not None:
                return True
            # 重试机制（重要，实测必需）：
            # 与引擎同时启动时，本进程可能因内存/IO 竞争导致 tokenizer 反序列化失败
            #（报 "Couldn't instantiate the backend tokenizer ..."），稍后重试即可成功。
            last = ""
            for attempt in range(3):
                try:
                    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
                    os.environ.setdefault("HF_HUB_OFFLINE", "1")
                    import torch
                    from transformers import AutoTokenizer, AutoModelForCausalLM
                    self.torch = torch
                    self.tok = AutoTokenizer.from_pretrained(MODEL_DIR)
                    self.model = AutoModelForCausalLM.from_pretrained(
                        MODEL_DIR, dtype=torch.float32)
                    self.model.eval()
                    self.model.to(DEVICE)
                    self.torch.set_num_threads(max(1, (os.cpu_count() or 4) - 1))
                    for L in "ABCD":
                        enc = self.tok.encode(L, add_special_tokens=False)
                        if enc:
                            self.ids[L] = enc[0]
                    if len(self.ids) != 4:
                        raise RuntimeError("未能定位 A/B/C/D token: %s" % self.ids)
                    self._prefill_prefix()
                    self.state = "就绪"
                    self.last_error = ""
                    return True
                except Exception as e:
                    last = "%s: %s" % (type(e).__name__, e)
                    # 清理半初始化的状态，避免脏缓存
                    self.model = None
                    self.tok = None
                    self.ids = {}
                    if attempt < 2:
                        import time as _t
                        _t.sleep(3 * (attempt + 1))   # 退避重试
            self.state = "不可用"
            self.last_error = last
            return False

    def _prefill_prefix(self):
        """**仅用于自检：当前刻意不做 KV 缓存，原因见下。**

        为什么不用前缀 KV 缓存（实测踩坑，勿再尝试）：
        Qwen3.5 含 **linear_attention 层**（config 里 `layer_types` 混排
        linear_attention / full_attention），它的"cache"不是标准 KV cache，
        deepcopy + 追加新 token 后位置编码与状态语义不成立 —— 实测走缓存路径
        概率完全失真："你好" -> control_pc(0.45)、"截个屏" -> chat(0.95)，
        134 条准确率从 87% 崩到 37%。**速度与精度不可兼得，精度优先。**

        控速手段改为：精简 prefix（16 条示例 ≈ 295 token）+ fp32
        （实测 bf16 在 CPU 慢 3 倍：5758ms vs 1933ms）。
        """
        pass

    def _next_token_logits(self, text):
        """全量前向「PREFIX + 用户:<text> + 类别：」，取末位 logits。

        只算 1 个输出 token（不生成文本），这是本方案快的核心。
        """
        torch = self.torch
        full = build_prompt(text)
        enc = self.tok(full, return_tensors="pt", truncation=True, max_length=MAX_TOKENS)
        with torch.no_grad():
            out = self.model(**enc)
        return out.logits[0, -1, :].float()

    # ---------- 分类 ----------
    def route(self, text):
        text = (text or "").strip()
        if not text:
            return {"intent": None, "confidence": 0.0, "abstain": True,
                    "probs": {}, "reason": "空消息"}

        # 第 0 层：敏感检测（关键词 + 正则，安全优先，完全不询问模型）
        hit, kw = hard_sensitive(text)
        if not hit:
            hit, kw = hard_sensitive_re(text)
        if hit:
            return {"intent": "sensitive", "confidence": 1.0, "abstain": False,
                    "probs": {"sensitive": 1.0}, "reason": "",
                    "via": "规则(%s)" % kw}

        if not self._ensure_loaded():
            return {"intent": None, "confidence": 0.0, "abstain": True,
                    "probs": {}, "reason": "Qwen 不可用（%s）" % self.last_error}

        try:
            logits = self._next_token_logits(text)
            # 只取 A/B/C/D 四个位置的 logits -> softmax -> 真实概率分布
            sel = self.torch.tensor([logits[self.ids[L]] for L in "ABCD"])
            probs_t = self.torch.softmax(sel, dim=-1)
            probs = {INTENTS[i]: float(probs_t[i]) for i in range(4)}
            best = max(probs, key=probs.get)
            conf = probs[best]
        except Exception as e:
            return {"intent": None, "confidence": 0.0, "abstain": True,
                    "probs": {}, "reason": "推理失败（%s）" % e}

        if conf < MIN_CONFIDENCE:
            return {"intent": best, "confidence": conf, "abstain": True,
                    "probs": probs,
                    "reason": "置信度 %.2f < %.2f，弃权" % (conf, MIN_CONFIDENCE),
                    "via": "Qwen"}

        return {"intent": best, "confidence": conf, "abstain": False,
                "probs": probs, "reason": "", "via": "Qwen"}

    def status(self):
        return {
            "state": self.state,
            "model": MODEL_ID,
            "device": DEVICE,
            "min_confidence": MIN_CONFIDENCE,
            "last_error": self.last_error,
        }


_ROUTER = None
_ROUTER_LOCK = threading.Lock()


def get_router():
    global _ROUTER
    if _ROUTER is None:
        with _ROUTER_LOCK:
            if _ROUTER is None:
                _ROUTER = QwenRouter()
    return _ROUTER


if __name__ == "__main__":
    r = get_router()
    print("Qwen 状态:", r.status())
    print("-" * 60)
    for t in ["你好", "帮我把E盘的文件调色后发微信", "现在引擎在跑吗", "把文件都删掉", "你会做什么"]:
        res = r.route(t)
        mark = "弃权" if res["abstain"] else LABELS.get(res["intent"], res["intent"])
        print("  %-24s -> %-6s (conf=%.2f) %s"
              % (t[:22], mark, res["confidence"],
                 ("<- " + res["reason"]) if res["reason"] else ""))
