#!/usr/bin/env python3
# chat_cli.py - 终端客户端（直接走控制器，共享 session.json）
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import controller

USAGE = ("命令：\n"
         "  /exit       退出\n"
         "  /summarize  压缩当前对话为每日摘要\n"
         "  /status     查看会话状态（活跃轮数 / 摘要长度 / 产物数 / 估算上下文字符）\n"
         "  /save       把上一条助手回复存为产物文件\n")


def main():
    sess = controller.Session()
    print("本地模型 CLI（上下文减肥已启用）。\n" + USAGE)
    last_reply = ""
    while True:
        try:
            u = input("\n你> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not u:
            continue
        if u == "/exit":
            break
        if u == "/summarize":
            print("\n[摘要]\n" + sess.summarize()); continue
        if u == "/status":
            print(json.dumps(sess.status(), ensure_ascii=False, indent=2)); continue
        if u == "/save":
            if last_reply:
                print("已存:", sess.save_artifact(last_reply, "手动保存"))
            else:
                print("没有可保存的回复"); continue
        sys.stdout.write("助手> "); sys.stdout.flush()
        last_reply = ""
        for kind, piece in sess.chat(u, stream=True):
            if kind == "reasoning":
                continue  # 终端只显示正文；思考内容不打印
            sys.stdout.write(piece); sys.stdout.flush()
            last_reply += piece
        print()


if __name__ == "__main__":
    main()
