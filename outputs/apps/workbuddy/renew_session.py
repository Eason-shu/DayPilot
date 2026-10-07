#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 EasonShu
"""服务器侧凭据续期 —— 用 refreshToken 链式续期，让签到长期脱离本机。

背景：导出的 accessToken 只有几十天有效期。本脚本读 session.json 里的
refreshToken，在临近过期时换一对新 token 并回写，使服务器无需重新导出凭据
也能长期签到。

用法：
  python renew_session.py [--file F] [--threshold-days N] [--force]
                          [--dry-run] [--status] [--endpoint URL] [--log PATH]
  --threshold-days  剩余天数低于该值才真正发请求（默认 10 天）
  --status          只读状态，不做任何修改
  --dry-run         只报告决策与请求形状，不发请求
  --force           忽略阈值，无条件续期一次

失败不阻断签到：续期只影响凭据寿命，拿不到新 token 时签到脚本仍按原凭据运行，
因此 multi_run.py 忽略本脚本的非零退出码。
"""

import argparse
import datetime
import json
import os
import shutil
import sys
import urllib.error
import urllib.request

DEFAULT_FILE = "/opt/workbuddy-signin/session.json"
DEFAULT_ENDPOINT = "https://copilot.tencent.com"
DEFAULT_PATH = "/v2/plugin/auth/token/refresh"
ACCESS_DAYS = 55
REFRESH_DAYS = 60


def now():
    return datetime.datetime.now()


def iso_from_ms(ms):
    if not isinstance(ms, (int, float)) or ms <= 0:
        return None
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def days_from_ms(ms):
    if not isinstance(ms, (int, float)) or ms <= 0:
        return None
    return round((ms / 1000 - now().timestamp()) / 86400, 1)


def mask(value):
    return "len=%d" % len(value) if isinstance(value, str) else "缺失"


def emit_log(log_path, record):
    record = dict(record, ts=now().strftime("%Y-%m-%d %H:%M:%S"))
    line = "[%s] %s\n" % (record["ts"], json.dumps(record, ensure_ascii=False))
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass
    sys.stdout.write(line)


def build_opener(endpoint):
    """localhost 永不走代理——否则本地联调会被 http_proxy 拦掉。"""
    host = endpoint.split("//", 1)[-1].split("/", 1)[0].split(":")[0]
    if host in ("127.0.0.1", "localhost", "::1"):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def call_refresh(endpoint, path, headers, timeout=20):
    url = endpoint.rstrip("/") + path
    req = urllib.request.Request(url, data=b"{}", method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    opener = build_opener(endpoint)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(65536).decode("utf-8", "replace")
    except Exception as e:
        return -1, "%s: %s" % (type(e).__name__, e)


def write_back(path, doc, log_path):
    """备份 → 原子替换 → 复读校验 → 失败回滚。"""
    backup = path + ".bak"
    try:
        shutil.copy2(path, backup)
    except OSError as e:
        emit_log(log_path, {"result": "ERROR", "stage": "backup", "detail": str(e)})
        return False

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
    if os.name == "posix":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    if os.name == "posix":
        os.chmod(path, 0o600)

    try:
        with open(path, encoding="utf-8") as f:
            check = json.load(f)
        got = check["auth"]["accessToken"]
    except (OSError, ValueError, KeyError) as e:
        shutil.copy2(backup, path)
        emit_log(log_path, {"result": "ERROR", "stage": "verify", "detail": str(e),
                            "action": "已从 .bak 回滚"})
        return False

    if got != doc["auth"]["accessToken"]:
        shutil.copy2(backup, path)
        emit_log(log_path, {"result": "ERROR", "stage": "verify",
                            "detail": "复读到的令牌与写入不一致", "action": "已从 .bak 回滚"})
        return False
    return True


def main():
    ap = argparse.ArgumentParser(description="refreshToken 链式续期")
    ap.add_argument("--file", default=os.environ.get("WORKBUDDY_AUTH_FILE", DEFAULT_FILE))
    ap.add_argument("--threshold-days", type=float, default=10.0,
                    help="accessToken 剩余天数低于此值才续期（默认 10）")
    ap.add_argument("--force", action="store_true", help="无条件续期一次")
    ap.add_argument("--dry-run", action="store_true", help="只报告决策与请求形状，不发请求")
    ap.add_argument("--status", action="store_true", help="只读状态，不做任何修改")
    ap.add_argument("--endpoint", default=None, help="覆盖后端地址（联调用）")
    ap.add_argument("--log", default=None, help="续期日志，默认与 session 同目录 renew.log")
    args = ap.parse_args()

    path = os.path.abspath(args.file)
    log_path = args.log or os.path.join(os.path.dirname(path), "renew.log")

    if not os.path.exists(path):
        sys.stderr.write("session 文件不存在：%s\n" % path)
        return 2
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        sys.stderr.write("session 文件读不了：%s\n" % e)
        return 2

    auth = doc.get("auth") or {}
    meta = doc.get("_portable") or {}
    renew = doc.get("_renew") or {}

    endpoint = args.endpoint or renew.get("endpoint") or auth.get("endpoint") or DEFAULT_ENDPOINT
    path_part = renew.get("path") or DEFAULT_PATH
    domain = (renew.get("domain") or auth.get("domain")
              or endpoint.split("//", 1)[-1].split("/", 1)[0])
    rt = renew.get("refreshToken")

    at_days = days_from_ms(meta.get("expiresAtMs"))
    rt_days = days_from_ms(renew.get("expiresAtMs"))

    if args.status:
        print("accessToken : %s（还有 %s 天）" % (meta.get("expiresAt"), at_days))
        print("refreshToken: %s（还有 %s 天，%s）" % (renew.get("expiresAt"), rt_days, mask(rt)))
        print("续期窗口    : 剩余 < %s 天时自动续期" % args.threshold_days)
        print("上次续期    : %s（累计 %s 次）"
              % (renew.get("lastRenewedAt", "从未"), renew.get("renewCount", 0)))
        return 0

    if not rt:
        print("未内置 refreshToken（导出时未加 --with-refresh），跳过续期。")
        emit_log(log_path, {"result": "NO_CHAIN"})
        return 0

    need = args.force or at_days is None or at_days < args.threshold_days
    if not need:
        print("accessToken 还有 %s 天，未到续期窗口，跳过。" % at_days)
        emit_log(log_path, {"result": "SKIPPED", "accessTokenDaysLeft": at_days})
        return 0

    headers = {
        "X-Refresh-Token": rt,
        "X-Auth-Refresh-Source": renew.get("sourceHeader") or "plugin",
        "X-Domain": domain,
    }
    url = endpoint.rstrip("/") + path_part

    if args.dry_run:
        print("将要请求：")
        print("  POST %s" % url)
        for k, v in headers.items():
            print("  %-22s %s" % (k + ":", mask(v) if k == "X-Refresh-Token" else v))
        print("  body                   {}")
        print("  x-auth-refresh-source  %s" % headers["X-Auth-Refresh-Source"])
        print("决策：%s" % ("--force 强制续期" if args.force else "剩余 %s 天 < %s 天" % (at_days, args.threshold_days)))
        return 0

    code, text = call_refresh(endpoint, path_part, headers)
    try:
        body = json.loads(text)
    except ValueError:
        body = {}
    flat = " ".join(text.split())[:200]

    if code != 200 or not isinstance(body, dict) or body.get("code") != 0 or not isinstance(body.get("data"), dict):
        print("续期失败：HTTP %s %s" % (code, flat))
        print("→ 不影响本次签到：继续使用现有 accessToken（fail-open）")
        emit_log(log_path, {"result": "REFRESH_REJECTED", "http": code,
                            "code": body.get("code") if isinstance(body, dict) else None,
                            "detail": flat})
        return 1

    data = body["data"]
    new_at = data.get("accessToken") or data.get("access_token")
    new_rt = data.get("refreshToken") or data.get("refresh_token")
    if not isinstance(new_at, str) or not new_at:
        emit_log(log_path, {"result": "ERROR", "stage": "parse",
                            "detail": "响应没有 accessToken，字段为 %s" % sorted(data)})
        return 1

    now_dt = now()
    now_ms = int(now_dt.timestamp() * 1000)
    assumed = False

    at_ms = data.get("expiresAt") or data.get("accessTokenExpiresAt")
    if not at_ms and isinstance(data.get("expiresIn"), (int, float)):
        at_ms = now_ms + int(data["expiresIn"]) * 1000
    if not (isinstance(at_ms, (int, float)) and at_ms > 0):
        at_ms = now_ms + ACCESS_DAYS * 86400 * 1000
        assumed = True

    rt_ms = data.get("refreshExpiresAt")
    if not rt_ms and isinstance(data.get("refreshExpiresIn"), (int, float)):
        rt_ms = now_ms + int(data["refreshExpiresIn"]) * 1000
    if not (isinstance(rt_ms, (int, float)) and rt_ms > 0):
        rt_ms = now_ms + REFRESH_DAYS * 86400 * 1000
        assumed = True

    doc["auth"]["accessToken"] = new_at
    if isinstance(new_rt, str) and new_rt:
        renew["refreshToken"] = new_rt
    renew["lastRenewedAt"] = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    renew["renewCount"] = int(renew.get("renewCount") or 0) + 1
    renew["expiresAtMs"] = rt_ms
    renew["expiresAt"] = iso_from_ms(rt_ms)
    renew["daysLeft"] = days_from_ms(rt_ms)
    if assumed:
        renew["expiryAssumed"] = True
    meta["expiresAtMs"] = at_ms
    meta["expiresAt"] = iso_from_ms(at_ms)
    meta["daysLeft"] = days_from_ms(at_ms)
    doc["_portable"] = meta          # 必须写回！否则 expiresAtMs 丢失，下次又判定"未知→续期"
    doc["_renew"] = renew

    if not write_back(path, doc, log_path):
        print("回写失败，已回滚；本次继续使用旧 accessToken（仍可用）")
        return 1

    print("续期成功：新 accessToken %s，新 refreshToken %s"
          % (mask(new_at), mask(new_rt) if new_rt else "服务端未返回（沿用旧值）"))
    print("  accessToken 到期 %s｜refreshToken 到期 %s%s"
          % (iso_from_ms(at_ms), iso_from_ms(rt_ms), "（服务端未给到期时间，按常量估算）" if assumed else ""))
    emit_log(log_path, {"result": "RENEWED", "accessTokenDaysLeft": days_from_ms(at_ms),
                        "refreshTokenDaysLeft": days_from_ms(rt_ms),
                        "refreshTokenRotated": bool(isinstance(new_rt, str) and new_rt),
                        "expiryAssumed": assumed,
                        "responseKeys": sorted(data)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
