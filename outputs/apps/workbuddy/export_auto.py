#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 EasonShu
r"""自动命名导出 —— export_session.py 的入口包装，按「当前登录账号」决定文件名。

要解决的问题：一台机器上先后登录过多个账号时，手工给导出的凭据起名容易撞车
或互相覆盖。本脚本先问「当前登录的是谁」，再按其昵称与 uid 生成安全文件名，
并把「账号 → 文件名」的映射记进 account-names.json，保证同一账号后续导出沿用
同名；--rename 可重新起名，--as-new 则在重名时另存一份而不覆盖。

用法：
  python export_auto.py [--dir DIR] [--name NAME] [--rename] [--as-new] [--who]

导出逻辑本身由 export_session.py 以子进程方式执行，本文件只负责命名与落位。
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
EXPORT = os.path.join(HERE, "export_session.py")
DEFAULT_DIR = os.path.join(HERE, "sessions")
# 名字映射绝不放在 sessions/ 里 —— 那个目录会被整个传上服务器当凭据用
NAMES = os.path.join(HERE, "account-names.json")

# Windows 文件名禁用的字符，顺带清理控制字符
ILLEGAL_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)} \
           | {"LPT%d" % i for i in range(1, 10)}


def _utf8_console():
    """控制台默认是 GBK，中文提示会变乱码 —— 强制 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def safe_name(text, limit=32):
    """把任意字符串洗成合法的 Windows 文件名（中文原样保留）。"""
    text = ILLEGAL_RE.sub("_", str(text or "")).strip()
    text = re.sub(r"\s+", " ", text)
    text = text.strip(". ")              # 文件名不能以点或空格结尾
    if not text:
        return ""
    if text.upper() in RESERVED:
        text = "_" + text                # CON / NUL / COM1 这类是设备名
    return text[:limit].strip(". ")


def load_names():
    try:
        with open(NAMES, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_names(mapping):
    tmp = NAMES + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(mapping, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, NAMES)


def try_decrypt(signin, value):
    """解 sym-v1 信封；解不出来返回空串，绝不把异常抛到用户脸上。"""
    if isinstance(value, str) and value:
        return value
    if not isinstance(value, dict):
        return ""
    try:
        result = signin._run_auth_helper(signin.find_workbuddy_runtime(),
                                         {"operation": "decrypt", "value": value})
        return result.get("accessToken") or ""
    except SystemExit:
        return ""
    except Exception:
        return ""


def read_identity(signin, raw):
    """从本机凭据里扒出这个账号是谁。"""
    acc = raw.get("account") or {}
    uid = str(acc.get("uid") or "")
    uin = str(acc.get("uin") or "").strip()
    atype = str(acc.get("type") or acc.get("accountType") or "").lower()
    kind = "企业号" if atype == "enterprise" else "个人号"
    nick = ""
    if acc.get("nickname"):
        nick = try_decrypt(signin, acc["nickname"]).strip()
    return {"nick": nick, "uin": uin, "uid": uid, "kind": kind}


def ask_name(default):
    """交互式问一句；没有真终端（重定向 / 计划任务）时直接回车。"""
    if not sys.stdin or not sys.stdin.isatty():
        return ""
    try:
        sys.stdout.write("给这个账号起个名字（直接回车用 %s）：" % default)
        sys.stdout.flush()
        return sys.stdin.readline().strip()
    except Exception:
        return ""


def resolve_name(info, mapping, args, interactive=True):
    """决定文件名，并在需要时记进映射表。--who 只是看看，不提问也不写盘。"""
    uin, uid, nick = info["uin"], info["uid"], info["nick"]
    remembered = ""
    entry = mapping.get(uin) if uin else None
    if isinstance(entry, dict):
        remembered = safe_name(entry.get("name") or "")
    elif isinstance(entry, str):
        remembered = safe_name(entry)

    if args.name:
        chosen = safe_name(args.name)
        source = "命令行指定"
    elif args.rename or not remembered:
        # 没记住过（或要求重命名）：先用昵称，拿不到就问一句
        chosen = safe_name(nick)
        source = "账号昵称"
        if not chosen and interactive:
            chosen = safe_name(ask_name(uin or uid[:8] or "account"))
            source = "手动命名"
        if not chosen:
            chosen = safe_name(uin) or safe_name(uid[:8]) or "account"
            source = "数字 ID"
    else:
        chosen = remembered
        source = "已记住的名字"

    if uin and interactive and (args.name or args.rename or not remembered):
        mapping[uin] = {"name": chosen, "uid": uid, "kind": info["kind"],
                        "updatedAt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        try:
            save_names(mapping)
        except OSError as exc:
            print("（名字没能记下来：%s —— 下次还得再问一次）" % exc)

    return chosen, source


def main():
    _utf8_console()
    ap = argparse.ArgumentParser(description="按当前登录账号自动命名导出 WorkBuddy 凭据")
    ap.add_argument("--dir", default=DEFAULT_DIR, help="输出目录，默认 <脚本目录>/sessions")
    ap.add_argument("--name", default=None, help="直接指定文件名（并记住）")
    ap.add_argument("--rename", action="store_true", help="重新起名，覆盖记住的名字")
    ap.add_argument("--as-new", action="store_true",
                    help="同名文件已存在时另存一份（<名>_MMDD.json），不覆盖")
    ap.add_argument("--who", action="store_true", help="只看当前登录的是谁、会叫什么名，不导出")
    args = ap.parse_args()

    if not os.path.isfile(EXPORT):
        print("找不到 export_session.py —— 本脚本要和它放在同一个目录：%s" % EXPORT)
        return 2

    sys.path.insert(0, HERE)
    try:
        import signin
    except Exception as exc:
        print("导入 signin.py 失败：%s" % exc)
        return 2

    auth_file = signin.find_auth_file()[0]
    if not auth_file or not os.path.exists(auth_file):
        print("未找到本机登录凭据。")
        print("先启动 WorkBuddy 桌面端并登录，然后再跑一次。")
        return 2

    print("=" * 62)
    print("WorkBuddy 凭据导出（自动命名）")
    print("=" * 62)
    print("凭据文件 : %s" % auth_file)

    try:
        raw = signin.load_session_retry(auth_file)
    except Exception as exc:
        print("读取凭据失败：%s" % exc)
        print("若客户端正在刷新令牌，过几秒重试即可。")
        return 2

    info = read_identity(signin, raw)
    mapping = load_names()
    name, source = resolve_name(info, mapping, args, interactive=not args.who)

    print("账号类型 : %s" % info["kind"])
    print("数字 ID  : %s" % (info["uin"] or "-"))
    print("UID      : %s" % ((info["uid"][:8] + "…") if info["uid"] else "-"))
    print("文件名   : %s.json   （来源：%s）" % (name, source))
    print("-" * 62)

    if args.who:
        return 0

    out_dir = os.path.abspath(args.dir)
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    path = os.path.join(out_dir, "%s.json" % name)
    existed = os.path.exists(path)
    if existed and args.as_new:
        path = os.path.join(out_dir, "%s_%s.json" % (name, datetime.datetime.now().strftime("%m%d")))

    print("输出到   : %s%s" % (path, "（覆盖已有文件）" if existed and not args.as_new else ""))
    print()

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # 解密与导出交给原脚本 —— 它还会顺手做一次「离开客户端也能跑通」的验证
    proc = subprocess.run([sys.executable, EXPORT, "--project", HERE, "--out", path],
                          env=env, cwd=HERE)
    if proc.returncode != 0:
        print()
        print("导出失败（退出码 %s），看上面的报错。" % proc.returncode)
        return proc.returncode

    print()
    print("-" * 62)
    print("sessions/ 目录现状：")
    try:
        found = sorted(f for f in os.listdir(out_dir) if f.endswith(".json"))
    except OSError:
        found = []
    if found:
        for f in found:
            print("  - %s" % f)
    else:
        print("  （空）")

    print()
    print("下一步：把 sessions/ 整个目录连同 4 个脚本传到服务器，执行 sh install.sh。")
    print("多账号：在客户端里切换登录，再跑一次本脚本 —— 会多出一个以该账号命名的 json。")
    print("改名字：python export_auto.py --rename")
    print()
    print("注意：这些 json 是明文凭据，等同于账号本身。传完删掉本机那份，别进版本库。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
