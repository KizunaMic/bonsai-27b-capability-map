# -*- coding: utf-8 -*-
"""
飞书机器人 —— 通过长连接（WebSocket 出站）接收你手机上的消息，
调用本机 Bonsai（Bonsai-2-27B，走带护栏的代理 18201），再把回复发回飞书。

特点：
- 不需要公网 IP / 端口映射 / 证书，SDK 自动 wss 加密 + 验签解密。
- 本机必须先启动引擎与代理：双击 E:\\ninfer-kvmem\\start_kvmem_all.bat
- 现在只做"远程聊天"；后续加 Laya 决策层 / 电脑操作层都在 handle_text() 里扩展。

运行：python bot.py
"""
import os
import sys
import json
import re
import time
import base64
import threading
import urllib.request
import urllib.error

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    P2ImMessageReceiveV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
    ReplyMessageResponse,
    CreateImageRequest,
    CreateImageRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
)

import config
import skills as skill_reg

# 控制器目录（存放 image_index.py / image_recall.py / memory 等长期记忆模块）
CTRL_DIR = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "controller"))

# 电脑操作层（阶段 5）。文件已从 operator.py 改名为 pc_operator.py：
# 同目录下叫 operator.py 的文件会**顶掉 Python 标准库的 operator**，
# 导致 collections/functools/enum 连锁 import 失败（ImportError: cannot import
# name 'eq' from 'operator'）。改名后可直接 import，无需 importlib。
try:
    import pc_operator as _pc
    pc_screenshot = _pc.screenshot
    pc_screen_info = _pc.screenshot_info
    pc_status = _pc.status
    _pc_import_err = None
except Exception as _e:      # 抓屏不可用时降级，不影响聊天
    _pc = None
    pc_screenshot = pc_screen_info = pc_status = None
    _pc_import_err = _e

# 5-3 多步任务模块（自然语言 → 计划 → 执行）
try:
    import pc_task
    import pc_planner
    _task_import_err = None
except Exception as _e:
    pc_task = None
    pc_planner = None
    _task_import_err = _e
# 全局 API 客户端（用于发送消息）。长连接的 ws.Client 只有 start()，
# 不带 API 方法；发消息必须用这个 lark.Client（app_id/app_secret 构建）。
api_client = None

# ---------- 日志 ----------
def log(*a):
    print("[%s]" % time.strftime("%H:%M:%S"), *a, flush=True)


# ---------- 调用本地 Bonsai ----------
def call_llm(user_text: str) -> str:
    """把用户消息发给本机 Bonsai 代理，返回正文文本。"""
    return _call_llm_messages([{"role": "user", "content": user_text}])


def call_llm_with_skill(user_text: str, skill, skill_text: str) -> str:
    """带技能指令调用：把 SKILL.md 作为行为准则注入。

    实现方式：在本轮 user 消息**前面**插一条 system 消息承载技能指令。
    为什么放 system 而不是拼进 user：技能是"本轮的行为准则"，不是用户说的话，
    分开更清晰，也避免污染会话历史里用户消息的语义。
    （代理 controller 会把 messages 里的历史压缩，system 段会被保留。）

    ⚠️ 附加一条**长度约束**到 user 消息末尾：实测不加约束时，Bonsai 会被技能的
    "给出选项/继续追问"诱导超长输出（单次 8023 字符、耗时 120s 超时）。这里显式
    要求"本轮只问一个问题、回复简短"，把单次输出压在可接受长度内。
    """
    block = skill_reg.build_skill_block(skill, skill_text)
    hint = ("\n\n（本轮请严格遵守上述准则：**只问一个问题**、回复**简短**、"
            "总长度控制在 500 字以内。）")
    msgs = [
        {"role": "system", "content": block},
        {"role": "user", "content": user_text + hint},
    ]
    return _call_llm_messages(msgs)


def _call_llm_messages(messages) -> str:
    payload = {
        "model": config.LOCAL_MODEL,
        "messages": messages,
        "stream": False,
        # 注意：不设客户端 max_tokens。按 04-卡死与循环的防治.md §9 禁令①，
        # 禁止用调小 max_tokens 来"治"长思考（会截在思考中间 ⇒ 正文 0 字）。
        # 思考长度由引擎侧 --reasoning-budget 体面收束。
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        config.LOCAL_LLM_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=config.LLM_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        # 超时也提示一句，别让用户干等；引擎侧已由 -n 上限兜底，不会一直烧
        if "timed out" in str(e).lower() or "timeout" in str(e).lower():
            return "⚠️ 模型思考超时了（%d 秒没答完）。这类问题模型容易陷入反复自我怀疑，稍后再问一次或换个问法试试。" % config.LLM_TIMEOUT
        return "⚠️ 连不上本地模型（%s）。请确认已双击 start_kvmem_all.bat 启动引擎和代理。" % e
    except Exception as e:
        return "⚠️ 调用模型出错：%s" % e

    try:
        obj = json.loads(raw)
    except Exception:
        return "⚠️ 模型返回异常：%s" % raw[:200]

    msg = obj.get("choices", [{}])[0].get("message", {})
    content = (msg.get("content") or "").strip()
    # 关键：绝不把 reasoning_content 当答案发给用户（那是模型内心思考，
    # 会把"你好，你好，我在。"这类碎片直接暴露出去）。正文为空就明确提示。
    if not content:
        return "⚠️ 模型这次只做了思考没给出正文（通常是思考预算被吃满）。请重试或把问题拆小一点。"

    if len(content) > config.MAX_REPLY_CHARS:
        content = content[: config.MAX_REPLY_CHARS] + "…（已截断）"
    return content


# ---------- 回复消息 ----------
def send_text(open_id: str, text: str):
    """主动推一条消息给用户（不是回复）。

    执行汇报要用它：多步任务执行中要**实时**告诉用户每一步做了什么，
    而那时候还没法用 reply（消息已经回复过了）。
    """
    try:
        body = (
            CreateMessageRequestBody.builder()
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .msg_type("text")
            .build()
        )
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(body)
            .build()
        )
        api_client.im.v1.message.create(req)
        return True
    except Exception as e:
        log("send_text 失败:", e)
        return False


def reply(message_id: str, text: str):
    """把 text 作为引用回复发回飞书。"""
    body = (
        ReplyMessageRequestBody.builder()
        .content(json.dumps({"text": text}, ensure_ascii=False))
        .msg_type("text")
        .build()
    )
    req = (
        ReplyMessageRequest.builder()
        .message_id(message_id)
        .request_body(body)
        .build()
    )
    resp = api_client.im.v1.message.reply(req)
    if not resp.success():
        log("回复失败:", resp.code, resp.msg)
    else:
        log("已回复(%d 字): %s" % (len(text), text[:40].replace("\n", " ")))


# ---------- 发送图片（截图回传，阶段 5-1） ----------
def _upload_image(path: str) -> str:
    """上传本地图片，返回 image_key。

    ⚠️ 关键：SDK 的 `CreateImageRequestBody.image()` 签名是 `image(IO[Any])`，
    要的是**文件对象**而不是 bytes。实测传 bytes 会报
    `code=234011 Can't recognize image format`（SDK 按流去读内容，读到的是乱字节）。
    所以必须用 open(path, "rb") 传文件句柄。
    """
    body = (
        CreateImageRequestBody.builder()
        .image_type("message")
        .image(open(path, "rb"))      # 文件对象，不是 bytes
        .build()
    )
    req = CreateImageRequest.builder().request_body(body).build()
    resp = api_client.im.v1.image.create(req)
    if not resp.success():
        raise RuntimeError("图片上传失败: code=%s msg=%s" % (resp.code, resp.msg))
    return resp.data.image_key


def send_image(receive_id: str, path: str):
    """把本地图片发到指定会话（receive_id = open_id）。"""
    key = _upload_image(path)
    body = (
        CreateMessageRequestBody.builder()
        .receive_id(receive_id)
        .msg_type("image")
        .content(json.dumps({"image_key": key}, ensure_ascii=False))
        .build()
    )
    req = (CreateMessageRequest.builder()
           .receive_id_type("open_id")
           .request_body(body)
           .build())
    resp = api_client.im.v1.message.create(req)
    if not resp.success():
        raise RuntimeError("图片发送失败: code=%s msg=%s" % (resp.code, resp.msg))
    return True


# ---------- 启动问候语（发到你手机） ----------
def _post_line(title: str, text: str, links=None):
    """构造飞书 post 富文本的一行。"""
    line = {"tag": "text", "text": title}
    if text:
        line["text"] = "%s  %s" % (title, text)
    if links:
        line["href"] = links
    return line


def _post_block(title, lines, color=None):
    """一个带标题的内容块。lines = [字符串, ...]"""
    block = {"elements": [_post_line("", s) for s in lines]}
    b = {"tag": "div", "text": {"tag": "lark_md", "content": title}}
    block["elements"].insert(0, b)
    return block


def send_greeting(receive_id: str):
    """bot 启动成功后给用户手机发一条排版精美的问候语（post 富文本）。

    内容刻意做成"能力清单 + 指令速查"，让你一开机就知道能干什么、怎么用。
    颜色用飞书内置调色板键：blue/green/orange/turquoise/grey。
    """
    post = {
        "zh_cn": {
            "title": "✅ 远程助手已上线",
            "content": [
                # ---- 标题区 ----
                [{"tag": "text", "text": "**Bonsai-2-27B + KVMem** 已在你电脑上跑起来了。"}],
                [{"tag": "text", "text": "对话链路：飞书 ⇄ 本机模型，全程本地闭环，不出公网。",
                  "color": "grey"}],
                [],
                # ---- 能做什么 ----
                [{"tag": "text", "text": "**🧰 现在能做的事**", "color": "blue"}],
                [{"tag": "text", "text": "💬 闲聊问答　　🖼️ 截图回传　　🧠 看图理解（能认界面/小字）"}],
                [{"tag": "text", "text": "🧠 长期记忆（姓名/职业/偏好，跨会话有效）　📊 服务状态查询"}],
                [{"tag": "text", "text": "🛡️ 敏感操作拦截（删除/发送/付款一律先问你）"}],
                [{"tag": "text", "text": "🧩 技能：grill-me（会一次问一个问题帮你盘问方案）", "color": "grey"}],
                [],
                # ---- 指令速查 ----
                [{"tag": "text", "text": "**⌨️ 指令速查**", "color": "green"}],
                [{"tag": "text", "text": "`/think on`", "color": "green"},
                 {"tag": "text", "text": " 开启深度思考（更稳，但 40-90 秒/轮）"}],
                [{"tag": "text", "text": "`/think off`", "color": "green"},
                 {"tag": "text", "text": " 关闭思考（默认，快，约 5-15 秒/轮）"}],
                [{"tag": "text", "text": "`/new`", "color": "green"},
                 {"tag": "text", "text": " 开新对话（清空上下文，历史仍存在本机）"}],
                [{"tag": "text", "text": "`grill me <你的想法>`", "color": "green"},
                 {"tag": "text", "text": " 启动盘问式追问"}],
                [{"tag": "text", "text": "`/images`", "color": "green"},
                 {"tag": "text", "text": " 查图片记忆（你发过的图和我截的屏，重启后仍能找回来）"}],
                [{"tag": "text", "text": "`/mouse`", "color": "green"},
                 {"tag": "text", "text": " 电脑操作层（默认关闭）· `/mouse dry` 先演练不真动"}],
                [{"tag": "text", "text": "**🖱️ 自然语言操作**", "color": "blue"}],
                [{"tag": "text", "text": "开启后直接说「打开画图给我画个圆」「打开微信把桌面上那个方案发给张三」，"
                 "我会一步步汇报着做。**发送/删除/付款会停下来等你确认。**", "color": "grey"}],
                [{"tag": "text", "text": "直接说「截个屏」「你看这个按钮在哪」即可，无需记指令。", "color": "grey"}],
                [],
                # ---- 状态 ----
                [{"tag": "text", "text": "**🔌 服务状态**", "color": "orange"}],
                [{"tag": "text", "text": "模型 18200 · 代理 18201 · 意图分流 18500"}],
                [{"tag": "text", "text": "思考模式：⚡ 已关闭（发 `/think` 可查）", "color": "grey"}],
            ],
        }
    }
    body = (
        CreateMessageRequestBody.builder()
        .receive_id(receive_id)
        .msg_type("post")
        .content(json.dumps(post, ensure_ascii=False))
        .build()
    )
    req = (CreateMessageRequest.builder()
           .receive_id_type("open_id")
           .request_body(body)
           .build())
    resp = api_client.im.v1.message.create(req)
    if not resp.success():
        raise RuntimeError("问候语发送失败: code=%s msg=%s" % (resp.code, resp.msg))
    return True


# ---------- 主人 open_id 持久化（供启动问候语用） ----------
_OWNER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "owner_open_id.txt")


def _remember_owner(open_id: str):
    """记住第一个给我发消息的人（= 主人），后续用于主动推送。"""
    if not open_id:
        return
    try:
        cur = ""
        if os.path.exists(_OWNER_FILE):
            cur = open(_OWNER_FILE, encoding="utf-8").read().strip()
        if cur != open_id:
            with open(_OWNER_FILE, "w", encoding="utf-8") as f:
                f.write(open_id)
            log("已记住你的 open_id（用于启动问候语）")
    except Exception as e:
        log("记住 open_id 失败:", e)


def _get_owner() -> str:
    try:
        if os.path.exists(_OWNER_FILE):
            return open(_OWNER_FILE, encoding="utf-8").read().strip()
    except Exception:
        pass
    return ""


# ---------- Qwen 意图决策层（可选，失败自动降级） ----------
# 2026-10-04 从 Laya 换为 Qwen3.5-0.8B：134 条中文基准上 Laya 决策准确率仅 57%、
# 有 3 条敏感漏判；Qwen 为 91%、**0 条漏判**。详见 qwen/qwen_router.py 顶部说明。
QWEN_URL = "http://127.0.0.1:18500"
QWEN_TIMEOUT = 20         # CPU 推理实测 ~2.1s，留足余量；超时直接放行给 Bonsai
LAYA_ENABLED = os.environ.get("LAYA_ENABLED", "1") != "0"   # 保留旧开关名，勿改（外部脚本在用）


def ask_laya(user_text: str) -> dict:
    """问 Qwen 这条消息的意图。任何异常都返回 abstain，绝不阻断 bot。"""
    if not LAYA_ENABLED:
        return {"intent": None, "abstain": True, "reason": "已关闭"}
    try:
        req = urllib.request.Request(
            QWEN_URL + "/route",
            data=json.dumps({"text": user_text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=QWEN_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"intent": None, "abstain": True,
                "reason": "Qwen 不可用(%s)" % type(e).__name__}


# ---------- 消息处理（后续扩展点） ----------
def handle_text(user_text: str, open_id: str = "") -> str:
    """
    处理一条用户文本，返回要回复的内容。
    流程：本地命令 -> 技能匹配 -> 图像上下文 -> Qwen 意图分流 -> 相应处理。
    这是后续加功能的主入口（电脑操作层就挂在 control_pc 分支上）。

    ⚠️ open_id 必须在 2026-10-04 加上：多步任务执行时要**主动推送**
    逐步进度（"正在执行第2步…"），而 reply() 只能回复一次、且此刻
    消息已经回过了。当时写成 try_natural_task(t, open_id) 但
    handle_text 根本没有这两个变量 → 每次自然语言任务都
    UnboundLocalError: cannot access local variable 't'。
    """
    # 本地命令：清空会话（飞书没有「新对话」按钮，用这条指令代替）
    cmd = user_text.strip().lower().rstrip("。")
    if cmd in ("/new", "/reset", "新对话", "清空对话", "重新开始"):
        try:
            req = urllib.request.Request(
                config.LOCAL_LLM_URL.replace("/v1/chat/completions", "/session/reset"),
                data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=15):
                pass
            return "✅ 已清空对话历史，我们重新开始。（旧记录仍存在本机 history 里）"
        except Exception as e:
            return "⚠️ 清空失败：%s" % e

    # 思考模式开关：/think on | /think off | /think
    # 为什么需要：引擎实测思考占输出 76%、"那好吧"要 86 秒，故默认关闭；
    # 引擎 help 写明 "request can override"，所以这里切换**无需重启引擎**。
    if cmd.startswith("/think") or cmd in ("思考", "思考模式"):
        arg = cmd[len("/think"):].strip() if cmd.startswith("/think") else ""
        base = config.LOCAL_LLM_URL.replace("/v1/chat/completions", "")
        try:
            if arg in ("on", "开", "开启", "打开"):
                req = urllib.request.Request(
                    base + "/think", data=json.dumps({"mode": "on"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST")
            elif arg in ("off", "关", "关闭", "停用"):
                req = urllib.request.Request(
                    base + "/think", data=json.dumps({"mode": "off"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST")
            else:
                # 不带参数 -> 查询当前状态
                req = urllib.request.Request(base + "/think/status", method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                st = json.loads(resp.read().decode("utf-8"))
            on = st.get("on")
            icon = "🧠" if on else "⚡"
            tip = ("回答会先思考再作答，**更稳但更慢**（40-90 秒/轮）"
                   if on else "直接作答，**更快**（5-15 秒/轮）")
            return ("%s 思考模式%s\n\n%s\n\n用 `/think on` 开、 `/think off` 关。"
                    % (icon, "已开启" if on else "已关闭", tip))
        except Exception as e:
            return "⚠️ 思考模式切换失败：%s" % e

    # ---- 图片记忆体检：/images ----
    # 为什么需要这个入口：之前"跨会话能召回"只在同一进程内测过，
    # 而索引里的死路径（shots/ 被清）只降权不报错，从飞书侧完全看不出来。
    # 给你一个能随时自问自答的检查口，把"看起来能用"变成"确实能用"。
    if user_text.strip() in ("/images", "/图片", "/图记"):
        return cmd_images_health()

    # ---- 目标白名单管理（第二道安全闸）----
    if pc_task is not None and re.match(r"^/?(target|目标)\b", user_text.strip(), re.I):
        return cmd_target(user_text)

    # ---- 自然语言多步电脑操作（5-3）----
    # 放在 Qwen 分流之前：这类消息（"打开微信把文件发给张三"）Qwen 会判成
    # control_pc 然后只回一句提示，不会真的去做。
    if _pc is not None and pc_task is not None:
        r = try_natural_task(user_text.strip(), open_id)
        if r is not None:
            return r

    # ---- 鼠标键盘操作层（5-2）：/mouse 开关 + 显式动作指令 ----
    # 放在 Qwen 分流之前：这类消息不该浪费一次推理，
    # 而且 Qwen 会把"点屏幕中央"判成 control_pc 然后回"开发中"。
    if _pc is not None:
        t = user_text.strip()
        if t in ("/mouse", "/鼠标", "/操作层"):
            return cmd_mouse_status()
        if re.match(r"^/mouse\s+(on|off|dry|wet|status)$", t, re.I) or \
           re.match(r"^/鼠标\s*(开|关|演练|状态)$", t):
            return cmd_mouse_toggle(t)
        r = try_pc_action(t)
        if r is not None:
            return r

    # ---- 技能系统：先看是否命中 skill（含 /技能名 显式调用） ----
    sk, sk_text = skill_reg.match_by_command(user_text)
    if not sk:
        sk, sk_text = skill_reg.match(user_text)
    if sk and sk_text:
        if sk.get("sensitive"):
            return ("⚠️ 技能「%s」被标记为敏感，需你明确确认后才会启用。\n"
                    "回复「确认启用 %s」以继续。" % (sk["name"], sk["name"]))
        log("技能命中: %s" % sk["name"])
        return call_llm_with_skill(user_text, sk, sk_text)

    # ---- 图像追问：用户没重发图，只说"把那个窗口关掉"这类话 ----
    # 放在意图分流之前：这类消息需要连同上一张图一起送模型（computer use 关键一环），
    # 若先走分流会被判成 control_pc 而回"开发中"。
    if wants_image_context(user_text):
        log("命中图像上下文追问，带图送模型")
        return handle_image_followup(user_text)

    # ---- 跨会话图片记忆：问"我早上吃的那个/那张菜单图"这类 ----
    # 放在意图分流之前：这类问题 Laya 会判成闲聊或操作，走了就找不到图。
    if wants_image_recall(user_text):
        log("命中图片回忆，去索引里找历史图片")
        return handle_image_recall(user_text)

    # Qwen 决策层分流
    d = ask_laya(user_text)
    intent = d.get("intent")
    if d.get("abstain") or not intent:
        # 弃权/不可用 —— 无感知回退到纯 Bonsai 聊天（最稳的默认路径）
        return call_llm(user_text)

    log("Qwen 判定: %s (conf=%.2f via=%s)" % (d.get("label") or intent,
                                             d.get("confidence", 0), d.get("via", "?")))

    if intent == "sensitive":
        return ("⚠️ 这条被判定为**敏感操作**（%s）。\n"
                "我识别到高危关键词，已暂停执行。\n\n"
                "原因：鼠标键盘操作层**有四道安全闸**，其中「确认闸」规定"
                "涉及删除/发送/上传等关键词的动作必须先经你明确同意。\n"
                "而且当前动作白名单只允许对记事本、文件资源管理器操作，"
                "默认还处于关闭状态——所以这类请求我不会自动执行。\n\n"
                "如果这确实是你要的，请明确说一句「确认执行 XXX」，"
                "我会再走一遍确认流程（仍受白名单和步数熔断约束）。\n"
                "（你的原话：%s）" % (d.get("label") or "敏感操作", user_text[:80]))
    if intent == "control_pc":
        # 5-2 已实现，这里给**真实状态**而不是"开发中"
        st = _pc.status() if _pc is not None else None
        if st and st["mouse_keyboard_enabled"]:
            fg = st["foreground"]
            if fg.get("allowed"):
                return ("🖱️ 识别到你想让我操作电脑：%s\n\n"
                        "操作层已开启，可以直接发固定句式（坐标要明确）：\n"
                        "· `点击 800,600`\n"
                        "· `双击 800,600`\n"
                        "· `移动鼠标到 400,300`\n"
                        "· `上滚 3` / `下滚 3`\n"
                        "· `输入：你好世界`（支持中文）\n\n"
                        "当前前台：「%s」✅ 在白名单\n"
                        "危险动作我会先问你；回车/Tab/Alt 组合键永久拒绝。"
                        % (user_text[:50], fg.get("title") or "(无标题)"))
            return ("🖱️ 操作层已开启，但**当前不能动手**。\n\n"
                    "前台窗口是「%s」（%s），不在白名单 %s 内。\n"
                    "请手动切到记事本或文件资源管理器，我再动手。\n\n"
                    "你的意图我记下了：%s"
                    % (fg.get("title") or "(无标题)", fg.get("proc") or "未知",
                       "、".join(_pc.APP_WHITELIST), user_text[:60]))
        return ("🖱️ 识别到你想让我操作电脑：%s\n\n"
                "操作层**已开发完成但默认关闭**（安全设计）。\n"
                "想开启发 `/mouse on`，想先看演练发 `/mouse dry`，查状态发 `/mouse`。\n\n"
                "开启后能做的：点击/双击/右击、鼠标移动、滚轮、输入中文文本。\n"
                "四道安全闸（总开关/白名单/确认/步数熔断）全程生效，"
                "回车和组合键永久拒绝。\n\n"
                "你的意图：%s" % user_text[:60])
    if intent == "query_status":
        # 状态查询：直接给真实状态，不浪费一次模型推理
        try:
            req = urllib.request.Request(
                config.LOCAL_LLM_URL.replace("/v1/chat/completions", "/session/status"))
            with urllib.request.urlopen(req, timeout=10) as resp:
                st = json.loads(resp.read().decode("utf-8"))
            return ("📊 当前状态：\n"
                    "· 会话轮次：%s\n"
                    "· 长期记忆：%s 条\n"
                    "· 护栏：%s（触发 %s 次）\n"
                    "· 引擎：%s"
                    % (st.get("active_turns"), st.get("memory_items"),
                       "开启" if st.get("guard_on") else "关闭",
                       st.get("guard_fired", 0),
                       "18200 端口在线" if _port_alive(18200) else "未启动"))
        except Exception:
            return call_llm(user_text)
    # chat 或其它：正常聊天
    return call_llm(user_text)


def _port_alive(port: int) -> bool:
    """本机端口是否有人监听（用于状态查询，不发请求）。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.5)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


# ---------- 截图指令（阶段 5-1） ----------
# 用正则而非固定关键词：口语说法太多（截个图/来张图/截一下/拍个屏…），
# 实测踩坑：早先只列 截图/截屏/看看屏幕，用户说"给我电脑现在截个图"没命中、
# 被当成普通聊天回答"操作层开发中"。这里改为模式匹配，cover 常见变体。
SHOT_PATTERNS = [
    # ---- 组1：抓图动作词（截/拍/抓 + 可选量词 + 图|屏|画面|桌面）----
    # 覆盖 截图/截屏/截个图/截个屏/截屏图/拍个屏/抓图/截图一下
    r"(截|拍|抓)\s*[一1]?\s*(个|张|幅|下)?\s*(图|屏|画面|桌面)",
    # 无宾语的省略式：截一下 / 拍一下 / 截个 / 抓一张 …
    # （"抓一张" 后面可能还跟别的字，所以不要求结尾）
    r"(截|拍|抓)\s*(一?下|一?个|一?张|一幅)",

    # ---- 组2：英文 ----
    r"\bscreenshot\b",
    r"\bscreen\s*shot\b",
    r"\bgrab\s*(the\s*)?screen\b",
    r"\bcapture\s*(the\s*)?(screen|window)\b",

    # ---- 组3：「屏幕/桌面/画面」+ 提问或索取动作（组合式，一网打尽）----
    # 覆盖 看看屏幕/看下当前画面/把当前屏幕给我/屏幕上有什么/现在屏幕上显示什么/
    #      桌面现在有什么/看一眼我的屏幕/我电脑现在是什么画面/帮我看下屏幕/发我一张屏幕 …
    # 用「(屏|桌面|画面)…(看|发|给|拉|调|什么|啥|是|显示|内容)」双向组合，
    # 间隔放宽到 12 字（"我电脑现在是什么画面"中间隔了 8 字），漏网自然收敛。
    r"(屏|桌面|画面)[^。？！\?]{0,12}?"
    r"(看|瞧|瞅|发|传|给|回|拉|调|抓|截|拍|打开|显示|有|是|什么|啥|啥样|内容|情况)",

    # ---- 组4：索取一张「屏幕/画面」的量词表达（发我一张图 / 来张图）----
    r"(发|传|给|回|来)\s*[我你]?\s*[一1]?\s*张\s*(图|屏幕|画面|桌面)",

    # ---- 组5：疑问词在前（什么画面 / 什么屏幕 / 啥画面）----
    r"(什么|啥|啥样)\s*(画面|屏幕|桌面)",

    # ---- 组6：动词在前且中间可插"当前/现在/我的"等限定词 ----
    # 覆盖 看看桌面 / 看下当前画面 / 看一眼我的屏幕 / 帮我看下屏幕
    r"(看|瞧|瞅|瞄|扫|打开|调出|拉出)\s*(一?下|一眼|下)?\s*"
    r"(当前|现在|目前的|我的|你的)?\s*(屏|桌面|画面)",
]
SHOT_RE = re.compile("|".join(SHOT_PATTERNS), re.IGNORECASE)

# 排除：这些是在**管理已有的图片文件**（截图文件夹在哪 / 把截图删掉 / 截图改名），
# 不是"抓一张新屏幕"。若不排除，"截"字会误判成抓屏请求。
SHOT_EXCLUDE_RE = re.compile(
    r"(删|删除|移走|移动到|重命名|改名|复制|整理|清空|在哪里|在哪|路径|文件夹|目录)")


def is_shot_request(text: str) -> bool:
    """判断消息是否是「抓一张当前屏幕」的请求（覆盖口语化变体）。"""
    t = (text or "").strip()
    if not t:
        return False
    if not SHOT_RE.search(t):
        return False
    # 命中"截/屏"但同时在操作已有图片文件 -> 不是抓屏请求
    if SHOT_EXCLUDE_RE.search(t):
        return False
    return True


def do_screenshot(open_id: str) -> str:
    """抓屏 → 通过飞书把图片发回用户手机。返回给用户的一句话说明。"""
    try:
        path = pc_screenshot("feishu")
        info = pc_screen_info()
        send_image(open_id, path)
        # 记住**原图**（不是压缩后的）：这样后续追问才能按需选 1024 / 1536 档。
        # 存压缩版会让"看仔细点"失效——压过的图放大只会糊，放不大。
        try:
            with open(path, "rb") as f:
                remember_image(f.read())
        except Exception:
            pass
        # 归档 + 写索引：截图同样要能被跨会话召回。
        # 修复前的缺口：截图只回传、不入索引，所以「我上午截的图里那个报错」
        # 这类问题永远召不回——schema 里有 source=screen_capture，
        # 但 do_screenshot 从来没调用过 image_index.add，那个值是死代码。
        # 这里不跑视觉（用户没问，白跑 40 秒），所以 summary 留空；
        # 召回时走 _answer_by_looking_again 重跑视觉即可答。
        try:
            sys.path.insert(0, CTRL_DIR)
            import image_index
            with open(path, "rb") as f:
                shot_bytes = f.read()
            arc = image_index.archive_path("shot", ".png")
            with open(arc, "wb") as f:
                f.write(shot_bytes)
            image_index.add(arc, "screen_capture", "", "")
            log("截图已归档入索引:", arc)
        except Exception as _e:
            log("截图入索引失败(可忽略):", _e)
        log("截图已回传: %s (%dx%d)" % (path, info.get("width"), info.get("height")))
        return ("🖼️ 已把你电脑的屏幕截图发到这里了（%dx%d）。\n"
                "可以直接接着问「那个窗口在哪」「上面写的什么」，我会带着这张图一起看。\n"
                "要更清楚就加一句「看仔细点」。\n"
                "这张截图我也存下来了，以后说「刚才/早上那张截图」我能找回来。"
                % (info.get("width"), info.get("height")))
    except Exception as e:
        log("截图失败:", e)
        return "⚠️ 截图失败：%s" % e


# ---------- 图片消息处理（让模型"看得见"） ----------
def _download_feishu_image(message_id: str, image_key: str) -> bytes:
    """从飞书下载用户发来的图片，返回原始字节。"""
    from lark_oapi.api.im.v1 import GetMessageResourceRequest
    req = (
        GetMessageResourceRequest.builder()
        .message_id(message_id)
        .file_key(image_key)
        .type("image")
        .build()
    )
    resp = api_client.im.v1.message_resource.get(req)
    if not resp.success():
        raise RuntimeError("下载图片失败: code=%s msg=%s" % (resp.code, resp.msg))
    return resp.data.file   # 已是 bytes


def _shrink_image(data: bytes, max_side=1024, quality=75) -> bytes:
    """把图片压缩成 JPEG（单张内联，不落盘）。

    实测必要性（见记忆 2026-10-04 的对照实验）：
      原图 2560px / 2048px -> 正文 0 字 + 180s 超时（04 文档的 EMPTY 病）
      1536px -> 60s，小字/按钮全对
      1024px -> 40s 最快，界面元素全对
    真正要精准定位小元素时，正确做法是**先看全图找到区域，再裁剪放大**（两阶段视觉）。
    """
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        img.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality)
        return buf.getvalue()
    except Exception:
        return data   # 压缩失败就用原图


# ---------- 精细档触发词（用户明确要"看清楚"时才上 1536） ----------
DETAIL_WORDS = ["看仔细", "仔细看", "看清楚", "看清", "细节", "小字", "文字",
                "读出", "读一下", "写了什么", "上面写", "报错", "参数", "分辨率",
                "放大", "高清", "细节图"]
# ---------- 图像上下文：用户接着上一张图说话 ----------
# 意图词：出现这些说明"他在指我刚发/他刚发的图"，需要把图一起送给模型。
IMAGE_REF_WORDS = ["这张", "那张", "这个图", "上图", "刚才", "刚刚", "上面",
                   "里面", "这个窗口", "那个窗口", "这里", "图里", "图中的",
                   "看图", "照着", "这个", "那个", "它", "界面"]

_last_image = {"data": None, "ts": 0.0, "desc": ""}


def remember_image(data: bytes, desc: str = ""):
    """记住最近一张图（bytes，压缩后的），供后续纯文字追问复用。"""
    _last_image["data"] = data
    _last_image["ts"] = time.time()
    _last_image["desc"] = desc


def _recent_image(max_age=None):
    """取最近一张图；过期或没有则返回 None。"""
    max_age = max_age or config.IMAGE_CONTEXT_TTL
    d = _last_image.get("data")
    if not d:
        return None
    if (time.time() - _last_image.get("ts", 0)) > max_age:
        return None
    return d


def wants_image_context(text: str) -> bool:
    """判断这条纯文字消息是否在追问"刚才那张图"。"""
    t = (text or "").strip()
    if not t or _recent_image() is None:
        return False
    return any(w in t for w in IMAGE_REF_WORDS)


# ---------- 跨会话图片记忆 ----------
# 场景：早上发过菜单照，聊一整天（上下文被压缩），晚上问「我早上吃了什么」。
# 关键词（组合式，不逐句枚举）：时间锚点 + 回忆意图 + 过去指向
RECALL_TIME_WORDS = ["早上", "早晨", "清晨", "中午", "下午", "晚上", "傍晚",
                     "昨天", "昨日", "前天", "上周", "那天", "那次", "上次",
                     "今天", "今日", "之前", "以前", "刚才", "刚刚"]
RECALL_INTENT_WORDS = ["吃了什么", "吃了啥", "喝的什么", "那张图", "那张照片",
                       "那张菜单", "哪张", "什么图", "图里", "图上", "图片",
                       "照片", "菜单", "截图", "回忆", "记得", "早上吃的",
                       "中午吃的", "晚上吃的", "昨天发的", "之前发的",
                       "是什么", "是啥", "叫什么", "写了啥", "有什么", "内容",
                       "报错", "错误", "代码", "数字", "价格", "地址", "电话"]
# 强意图：本身就明确在指某张图，可单独命中
STRONG_RECALL_WORDS = ["那张图", "那张照片", "那张菜单", "那张截图", "哪张",
                       "什么图", "图里", "图上", "回忆", "记得", "之前发的",
                       "昨天发的", "我发的图", "那张图里", "图上写的"]
# 弱意图：含义泛化，必须配合时间锚点才命中（避免"今天天气是什么"误触）
WEAK_RECALL_WORDS = ["吃了什么", "吃了啥", "喝的什么", "图片", "照片", "菜单",
                     "截图", "早上吃的", "中午吃的", "晚上吃的", "是什么",
                     "是啥", "叫什么", "写了啥", "有什么", "内容", "报错",
                     "错误", "代码", "数字", "价格", "地址", "电话"]
# 常识/闲聊类排除词：这些即使含时间锚点也不是在回忆图片
GENERIC_Q_WORDS = ["天气", "气温", "几点", "现在什么", "新闻", "股价", "汇率",
                   "彩票", "比赛", "谁赢", "多少钱一个", "是什么牌子"]


# ---- 自然语言多步任务（5-3）----
# 触发条件：消息里同时出现「操作意图动词」+「不可逆词或图形词」。
# 刻意**不用**"只要提到应用名就接管"——那样"微信怎么发文件"这种
# 咨询问题会被误当成执行指令。所以要求有明确的**动作动词**。
# ⚠️ 动词表必须覆盖自然口语变体。这是今天第三次栽在这上面
# （前两次：截图指令、鼠标移动语序）。
# 漏掉「点开」「发一句」→ 用户最自然的说法直接不触发。
_TASK_VERBS = re.compile(
    r"(打开|开启|启动|运行|点开|双击|画|绘制|"
    r"发给|发到|发送|传给|发个|发一句|发我|发出去|发过来|发个消息|留言|"
    r"删除|删掉|上传|下载|保存|另存|点一下|点击|截图|截屏|"
    r"定位|找|找到|搜索|确定|执行)")
_TASK_NOUNS = re.compile(
    r"(微信|wechat|weixin|画图|画板|绘图|paint|记事本|notepad|"
    r"浏览器|chrome|qq|资源管理器|文件夹|文件|图片|圆|方形|正方|三角|直线|星)")


# 待确认任务：不可逆动作停下后，把"待执行的剩余步骤"存这里，
# 等用户回复「继续」时接着跑。这是"逐步汇报+确认闸"的必要闭环——
# 没有它，用户确认完就没人管了。
#
# ⚠️ 字段命名教训：这里原本用 `at` 同时表达"停在第几步"和"什么时候停的"，
# 结果 `_pending_expiry()` 拿步骤索引去和时间戳比大小，**永远判定过期**，
# 于是「继续」和「取消」都静默失效。现在拆成 `stop_idx` 和 `ts` 两个字段。
_pending_task = {"steps": None, "executor": None,
                 "stop_idx": 0, "ts": 0.0, "desc": ""}
PENDING_TTL = 900      # 15 分钟过期，避免几小时前的任务被误触发


def _pending_expiry():
    """待确认任务是否过期/不存在。过期返回 True。"""
    if not _pending_task["steps"]:
        return True
    if time.time() - _pending_task["ts"] > PENDING_TTL:
        return True
    return False


def _clear_pending():
    _pending_task.update(steps=None, executor=None, stop_idx=0,
                         ts=0.0, desc="")


def _set_pending(ex):
    """记录一个停在不可逆动作前的执行器。"""
    _pending_task.update(steps=ex.steps, executor=ex,
                         stop_idx=ex.stopped_at,
                         ts=time.time(),
                         desc=ex.steps[ex.stopped_at].target_desc)


def cmd_task_continue(user_text: str) -> str:
    """用户确认「继续」后执行剩余步骤。"""
    if _pending_expiry():
        _clear_pending()
        return ("ℹ️ 没有待确认的任务（可能已过期，或上一个任务已经结束了）。\n"
                "要执行新任务直接说就行。")
    ex = _pending_task["executor"]
    steps = _pending_task["steps"]
    start = _pending_task["stop_idx"]
    desc = _pending_task["desc"]

    # 确认后把这一步标记为已确认
    st = steps[start]
    log("用户确认执行不可逆动作: %s" % st.brief())

    # 真正执行
    try:
        if st.action == "stop":
            # `stop` 现在是**纯确认门**，不是可派发动作。
            # 规划器已把不可逆步骤放在第一步（见 pc_planner 的注释），
            # 这样任何后续步骤失败都不会把确认门藏起来。
            # 所以这里不点任何东西，直接进入剩余步骤。
            rest = steps[start + 1:]
            if not rest:
                _clear_pending()
                return ("✅ 已确认：%s\n\n后面没有其他步骤了，任务结束。"
                        % (desc or st.target_desc))
            ex2 = pc_task.Executor(rest)
            done, report = ex2.run()
            if (not ex2.rejected) and ex2.stopped_at is not None and \
                    ex2.steps[ex2.stopped_at].irreversible:
                _set_pending(ex2)
                report += "\n\n回复「**继续**」我就执行下一步。"
            else:
                _clear_pending()
            return "✅ 已确认：%s\n\n%s" % (desc or st.target_desc, report)
        if st.action == "click":
            _pc.click(st.x, st.y, confirmed=True, label=st.target_desc)
        elif st.action == "type":
            _pc.type_text(st.text or "", confirmed=True, label=st.target_desc)
        elif st.action == "key":
            _pc.tap(st.keys, confirmed=True)
        elif st.action == "drag":
            _pc.drag(st.x, st.y, st.x2, st.y2, confirmed=True,
                     label=st.target_desc)
        else:
            return "⚠️ 这一步（%s）我没法在确认后执行，已停下。" % st.brief()
    except _pc.SafetyError as e:
        return "🛡️ 仍被安全闸拒绝：%s" % e
    except Exception as e:
        return "⚠️ 执行出错：%s" % e

    # click/type/key/drag 这类可派发步骤执行完就算完成
    # （send 任务的这些步骤都在 stop 门之后，已在上面 early return 处理）
    _clear_pending()
    return "✅ 已执行：%s\n\n任务结束。" % (desc or st.target_desc)


# ---- 上一句任务上下文（让多轮追问能接上）----
# ⚠️ 2026-10-04 用户真机测试暴露的**根本缺陷**：
#   用户说「打开微信」失败后追问「那你定位啊」，bot 完全没听懂，
#   因为 `try_natural_task` 是**无状态的**——每次只看当前这一句话。
#   而「那你定位啊」是**接着上一句**说的，它省略了宾语（定位什么）。
#
# 这不是加个"定位"关键词能解决的——**多步对话必须有上下文**。
# 最小实现：记住上一次的任务/意图，追问时补全省略成分。
_LAST_TASK = {"text": "", "kind": "", "app": "", "recipient": "",
              "ts": 0.0, "steps": None}
_TASK_CTX_TTL = 300      # 5 分钟内算同一轮对话


def _task_ctx_alive():
    return (time.time() - _LAST_TASK["ts"]) < _TASK_CTX_TTL


def _remember_task(text, meta, steps=None):
    _LAST_TASK.update({
        "text": text, "kind": meta.get("kind", ""),
        "app": meta.get("app", ""), "recipient": meta.get("recipient", ""),
        "ts": time.time(), "steps": steps,
    })


# 追问词：单独出现时指代上一句的任务（宾语被省略）
_FOLLOWUP_RE = re.compile(
    r"^(?:那你|你|您|那|然后|接着|继续|再|please)?\s*"
    r"(定位|找|找到|搜索|搜一下|打开|启动|运行|点击|点一下|双击|"
    r"确定|执行|做|干|弄|处理|试|试下|"
    r"where|find|locate|click|open|do|go|try)\s*"
    r"(?:它|他|她|这个|那个|一下|下|啊|呀|吧|呢|了|一下下)?\s*"
    r"(?:啊|呀|吧|呢|谢谢|thanks|please)?[。！？!?]?$", re.I)


def _is_followup(t):
    """是否是对上一句任务的追问（含省略宾语的口语）。"""
    if not _task_ctx_alive():
        return False
    s = (t or "").strip()
    if not s or len(s) > 14:
        return False
    return bool(_FOLLOWUP_RE.match(s))


def _resolve_followup(t):
    """把追问补全成完整指令。上下文过期返回 None。

    ⚠️ 必须在这里**也**检查过期，不能只靠 `_is_followup`：
    `_is_followup` 是"是否像追问"的判断，调用方可能没先调它
    （测试就是直接调的）。缺了这道检查 -> 半小时前的任务会被
    突然"想起来"，拿旧上下文去执行。
    """
    if not _task_ctx_alive():
        return None
    ctx = _LAST_TASK.get("text", "")
    if not ctx:
        return None
    m = _FOLLOWUP_RE.match(t.strip())
    act = m.group(1) if m else "继续"
    app = _LAST_TASK.get("app", "") or ""
    # 上一句已识别出应用 -> 把追问接到那个应用上
    if app and app not in ("未知", "None", ""):
        merged = "%s %s" % (app, act)
    else:
        merged = "%s %s" % (ctx, act)
    log("追问补全: %r -> %r (上下文: %r)" % (t, merged, ctx[:30]))
    return merged


def cmd_target(user_text: str) -> str:
    """`/target` —— 管理不可逆操作的目标白名单。

    这是**第二道安全闸**（第一道是"停下确认"）。
    为什么要两道：
      「停下来问你」仍然可能是错的——模型把张三认成李四，
      你确认了"发给李四"但实际发给张三。
    白名单在确认之前就拦：**未授权的目标连问都不问，直接拒绝。**

    ⚠️ 默认空名单 = 全部拒绝。授权必须显式添加，且随时可撤销。
    """
    if pc_task is None:
        return "⚠️ 任务模块没加载成功：%s" % _task_import_err
    t = (user_text or "").strip()
    m = re.match(r"^/?target\s+(add|del|list)\s*(.*)$", t, re.I)
    if not m:
        lst = pc_task.target_list()
        lines = ["🎯 **不可逆操作目标白名单**", ""]
        if not lst:
            lines.append("当前**空**。空名单意味着**所有**发送/删除/付款"
                         "类任务都会被拒绝。")
        else:
            lines.append("已授权 %d 个：" % len(lst))
            for a in lst:
                lines.append("· **%s**  （%s，%s）"
                             % (a["name"], a.get("at", "?"),
                                a.get("reason") or "未注明原因"))
        lines.append("")
        lines.append("指令：")
        lines.append("`/target add 文件传输助手` 授权一个目标")
        lines.append("`/target del 文件传输助手` 撤销授权")
        lines.append("`/target list` 看当前列表")
        lines.append("")
        lines.append("注意：即使目标已授权，**执行前我仍会停下来问你**"
                     "（除非是明确测试场景）。")
        return "\n".join(lines)

    act, arg = m.group(1).lower(), m.group(2).strip()
    if act == "add":
        if not arg:
            return "❌ 要授权谁？例如：`/target add 文件传输助手`"
        # 只允许授权单个名字，不接受通配
        if len(arg) > 30 or any(c in arg for c in "*?/"):
            return "❌ 只能授权单个明确的名字，不能用通配符。"
        pc_task.add_target(arg, reason="用户通过 /target add 授权")
        return ("✅ 已授权「%s」。\n\n"
                "现在我**可以**对它执行发送类操作，但**每次执行前仍会停下来"
                "问你确认**（你要的是日常使用一律停）。\n"
                "撤销：`/target del %s`" % (arg, arg))
    if act == "del":
        if not arg:
            return "❌ 要撤销哪个？例如：`/target del 文件传输助手`"
        ok = pc_task.remove_target(arg)
        return ("✅ 已撤销「%s」。" % arg) if ok else \
               ("ℹ️ 「%s」本来就不在白名单里。" % arg)
    lst = pc_task.target_list()
    if not lst:
        return "当前白名单为空（所有发送类任务都会被拒绝）。"
    return "当前授权：" + "、".join(a["name"] for a in lst)


def try_natural_task(t: str, open_id: str = ""):
    """尝试把消息当自然语言多步任务处理。返回 None 表示不是。"""
    if pc_task is None or pc_planner is None:
        return None
    s = (t or "").strip()
    if not s or s.startswith("/"):
        return None
    # 「继续」—— 执行被不可逆闸拦下后的下一步
    if s in ("继续", "确认", "确认继续", "执行", "go", "continue"):
        if not _pending_expiry():
            return cmd_task_continue(s)
        return None
    # 「取消」—— 放弃待确认任务
    if s in ("取消", "算了", "不做了", "停", "cancel"):
        if not _pending_expiry():
            _clear_pending()
            return "🛑 已取消待确认任务，什么都没做。"
        return None
    # ---- 追问补全（必须在动词/名词检查**之前**）----
    # 「那你定位啊」这类省略宾语的句子，动词表里根本没有"定位"，
    # 直接往下走必然漏。所以先看它是不是对上一句的追问。
    if not (_TASK_VERBS.search(s) and _TASK_NOUNS.search(s)):
        if _is_followup(s):
            merged = _resolve_followup(s)
            if merged:
                s = merged
        else:
            return None
    # 必须同时有「动作动词」和「操作对象」，避免误伤咨询类提问
    if not (_TASK_VERBS.search(s) and _TASK_NOUNS.search(s)):
        return None
    # 明显是问句/咨询的走正常聊天。
    # ⚠️ 「请问微信在哪里下载」这类**开头有礼节词**的问句曾误触，
    # 因为只查了句尾问号和疑问词，漏了"请问/我想问/求助"这种前缀。
    # 误触的代价是"我以为你要发消息，结果它开始点微信"——不可接受。
    if s.endswith("？") or s.endswith("?") or \
            re.search(r"(怎么|如何|为什么|是什么|哪里|哪儿|能不能|可以吗|"
                      r"教我|请问|请问我|想问|求助|告诉我|解释|推荐|哪个好)", s):
        return None

    # 操作层必须开着，否则明确告诉用户怎么开（不静默失败）
    if not _pc.ENABLE_MOUSE_KEYBOARD:
        return ("🖱️ 识别到你想让我操作电脑：%s\n\n"
                "但**操作层是关闭的**（安全默认）。\n"
                "想开启发 `/mouse on`，想先看演练发 `/mouse dry`。" % s[:50])

    # 先规划（纯规则，零延迟）
    steps, meta = pc_planner.plan(s)
    if not steps:
        return None
    if meta["kind"] == "unknown":
        return None      # 认不出来，交给 Qwen 分流当普通聊天
    # 记住这次任务，供下一轮追问补全（见 _resolve_followup）
    _remember_task(t, meta, steps)

    # 认出来了但只是"打开应用"且不在白名单 → 明确拒绝
    app = meta.get("app") or ""
    if meta["kind"] == "open_app" and steps[0].text not in _pc.APP_WHITELIST:
        return ("🚫 不能操作「%s」。\n\n"
                "当前白名单只有：%s\n"
                "要加的话得改 `pc_operator.APP_WHITELIST`——"
                "因为白名单意味着「我可能真的会动这个程序」。"
                % (app, "、".join(_pc.APP_WHITELIST)))

    # 白名单预检：未授权时**不承诺执行**。
    # 之前无论能否执行都先发「执行计划 + 正在执行…」，然后才拒绝——
    # 那是**虚假的承诺**（说着"正在执行"却一个动作都没做），会让人困惑。
    _blocked = [s for s in steps if s.irreversible
                and not pc_task.target_allowed(s.target)]

    if not _blocked:
        # 逐步汇报模式：先发计划
        plan_lines = ["📋 **执行计划**（%s）" % meta.get("note", "")]
        for i, st in enumerate(steps, 1):
            mark = "⏸" if st.irreversible else "·"
            plan_lines.append("%s 第%d步 %s" % (mark, i, st.brief()))
        if meta["need_confirm"]:
            plan_lines.append("")
            plan_lines.append("⚠️ 其中包含**不可逆动作**，我会停在那一步等你确认。")
        plan_lines.append("")
        plan_lines.append("_正在执行…_")
        if open_id:
            try:
                send_text(open_id, "\n".join(plan_lines))
            except Exception as e:
                log("计划推送失败(继续执行):", e)

    # 执行（带实时汇报回调）
    def _progress(msg):
        if not open_id:
            return
        # 预检阶段的 rejected 消息**不推送**。
        # 它的 action 文本是 "⏸ 停下等你确认：…"，而实际语义是「已拒绝」——
        # 两条消息矛盾会让人以为还能「继续」。拒绝的完整说明由最终
        # report() 那一条给出，不要在这里重复。
        st0 = msg["status"].split(":")[0]
        if st0 == "rejected":
            return
        try:
            icon = {"ok": "✅", "fail": "❌", "stopped": "⏸",
                    "doing": "▶️"}.get(st0, "·")
            send_text(open_id, "%s [%d/%d] %s%s"
                      % (icon, msg["i"], msg["n"], msg["action"],
                         ("  —— " + msg["status"].split(": ", 1)[-1])
                         if ": " in msg["status"] else ""))
        except Exception:
            pass

    ex = pc_task.Executor(steps, on_progress=_progress)
    done, report = ex.run()
    # ⚠️ 必须排除 rejected：被白名单**拒绝**和停下等**确认**是两件事。
    # 拒绝是终局（用户要改授权才行），确认是暂停（等「继续」）。
    # 混在一起会让"拒绝"的任务进入待确认队列，回「继续」就绕过白名单了。
    if (not ex.rejected) and ex.stopped_at is not None \
            and ex.steps[ex.stopped_at].irreversible:
        _set_pending(ex)
        report += ("\n\n回复「**继续**」我就执行这一步；"
                   "不回复就一直停在这里。")
    return report


def cmd_mouse_status() -> str:
    """`/mouse` —— 查操作层状态。"""
    if _pc is None:
        return "⚠️ 操作层没加载成功：%s" % _pc_import_err
    st = _pc.status()
    fg = st["foreground"]
    if not st["mouse_keyboard_enabled"]:
        head = "🖱️ **鼠标键盘操作层：已关闭**（安全默认）\n\n"
    elif st.get("dry_run"):
        head = "🖱️ **鼠标键盘操作层：已开启（演练模式）**\n\n动作只记录不执行。\n\n"
    else:
        head = "🖱️ **鼠标键盘操作层：已开启** ⚠️ 会真的动你的电脑\n\n"

    lines = [head]
    lines.append("· 屏幕：%s" % st["screen"])
    lines.append("· 当前前台窗口：「%s」（%s）%s"
                 % (fg.get("title") or "(无标题)", fg.get("proc") or "未知",
                    "✅ 在白名单" if fg.get("allowed") else "❌ 不在白名单"))
    lines.append("· 白名单应用：%s" % "、".join(st["app_whitelist"]))
    lines.append("· 允许按键：%s" % "、".join(st["allowed_keys"]))
    lines.append("· 单任务步数上限：%d" % st["max_steps"])
    lines.append("· 需确认的关键词：%s" % "、".join(st["confirm_keywords"]))
    if st.get("dry_run"):
        lines.append("· 演练已记录动作：%d 条" % st.get("dry_log_count", 0))
    lines.append("")
    if not fg.get("allowed"):
        lines.append("⚠️ 现在**不能操作**——请先手动切到记事本或文件资源管理器。")
    lines.append("开关：`/mouse on` 开启 · `/mouse off` 关闭 · "
                 "`/mouse dry` 演练（只记录不执行）")
    return "\n".join(lines)


def cmd_mouse_toggle(t: str) -> str:
    """`/mouse on|off|dry|wet|status` —— 切换操作层开关。"""
    if _pc is None:
        return "⚠️ 操作层没加载成功：%s" % _pc_import_err
    m = re.search(r"(on|off|dry|wet|status|开|关|演练|状态)", t, re.I)
    if not m:
        return cmd_mouse_status()
    arg = m.group(1).lower()

    if arg in ("status", "状态"):
        return cmd_mouse_status()
    if arg in ("off", "关"):
        _pc.ENABLE_MOUSE_KEYBOARD = False
        _pc.set_dry_run(False)
        return "🖱️ 已**关闭**鼠标键盘操作层。聊天、截图、看图都不受影响。"
    if arg in ("dry", "演练"):
        _pc.ENABLE_MOUSE_KEYBOARD = True
        _pc.set_dry_run(True)
        return ("🖱️ 已开启**演练模式**（dry-run）。\n\n"
                "我会照常过所有安全闸、照常解析你的指令，但**一个真实动作都不会发**——"
                "只记在日志里。你可以用 `/mouse status` 看记录了多少条。\n\n"
                "确认没问题后发 `/mouse on` 进入真实模式。")
    if arg in ("wet", "on", "开"):
        fg = _pc.focus_check()
        _pc.ENABLE_MOUSE_KEYBOARD = True
        _pc.set_dry_run(False)
        warn = ""
        if not fg.get("allowed"):
            warn = ("\n\n⚠️ 注意：当前前台是「%s」，不在白名单 %s 内，"
                    "所以**现在发指令也会被拒绝**。请先手动切到记事本或文件资源管理器。"
                    % (fg.get("title") or "(无标题)", _pc.APP_WHITELIST))
        return ("🖱️ 已开启**真实模式** ⚠️\n\n"
                "从现在开始我发的鼠标键盘动作会**真的作用在你电脑上**。\n\n"
                "安全边界（已生效，无法绕过）：\n"
                "· 只能对白名单 %s 的**前台窗口**操作\n"
                "· 危险动作（%s…）必须你回「确认执行…」\n"
                "· 单任务超过 %d 步自动熔断\n"
                "· 回车/Tab/Alt 组合键**永久拒绝**（要单独授权）\n\n"
                "不放心就发 `/mouse dry` 切回演练，或 `/mouse off` 直接关。"
                % ("、".join(_pc.APP_WHITELIST),
                   "、".join(_pc.CONFIRM_REQUIRED[:4]), _pc.MAX_STEPS) + warn)
    return cmd_mouse_status()


# ---- 显式动作指令（严格白名单，不做自然语言理解）----
# 设计取舍：5-2 **刻意不做**"用户随便怎么说都能操作"。
# 原因是自然语言→坐标的映射必然有歧义，而误点的代价是真实的。
# 所以这里只认少数几种**参数明确的固定句式**，其余一律不执行。
# 等 Qwen 分流验证稳定后，再考虑让模型输出结构化动作 JSON。
#
# ⚠️ 语序教训（踩过）：正则必须同时接受「动词+名词」和「名词+动词」两种语序。
# 第一版写成 `鼠标移动到 x,y`，结果用户说「移动鼠标到 400,300」直接不匹配——
# 因为中文里动词在前更自然。枚举语序是这类 bug 的主要来源，
# 所以统一用 `[^0-9]{0,6}` 之类的间隔容错，而不是逐条补语序。
_NUM = r"(\d+)\s*[,，]\s*(\d+)"          # x,y 坐标
_G = r"[^0-9]{0,6}"                       # 短间隔容错（容忍修饰词/助词）
_PC_ACTION_RES = [
    # 鼠标移动：移动鼠标到 / 鼠标移动到 / 把鼠标移到 都能命中
    (re.compile(r"^(?:把)?(?:鼠标)?(?:移动|移|挪)(?:动|到|至|过去)?" + _G
                + _NUM + r"(?:$|[^\d])"), "move"),
    # 点击 / 单击 / 点一下：动词在前
    (re.compile(r"^(?:请)?(?:点|点击|单击|戳)(?:击|一下|下)?" + _G
                + _NUM + r"(?:$|[^\d])"), "click"),
    (re.compile(r"^(?:请)?双击" + _G + _NUM + r"(?:$|[^\d])"), "dblclick"),
    (re.compile(r"^(?:请)?(?:右击|右键(?:点击|点)?)" + _G + _NUM + r"(?:$|[^\d])"),
     "rightclick"),
    (re.compile(r"^(?:请)?(?:向|往)?上(?:滚|滑动)\s*(\d+)"), "scrollup"),
    (re.compile(r"^(?:请)?(?:向|往)?下(?:滚|滑动)\s*(\d+)"), "scrolldown"),
    # 输入文本。放在最后（最宽松），但仍要求有明确分隔符或足够长的内容。
    (re.compile(r"^(?:请)?(?:在[^0-9]{0,8})?(?:输入|输入文本|打字|键入)"
                r"\s*[:：]?\s*(.{1,200})$"), "type"),
]


def try_pc_action(text: str):
    """尝试把消息解析成一个显式操作指令。

    返回 None 表示"不是操作指令"，交给后续流程。
    返回字符串表示已处理（成功或拒绝原因）。
    """
    if _pc is None:
        return None
    t = (text or "").strip()
    if not t or t.startswith("/"):
        return None
    for rx, act in _PC_ACTION_RES:
        m = rx.match(t)
        if not m:
            continue
        return _exec_pc_action(act, m)
    return None


def _exec_pc_action(act: str, m) -> str:
    """执行一个已解析的动作。安全异常原样转达，不重试。"""
    try:
        # move/click/dblclick/rightclick 的坐标是 group(1), group(2)
        if act in ("move", "click", "dblclick", "rightclick"):
            x, y = int(m.group(1)), int(m.group(2))
            if act == "move":
                _pc.move(x, y)
                return "✅ 鼠标已移到 (%d, %d)" % (x, y)
            if act == "click":
                _pc.click(x, y)
                return "✅ 已点击 (%d, %d)" % (x, y)
            if act == "dblclick":
                _pc.click(x, y, clicks=2)
                return "✅ 已双击 (%d, %d)" % (x, y)
            _pc.click(x, y, button="right")
            return "✅ 已右击 (%d, %d)" % (x, y)
        if act == "scrollup":
            n = int(m.group(1))
            _pc.scroll(n)
            return "✅ 已向上滚动 %d 格" % n
        if act == "scrolldown":
            n = int(m.group(1))
            _pc.scroll(-n)
            return "✅ 已向下滚动 %d 格" % n
        if act == "type":
            content = m.group(1).strip()
            if not content:
                return "❌ 没说输入什么。用法：输入：你好世界"
            _pc.type_text(content)
            return "✅ 已输入「%s」" % content[:40]
    except _pc.SafetyError as e:
        # 安全拒绝：原样转达，不重试、不绕过
        return "🛡️ 已拒绝：%s" % e
    except Exception as e:
        return "⚠️ 操作失败：%s" % e
    return None


def cmd_images_health() -> str:
    """`/images` —— 跨会话图片记忆体检。

    直接读盘上的索引和原图，不问模型（省 5-15 秒，且答案确定）。
    """
    try:
        sys.path.insert(0, CTRL_DIR)
        import image_index
        h = image_index.health()
    except Exception as e:
        return "⚠️ 图片记忆体检失败：%s" % e

    if not h["total"]:
        return ("🖼️ 图片记忆：目前是空的。\n\n"
                "你发过的图片和截图会自动归档并记进索引，之后即使重启也能用"
                "「上次那张图」「今天下午那张截图」找回来。\n"
                "（归档目录：%s）" % image_index.ARCHIVE_ROOT)

    lines = ["🖼️ **图片记忆体检**", ""]
    lines.append("· 索引条数：%d 张" % h["total"])
    lines.append("· 原图还在：%d 张" % h["alive"])
    if h["dead"]:
        lines.append("· ⚠️ 原图丢失：%d 张（这些只能凭摘要回答，"
                     "要细节得重发）" % h["dead"])
    lines.append("· 有文字摘要：%d 张（这些能秒答，不用重跑视觉）" % h["with_summary"])
    lines.append("· 占用空间：%.2f MB" % h["alive_mb"])
    if "span" in h:
        lines.append("· 覆盖时间：%s" % h["span"])
        bs = h.get("by_source") or {}
        if bs:
            lines.append("· 来源分布：%s" % "、".join(
                "%s %d" % ({"user_sent": "你发的", "screen_capture": "我截的"}
                           .get(k, k), v) for k, v in bs.items()))
    if h["last"]:
        lines.append("")
        lines.append("最近一张：**%s %s**（%s）"
                     % (h["last"]["date"], h["last"]["time"],
                        {"user_sent": "你发的", "screen_capture": "我截的"}
                        .get(h["last"]["source"], h["last"]["source"])))
        if h["last"]["summary"]:
            lines.append("摘要：%s" % h["last"]["summary"])
        else:
            lines.append("（无摘要，提问时我会重看原图，约 40 秒）")
    lines.append("")
    lines.append("结论：%s" % ("✅ 可用" if h["ok"] else "❌ 不可用"))
    if h["ok"]:
        lines.append("直接说「上次那张图里写了什么」就能召回。")
    return "\n".join(lines)


def _image_index_ready() -> bool:
    """图片索引是否可用（模块能 import 且有记录）。"""
    try:
        sys.path.insert(0, CTRL_DIR)
        import image_index
        return image_index.stats()["total"] > 0
    except Exception:
        return False


def wants_image_recall(text: str) -> bool:
    """判断是否在跨会话回忆某张历史图片。

    两条命中路径，避免误触：
      ① 强意图词（那张图/那张菜单/回忆/记得…）——直接命中
      ② 弱意图词（是什么/有什么…）——必须**同时**有时间锚点，
         否则"今天天气怎么样是什么"之类会被误判。
    """
    t = (text or "").strip()
    if not t or _recent_image() is not None:
        return False        # 手里有刚发的图 -> 走图像上下文追问，不用查索引
    if not _image_index_ready():
        return False
    if any(w in t for w in STRONG_RECALL_WORDS):
        return True
    # 排除常识/闲聊类问题（"今天天气是什么"含 时间锚点+是什么，但不是回忆图片）
    if any(w in t for w in GENERIC_Q_WORDS):
        return False
    has_time = any(w in t for w in RECALL_TIME_WORDS)
    has_weak = any(w in t for w in WEAK_RECALL_WORDS)
    return has_time and has_weak


def handle_image_recall(user_text: str) -> str:
    """跨会话图片回忆：**先读索引摘要秒答**，答不了再问要不要看原图。

    两级应答（这是本功能的核心价值 —— 避免每次追问都等 40 秒视觉）：
      ① 索引里已有摘要 -> 直接把它当上下文送模型作答（约 5-15 秒，一次普通推理）
      ② 摘要明显不够（用户问的是"具体细节/我没说过的东西"）-> 提示并重跑视觉
    """
    try:
        sys.path.insert(0, CTRL_DIR)
        import image_recall
        cands = image_recall.recall(user_text, limit=3)
        if not cands:
            return ""
    except Exception as e:
        log("图片回忆检索失败(可忽略):", e)
        return ""

    top = cands[0]
    rec = top["rec"]
    log("图片回忆命中: %s %s score=%.2f" % (rec.get("date"), rec.get("time"),
                                            top["score"]))

    # ---- 情况1：只有一个候选，直接把它当上下文 ----
    if len(cands) == 1 or (cands[1]["score"] < top["score"] - 0.15):
        ctx_lines = [
            "【你之前发过的一张图片（%s %s）】" % (rec.get("date"), rec.get("time")),
        ]
        if rec.get("said"):
            ctx_lines.append("你当时说：%s" % rec["said"])
        if rec.get("summary"):
            ctx_lines.append("图里内容（我当时的记录）：%s" % rec["summary"])
        else:
            ctx_lines.append("（当时没留下文字记录）")
        if len(cands) > 1:
            ctx_lines.append("（另有 %d 张较不像的图：%s）"
                             % (len(cands) - 1,
                                "、".join("%s %s" % (c["rec"].get("date"),
                                                     c["rec"].get("time"))
                                          for c in cands[1:])))
        q = ("用户在对话中问：「%s」\n\n%s\n\n"
             "请只根据上面的记录回答，并**明确区分**「记录里有的」和「需要重看图片才知道的」。"
             "如果问题需要看图细节（如图上位置、数量对比），请直接说「需要重看原图」，"
             "我会再跑一次视觉识别。"
             % (user_text, "\n".join(ctx_lines)))
        ans = call_llm(q)
        # 模型说需要重看原图 -> 直接重跑视觉（省得用户再发一次指令）
        if isinstance(ans, str) and "需要重看" in ans and top.get("alive"):
            log("模型判定需要重看原图，自动重跑视觉")
            return _answer_by_looking_again(user_text, rec)
        return ans

    # ---- 情况2：多张候选接近 -> 列出让用户选（零歧义）----
    lines = ["我按你的描述找到 %d 张差不多的图，你说的是哪张？" % len(cands)]
    for i, c in enumerate(cands, 1):
        r = c["rec"]
        lines.append("%d) %s %s —— %s"
                     % (i, r.get("date"), r.get("time"),
                        (r.get("summary") or r.get("said") or "无记录")[:40]))
    lines.append("回数字就行，我会用那张图来回答。")
    return "\n".join(lines)


def _answer_by_looking_again(user_text: str, rec) -> str:
    """拿历史原图重跑一次视觉来回答问题（慢档，约 40 秒）。"""
    path = rec.get("path", "")
    if not path or not os.path.exists(path):
        return "⚠️ 那张图的原文件已经不在了，我只能凭当时的记录回答。"
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception as e:
        return "⚠️ 读取原图失败：%s" % e
    side = config.IMAGE_SIDE_FAST
    small = _shrink_image(data, side, config.IMAGE_QUALITY)
    b64 = base64.b64encode(small).decode("ascii")
    when = "%s %s" % (rec.get("date", ""), rec.get("time", ""))
    q = ("这是你在 %s 发给我的一张图。现在回答这个问题：「%s」\n"
         "请针对这个问题作答，说清图里相关的细节。" % (when, user_text))
    payload = {
        "model": config.LOCAL_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": q},
            {"type": "image_url",
             "image_url": {"url": "data:image/jpeg;base64,%s" % b64}}]}],
        "stream": False,
    }
    req = urllib.request.Request(
        config.LOCAL_LLM_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            obj = json.loads(resp.read().decode("utf-8", errors="replace"))
        content = (obj["choices"][0]["message"].get("content") or "").strip()
        if not content:
            return "（这次没看出什么，可以说得更具体点吗）"
        return "🖼️ 重看 %s 的那张图：\n\n%s" % (when, content)
    except Exception as e:
        return "⚠️ 重看图片失败：%s" % e


def wants_detail(text: str) -> bool:
    t = (text or "").strip()
    return any(w in t for w in DETAIL_WORDS)


def handle_image_message(msg, open_id: str) -> str:
    """处理用户发来的图片：下载 -> 压缩 -> 交给 Bonsai 视觉理解。

    这里是"看着屏幕干活"的关键第一步：模型能看见界面，才能决定下一步点哪里。
    Bonsai 走的是旧 KVMem 栈（带 mmproj 视觉投影器），支持看图。
    """
    # 1) 从 content 取 image_key
    try:
        info = json.loads(msg.content)
        image_key = info.get("image_key") or ""
    except Exception:
        image_key = ""
    if not image_key:
        return "（没能从这条图片消息里取到图片内容，麻烦重发一次。）"

    # 2) 下载
    data = _download_feishu_image(msg.message_id, image_key)
    if not data:
        return "（图片下载下来是空的，麻烦重发一次。）"
    log("已下载用户图片 %d bytes" % len(data))

    # 3) 存**原图**到归档区（memory/archive/YYYY-MM-DD/），并写进图片索引。
    #    存原图而非压缩版：后续"重看原图"要用，否则精细档拿到的还是糊图。
    #    ⚠️ 不存 shots/：那是临时截图工作区，被清掉索引就变死路径。
    _img_ts = time.time()
    _img_path = ""
    try:
        sys.path.insert(0, CTRL_DIR)
        import image_index
        _img_path = image_index.archive_path("recv", ".jpg", _img_ts)
        with open(_img_path, "wb") as f:
            f.write(data)
        log("用户图片已归档:", _img_path)
        # 写索引（时间+路径+用户配文，摘要稍后补）
        image_index.add(_img_path, "user_sent", "", "")
    except Exception as _e:
        log("写图片索引失败(可忽略):", _e)

    # 4) 压缩后再送模型（大图会让正文 0 字并超时）
    #    分档：默认 1024（快、界面元素准）。用户想要更清楚时，
    #    直接再发一条"看仔细点"——会带上**原图**用 1536 重看一次。
    #    ⚠️ 必须 remember 原始 bytes 而不是压缩后的，否则"看仔细点"是假的
    #    （压过的图只能缩小不能放大，1536 档拿到的还是 1024 的清晰度）。
    side = config.IMAGE_SIDE_FAST
    small = _shrink_image(data, side, config.IMAGE_QUALITY)
    log("图片压缩: %d -> %d bytes (side=%d)" % (len(data), len(small), side))

    # 记住**原图**：之后你直接发文字"把那个窗口关掉"也能引用它
    remember_image(data)
    b64 = base64.b64encode(small).decode("ascii")

    # 5) 交给 Bonsai 视觉：OpenAI 兼容的 image_url data URI
    #    提示词刻意要求"简洁"，因为这份回答会拆成两部分：
    #      ① 前 200 字 -> 存进图片索引作为**图内文字摘要**（供跨会话回忆秒答）
    #      ② 剩下的   -> 作为给用户看的详细描述
    payload = {
        "model": config.LOCAL_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text":
                    "请分两段回答，不要写开场白。\n"
                    "【摘要】第一段：用**不超过 80 字**写出图里的关键信息"
                    "（如：菜单上有哪些菜 / 报错代码是什么 / 这是什么界面）。\n"
                    "【详情】第二段：再补充说明这是电脑界面还是照片、"
                    "有哪些窗口/按钮/图标、文字具体写了什么。"},
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64,%s" % b64}},
            ],
        }],
        "stream": False,
    }
    req = urllib.request.Request(
        config.LOCAL_LLM_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=config.LLM_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        obj = json.loads(raw)
        content = (obj["choices"][0]["message"].get("content") or "").strip()
        if not content:
            content = "（模型看到了图片但没给出描述，可能视觉能力受限。你可以描述图片内容让我帮你处理。）"
    except urllib.error.URLError as e:
        return "⚠️ 调用模型失败：%s" % e
    except Exception as e:
        return "⚠️ 图片理解出错：%s" % e

    if len(content) > config.MAX_REPLY_CHARS:
        content = content[: config.MAX_REPLY_CHARS] + "…（已截断）"

    # ---- 拆出【摘要】写回图片索引（跨会话图片记忆的关键）----
    # 这次视觉推理本来就要跑，拆摘要的边际成本≈0。有了它，晚上问
    # 「我早上吃了什么」可以直接读索引秒答，不必重跑视觉。
    try:
        summary, detail = _split_summary(content)
        if summary:
            sys.path.insert(0, CTRL_DIR)
            import image_index
            if image_index.update_summary(_img_ts, summary):
                log("图片摘要已入索引:", summary[:40])
            content = detail or content
    except Exception as _e:
        log("写图片摘要失败(可忽略):", _e)

    _mouse_note = ""
    try:
        _st = _pc.status() if _pc is not None else None
        if _st and _st["mouse_keyboard_enabled"]:
            _fg = _st["foreground"]
            _mouse_note = (
                "\n操作层已开启%s，你可以直接发 `点击 x,y` / `输入：文字` 让我动手。"
                % ("，当前前台「%s」在白名单内" % (_fg.get("title") or "")
                   if _fg.get("allowed") else "，但当前前台窗口不在白名单内需先切窗口"))
        else:
            _mouse_note = "\n想让我动手就发 `/mouse on`（默认关闭是安全设计）。"
    except Exception:
        _mouse_note = ""

    return ("👀 我看到你发的图片了。\n\n%s\n\n"
            "（可以直接接着说「把那个窗口关掉」「上面写的什么」——"
            "我会带上这张图一起理解，不用重发。\n"
            "要更清楚就加一句「看仔细点」，我会用更高分辨率再看一次。\n"
            "我还把这张图记住了，以后你说「早上那张图」我也能找回来。%s）"
            % (content, _mouse_note))


def _split_summary(text: str):
    """把模型的【摘要】/【详情】两段拆开。拆不出就返回 ("", 原文)。"""
    t = (text or "").strip()
    # 兼容模型可能写成 "摘要：xxx" / "【摘要】xxx" / "Summary: xxx"
    m = re.search(r"(?:【\s*摘要\s*】|摘要\s*[:：]|Summary\s*[:：])\s*(.*?)(?=\n|$)",
                  t, re.S | re.I)
    if not m:
        return "", t
    summary = m.group(1).strip()[:200]
    rest = re.sub(r"(?:【\s*摘要\s*】|摘要\s*[:：]|Summary\s*[:：])[^\n]*\n?", "", t,
                   flags=re.I)
    rest = re.sub(r"(?:【\s*详情\s*】|详情\s*[:：]|Details?\s*[:：])\s*", "", rest,
                   flags=re.I).strip()
    return summary, rest or t


def handle_image_followup(user_text: str) -> str:
    """接着上一张图追问（用户没重发图，只发了文字）。

    这是 computer use 的关键一环：bot 抓屏/用户发图 → 模型看着图 →
    用户说"把那个窗口关掉" → 模型知道说的是哪个窗口。
    """
    img = _recent_image()
    if not img:
        return ""
    side = config.IMAGE_SIDE_DETAIL if wants_detail(user_text) else config.IMAGE_SIDE_FAST
    use = _shrink_image(img, side, config.IMAGE_QUALITY)
    log("图像追问: side=%d, %d bytes" % (side, len(use)))
    b64 = base64.b64encode(use).decode("ascii")
    # 提示词要与**真实能力**一致。之前一直写"还只能看不能动"，
    # 现在操作层已实现，再这么写会误导模型给过时答案。
    _op_state = "鼠标键盘操作层尚未开启，用户暂时不能让我动手（需发 /mouse on）。"
    try:
        _st = _pc.status() if _pc is not None else None
        if _st and _st["mouse_keyboard_enabled"]:
            _op_state = ("鼠标键盘操作层已开启。如果用户要求点击/拖拽，"
                         "请告诉他可以发固定句式如「点击 x,y」，坐标由他确认。")
    except Exception:
        pass
    hint = ("\n\n（这是同一张图的追问。请只针对图里能确认的内容回答；"
            "如果涉及点击/拖拽等操作，%s）" % _op_state)
    msgs = [{
        "role": "user",
        "content": [
            {"type": "text", "text": user_text + hint},
            {"type": "image_url",
             "image_url": {"url": "data:image/jpeg;base64,%s" % b64}},
        ],
    }]
    return _call_llm_messages(msgs)


def on_message(data: P2ImMessageReceiveV1):
    try:
        msg = data.event.message
        if msg is None:
            return

        mtype = msg.message_type
        log("收到消息类型:", mtype)

        # 取发送者 open_id（回图/回文本都可能用到）
        open_id = ""
        try:
            open_id = (data.event.sender.sender_id.open_id or "") if data.event.sender else ""
        except Exception:
            pass
        # 持久化你的 open_id：这样 bot 下次启动时才知道问候语该发给谁
        if open_id:
            _remember_owner(open_id)

        # ---- 图片消息：下载到本地，交给 Bonsai 视觉理解 ----
        # 早先这里只处理 text，图片被直接丢弃，导致"你看看这张图里包含什么"回复
        # "没收到图片"——而模型其实能看图（旧 KVMem 栈有 mmproj 视觉）。
        if mtype == "image":
            def work_img():
                try:
                    reply(msg.message_id, handle_image_message(msg, open_id))
                except Exception as e:
                    reply(msg.message_id, "⚠️ 处理图片失败：%s" % e)
            threading.Thread(target=work_img, daemon=True).start()
            return

        # ---- 其它类型：明确告知，不静默丢弃 ----
        if mtype != "text":
            reply(msg.message_id, "（我目前只支持文字和图片消息，%s 类型的消息我先不处理）" % mtype)
            return

        # 提取文本内容（飞书文本消息是 {"text":"..."} 的 JSON 串）
        raw = msg.content
        try:
            text = json.loads(raw).get("text", "")
        except Exception:
            text = raw or ""
        text = text.strip()
        if not text:
            return

        log("收到消息:", text[:60].replace("\n", " "))

        # 群里 @ 机器人时，content 里可能带 @_user_1 之类前缀，清理掉
        text = re.sub(r"@_user_\d+", "", text).strip()
        if not text:
            return

        # 调模型（放线程里，避免阻塞事件处理循环）
        def work():
            try:
                # 截图走独立分支：需要 open_id 发图片，且不经过 Laya/技能。
                # 必须排在 handle_text 之前——否则会被 Laya 判成 control_pc
                # 而回"操作层开发中"（实测踩过：用户说"截个图"没命中关键词）。
                if is_shot_request(text):
                    log("命中截图指令: %s" % text[:30])
                    if pc_screenshot is None:
                        out = ("⚠️ 截图功能未就绪：%s\n"
                               "请确认 mss / Pillow 已安装。" % getattr(_pc_import_err, "msg", _pc_import_err))
                    elif not open_id:
                        out = "⚠️ 拿不到你的 open_id，无法把图片发回给你。请重新给机器人发一次。"
                    else:
                        out = do_screenshot(open_id)
                else:
                    out = handle_text(text, open_id)
            except Exception as e:
                out = "⚠️ 处理出错：%s" % e
            reply(msg.message_id, out)

        threading.Thread(target=work, daemon=True).start()

    except Exception as e:
        log("on_message 异常:", e)


# ---------- 启动 ----------
if __name__ == "__main__":
    if not config.APP_ID or not config.APP_SECRET:
        print("=" * 60)
        print("请先编辑 config.py，填入 APP_ID 和 APP_SECRET！")
        print("=" * 60)
        sys.exit(1)

    log("飞书 Bot 启动中（长连接模式，无需公网/证书）…")
    log("模型代理:", config.LOCAL_LLM_URL)
    # Qwen 决策层探测（不可用也不影响，仅提示）
    try:
        with urllib.request.urlopen(QWEN_URL + "/ping", timeout=3):
            log("Qwen 决策层: 已连接（%s）" % QWEN_URL)
    except Exception:
        log("Qwen 决策层: 未启动（将自动退回纯聊天模式，功能不受影响）")

    # API 客户端（发消息用）
    api_client = (
        lark.Client.builder()
        .app_id(config.APP_ID)
        .app_secret(config.APP_SECRET)
        .build()
    )

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )

    client = lark.ws.Client(
        config.APP_ID,
        config.APP_SECRET,
        event_handler=handler,
        log_level=lark.LogLevel.WARNING,
    )

    # ---- 启动问候语：连上飞书后 3 秒发到你的手机 ----
    def greet():
        time.sleep(3.0)     # 等长连接真正建好，避免第一条发不出去
        owner = _get_owner()
        if not owner:
            log("还不知道你的 open_id（首次使用请先给机器人发条消息，"
                "之后每次启动都会给你发问候语）")
            return
        try:
            send_greeting(owner)
            log("启动问候语已发到你的手机 ✅")
        except Exception as e:
            log("问候语发送失败（可忽略）:", e)

    threading.Thread(target=greet, daemon=True).start()

    try:
        client.start()
    except KeyboardInterrupt:
        log("已停止")
    except Exception as e:
        log("连接失败:", e)
