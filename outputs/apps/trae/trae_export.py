#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 EasonShu
r"""导出 TRAE 桌面端凭据 —— 供服务器每日签到使用。

与 WorkBuddy 的 export_session.py 思路相同但实现完全独立：TRAE 的登录态藏在
桌面端的 storage.json 与 iCubeAuthInfo 里，本脚本负责按平台定位这些位置
（Windows 的 APPDATA、macOS 的 Application Support、Linux 的 ~/.config），
取出登录态并写出凭据文件。

用法：
  python trae_export.py [--dir DIR] [--name NAME] [--rename] [--as-new]
                        [--who] [--storage PATH]
  --who      只显示当前登录的是谁，不导出
  --storage  手动指定 storage.json，跳过自动查找

账号 → 文件名的映射记在 trae-account-names.json，同一账号后续沿用同名。
"""

import argparse
import datetime
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DIR = os.path.join(HERE, "trae-sessions")
NAMES = os.path.join(HERE, "trae-account-names.json")

# Windows 文件名禁用的字符
ILLEGAL_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)} \
           | {"LPT%d" % i for i in range(1, 10)}

# 已知用过的目录名，按优先级排；扫不到时再模糊匹配
KNOWN_DIRS = ("TRAE SOLO CN", "Trae CN", "TRAE CN", "Trae", "trae")


def _utf8_console():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def safe_name(text, limit=32):
    text = ILLEGAL_RE.sub("_", str(text or "")).strip()
    text = re.sub(r"\s+", " ", text).strip(". ")
    if not text:
        return ""
    if text.upper() in RESERVED:
        text = "_" + text
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


def candidate_roots():
    """各平台下客户端数据目录的父目录。"""
    home = os.path.expanduser("~")
    roots = []
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        roots.append(appdata)
    elif sys.platform == "darwin":
        roots.append(os.path.join(home, "Library", "Application Support"))
    else:
        roots.append(os.path.join(home, ".config"))
    return roots


def find_storage(explicit=None):
    """定位 storage.json。显式指定优先，否则按已知目录名 + 模糊匹配扫一遍。"""
    if explicit:
        return explicit if os.path.isfile(explicit) else None

    found = []
    for root in candidate_roots():
        if not os.path.isdir(root):
            continue
        names = []
        try:
            names = os.listdir(root)
        except OSError:
            continue
        # 先按已知目录名精确命中
        for known in KNOWN_DIRS:
            for n in names:
                if n == known:
                    p = os.path.join(root, n, "User", "globalStorage", "storage.json")
                    if os.path.isfile(p):
                        found.append(p)
        # 再模糊匹配，兼容以后改名字
        for n in names:
            if "trae" in n.lower():
                p = os.path.join(root, n, "User", "globalStorage", "storage.json")
                if os.path.isfile(p) and p not in found:
                    found.append(p)
    return found[0] if found else None


# 签到接口真正认的那个设备 ID（aha 注册设备号）。
#
# 为什么必须专门采它：storage.json 里的 telemetry.devDeviceId 是个 **VS Code 遥测
# UUID**（形如 0a9d7b60-...），服务端并不把它当注册设备。实测（2026-10-01）用同一个
# token 打签到接口：
#     遥测 UUID              -> {"code": 9074, "message": "当前参与用户太多，请稍后再试"}
#     真实设备号 3129640255512713 -> {"code": 9095, "message": "当前设备今日已经签到…"}
# 9074 其实不是"人太多"，而是"这个设备号我不认识"。换成真实设备号后，返回立刻变成
# 有意义的确定性结果。
#
# 两个来源，按可靠性排序：
#   1. <Trae目录>/ahanet/tt_net_config.config  里的 device_id&#*<数字>@$*
#   2. storage.json 里签名密钥的键名 iCubeAuthInfo://icube-dc:<数字>
#      —— 这个在凭据里本身就有，所以旧凭据也能补出来，是很好的兜底。
_AHA_CONFIG_RE = re.compile(r"device_id&#\*(\d+)@")
_DC_KEY_RE = re.compile(r"^iCubeAuthInfo://icube-dc:(\d+)$")


def read_aha_device_id(storage_path, signing_entries):
    """采真实设备号。采不到返回空串（调用方会退回遥测 UUID 并给出警告）。"""
    trae_root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(storage_path))))
    cfg = os.path.join(trae_root, "ahanet", "tt_net_config.config")
    if os.path.isfile(cfg):
        try:
            with open(cfg, encoding="utf-8", errors="replace") as fh:
                m = _AHA_CONFIG_RE.search(fh.read())
            if m:
                return m.group(1)
        except OSError:
            pass

    for entry in signing_entries or []:
        m = _DC_KEY_RE.match(str(entry.get("key") or ""))
        if m:
            return m.group(1)
    return ""


def read_credential(storage_path):
    """读 storage.json 并解出可搬运的凭据。"""
    sys.path.insert(0, HERE)
    import trae_crypto

    with open(storage_path, encoding="utf-8") as fh:
        storage = json.load(fh)

    auth_key = "iCubeAuthInfo://icube.cloudide"
    encrypted = storage.get(auth_key)
    if not isinstance(encrypted, str):
        raise SystemExit("storage.json 里没有登录信息（%s）。\n"
                         "先在 TRAE 桌面端登录，再跑一次。" % auth_key)

    auth_info = trae_crypto.decrypt_envelope(encrypted)

    dc_prefix = "iCubeAuthInfo://icube-dc:"
    signing_entries = [{"key": k, "value": v}
                       for k, v in storage.items()
                       if k.startswith(dc_prefix) and isinstance(v, str)]
    if not signing_entries:
        raise SystemExit("storage.json 里没有设备签名密钥（%s*）。\n"
                         "客户端重装或换机器后可能出现，重新登录一次 TRAE 再试。" % dc_prefix)

    signing = trae_crypto.decrypt_envelope(signing_entries[0]["value"])

    device_id = storage.get("telemetry.devDeviceId")
    machine_id = storage.get("telemetry.machineId")
    if not device_id or not machine_id:
        raise SystemExit("storage.json 里缺少 devDeviceId / machineId，无法构造请求头。")

    token = auth_info.get("token")
    if not isinstance(token, str) or not token:
        raise SystemExit("解出来的 token 无效。")

    return {
        "authInfo": auth_info,
        "token": token,
        "refreshToken": auth_info.get("refreshToken") or "",
        "host": auth_info.get("host") or "",
        "userId": str(auth_info.get("userId") or ""),
        "accountName": (auth_info.get("account") or {}).get("username") or "",
        "deviceId": str(device_id),
        "machineId": str(machine_id),
        "ahaDeviceId": read_aha_device_id(storage_path, signing_entries),
        "appVersion": str(storage.get("iCubeLastVersion") or ""),
        "privateKeyPEM": signing.get("privateKeyPEM") or "",
        "publicKeyPEM": signing.get("publicKeyPEM") or "",
        "signingEntries": signing_entries,
        "expiresAt": auth_info.get("expiredAt"),
        "refreshExpiresAt": auth_info.get("refreshExpiredAt"),
    }


_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?"
    r"(?:([Zz])|([+-])(\d{2}):?(\d{2}))?$")


def _parse_iso(text):
    """手撕 ISO-8601 → (本地 naive datetime, epoch 秒)。

    不用 datetime.fromisoformat：那是 3.7 才有的，老服务器（CentOS 7 还在跑 3.6）会炸。
    """
    m = _ISO_RE.match(text.strip())
    if not m:
        return None, None
    year, month, day, hour, minute, second = (int(m.group(i)) for i in range(1, 7))
    frac = m.group(7) or ""
    micro = int((frac + "000000")[:6]) if frac else 0
    try:
        dt = datetime.datetime(year, month, day, hour, minute, second, micro)
    except ValueError:
        return None, None
    offset = 0
    if m.group(9):
        sign = -1 if m.group(9) == "-" else 1
        offset = sign * (int(m.group(10)) * 3600 + int(m.group(11)) * 60)
    epoch = (dt - datetime.datetime(1970, 1, 1)).total_seconds() - offset
    local = datetime.datetime.fromtimestamp(epoch)
    return local, epoch


def parse_ms_iso(value):
    """TRAE 给的是 ISO 字符串（2026-10-14T23:21:45.286Z），转成 (datetime, 剩余天数)。

    续期之后这个字段会变成毫秒数字（或数字字符串），所以两种都要认。
    """
    if not value:
        return None, None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        if v > 1e11:
            v /= 1000.0
        return datetime.datetime.fromtimestamp(v), v
    text = str(value).strip()
    if re.fullmatch(r"-?\d+(\.\d+)?", text):
        v = float(text)
        if v > 1e11:
            v /= 1000.0
        return datetime.datetime.fromtimestamp(v), v
    local, epoch = _parse_iso(text)
    if local is None:
        return None, None
    days = round((epoch - time.time()) / 86400.0, 1)
    return local, days


def build_portable(cred):
    exp_dt, exp_days = parse_ms_iso(cred["expiresAt"])
    ref_dt, ref_days = parse_ms_iso(cred["refreshExpiresAt"])
    now = datetime.datetime.now()
    return {
        "_portable": {
            "exportedAt": now.strftime("%Y-%m-%d %H:%M:%S"),
            "expiresAt": exp_dt.strftime("%Y-%m-%d %H:%M:%S") if exp_dt else "",
            "daysLeft": exp_days,
            "refreshExpiresAt": ref_dt.strftime("%Y-%m-%d %H:%M:%S") if ref_dt else "",
            "refreshDaysLeft": ref_days,
            "note": "明文凭据，等同于 TRAE 账号本身，别外传",
        },
        "account": {
            "userId": cred["userId"],
            "accountName": cred["accountName"],
            "host": cred["host"],
        },
        "auth": {
            "token": cred["token"],
            "refreshToken": cred["refreshToken"],
            "expiresAt": cred["expiresAt"],
            "refreshExpiresAt": cred["refreshExpiresAt"],
            "deviceId": cred["deviceId"],
            "machineId": cred["machineId"],
            "ahaDeviceId": cred.get("ahaDeviceId") or "",
            "appVersion": cred.get("appVersion") or "",
            "host": cred["host"],
        },
        "signing": {
            "privateKeyPEM": cred["privateKeyPEM"],
            "publicKeyPEM": cred["publicKeyPEM"],
            "entries": cred["signingEntries"],
        },
        "authInfo": cred["authInfo"],
    }


def write_secure(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)          # 明文令牌：仅本人可读
    except OSError:
        pass


def ask_name(default):
    if not sys.stdin or not sys.stdin.isatty():
        return ""
    try:
        sys.stdout.write("给这个账号起个名字（直接回车用 %s）：" % default)
        sys.stdout.flush()
        return sys.stdin.readline().strip()
    except Exception:
        return ""


def resolve_name(info, mapping, args, interactive=True):
    uid = info["userId"]
    remembered = ""
    entry = mapping.get(uid)
    if isinstance(entry, dict):
        remembered = safe_name(entry.get("name") or "")
    elif isinstance(entry, str):
        remembered = safe_name(entry)

    if args.name:
        chosen, source = safe_name(args.name), "命令行指定"
    elif args.rename or not remembered:
        chosen, source = safe_name(info["accountName"]), "TRAE 用户名"
        if not chosen and interactive:
            chosen = safe_name(ask_name(uid or "trae"))
            source = "手动命名"
        if not chosen:
            chosen, source = safe_name(uid) or "trae", "数字 ID"
    else:
        chosen, source = remembered, "已记住的名字"

    if uid and interactive and (args.name or args.rename or not remembered):
        mapping[uid] = {"name": chosen, "accountName": info["accountName"],
                        "updatedAt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        try:
            save_names(mapping)
        except OSError as exc:
            print("（名字没能记下来：%s —— 下次还得再问一次）" % exc)
    return chosen, source


def main():
    _utf8_console()
    ap = argparse.ArgumentParser(description="导出 TRAE 桌面端凭据（自动命名）")
    ap.add_argument("--dir", default=DEFAULT_DIR, help="输出目录，默认 <脚本目录>/trae-sessions")
    ap.add_argument("--name", default=None, help="指定文件名（并记住）")
    ap.add_argument("--rename", action="store_true", help="重新起名")
    ap.add_argument("--as-new", action="store_true", help="同名文件已存在时另存一份，不覆盖")
    ap.add_argument("--who", action="store_true", help="只看当前登录的是谁，不导出")
    ap.add_argument("--storage", default=None, help="手动指定 storage.json 路径")
    args = ap.parse_args()

    print("=" * 62)
    print("TRAE 凭据导出（自动命名）")
    print("=" * 62)

    storage = find_storage(args.storage)
    if not storage:
        print("没找到 TRAE 的 storage.json。")
        print("确认已安装并登录 TRAE 桌面端；若装在非常规位置，用 --storage 指定路径。")
        return 2
    print("凭据文件 : %s" % storage)

    try:
        cred = read_credential(storage)
    except SystemExit as exc:
        print(exc)
        return 2
    except Exception as exc:
        print("读取/解密失败：%s" % exc)
        return 2

    portable = build_portable(cred)
    meta = portable["_portable"]
    info = {"userId": cred["userId"], "accountName": cred["accountName"]}

    print("账号     : %s（userId=%s）" % (cred["accountName"] or "-", cred["userId"] or "-"))
    print("接口     : %s" % (cred["host"] or "-"))
    print("到期     : %s（还有 %s 天）" % (meta["expiresAt"], meta["daysLeft"]))
    print("续期链   : %s（还有 %s 天）" % (meta["refreshExpiresAt"], meta["refreshDaysLeft"]))
    aha = cred.get("ahaDeviceId") or ""
    print("设备号   : %s%s" % (aha or "（没采到）",
                              "   ← 签到接口认的就是它" if aha else ""))
    if not aha:
        print("           [!] 没采到真实设备号，签到会退回遥测 UUID，")
        print("               届时服务端只会回 9074（看起来像'人太多'，其实是设备号不认识）。")
    print("token    : 长度 %d，已省略" % len(cred["token"]))

    mapping = load_names()
    name, source = resolve_name(info, mapping, args, interactive=not args.who)
    print("文件名   : %s.json   （来源：%s）" % (name, source))
    print("-" * 62)

    if args.who:
        return 0

    out_dir = os.path.abspath(args.dir)
    path = os.path.join(out_dir, "%s.json" % name)
    existed = os.path.exists(path)
    if existed and args.as_new:
        path = os.path.join(out_dir, "%s_%s.json"
                            % (name, datetime.datetime.now().strftime("%m%d")))
    write_secure(path, portable)
    print("已导出   : %s%s" % (path, "（覆盖已有文件）" if existed and not args.as_new else ""))
    print()
    print("trae-sessions/ 目录现状：")
    try:
        found = sorted(f for f in os.listdir(out_dir) if f.endswith(".json"))
    except OSError:
        found = []
    for f in found:
        print("  - %s" % f)

    print()
    if meta["daysLeft"] is not None and meta["daysLeft"] < 3:
        print("注意：令牌不到 3 天就到期，建议尽快部署并让服务器自动续期。")
    print("下一步：把 trae-sessions/ 连同 trae_signin.py / trae_install.sh 传到服务器，")
    print("        执行 sh trae_install.sh（与 WorkBuddy 完全分开的两套）。")
    print("多账号：在 TRAE 里切换登录，再跑一次本脚本。")
    print()
    print("注意：这是明文凭据，等同于 TRAE 账号本身。传完删掉本机那份，别进版本库。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
