#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 作者：EasonShu
r"""TRAE 每日签到 —— 与 WorkBuddy 那套**完全独立**的第二套自动签到。
"""

import argparse
import datetime
import json
import os
import platform
import random
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# ---------------------------------------------------------------- 常量 ----
CLAIM_PATH = "/trae/api/v2/ug/checkin_credits/claim"
# 只读探活用：返回今天签没签、今天能领多少。不发请求也能判断凭据死活。
STATUS_PATH = "/trae/api/v2/ug/checkin_credits/status"
# 兜底探活 + 取总积分：权益包额度减已用。
ENTITLEMENT_PATH = "/trae/api/v2/pay/user_current_entitlement_list"
# 签名 payload 的第二段就是它，路径写错会导致续期被服务端拒绝。
EXCHANGE_PATH = "/trae/api/v3/oauth/ExchangeToken"

CLIENT_ID = "en1oxy7wnw8j9n"
CLIENT_VERSION = "0.1.43"
PLATFORM_CODE = "SOLO_PC"
DEFAULT_HOST = "https://api.trae.cn"

# 签名密钥的键名里藏着真实设备号，可作为老凭据的兜底来源
_DC_KEY_RE = re.compile(r"^iCubeAuthInfo://icube-dc:(\d+)$")

# 剩余天数少于这个值就顺手续期（续期失败不阻断签到）
RENEW_DAYS = 10
# 剩余秒数少于这个值也续期（对齐 TRAE 客户端"临过期才换"的做法）
RENEW_SECONDS_FLOOR = 5 * 60

VALID_MODES = ("silent", "silent-poll", "status", "doctor")
READ_ONLY_MODES = ("status", "doctor")

# 多账号轮换状态（只属于本套，与 WorkBuddy 无关）
ORDER_FILE = "trae-order.json"

UA = "axios/1.7.7"

# ---------------------------------------------------------------------------
# 模拟客户端 / 出口 IP：签到本质是在替客户端做"模拟登录"，请求若永远顶着同一
# 个 UA、同一条 XFF，服务端一眼就能认出是脚本。这里给签到请求随机伪装成一台
# 现实桌面设备（Electron/Chrome 观感）与一个公网观感的出口 IP 链。
#
#   真正的源 IP 由本机网络栈决定，这里只在应用层补代理常用头，对端是否采信
#   不可控；主要价值是让每次请求的"客户端 + 出口"看起来更接近真人。
#
#   置 TRAE_NO_SPOOF=1 可整体关闭，退回固定 UA、不发伪装头。
# ---------------------------------------------------------------------------
_SPOOF_LOCKS = {}

# Electron/Chromium 观感的桌面 UA 模板，随机拼版本，贴近 TRAE 官方桌面端。
def _spoof_ua():
    def ver(major):
        return "%d.%d.%d.%d" % (major, random.randint(0, 9), random.randint(0, 9), random.randint(0, 9))
    chrome = ver(random.randint(105, 130))
    templates = [
        # Windows 桌面：Electron / Edge / Opera / Chrome / Firefox / 老 Win7 老内核
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/%s Safari/537.36 Electron/%d.%d.%d" % (chrome, random.randint(25, 33), random.randint(0, 5), random.randint(0, 9)),
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/%s Safari/537.36 Edg/%s" % (chrome, ver(random.randint(110, 130))),
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/%s Safari/537.36 OPR/%s" % (chrome, ver(random.randint(90, 112))),
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:%d.0) Gecko/20100101 Firefox/%d.0" % (random.randint(115, 130), random.randint(115, 130)),
        "Mozilla/5.0 (Windows NT 6.1; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/%s Safari/537.36 Electron/%d.%d.%d" % (chrome, random.randint(22, 26), random.randint(0, 2), random.randint(0, 9)),
        # macOS 桌面：Chrome / Electron / Safari / Firefox
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/%s Safari/537.36" % chrome,
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/%s Safari/537.36 Electron/%d.%d.%d" % (chrome, random.randint(25, 33), random.randint(0, 5), random.randint(0, 9)),
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/%d.%d Safari/605.1.15" % (random.randint(14, 18), random.randint(0, 6)),
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7; rv:%d.0) Gecko/20100101 Firefox/%d.0" % (random.randint(115, 130), random.randint(115, 130)),
        # Linux 桌面：Chromium / Electron / Firefox
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/%s Safari/537.36 Electron/%d.%d.%d" % (chrome, random.randint(22, 28), random.randint(0, 2), random.randint(0, 9)),
        "Mozilla/5.0 (X11; Linux x86_64; rv:%d.0) Gecko/20100101 Firefox/%d.0" % (random.randint(115, 130), random.randint(115, 130)),
    ]
    return random.choice(templates)


def _spoof_ip():
    """生成一个"看着像公网"的 IPv4（避开常见保留段），用于伪装出口。"""
    return ".".join(map(str, [random.randint(1, 223)] + [random.randint(2, 254) for _ in range(3)]))


def spoof_client_headers():
    """同一轮签到内缓存一份 UA + 出口 IP，保证一整个过程是"同一个客户端"。"""
    if not _SPOOF_LOCKS:
        ip = _spoof_ip()
        hop = _spoof_ip()
        _SPOOF_LOCKS.update({
            "User-Agent": _spoof_ua(),
            "X-Forwarded-For": "%s, %s" % (hop, ip),
            "last_ip": ip,
        })
    return _SPOOF_LOCKS


def apply_request_spoof(req):
    """给 urllib 请求附加模拟客户端 / 出口 IP 头。TRAE_NO_SPOOF=1 时关闭。"""
    if os.environ.get("TRAE_NO_SPOOF") == "1":
        req.add_header("User-Agent", UA)
        return
    h = spoof_client_headers()
    req.add_header("User-Agent", h["User-Agent"])
    req.add_header("X-Forwarded-For", h["X-Forwarded-For"])
    req.add_header("X-Real-IP", h["last_ip"])
    req.add_header("Client-IP", h["last_ip"])

# 设备名额用完：本机这个真实设备号今天已经被别人用掉了。
# 这不是"本账号签过"，也不是需要重试的故障，单独一类。
DEVICE_DONE_CODE = 9095

# 结果分类：与 notify.py 的 SUCCESS/QUIET/ERROR 三档对齐
SUCCESS_RESULTS = ("CLAIMED",)
QUIET_RESULTS = ("ALREADY", "STATUS", "AUTH_READY", "DEVICE_DONE")
ERROR_RESULTS = ("ERROR", "UNKNOWN", "NETWORK", "TIMEOUT",
                 "NO_AUTH", "AUTH_ERROR", "AUTH_REJECTED", "FORBIDDEN")

# claim 返回里的"用户级已签到"措辞 vs "设备级拦截"措辞
USER_DONE_WORDS = ("已签到", "已经签到", "明日再来", "今日已完成", "已领取")
USER_DONE_WORDS_EN = ("already", "checked", "claimed")
DEVICE_WORDS = ("设备", "device", "machine")

# 可重试的错误码。
#
# 9074「当前参与用户太多，请稍后再试」经过变量分离实测（2026-10-01）后定性为
# **设备号不被识别**：同一个 token 下，
#     遥测 UUID / 任意带后缀的值        -> 稳定 9074
#     真实注册设备号 3129640255512713   -> 立刻变成 9095（设备今日已签）
# 也就是说 9074 的"人太多"是句推托话，实际原因是服务端不认识这个设备号。
# 它仍然是可重试的（万一真有排队），但指望重试能解决是错的 ——
# **正解是把 x-device-id 换成真实设备号**，见 account_device_id()。
#
# ⚠️ 判断顺序很重要：9074 的措辞里带"稍后再试"，必须在"已签到"之前判定，
#    否则会被错误地当成"这账号今天签过了"而静默吞掉一次签到。
TRANSIENT_CODES = (9074, 429, 500, 502, 503, 504)
TRANSIENT_WORDS = ("太多", "稍后", "稍候", "繁忙", "拥挤", "排队", "限流", "请重试")
TRANSIENT_WORDS_EN = ("too many", "try again", "try later", "busy", "rate limit",
                      "too frequent", "temporarily")

# 重试策略：对齐上游 trae-check 的 retryCount=3 / retryDelay=60s
DEFAULT_RETRY = 3
DEFAULT_RETRY_DELAY = 60.0


# ------------------------------------------------------------ 基础工具 ----
def _utf8_console():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def stamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?"
    r"(?:([Zz])|([+-])(\d{2}):?(\d{2}))?$")

_EPOCH = datetime.datetime(1970, 1, 1)


def _parse_iso(text):
    """手撕 ISO-8601 → epoch 秒。

    为什么不用 datetime.fromisoformat：那是 Python 3.7 才加的，
    而 CentOS 7 / Debian 9 这类老服务器还在跑 3.6，一调就 AttributeError。
    本脚本要能在 python3.6 上跑，所以自己解析。
    """
    m = _ISO_RE.match(text.strip())
    if not m:
        return None
    year, month, day, hour, minute, second = (int(m.group(i)) for i in range(1, 7))
    frac = m.group(7) or ""
    micro = int((frac + "000000")[:6]) if frac else 0
    try:
        dt = datetime.datetime(year, month, day, hour, minute, second, micro)
    except ValueError:
        return None
    offset = 0
    if m.group(9):                       # 带 ±HH:MM 偏移
        sign = -1 if m.group(9) == "-" else 1
        offset = sign * (int(m.group(10)) * 3600 + int(m.group(11)) * 60)
    # dt 按 UTC 墙上时间算，再减掉时区偏移 = 真正的 UTC epoch
    return (dt - _EPOCH).total_seconds() - offset


def parse_expiry(value):
    """把 TRAE 的到期字段统一成 epoch 秒。

    实测它有两种形态，必须都吃：
      * 登录时写的 ISO 串      "2026-10-14T23:21:45.286Z"
      * 续期后写的毫秒数字     1760484105000（也可能是这种数字的字符串）
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 1e11 else v
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"-?\d+(\.\d+)?", text):
        v = float(text)
        return v / 1000.0 if v > 1e11 else v
    return _parse_iso(text)


def http_json(url, headers=None, body=None, timeout=30, method=None):
    """发一个 JSON 请求。返回 (数据dict|None, 错误信息, HTTP码)。

    不抛异常——调用方统一按 (None, err, code) 处理，方便把网络问题
    归类成 NETWORK / TIMEOUT / AUTH_ERROR 这些结果。
    """
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    method = method or ("POST" if data is not None else "GET")
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    apply_request_spoof(req)  # 随机 UA + 模拟出口 IP（TRAE_NO_SPOOF=1 关闭）
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            code = resp.getcode()
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        return None, "HTTP %s" % exc.code, exc.code
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        text = str(reason).lower()
        if "timed out" in text or "timeout" in text:
            return None, "请求超时（%ss）" % timeout, None
        return None, "网络不可达：%s" % reason, None
    except Exception as exc:                       # socket.timeout 等
        return None, "请求失败：%s" % exc, None

    try:
        return json.loads(raw), "", code
    except ValueError:
        return None, "响应不是 JSON：%s" % raw[:120], code


def api_succeeded(data):
    return (isinstance(data, dict)
            and (data.get("code") in (0, 200)
                 or data.get("success") is True
                 or data.get("status") == "success"))


def dig(obj, *keys):
    """依次取 obj 里的键，取到第一个非空值就返回。"""
    if not isinstance(obj, dict):
        return None
    for k in keys:
        v = obj.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


# ------------------------------------------------------------ 凭据装载 ----
def normalize_credential(raw):
    """把导出的 json 摊平成一张统一的凭据表。

    兼容两种形状：
      * trae_export.py 的产物（auth / signing / account / authInfo 分块）
      * 手搓的扁平对象（token / deviceId / privateKeyPEM …… 直接在顶层）
    """
    auth = raw.get("auth") if isinstance(raw.get("auth"), dict) else {}
    signing = raw.get("signing") if isinstance(raw.get("signing"), dict) else {}
    account = raw.get("account") if isinstance(raw.get("account"), dict) else {}
    auth_info = raw.get("authInfo") if isinstance(raw.get("authInfo"), dict) else {}

    def pick(*keys):
        for src in (auth, raw, auth_info, account):
            for k in keys:
                v = src.get(k)
                if v not in (None, ""):
                    return v
        return ""

    cred = {
        "token": pick("token"),
        "refreshToken": pick("refreshToken"),
        "deviceId": str(pick("deviceId")),
        "machineId": str(pick("machineId")),
        "userId": str(pick("userId")),
        "accountName": pick("accountName", "username", "name"),
        "host": (pick("host") or DEFAULT_HOST).rstrip("/"),
        "privateKeyPEM": signing.get("privateKeyPEM") or raw.get("privateKeyPEM") or "",
        "publicKeyPEM": signing.get("publicKeyPEM") or raw.get("publicKeyPEM") or "",
        "ahaDeviceId": str(pick("ahaDeviceId")),
        "appVersion": str(pick("appVersion")),
        "expiresAt": pick("expiresAt", "expiredAt"),
        "refreshExpiresAt": pick("refreshExpiresAt", "refreshExpiredAt"),
    }
    # 老凭据没有 ahaDeviceId：从签名密钥的键名 iCubeAuthInfo://icube-dc:<数字> 反推出来。
    # 这个键名就在凭据自身里，所以不需要回头找客户端文件。
    if not cred["ahaDeviceId"]:
        for entry in (signing.get("entries") or []):
            m = _DC_KEY_RE.match(str((entry or {}).get("key") or ""))
            if m:
                cred["ahaDeviceId"] = m.group(1)
                break
    return cred


def credential_problems(cred):
    """离线体检：缺什么列什么。doctor 模式就靠它。"""
    problems = []
    if not cred["token"]:
        problems.append("缺 token")
    if not cred["refreshToken"]:
        problems.append("缺 refreshToken（到期后无法自动续期）")
    if not cred["deviceId"] or not cred["machineId"]:
        problems.append("缺 deviceId / machineId")
    if not cred["privateKeyPEM"]:
        problems.append("缺设备私钥 privateKeyPEM（续期签名要用）")
    if not cred["userId"]:
        problems.append("缺 userId（多账号要靠它拼独立设备 ID）")
    return problems


def days_left(cred):
    exp = parse_expiry(cred["expiresAt"])
    if exp is None:
        return None
    return (exp - time.time()) / 86400.0


def need_renew(cred):
    exp = parse_expiry(cred["expiresAt"])
    if exp is None:
        return False                      # 解析不出来就别瞎续，直接拿去用
    left = exp - time.time()
    return left <= RENEW_SECONDS_FLOOR or left <= RENEW_DAYS * 86400.0


# -------------------------------------------------------------- 续期 ------
def refresh_credential(cred, timeout=60):
    """用 refreshToken 换新 token。返回 (新凭据片段 dict, 备注, 是否成功)。

    签名这一段必须对：payload 是六段用 \\n 拼起来的文本，
    用设备私钥（EC P-256）签 SHA-256，结果 base64 放进 DeviceProof.Signature。
    """
    import trae_ecdsa

    if not cred["refreshToken"]:
        return None, "没有 refreshToken，无法续期", False
    if not cred["privateKeyPEM"]:
        return None, "没有设备私钥，无法续期", False
    if not cred.get("ahaDeviceId"):
        # 不早退的话，下面会拿遥测 UUID 去请求，必然 401 "Token device not match"，
        # 而那个报错完全看不出是设备号的锅。这里直接说清楚。
        return None, ("凭据里没有真实设备号（ahaDeviceId），续期必被服务端拒绝；"
                      "请在本机用最新版 trae_export.py 重新导出后覆盖"), False

    timestamp = int(time.time())
    nonce = os.urandom(16).hex()
    payload = "\n".join(["POST", EXCHANGE_PATH, CLIENT_ID,
                         cred["refreshToken"], str(timestamp), nonce])
    try:
        signature = trae_ecdsa.sign_b64(payload, cred["privateKeyPEM"])
    except Exception as exc:
        return None, "签名失败：%s" % exc, False

    body = {
        "ClientID": CLIENT_ID,
        "ClientSecret": "",
        "RefreshToken": cred["refreshToken"],
        "DeviceInfo": {
            # ⚠️ 这里必须是 aha 真实设备号，不能是 storage.json 里的遥测 UUID。
            # 实测（2026-10-01）用遥测 UUID 会稳定拿到
            #   HTTP 401 / code 20403 / "Token device not match."
            # 换 aha 设备号立刻 200。这与签到接口的 9074 是同一个根因：
            # 遥测 UUID 只是 VS Code 的设备标识，服务端不认它是「注册设备」。
            # 另外注意：这个错和 x-cloudide-token 无关 —— 把该头换成垃圾值、
            # 甚至整个去掉，返回都一字不差，所以别往 token 方向排查。
            "DeviceID": account_device_id(cred),
            "MachineID": cred["machineId"],
            "PlatformCode": PLATFORM_CODE,
            "DeviceType": "PC",
            "DeviceName": os.environ.get("USER") or os.environ.get("USERNAME") or "trae-signin",
            "DeviceModel": "",
            "ClientVersion": CLIENT_VERSION,
            "DevicePublicKey": cred["publicKeyPEM"],
            "DeviceBrand": "",
            "DeviceCPU": "",
            "OSInfo": platform.system() or "Linux",
            "OSVersion": platform.release() or "",
        },
        "DeviceProof": {"Signature": signature, "Timestamp": timestamp, "Nonce": nonce},
        "IDEVersion": CLIENT_VERSION,
    }
    headers = {"Content-Type": "application/json", "x-cloudide-token": cred["token"]}
    data, err, code = http_json(cred["host"] + EXCHANGE_PATH, headers, body, timeout=timeout)
    if data is None:
        return None, "续期请求失败：%s" % err, False

    result = (data.get("Result") or {}) if isinstance(data, dict) else {}
    token = result.get("Token")
    refresh_token = result.get("RefreshToken")
    if not token or not refresh_token:
        msg = dig(data, "message", "msg") or ("HTTP %s" % code if code else "未知错误")
        return None, "续期被拒：%s" % msg, False

    return {
        "token": token,
        "refreshToken": refresh_token,
        "expiresAt": result.get("TokenExpireAt"),
        "refreshExpiresAt": result.get("RefreshExpireAt"),
    }, "已续期", True


def apply_refresh(raw, cred, refreshed):
    """把续期结果写回原始 json（auth + authInfo 两处都更新），保持文件结构不变。"""
    auth = raw.get("auth")
    if not isinstance(auth, dict):
        auth = {}
        raw["auth"] = auth
    auth["token"] = refreshed["token"]
    auth["refreshToken"] = refreshed["refreshToken"]
    if refreshed.get("expiresAt") is not None:
        auth["expiresAt"] = refreshed["expiresAt"]
    if refreshed.get("refreshExpiresAt") is not None:
        auth["refreshExpiresAt"] = refreshed["refreshExpiresAt"]

    auth_info = raw.get("authInfo")
    if isinstance(auth_info, dict):
        auth_info["token"] = refreshed["token"]
        auth_info["refreshToken"] = refreshed["refreshToken"]
        if refreshed.get("expiresAt") is not None:
            auth_info["expiredAt"] = refreshed["expiresAt"]
        if refreshed.get("refreshExpiresAt") is not None:
            auth_info["refreshExpiredAt"] = refreshed["refreshExpiresAt"]

    # 同步刷新 _portable 的到期展示，省得人肉看 json 猜
    portable = raw.get("_portable")
    if isinstance(portable, dict):
        exp = parse_expiry(refreshed.get("expiresAt"))
        ref = parse_expiry(refreshed.get("refreshExpiresAt"))
        if exp is not None:
            portable["expiresAt"] = datetime.datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M:%S")
            portable["daysLeft"] = round((exp - time.time()) / 86400.0, 1)
        if ref is not None:
            portable["refreshExpiresAt"] = datetime.datetime.fromtimestamp(ref).strftime("%Y-%m-%d %H:%M:%S")
            portable["refreshDaysLeft"] = round((ref - time.time()) / 86400.0, 1)
    return raw


def write_secure(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# -------------------------------------------------------------- 签到 ------
def _host_brand():
    """机型。Windows 读注册表，取不到返回 "PC"；其它平台用架构名。"""
    if platform.system() == "Windows":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"HARDWARE\DESCRIPTION\System\BIOS") as key:
                for name in ("SystemProductName", "BaseBoardProduct"):
                    try:
                        value = winreg.QueryValueEx(key, name)[0]
                    except OSError:
                        continue
                    if value:
                        return str(value)
        except Exception:
            pass
        return "PC"
    return platform.machine() or ""


def device_fingerprint_headers(cred):
    """官方客户端随请求带的四个设备指纹头。

    ⚠️ 实测（2026-10-01，变量分离）：**这四个头在本机当前服务端版本上对签到结果
    没有任何影响** —— 带与不带、brand 用机型还是用 "windows"、type 用 "windows"
    还是 "win32"，返回逐字相同（都是 9095）。真正决定结果的自始至终只有 x-device-id。

    保留它只是为了贴近官方客户端、降低将来服务端校验变严的风险。
    **别指望它解决 9074** —— 那是设备号不被识别，只有换真实设备号才有用。

    取不到的值就不发（发空头反而可能被拒）。置 TRAE_NO_FINGERPRINT=1 可整体关掉。
    """
    if os.environ.get("TRAE_NO_FINGERPRINT") == "1":
        return {}
    headers = {}
    brand = os.environ.get("TRAE_DEVICE_BRAND") or _host_brand()
    if brand:
        headers["x-device-brand"] = brand
    os_name = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}.get(
        platform.system(), (platform.system() or "").lower())
    if os_name:
        headers["x-device-type"] = os.environ.get("TRAE_DEVICE_TYPE") or os_name
    version = platform.version() or platform.release()
    if version:
        headers["x-os-version"] = os.environ.get("TRAE_OS_VERSION") or version
    app_version = cred.get("appVersion") or CLIENT_VERSION
    if app_version:
        headers["x-app-version"] = str(app_version)
    return headers


def account_device_id(cred):
    """对外声明的 x-device-id —— **必须用真实注册设备号**。

    2026-10-01 变量分离实测（同一账号、同一 token、当天尚未签到）：

        x-device-id = 遥测 UUID（0a9d7b60-…）         -> 9074「当前参与用户太多，请稍后再试」
        x-device-id = 遥测 UUID + "-<userId>"          -> 9074
        x-device-id = 3129640255512713（真实设备号）    -> 9095「当前设备今日已经签到」
        x-device-id = 3129640255512713 + "-<userId>"   -> 9074
        再把四个指纹头加加减减                          -> 结果不变

    两个结论：
      1. **9074 不是"人太多"，而是"这个设备号我不认识"。** 遥测 UUID 不是注册设备，
         服务端不认它，于是丢一个泛化的排队错误回来。换成真实设备号，返回立刻变成
         有意义的确定性结果。
      2. **给设备号拼 userId 是有害的**（上游 trae-check 正是这么做的）：有效设备号
         一旦加后缀就重新变成"不认识的设备"，白回 9074。多账号也没法靠这招伪装成
         多设备 —— 服务端按真实设备号去重，一台机器每天只能签一个账号（见 DEVICE_DONE）。
    """
    if cred.get("ahaDeviceId"):
        return str(cred["ahaDeviceId"])
    return cred["deviceId"]


def request_headers(cred, json_body=False):
    """签到相关请求的公共头。"""
    headers = {"Authorization": "Cloud-IDE-JWT %s" % cred["token"],
               "x-device-id": account_device_id(cred)}
    if json_body:
        headers["Content-Type"] = "application/json"
    headers.update(device_fingerprint_headers(cred))
    return headers


def checkin_state(cred, timeout=20):
    """只读：今天签没签。返回 (checked_in, today_credit, 错误说明)。

    checked_in 为 None 表示这次没问出来（网络/鉴权问题），调用方要当心，
    别把"没问出来"当成"没签到"。
    """
    data, err, code = http_json(cred["host"] + STATUS_PATH, request_headers(cred), None,
                                timeout=timeout, method="GET")
    if data is None:
        return None, None, err
    if api_succeeded(data):
        return bool(data.get("checked_in")), data.get("credits"), ""
    return None, None, str(dig(data, "message", "msg") or err or "status 接口无有效返回")


def claim_once(cred, timeout=30):
    """发一次领取请求。返回 (结果, 记录, 是否值得重试)。"""
    data, err, code = http_json(cred["host"] + CLAIM_PATH, request_headers(cred, json_body=True),
                                {}, timeout=timeout)

    if data is None:
        if code in (401, 403):
            result = "AUTH_ERROR" if code == 401 else "FORBIDDEN"
            return result, {"result": result, "note": "凭据被服务端拒绝（%s）" % err,
                            "needs_attention": True}, False
        if err.startswith("请求超时"):
            return "TIMEOUT", {"result": "TIMEOUT", "note": err,
                               "needs_attention": True}, True
        return "NETWORK", {"result": "NETWORK", "note": err,
                           "needs_attention": True}, True

    if api_succeeded(data):
        note = dig(data, "message", "msg") or "签到成功"
        if note in ("success", "ok"):
            note = "签到成功"
        return "CLAIMED", {"result": "CLAIMED", "note": str(note),
                           "needs_attention": False}, False

    api_code = data.get("code")
    msg = str(dig(data, "message", "msg") or "")
    lower = msg.lower()

    # ⚠️ 判定顺序：设备名额用完 → 限流 → 设备拦截 → 账号已签到 → 其它失败。
    transient = (api_code in TRANSIENT_CODES
                 or any(w in msg for w in TRANSIENT_WORDS)
                 or any(w in lower for w in TRANSIENT_WORDS_EN))
    mentions_device = (any(w in msg for w in DEVICE_WORDS)
                       or any(w in lower for w in DEVICE_WORDS[1:]))
    mentions_done = (any(w in msg for w in USER_DONE_WORDS)
                     or any(w in lower for w in USER_DONE_WORDS_EN))
    # 设备名额用完：措辞是"设备 + 今天已经签到"（实测 code 9095）。
    # 注意这**不是**本账号签过 —— 是本机这个真实设备号今天已经被用掉了。
    # 必须和 ALREADY 分开报，否则用户会以为两个账号都领到了。
    device_done = (api_code == DEVICE_DONE_CODE) or (mentions_device and mentions_done)
    blocked = mentions_device and not mentions_done
    already = (not mentions_device and not transient
               and (api_code == 1001 or mentions_done))

    if device_done:
        return "DEVICE_DONE", {
            "result": "DEVICE_DONE", "code": api_code,
            "note": "%s（本机设备名额今日已用完：同一台设备每天只认一个账号）"
                    % (msg or "当前设备今日已签到"),
            "needs_attention": False}, False
    if transient:
        return "BUSY", {"result": "ERROR", "code": api_code,
                        "note": msg or "服务端繁忙", "needs_attention": True}, True
    if already:
        return "ALREADY", {"result": "ALREADY", "note": msg or "今日已签到",
                           "needs_attention": False}, False
    if blocked:
        return "ERROR", {"result": "ERROR", "note": msg or "设备级去重拦截",
                         "needs_attention": True}, False
    return "ERROR", {"result": "ERROR", "note": msg or "签到失败",
                     "code": api_code, "needs_attention": True}, False


def claim(cred, timeout=30, attempts=DEFAULT_RETRY, delay=DEFAULT_RETRY_DELAY, log=None):
    """带重试的领取。返回 (结果字符串, 记录 dict)。

    为什么要重试：多账号连打时，排在后面的账号经常撞上服务端排队
    （code 9074「当前参与用户太多」）。这不是账号或凭据的问题，过一会儿
    再打就通 —— 上游 trae-check 也是失败重试 3 次、每次隔 60 秒。
    """
    result, rec = "ERROR", {"result": "ERROR", "note": "未执行", "needs_attention": True}
    for i in range(attempts + 1):
        result, rec, retryable = claim_once(cred, timeout=timeout)
        if not retryable:
            return result, rec
        if i < attempts:
            if log:
                log("服务端繁忙未通（%s），%.0f 秒后重试（第 %d/%d 次）"
                    % (rec.get("note") or "", delay, i + 1, attempts))
            time.sleep(delay)
    # 重试用尽：把 BUSY 归一成 ERROR，让通知里看得出来是"重试过还是没通"
    if result == "BUSY":
        rec = dict(rec)
        rec["result"] = "ERROR"
        rec["note"] = "%s（已重试 %d 次仍未通，等下次轮询）" % (
            rec.get("note") or "服务端繁忙", attempts)
        rec["needs_attention"] = True
        return "ERROR", rec
    return result, rec


def extract_remaining_credits(data):
    """从权益包里算剩余积分：Σ(额度 - 已用)。取不到就返回 None。"""
    packs = (data or {}).get("user_entitlement_pack_list")
    if not isinstance(packs, list) or not packs:
        return None
    remaining, found = 0, False
    for pack in packs:
        if not isinstance(pack, dict):
            continue
        base = pack.get("entitlement_base_info") or {}
        quota = base.get("quota") or {}
        limit = quota.get("credits_limit")
        used = (pack.get("usage") or {}).get("credits_amount") or 0
        if isinstance(limit, (int, float)) and limit > 0:
            found = True
            remaining += max(int(limit) - (int(used) if isinstance(used, (int, float)) else 0), 0)
    return remaining if found else None


def probe(cred, timeout=20):
    """只读探活：查今天签没签，顺带确认凭据还活着。不发签到请求。

    status 接口是唯一稳定可用的只读入口（实测 credits/balance 那些路径已 404）。
    它还能区分「凭据死了」和「今天已签到」：假 token 会返回 code 1001 + 鉴权提示。
    """
    headers = request_headers(cred)
    code = None
    data, err, code = http_json(cred["host"] + STATUS_PATH, headers, None,
                                timeout=timeout, method="GET")
    if data is not None:
        if api_succeeded(data):
            checked = bool(data.get("checked_in"))
            return "STATUS", {
                "result": "STATUS",
                "note": "今日已签到" if checked else "今日尚未签到",
                "checked_in": checked,
                "today_credit": data.get("credits"),
                "needs_attention": False}
        msg = str(dig(data, "message", "msg") or "")
        if data.get("code") == 1001 or "authenticate" in msg.lower() or "unauthorized" in msg.lower():
            return "AUTH_ERROR", {"result": "AUTH_ERROR",
                                  "note": "凭据被服务端拒绝：%s" % (msg or err),
                                  "needs_attention": True}

    # 兜底：换权益接口再确认一次，顺带把总积分取回来
    data2, err2, code2 = http_json(cred["host"] + ENTITLEMENT_PATH, headers,
                                   {"require_usage": True}, timeout=timeout, method="POST")
    if data2 is not None and api_succeeded(data2):
        total = extract_remaining_credits(data2)
        return "STATUS", {"result": "STATUS", "note": "凭据有效",
                          "total_credits": total, "needs_attention": False}
    if code in (401, 403) or code2 in (401, 403):
        return "AUTH_ERROR", {"result": "AUTH_ERROR",
                              "note": "凭据被服务端拒绝（HTTP %s）" % (code2 or code),
                              "needs_attention": True}
    return "UNKNOWN", {"result": "UNKNOWN",
                       "note": "探活未拿到有效数据（%s）" % (err2 or err or "无响应"),
                       "needs_attention": True}


def sign_in(cred, args):
    """领取流程：先只读确认没签过 → 再领取（带重试）→ 成功后复核。

    为什么要绕这一圈：claim 接口对**今天已经签到的账号同样回 code:0 success**
    （实测：同一账号连打两次都是 success，把 x-device-id 换成完全无关的随机码
    也还是 success）。所以「接口返回成功」本身证明不了新领到了什么 ——
    原来的实现就因此会对已签到的账号假报「签到成功」。

    解法不是在成功后猜，而是**在发领取之前用 status 把已签到的挡在门外**：
    这样一来消除假阳性，二来少发一次没有意义的请求。
    """
    checked, today, err = checkin_state(cred, timeout=args.timeout)

    if checked is True:
        return "ALREADY", {"result": "ALREADY",
                           "note": "今日已签到（已跳过领取，未重复请求）",
                           "checked_in": True, "today_credit": today,
                           "needs_attention": False}

    result, rec = claim(cred, timeout=args.timeout,
                        attempts=args.retry, delay=args.retry_delay,
                        log=lambda m: print("      " + m))

    if result != "CLAIMED":
        rec = dict(rec)
        if checked is None and err:
            rec["note"] = "%s（且状态接口未通：%s）" % (rec.get("note") or "", err)
        # 9074 撞上、而这份凭据又没有真实设备号 —— 基本可以断定是设备号不被识别。
        # 不把话说到位，用户只会看到一句莫名其妙的"当前参与用户太多"。
        if rec.get("code") in TRANSIENT_CODES and not cred.get("ahaDeviceId"):
            rec["note"] = ("%s；这份凭据没有真实设备号（ahaDeviceId），"
                           "9074 多半是设备号不被服务端识别 —— "
                           "请用最新版 trae_export.py 重新导出后覆盖"
                           % (rec.get("note") or ""))
        return result, rec

    # 复核：确认是真领到了，而不是接口的空成功
    time.sleep(1.5)
    after, today2, _ = checkin_state(cred, timeout=args.timeout)
    rec = dict(rec)
    if today2 is not None:
        rec["today_credit"] = today2
    elif today is not None:
        rec["today_credit"] = today
    if after is True:
        rec["checked_in"] = True
        rec["verified"] = True
    else:
        # 复核没跟上不算失败：领取请求确实被受理了，只是状态还没刷新。
        # 记 verified=False 留痕，但不升级成错误，免得天天误报。
        rec["verified"] = False
        rec["note"] = "%s（领取已受理，复核时状态未刷新）" % (rec.get("note") or "签到成功")
    return result, rec


# -------------------------------------------------------------- 日志 ------
def append_log(log_path, record):
    """写成 notify.py 认得的格式：[时间] {json}。"""
    os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
    line = "[%s] %s\n" % (stamp(), json.dumps(record, ensure_ascii=False, sort_keys=True))
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(line)
    return line.strip()


def append_multi(path, summary):
    if not path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")


# -------------------------------------------------------------- 主流程 ----
def apply_order(accounts, args):
    """决定账号的处理顺序。

    为什么必须把这件事显式化：**一台设备每天只有一个签到名额**（实测，见
    DEVICE_DONE）。谁先发谁拿到，后面的账号只会收到 9095。所以"哪个账号先上"
    直接决定当天谁有收益 —— 这不能靠文件名排序碰运气。

      first   按文件名序，第一个账号每天拿名额（默认，结果可预测）
      rotate  每次「真签到」换一个账号当头名，多账号轮流拿

    轮换状态写在 trae-order.json（只属于本套，不碰 WorkBuddy 的任何文件）。
    只读模式（status/doctor）不动状态；轮询（silent-poll）按现有顺序跑但不推进，
    免得一天翻六次。
    """
    mode = str(args.order or os.environ.get("TRAE_ORDER") or "first").lower()
    if len(accounts) < 2 or mode not in ("first", "rotate"):
        return accounts

    path = os.path.join(HERE, ORDER_FILE)
    leader = ""
    try:
        with open(path, encoding="utf-8") as fh:
            leader = (json.load(fh) or {}).get("leader") or ""
    except Exception:
        leader = ""

    names = [name for name, _ in accounts]
    if mode == "rotate" and leader in names:
        start = (names.index(leader) + 1) % len(names)     # 换下一个当头名
    else:
        start = 0
    ordered = accounts[start:] + accounts[:start]

    if mode == "rotate" and args.mode not in READ_ONLY_MODES and args.mode == "silent":
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"leader": ordered[0][0], "updatedAt": stamp()},
                          fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            print("（轮换状态写不进去：%s —— 本次仍按上面顺序执行）" % exc)
    return ordered


def warn_duplicate_accounts(accounts):
    """体检：同一个 userId 出现在多个凭据文件里 —— 会重复签到同一个账号。

    多账号场景的常见坑：把同一个账号导出两次（一份改名当"第二个账号"用），
    于是同一个账号被多个文件代表。这不是致命错误（前置闸门会把第二份变成
    「今日已签到」），但通知里会出现"一签一已签"的迷惑记录，最好早点发现。
    """
    seen = {}
    for name, path in accounts:
        try:
            with open(path, encoding="utf-8") as fh:
                cred = normalize_credential(json.load(fh))
        except Exception:
            continue
        if cred["userId"]:
            seen.setdefault(cred["userId"], []).append(name)
    dups = {uid: names for uid, names in seen.items() if len(names) > 1}
    if dups:
        print()
        print("[!] 有账号被放了多份凭据，会重复签到同一个账号：")
        for uid, names in dups.items():
            print("      userId %s  ->  %s" % (uid, "、".join(names)))
        print("    处理：一个账号只留一份 json，多余的删掉。")
    return dups


def load_accounts(args):
    """找出所有凭据文件，返回 [(名字, 路径)]。"""
    single = args.file or os.environ.get("TRAE_SESSION_FILE")
    if single:
        single = os.path.abspath(single)
        if not os.path.isfile(single):
            print("凭据文件不存在：%s" % single)
            return []
        return [(os.path.splitext(os.path.basename(single))[0], single)]

    sess_dir = os.path.abspath(args.dir or os.environ.get("TRAE_SESSION_DIR")
                               or os.path.join(HERE, "trae-sessions"))
    if not os.path.isdir(sess_dir):
        print("凭据目录不存在：%s" % sess_dir)
        print("先在 TRAE 桌面端登录，用 trae_export.py 导出，再放到这里。")
        return []
    out = []
    for name in sorted(os.listdir(sess_dir)):
        if name.endswith(".json") and not name.startswith("."):
            out.append((name[:-5], os.path.join(sess_dir, name)))
    return out


def run_one(name, path, args, log_dir):
    """跑一个账号。返回 (结果字符串, 是否需要注意)。"""
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception as exc:
        rec = {"result": "ERROR", "note": "凭据文件读不了：%s" % exc, "needs_attention": True}
        append_log(os.path.join(log_dir, "%s.log" % name), rec)
        return "ERROR", True

    cred = normalize_credential(raw)
    problems = credential_problems(cred)
    if problems:
        rec = {"result": "ERROR" if not cred["token"] else "AUTH_READY",
               "note": "；".join(problems), "needs_attention": not cred["token"]}
        append_log(os.path.join(log_dir, "%s.log" % name), rec)
        return rec["result"], rec["needs_attention"]

    left = days_left(cred)
    print("  [%s] %s  到期还剩 %s 天" %
          (name, cred["accountName"] or cred["userId"],
           "-" if left is None else round(left, 1)))

    if args.mode == "doctor":
        if not cred.get("ahaDeviceId"):
            print("      [!] 这份凭据里没有真实设备号（ahaDeviceId）")
            print("          签到会退回遥测 UUID，服务端只回 9074「当前参与用户太多」")
            print("          修法：用最新版 trae_export.py 重新导出，会一并采到真实设备号")
        rec = {"result": "AUTH_READY", "note": "凭据完整（离线检查通过）",
               "needs_attention": False}
        append_log(os.path.join(log_dir, "%s.log" % name), rec)
        return "AUTH_READY", False

    # 续期：只在快到期时才真发请求，失败不阻断
    if need_renew(cred) and not args.dry_run:
        refreshed, note, ok = refresh_credential(cred, timeout=max(60, args.timeout))
        if ok:
            raw = apply_refresh(raw, cred, refreshed)
            try:
                write_secure(path, raw)
            except OSError as exc:
                print("      （续期成功但写回文件失败：%s）" % exc)
            cred = normalize_credential(raw)
            print("      续期：%s" % note)
        else:
            print("      续期：失败（%s）—— 继续用旧 token 试" % note)

    if args.mode == "status":
        result, rec = probe(cred, timeout=args.timeout)
    elif args.dry_run:
        result, rec = "STATUS", {"result": "STATUS", "note": "dry-run，未发签到请求",
                                 "needs_attention": False}
        print("      dry-run：跳过签到请求")
    else:
        result, rec = sign_in(cred, args)

    append_log(os.path.join(log_dir, "%s.log" % name), rec)
    print("      %s  %s" % (result, rec.get("note") or ""))
    return result, bool(rec.get("needs_attention"))


def main():
    _utf8_console()
    ap = argparse.ArgumentParser(description="TRAE 每日签到（独立于 WorkBuddy 的一套）")
    ap.add_argument("mode", nargs="?", default="silent",
                    help="运行模式：%s" % " / ".join(VALID_MODES))
    ap.add_argument("--dir", default=None, help="凭据目录（默认 ./trae-sessions）")
    ap.add_argument("--file", default=None, help="单个凭据文件")
    ap.add_argument("--log-dir", default=None, help="日志目录（默认 ./logs）")
    ap.add_argument("--multi-log", default=None, help="汇总日志路径")
    ap.add_argument("--gap", type=float, default=None, help="账号间隔秒数")
    ap.add_argument("--timeout", type=float, default=30, help="请求超时秒数")
    ap.add_argument("--retry", type=int, default=None,
                    help="服务端繁忙时的重试次数（默认 %d）" % DEFAULT_RETRY)
    ap.add_argument("--retry-delay", type=float, default=None,
                    help="每次重试之间的间隔秒数（默认 %.0f）" % DEFAULT_RETRY_DELAY)
    ap.add_argument("--order", default=None,
                    help="多账号顺序：first（默认，按文件名）| rotate（每天换头名）")
    ap.add_argument("--dry-run", action="store_true", help="不发签到请求")
    args = ap.parse_args()

    if args.retry is None:
        try:
            args.retry = int(os.environ.get("TRAE_RETRY") or DEFAULT_RETRY)
        except ValueError:
            args.retry = DEFAULT_RETRY
    if args.retry_delay is None:
        try:
            args.retry_delay = float(os.environ.get("TRAE_RETRY_DELAY")
                                     or DEFAULT_RETRY_DELAY)
        except ValueError:
            args.retry_delay = DEFAULT_RETRY_DELAY
    args.retry = max(0, args.retry)
    args.retry_delay = max(0.0, args.retry_delay)

    if args.mode not in VALID_MODES:
        print("未知模式：%s（可选：%s）" % (args.mode, " / ".join(VALID_MODES)))
        return 2

    log_dir = os.path.abspath(args.log_dir or os.environ.get("TRAE_LOG_DIR")
                              or os.path.join(HERE, "logs"))
    multi_log = args.multi_log or os.environ.get("TRAE_MULTI_LOG") \
        or os.path.join(HERE, "trae-multi.log")

    accounts = load_accounts(args)
    if not accounts:
        return 2
    accounts = apply_order(accounts, args)

    if args.gap is not None:
        gap = args.gap
    elif os.environ.get("TRAE_ACCOUNT_GAP"):
        gap = float(os.environ["TRAE_ACCOUNT_GAP"])
    else:
        gap = 30.0 if args.mode == "silent-poll" else 60.0

    print("=" * 62)
    print("TRAE 自动签到  mode=%s  账号 %d 个%s"
          % (args.mode, len(accounts), "（只读）" if args.mode in READ_ONLY_MODES else ""))
    print("=" * 62)

    if len(accounts) > 1 and args.mode not in READ_ONLY_MODES:
        print("签到顺序：%s" % " → ".join(name for name, _ in accounts))
        print("说明：一台设备每天只有 1 个签到名额，排最前的账号拿到，其余会报「设备名额已用完」")

    if args.mode == "doctor":
        warn_duplicate_accounts(accounts)

    ok, failed, attention = [], [], []
    for index, (name, path) in enumerate(accounts):
        if index and gap > 0 and args.mode not in READ_ONLY_MODES:
            time.sleep(gap)          # 账号之间错峰，不会挤在同一秒发请求
        try:
            result, needs = run_one(name, path, args, log_dir)
        except Exception as exc:
            result, needs = "ERROR", True
            append_log(os.path.join(log_dir, "%s.log" % name),
                       {"result": "ERROR", "note": "未捕获异常：%s" % exc,
                        "needs_attention": True})
            print("  [%s] 异常：%s" % (name, exc))
        (ok if result in SUCCESS_RESULTS + QUIET_RESULTS else failed).append(name)
        if needs:
            attention.append(name)

    if args.mode not in READ_ONLY_MODES:
        append_multi(multi_log, {
            "ts": stamp(), "mode": args.mode, "accounts": len(accounts),
            "ok": len(ok), "failed": failed, "needs_attention": bool(attention)})

    print("-" * 62)
    print("成功 %d ／ 失败 %d ／ 需关注 %d" % (len(ok), len(failed), len(attention)))
    if failed:
        print("失败：%s" % "、".join(failed))
    print("日志：%s" % log_dir)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
