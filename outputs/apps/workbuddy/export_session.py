#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 作者：EasonShu
"""把本机加密凭据导出成「可搬运的明文 session」，供异地常开设备使用。
"""

import argparse
import datetime
import json
import os
import subprocess
import sys

DEFAULT_PROJECT = os.path.dirname(os.path.abspath(__file__))
SENTINEL = "RUNTIME_MUST_NOT_BE_CALLED"   # 故意指向不存在的 exe，用来证明没走解密


def _iso(ms):
    if not isinstance(ms, (int, float)) or ms <= 0:
        return None
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def _days_left(ms):
    if not isinstance(ms, (int, float)) or ms <= 0:
        return None
    return round((ms / 1000 - datetime.datetime.now().timestamp()) / 86400, 1)


def load_signin(project):
    project = os.path.abspath(project)
    entry = os.path.join(project, "signin.py")
    if not os.path.exists(entry):
        raise SystemExit("找不到 %s —— 用 --project 指定 workbuddy-auto-signin 所在目录" % entry)
    sys.path.insert(0, project)
    import signin  # noqa: E402
    return signin, entry


def decrypt_field(signin, value, what):
    """把可能是 sym-v1 信封的字段解成明文；已经是字符串就原样返回。"""
    if isinstance(value, str) and value:
        return value
    if not isinstance(value, dict):
        raise SystemExit("%s 字段格式不认识（既不是字符串也不是加密信封）" % what)
    try:
        return signin._run_auth_helper(signin.find_workbuddy_runtime(),
                                       {"operation": "decrypt", "value": value})["accessToken"]
    except Exception as e:
        raise SystemExit("解密 %s 失败：%s" % (what, e))


def build_portable(signin, raw, with_refresh=True):
    """raw = 本机加密 session；返回可直接被任何机器读的明文 session。"""
    session = signin.resolve_session(raw)      # ← 唯一的本机依赖：调客户端解出明文 accessToken
    auth = session.get("auth") or {}
    raw_auth = raw.get("auth") or {}
    account = session.get("account") or {}

    token = auth.get("accessToken")
    if not signin._valid_token(token):
        raise SystemExit("解密后的 token 不合法，请更新脚本或重新登录客户端")

    portable = {
        "auth": {
            "accessToken": token,
            "tokenType": auth.get("tokenType") or "Bearer",
            "domain": auth.get("domain") or "",
            "endpoint": auth.get("endpoint") or signin.DEFAULT_ENDPOINT,
        },
        "account": {
            "uid": account.get("uid"),
            "enterpriseId": account.get("enterpriseId") or "",
        },
        "_portable": {
            "exportedAt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "expiresAt": _iso(auth.get("expiresAt")),
            "refreshExpiresAt": _iso(auth.get("refreshExpiresAt")),
            "expiresAtMs": auth.get("expiresAt"),
            "refreshExpiresAtMs": auth.get("refreshExpiresAt"),
            "daysLeft": _days_left(auth.get("expiresAt")),
            "note": "明文凭据，等价于账号本身；勿提交到版本库。到期前回本机重跑本脚本。",
        },
    }

    if with_refresh:
        refresh_raw = raw_auth.get("refreshToken")
        endpoint = portable["auth"]["endpoint"]
        # 域名：优先 credentials 里的 domain；缺省时从 endpoint 反推主机名
        domain = portable["auth"]["domain"] or endpoint.split("//", 1)[-1].split("/", 1)[0]
        block = {
            "refreshToken": decrypt_field(signin, refresh_raw, "refreshToken"),
            "domain": domain,
            "endpoint": endpoint,
            "path": "/v2/plugin/auth/token/refresh",
            "sourceHeader": "plugin",
            "obtainedAt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "expiresAt": _iso(raw_auth.get("refreshExpiresAt")),
            "expiresAtMs": raw_auth.get("refreshExpiresAt"),
            "daysLeft": _days_left(raw_auth.get("refreshExpiresAt")),
            "note": "服务器续期用。每次续期成功必须回写新值，否则断链。效力强于 accessToken。",
        }
        portable["_renew"] = block

    return portable


def write_secure(path, payload):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    if os.name == "posix":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    if os.name == "posix":
        os.chmod(path, 0o600)


def write_env(path, portable, session_path):
    """生成可直接粘进容器平台「环境变量」的 KEY=VALUE 清单。

    带上 *_EXPIRES_AT 很重要：容器侧据此判断是否需要续期。
    缺了它，容器首次启动会误判「到期时间未知」而白续期一次（会轮换令牌）。
    """
    a = portable.get("auth") or {}
    m = portable.get("_portable") or {}
    r = portable.get("_renew") or {}
    acc = portable.get("account") or {}
    at_exp = m.get("expiresAt") or "?"
    rt_exp = m.get("refreshExpiresAt") or r.get("expiresAt") or "?"
    lines = [
        "# WorkBuddy 自动签到 —— 容器平台环境变量",
        "# 生成于 %s ｜ 凭据到期 %s ｜ 续期链到期 %s" % (m.get("exportedAt", "?"), at_exp, rt_exp),
        "# 本文件等同账号凭据，勿提交到公开仓库；配套 session.json 在 %s" % session_path,
        "",
        "WB_ACCESS_TOKEN=%s" % a.get("accessToken", ""),
        "WB_REFRESH_TOKEN=%s" % r.get("refreshToken", ""),
        "WB_DOMAIN=%s" % a.get("domain", ""),
        "WB_ENDPOINT=%s" % a.get("endpoint", ""),
        "WB_UID=%s" % (acc.get("uid") or ""),
        "WB_ENTERPRISE_ID=%s" % (acc.get("enterpriseId") or ""),
        "WB_ACCESS_EXPIRES_AT=%s" % (m.get("expiresAt") or ""),
        "WB_REFRESH_EXPIRES_AT=%s" % (m.get("refreshExpiresAt") or r.get("expiresAt") or ""),
        "TZ=Asia/Shanghai",
        "PORT=5000",
        "",
    ]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    if os.name == "posix":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    if os.name == "posix":
        os.chmod(path, 0o600)


def verify(signin_entry, session_path):
    """用「不存在客户端」的环境跑 status，证明只靠这一个文件就能通。"""
    env = dict(os.environ)
    env["WORKBUDDY_AUTH_FILE"] = session_path
    env["WORKBUDDY_EXE"] = SENTINEL          # 若脚本尝试解密，会立刻在 RUNTIME_NOT_FOUND 上炸
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        p = subprocess.run([sys.executable, signin_entry, "status"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=120, env=env)
    except subprocess.TimeoutExpired:
        return False, "验证超时"
    out = (p.stdout or b"").decode("utf-8", "replace").strip()
    try:
        body = json.loads(out.splitlines()[-1]) if out else {}
    except ValueError:
        return False, "无法解析输出：%s" % out[:200]
    if p.returncode == 0 and 200 <= body.get("http", 0) < 300:
        return True, body
    return False, body or (p.stderr or b"").decode("utf-8", "replace")[:200]


def main():
    ap = argparse.ArgumentParser(description="导出可搬运的明文签到凭据")
    ap.add_argument("--project", default=DEFAULT_PROJECT,
                    help="workbuddy-auto-signin 项目目录（含 signin.py）")
    ap.add_argument("--out", default=None, help="输出路径，默认 <project>/deploy/session.json")
    ap.add_argument("--auth-file", default=None, help="覆盖本机凭据文件路径")
    ap.add_argument("--verify", dest="verify", action="store_true", default=True,
                    help="导出后立刻验证无客户端也能跑通（默认开）")
    ap.add_argument("--no-verify", dest="verify", action="store_false")
    ap.add_argument("--with-refresh", dest="with_refresh", action="store_true", default=True,
                    help="一并解出 refreshToken，供服务器链式自续期（默认开）")
    ap.add_argument("--no-refresh", dest="with_refresh", action="store_false",
                    help="只导出 accessToken（更保守，需每 55 天重导）")
    ap.add_argument("--emit-env", default=None, nargs="?", const="auto",
                    help="额外生成 KEY=VALUE 环境变量清单（容器平台用）；给路径或留空取同名 .env")
    args = ap.parse_args()

    signin, entry = load_signin(args.project)

    auth_file = args.auth_file or signin.find_auth_file()[0]
    if not auth_file or not os.path.exists(auth_file):
        raise SystemExit("未找到本机登录凭据，请先登录 WorkBuddy 桌面端")

    raw = signin.load_session_retry(auth_file)
    portable = build_portable(signin, raw, with_refresh=args.with_refresh)

    out = args.out or os.path.join(os.path.abspath(args.project), "session.json")
    write_secure(out, portable)

    env_path = None
    if args.emit_env:
        env_path = (os.path.join(os.path.dirname(out), "workbuddy.env")
                    if args.emit_env == "auto" else args.emit_env)
        write_env(env_path, portable, out)

    meta = portable["_portable"]
    print("已导出 : %s" % out)
    print("账号   : uid=%s%s" % (
        (portable["account"]["uid"] or "")[:8] + "…",
        "（企业号）" if portable["account"]["enterpriseId"] else "（个人号）"))
    print("后端   : %s" % portable["auth"]["endpoint"])
    print("到期   : %s（还有 %s 天）" % (meta["expiresAt"], meta["daysLeft"]))
    print("刷新期 : %s" % meta["refreshExpiresAt"])
    print("token  : 长度 %d，已省略（本文件等同账号，别外传）" % len(portable["auth"]["accessToken"]))
    rn = portable.get("_renew")
    if rn:
        print("续期链 : 已内置 refreshToken（长度 %d）→ 到 %s（还有 %s 天）"
              % (len(rn["refreshToken"]), rn["expiresAt"], rn["daysLeft"]))
    else:
        print("续期链 : 未导出 —— 需每 55 天回本机重导")

    if env_path:
        print("环境变量 : %s（可直接粘进容器平台，含 WB_* 与 TZ）" % env_path)

    if args.verify:
        ok, detail = verify(entry, out)
        if ok:
            print("\n[验证通过] 断开客户端依赖也跑通了：")
            print("  http=%s  needs_attention=%s" % (detail.get("http"), detail.get("needs_attention")))
            print("  （该次调用把 WORKBUDDY_EXE 指向不存在的路径，仍成功 → 全程未走解密）")
        else:
            print("\n[验证失败] %s" % json.dumps(detail, ensure_ascii=False)[:400])
            return 1

    print("\n下一步：把本目录下的 4 个文件传到服务器，执行 sh install.sh（步骤见 使用说明.html）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
