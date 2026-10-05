"""
飞书机器人配置

⚠️ **凭据一律从环境变量读取，不写死在代码里。**

为什么要这样：写死的密钥一旦提交进 Git，即使之后删掉，
它仍然留在 commit 历史里（`git log -p` 就能看到），
而 GitHub 的 secret scanning 会主动通知平台方。
**唯一的补救方式是去开放平台重置密钥**，但重置之前它已经泄露了。

配置方式（二选一）：

  A. 环境变量
       set FEISHU_APP_ID=cli_xxxxxxxxxxxx
       set FEISHU_APP_SECRET=xxxxxxxxxxxxxxxx
       set FEISHU_ENCRYPT_KEY=xxxxxxxx
       set FEISHU_VERIFICATION_TOKEN=xxxxxxxx

  B. 本地覆盖文件（推荐，无需每次设环境变量）
       cp config.example.py config_local.py
       # 填入凭据后保存。config_local.py 已在 .gitignore 里，不会被提交。

获取位置：飞书开放平台 → 你的应用 → 凭证与基础信息
"""

import os

# ---- 可选：本地覆盖文件（不进版本库）----
# 开发时不用每次设环境变量，同时保证凭据不会误提交。
try:
    import config_local as _local
except ImportError:
    _local = None


def _get(name, default=""):
    """读取配置。**优先级：环境变量 > config_local.py > default**

    ⚠️ 顺序不能反：早先版本先 star-import config_local，再用
    `os.environ.get(name, default)` 赋值，结果环境变量为空时
    把 config_local 的值覆盖成了空字符串 —— 凭据明明填了却报缺失。
    """
    v = os.environ.get(name)
    if v:
        return v
    if _local is not None and hasattr(_local, name):
        return getattr(_local, name)
    return default


# ---- 凭据（必填）----
APP_ID = _get("APP_ID")
APP_SECRET = _get("APP_SECRET")

# ---- 凭据（可选）----
# Encrypt Key 留空也能跑；填了则消息体加密。
ENCRYPT_KEY = _get("ENCRYPT_KEY")
# Verification Token：SDK 长连接模式下通常不需要
VERIFICATION_TOKEN = _get("VERIFICATION_TOKEN")


def check():
    """启动时调用，缺凭据就明确报错，而不是等到发消息才失败。"""
    missing = []
    if not APP_ID:
        missing.append("FEISHU_APP_ID")
    if not APP_SECRET:
        missing.append("FEISHU_APP_SECRET")
    if missing:
        raise RuntimeError(
            "缺少飞书凭据: %s\n\n"
            "设置方法（任选其一）：\n"
            "  1. 环境变量:  set %s=你的值\n"
            "  2. 本地文件:  cp config.example.py config_local.py 然后填入\n\n"
            "凭据获取: 飞书开放平台 → 你的应用 → 凭证与基础信息"
            % ("、".join(missing), missing[0]))
    return True


# ---- 以下一般不用改 ----

# 本地推理代理地址（带防退化护栏的那个）
LOCAL_LLM_URL = _get("LOCAL_LLM_URL",
                     "http://127.0.0.1:18201/v1/chat/completions")
LOCAL_MODEL = _get("LOCAL_MODEL", "Ternary-Bonsai-2-27B-PTQ1_0.gguf")

# 调模型的最长等待（秒）。仅作客户端兜底：思考长度由引擎侧
# --reasoning-budget 收束（注入提示 + 补 </think>），
# **不使用客户端 max_tokens 或 -n 硬截**（见《04-卡死与循环的防治.md》§9 禁令①）。
# 此超时只是让等待的用户尽早拿到提示。
LLM_TIMEOUT = int(_get("LLM_TIMEOUT", "120"))

# 单条回复最大字符数（超了截断，避免飞书消息过长）
MAX_REPLY_CHARS = int(_get("MAX_REPLY_CHARS", "3000"))

# ---- 视觉分档（实测得出）----
# 大图不是"更清楚所以更好"，而是"大到模型思考被吃光、直接罢工"：
#   原图 2560px / 2048px -> 正文 0 字并 180s 超时（04 文档的 EMPTY 病）
#   1536px -> 60s，小字/按钮全识别对
#   1024px -> 40s 最快，界面元素（按钮/图标）全识别对
#   768px  -> 45s，也能全对
# 所以：默认 1024（快、够准）；要读小字时用 1536。
IMAGE_SIDE_FAST = int(_get("IMAGE_SIDE_FAST", "1024"))       # 找窗口/图标/按钮
IMAGE_SIDE_DETAIL = int(_get("IMAGE_SIDE_DETAIL", "1536"))   # 读小字/报错/参数
IMAGE_QUALITY = int(_get("IMAGE_QUALITY", "75"))

# 最近一张图（bot 自己抓的或用户发来的）在内存里保留多久，
# 期间用户可以纯文字说"把那个窗口关掉"而不用重发图。
IMAGE_CONTEXT_TTL = int(_get("IMAGE_CONTEXT_TTL", "900"))     # 秒（15 分钟）
