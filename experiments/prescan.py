"""
prescan.py —— 发布前安全扫描

发到 GitHub 就收不回来了（即使删掉，commit 历史里还在）。
所以这一步必须**在 push 之前**做完。

★ 关键设计：**只扫描「实际会被提交的文件」**

早先版本遍历整个目录树，于是报出一堆假警报：
  - config_local.py 含真凭据 —— 但它已被 .gitignore 排除，根本不会提交
  - config.example.py 里的 "xxxxxxxx" 占位符 —— 被误判为密钥
这会让真正的风险淹没在噪声里。

现在改为：如果仓库已初始化，就用 `git ls-files` 取**待提交清单**
（已自动应用 .gitignore）；否则退回目录遍历。
**扫描对象 = 提交对象**，这是唯一有意义的口径。

用法:
    python prescan.py
"""

import os
import re
import subprocess

ROOT = os.environ.get(
    "KVME_ROOT",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 未初始化仓库时的目录排除名单
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}

SECRET_PATTERNS = [
    ("飞书 App ID(真)", r'cli_[a-z0-9]{16,}'),
    ("App Secret", r'APP_SECRET\s*=\s*["\'][^"\']{20,}["\']'),
    ("加密密钥", r'ENCRYPT_KEY\s*=\s*["\'][^"\']{8,}["\']'),
    ("验证 token", r'VERIFICATION_TOKEN\s*=\s*["\'][^"\']{16,}["\']'),
    ("通用密钥", r'(?i)(secret|token|password|passwd|api[_-]?key)'
                 r'\s*[:=]\s*["\'][A-Za-z0-9_\-]{20,}["\']'),
    ("私钥块", r'-----BEGIN [A-Z ]*PRIVATE KEY-----'),
]

PII_PATTERNS = [
    ("Windows 用户名", r'Kizuna' + r'Mic'),
    ("用户目录", r'[A-Z]:\\Users\\[A-Za-z0-9_]+'),
    ("本机专有路径", r'[A-Z]:\\Bonsai-demo'),
]

# 这些模式本身就是扫描规则/迁移脚本，含关键词属正常
SELF_EXEMPT = {"prescan.py", "patch_bat_paths.py", "fix_legacy_paths.py",
               "prescan_spellcheck.py"}

TEXT_EXT = {".py", ".md", ".json", ".bat", ".ps1", ".txt", ".yaml", ".yml",
            ".toml", ".cfg", ".ini", ".sh", ".html", ".js", ".css"}
BIG = 5 * 1024 * 1024

# ---------------------------------------------------------------------------
# ★ 内容级个人数据检测
#
# 这是本扫描器**最容易漏**的一类，也是实际踩过的坑：
#   controller/summaries/*.md 里写着用户的就业城市、职业方向、称呼偏好；
#   controller/history/*.jsonl 里是完整往来消息；
#   feishu-bot/owner_open_id.txt 是用户的平台标识符。
# 它们**不含密码/token 这类显式特征**，纯模式匹配抓不到。
#
# 所以必须按**用途**判断：凡是「对话 / 记忆 / 会话」类运行状态，
# 默认视为个人数据，无条件排除。
# ---------------------------------------------------------------------------

PERSONAL_PATH = [
    (r"(^|/)history/", "对话历史目录"),
    (r"(^|/)summaries?/", "对话摘要目录"),
    (r"(^|/)memory/", "长期记忆目录"),
    (r"(^|/)artifacts?/", "智能体产物目录"),
    (r"session\.json$", "会话状态"),
    (r"think_mode\.txt$", "运行状态"),
    (r"owner_.*\.txt$", "用户标识"),
    (r"\.jsonl$", "逐轮记录（对话形态）"),
]

PERSONAL_CONTENT = [
    (r'"role"\s*:\s*"(user|assistant)"', "对话轮次记录"),
    (r"【已有摘要】", "对话摘要"),
    (r"称呼|就业|求职|简历|面试", "个人处境描述"),
    (r"用户的?职业|用户目标|用户偏好", "用户画像"),
]

# ★ 内容检查**只对数据/文档生效**，跳过代码文件。
#
# 原因：代码里也会出现 `"role": "user"` —— 那是构造 API 请求体的参数，
# 不是对话数据。对 .py 做内容检查会把 bot.py / proxy_server.py 全部误报。
#
# 判据是**文件类型**：
#   数据/文档（json/jsonl/md/txt/csv…）→ 内容里出现对话特征 = 真个人数据
#   代码（py/ps1/bat/js…）              → 出现同样字符串只是程序逻辑
CODE_EXT = {".py", ".ps1", ".bat", ".sh", ".js", ".ts", ".html", ".css",
            ".cmd", ".psm1"}


def tracked_files():
    """返回待提交文件列表。仓库已初始化时用 git ls-files（应用 .gitignore）。"""
    try:
        r = subprocess.run(["git", "ls-files"], cwd=ROOT,
                           capture_output=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            files = [l for l in r.stdout.decode("utf-8", "replace")
                     .splitlines() if l.strip()]
            return files, "git ls-files（已应用 .gitignore）"
    except Exception:
        pass
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            out.append(os.path.relpath(os.path.join(dirpath, fn), ROOT))
    return out, "目录遍历（仓库未初始化，.gitignore 未生效）"


def _is_placeholder(s):
    """判断是不是占位符而不是真凭据。

    模板文件里的值形如 APP_ID = "cli_xxxxxxxx" / APP_SECRET = "xxxx..."。
    这些会被密钥正则命中，但显然是占位符。

    判据：**先取出引号内的值**（去掉 `KEY = ` 这样的前缀），
    再把常见占位前缀剥掉，剩余部分若只由同一字符构成即为占位符。
    不能简单地「含 x 就算」——真密钥里也可能有 x。
    """
    m = re.search(r'["\']([^"\']+)["\']', s)
    body = m.group(1) if m else s
    body = re.sub(r'[^A-Za-z0-9]', '', body)
    for pre in ("cli", "sk", "ghp", "gho"):
        if body.lower().startswith(pre):
            body = body[len(pre):]
    if not body:
        return True
    low = body.lower()
    if low in ("yourkey", "yourtoken", "yoursecret", "apikey",
               "changeme", "placeholder"):
        return True
    return len(set(low)) == 1


def main():
    files, how = tracked_files()
    print("=" * 72)
    print("发布前安全扫描  ——  只检查会被提交的文件")
    print("=" * 72)
    print("扫描口径: %s" % how)
    print("文件数  : %d" % len(files))

    secrets, pii, big, personal = [], [], [], []
    for rel in files:
        path = os.path.join(ROOT, rel)
        # ★ 先按路径判断是不是个人数据 —— 这类文件连读都不用读
        norm = rel.replace("\\", "/")
        for pat, label in PERSONAL_PATH:
            if re.search(pat, norm, re.I):
                personal.append((rel, label, "路径特征"))
                break
        if not os.path.exists(path):
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size > BIG:
            big.append((rel, size))
            continue
        if os.path.splitext(rel)[1].lower() not in TEXT_EXT:
            continue
        if size > 3 * 1024 * 1024:
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                txt = f.read()
        except Exception:
            continue

        base = os.path.basename(rel)
        ext = os.path.splitext(rel)[1].lower()
        for label, pat in SECRET_PATTERNS:
            for m in re.finditer(pat, txt):
                val = m.group(0)
                if _is_placeholder(val):
                    continue
                line = txt[:m.start()].count("\n") + 1
                secrets.append((rel, line, label, val[:46]))
        if base not in SELF_EXEMPT:
            for label, pat in PII_PATTERNS:
                n = len(re.findall(pat, txt))
                if n:
                    pii.append((rel, n, label))
            # ★ 内容级个人数据 —— **只查数据/文档，跳过代码**
            #   （代码里也有 '"role": "user"'，那是 API 参数不是对话）
            if ext not in CODE_EXT:
                for pat, label in PERSONAL_CONTENT:
                    found = re.findall(pat, txt)
                    if found:
                        personal.append(
                            (rel, label, "内容特征 ×%d" % len(found)))
                        break

    print("\n【1】硬编码凭证（阻塞项）")
    if secrets:
        for rel, line, label, val in secrets:
            print("  ✗ %-36s L%-4d %-14s %s" % (rel[:36], line, label, val))
    else:
        print("  ✓ 未发现")

    print("\n【2】个人信息 / 本机路径")
    if pii:
        for rel, n, label in sorted(pii):
            print("  ! %-44s %-14s ×%d" % (rel[:44], label, n))
    else:
        print("  ✓ 未发现")

    print("\n【2b】个人数据文件（对话 / 记忆 / 会话状态）")
    if personal:
        seen = set()
        for rel, label, src in sorted(personal):
            if rel in seen:
                continue
            seen.add(rel)
            print("  ✗ %-46s %-12s %s" % (rel[:46], label, src))
        print("\n     ⚠️ 这类文件不含密码特征，模式匹配抓不到，")
        print("        必须按**用途**判断：对话/记忆/会话状态一律排除。")
    else:
        print("  ✓ 未发现")

    print("\n【3】大文件（>5MB）")
    if big:
        for rel, size in sorted(big, key=lambda x: -x[1])[:8]:
            print("  ! %-48s %7.1f MB" % (rel[:48], size / 1048576))
    else:
        print("  ✓ 未发现")

    print("\n" + "=" * 72)
    ok = not secrets and not big and not personal
    print("结论: %s" % ("✓ 可以提交" if ok else "✗ 存在阻塞项，先处理"))
    if secrets:
        print("  → 凭证必须清除，且**去开放平台重置**（已暴露的值作废）")
    if personal:
        print("  → 个人数据文件必须加入 .gitignore")
    if big:
        print("  → 大文件必须加入 .gitignore（仓库会被拖慢或拒收）")
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
