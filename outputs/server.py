#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 作者：EasonShu
"""签到工作台：Python 静态网页服务 + 统一账号/调度管理。

本目录是完整可部署项目，运行时只依赖 outputs 内部文件：
  outputs/apps/workbuddy  WorkBuddy 自动签到逻辑
  outputs/apps/trae       TRAE 自动签到逻辑

可通过环境变量覆盖：
  SIGNIN_ROOT               工作台根目录，默认 server.py 所在目录
  DASHBOARD_HOST            监听地址，默认 0.0.0.0
  DASHBOARD_PORT            监听端口，默认 8000
  DASHBOARD_TZ              日期时区，默认 Asia/Shanghai
"""

import argparse
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler

# ThreadingHTTPServer 是 Python 3.7 才进入 http.server 的。
try:  # pragma: no cover - 新版本直接命中
    from http.server import ThreadingHTTPServer
except ImportError:  # pragma: no cover - Python 3.6 兜底
    from http.server import HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):  # type: ignore
        """与 3.7+ 的 http.server.ThreadingHTTPServer 行为一致的等价实现。"""

        daemon_threads = True
        allow_reuse_address = True

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python 3.8 fallback
    ZoneInfo = None  # type: ignore


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
PROJECT_ROOT = Path(os.environ.get("SIGNIN_ROOT") or APP_DIR).resolve()
APPS_DIR = PROJECT_ROOT / "apps"
TIMEZONE_NAME = os.environ.get("DASHBOARD_TZ", "Asia/Shanghai")
DATA_DIR = APP_DIR / "data"
ARCHIVE_DIR = APP_DIR / "archive"
RUNTIME_DIR = APP_DIR / "runtime"
CONFIG_PATH = DATA_DIR / "workbench.json"
DB_PATH = DATA_DIR / "dailyhub.sqlite3"
USER_DATA_DIR = DATA_DIR / "users"
SESSION_COOKIE = "signin_workbench_session"
SESSION_MAX_AGE = 7 * 24 * 60 * 60
PRODUCTS = ("WorkBuddy", "TRAE")
PASSWORD_ITERATIONS = 260_000
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,15}$")

DEFAULT_CONFIG: Dict[str, Any] = {
    "auth": {
        "username": "admin",
        "password": "admin123456",
        "secret": "",
    },
    "schedules": {
        "WorkBuddy": {
            "enabled": True,
            "daily_time": "00:05",
            "daily_mode": "silent",
            "poll_enabled": True,
            "poll_times": ["05:00", "12:00", "20:00"],
            "poll_mode": "silent-poll",
        },
        "TRAE": {
            "enabled": True,
            "daily_time": "00:35",
            "daily_mode": "silent",
            "poll_enabled": True,
            "poll_times": ["05:30", "12:30", "20:30"],
            "poll_mode": "silent-poll",
        },
    },
    "notifications": {
        "WorkBuddy": {
            "channel": "serverchan",
            "key": "",
            "url": "",
            "on": "daily",
            "group": "WorkBuddy",
            "enabled": False,
        },
        "TRAE": {
            "channel": "serverchan",
            "key": "",
            "url": "",
            "on": "daily",
            "group": "Trae",
            "enabled": False,
        },
    },
}

TASKS: Dict[str, Dict[str, Any]] = {}
TASK_LOCK = threading.Lock()
SCHEDULER_STOP = threading.Event()

# ---------------------------------------------------------------- 调度器互斥
# 同一个 outputs/ 目录下若跑了多个 server.py，每个实例都会起一份 scheduler_loop，
SCHEDULER_LOCK_PATH = RUNTIME_DIR / "scheduler.lock"
# 无凭据账号清理的独立锁：跟调度锁分开，避免同进程里调度与清理互相占用。
CLEANUP_LOCK_PATH = RUNTIME_DIR / "cleanup.lock"
FIRED_KEYS_PATH = RUNTIME_DIR / "scheduler-fired.json"
SLOT_DIRNAME = "slots"
SCHEDULER_LOCK_HANDLE: Any = None
CLEANUP_LOCK_HANDLE: Any = None
FIRED_SCHEDULE_KEYS: set = set()
# 「注册后多久未上传凭据就自动删除」及清理线程的运行周期（小时 / 秒）。
PURGE_NO_CREDENTIAL_HOURS = float(os.environ.get("PURGE_NO_CREDENTIAL_HOURS", "24"))
PURGE_INTERVAL_SECONDS = int(os.environ.get("PURGE_INTERVAL_SECONDS", "3600"))
# 界面上要回答的是「系统层面有没有调度器在工作」，所以这两种都算 active。
SCHEDULER_DELEGATED = False

LOG_RE = re.compile(r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s*(?P<body>\{.*\})\s*$")
ERROR_RESULTS = {
    "ERROR",
    "UNKNOWN",
    "NETWORK",
    "TIMEOUT",
    "NO_AUTH",
    "NO_SESSION",
    "AUTH_ERROR",
    "AUTH_REJECTED",
    "FORBIDDEN",
}
SIGNED_RESULTS = {"CLAIMED", "ALREADY"}
QUIET_RESULTS = {"STATUS", "AUTH_READY", "INACTIVE", "GROWTH"}


def local_tz():
    if ZoneInfo is None:
        return datetime.now().astimezone().tzinfo or timezone.utc
    try:
        return ZoneInfo(TIMEZONE_NAME)
    except Exception:
        return datetime.now().astimezone().tzinfo or timezone.utc


TZ = local_tz()


def now_local() -> datetime:
    return datetime.now(TZ)


def product_base(product: str) -> Path:
    if product == "WorkBuddy":
        return APPS_DIR / "workbuddy"
    if product == "TRAE":
        return APPS_DIR / "trae"
    raise ValueError("未知产品")


def product_dirs(product: str) -> Dict[str, Path]:
    base = product_base(product)
    if product == "WorkBuddy":
        return {
            "base": base,
            "sessions": base / "sessions",
            "disabled": base / "sessions.disabled",
            "logs": base / "logs",
            "multi_log": base / "multi.log",
            "notify": base / "config" / "notify.json",
            "script": base / "multi_run.py",
        }
    return {
        "base": base,
        "sessions": base / "trae-sessions",
        "disabled": base / "trae-sessions.disabled",
        "logs": base / "logs",
        "multi_log": base / "trae-multi.log",
        "notify": base / "config" / "notify.json",
        "script": base / "trae_signin.py",
    }


def normalize_product(product: Any) -> str:
    return "TRAE" if str(product or "").lower() == "trae" else "WorkBuddy"


def user_workspace_root(user: Optional[Dict[str, Any]]) -> Optional[Path]:
    if not user or not user.get("id"):
        return None
    return USER_DATA_DIR / ("u%s" % int(user["id"]))


def workspace_archive_dir(user: Optional[Dict[str, Any]]) -> Path:
    root = user_workspace_root(user)
    return (root / "archive") if root else ARCHIVE_DIR


def workspace_runtime_dir(user: Optional[Dict[str, Any]]) -> Path:
    root = user_workspace_root(user)
    return (root / "runtime") if root else RUNTIME_DIR


def product_dirs_for(product: str, user: Optional[Dict[str, Any]] = None) -> Dict[str, Path]:
    """脚本目录仍在 apps/，账号凭据/日志按登录用户隔离。"""
    product = normalize_product(product)
    base = product_base(product)
    root = user_workspace_root(user)
    if not root:
        return product_dirs(product)

    key = "workbuddy" if product == "WorkBuddy" else "trae"
    app_data = root / "apps" / key
    multi_name = "multi.log" if product == "WorkBuddy" else "trae-multi.log"
    script_name = "multi_run.py" if product == "WorkBuddy" else "trae_signin.py"
    return {
        "base": base,
        "sessions": app_data / "sessions",
        "disabled": app_data / "sessions.disabled",
        "logs": app_data / "logs",
        "multi_log": app_data / multi_name,
        "notify": app_data / "notify.json",
        "notify_state": app_data / "notify_state.json",
        "lock": app_data / "run.lock",
        "script": base / script_name,
    }


def ensure_user_workspace(user: Optional[Dict[str, Any]]) -> None:
    root = user_workspace_root(user)
    if not root:
        return
    for product in PRODUCTS:
        dirs = product_dirs_for(product, user)
        for key in ("sessions", "disabled", "logs"):
            dirs[key].mkdir(parents=True, exist_ok=True)


def user_workspace_path(path: Path) -> bool:
    try:
        path.resolve().relative_to(USER_DATA_DIR.resolve())
        return True
    except Exception:
        return False


def copy_json_files(source: Path, target: Path) -> None:
    if not source.is_dir():
        return
    target.mkdir(parents=True, exist_ok=True)
    for path in source.glob("*.json"):
        if path.name.startswith(".") or path.name.endswith(".example.json"):
            continue
        dest = target / path.name
        if not dest.exists():
            shutil.copy2(str(path), str(dest))


def copy_log_files(source: Path, target: Path) -> None:
    if not source.is_dir():
        return
    target.mkdir(parents=True, exist_ok=True)
    for path in source.glob("*.log"):
        dest = target / path.name
        if not dest.exists():
            shutil.copy2(str(path), str(dest))


def bootstrap_admin_workspace(user: Optional[Dict[str, Any]]) -> None:
    """把旧单用户目录复制给管理员一次，避免升级后现有账号看不见。"""
    if not user or user.get("role") != "admin":
        return
    root = user_workspace_root(user)
    if not root:
        return
    marker = root / ".legacy-copied"
    if marker.exists():
        return
    ensure_user_workspace(user)
    for product in PRODUCTS:
        source = product_dirs(product)
        target = product_dirs_for(product, user)
        copy_json_files(source["sessions"], target["sessions"])
        copy_json_files(source["disabled"], target["disabled"])
        copy_log_files(source["logs"], target["logs"])
        if source["multi_log"].is_file() and not target["multi_log"].exists():
            target["multi_log"].parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(source["multi_log"]), str(target["multi_log"]))
        if product == "WorkBuddy":
            single = source["base"] / "session.json"
            dest = target["sessions"] / "default.json"
            if single.is_file() and not dest.exists():
                shutil.copy2(str(single), str(dest))
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(iso(now_local()) or "", encoding="utf-8")


def merge_dict(default: Dict[str, Any], actual: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    merged = json.loads(json.dumps(default, ensure_ascii=False))
    if not isinstance(actual, dict):
        return merged
    for key, value in actual.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config() -> Dict[str, Any]:
    data = read_json(CONFIG_PATH) if CONFIG_PATH.exists() else None
    return merge_dict(DEFAULT_CONFIG, data)


def save_config(config: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    tmp.replace(CONFIG_PATH)


def ensure_runtime_config() -> Dict[str, Any]:
    actual = read_json(CONFIG_PATH) if CONFIG_PATH.exists() else None
    config = merge_dict(DEFAULT_CONFIG, actual)
    auth = config.setdefault("auth", {})
    if not str(auth.get("secret") or "").strip():
        auth["secret"] = secrets.token_urlsafe(32)
    if actual != config:
        save_config(config)
    return config


def mask_secret(value: Any) -> str:
    text = str(value or "")
    if not text:
        return "未配置"
    if len(text) <= 8:
        return "已配置"
    return text[:3] + "..." + text[-4:]


def sanitize_name(name: str) -> str:
    stem = Path(name or "account").stem.strip()
    stem = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", stem, flags=re.UNICODE)
    stem = stem.strip(" ._")
    return stem[:80] or "account"


def parse_clock(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", text)
    if not match:
        return None
    return "%02d:%02d" % (int(match.group(1)), int(match.group(2)))


def normalize_times(values: Any) -> List[str]:
    if isinstance(values, str):
        parts = re.split(r"[\s,，;；]+", values)
    elif isinstance(values, list):
        parts = values
    else:
        parts = []
    out = []
    for part in parts:
        value = parse_clock(part)
        if value and value not in out:
            out.append(value)
    return out


def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat(timespec="seconds") if dt else None


def rel(path: Optional[Path]) -> str:
    if not path:
        return ""
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(path)


def read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def tail_text(path: Path, limit_bytes: int = 512 * 1024) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > limit_bytes:
                handle.seek(size - limit_bytes)
                handle.readline()
            return handle.read().decode("utf-8", "replace")
    except Exception:
        return ""


def parse_time(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        raw = float(value)
        if raw > 1e11:
            raw = raw / 1000.0
        try:
            return datetime.fromtimestamp(raw, TZ)
        except Exception:
            return None

    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"-?\d+(\.\d+)?", text):
        return parse_time(float(text))

    normalized = text.replace("Z", "+00:00").replace("z", "+00:00")
    # datetime.fromisoformat 是 Python 3.7 才有的，3.6 上没有 —— 用 strptime 表兜底。
    iso_parser = getattr(datetime, "fromisoformat", None)
    if iso_parser is not None:
        try:
            dt = iso_parser(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ)
            return dt.astimezone(TZ)
        except Exception:
            pass

    # Python 3.6 的 strptime 不认 "+08:00" 这种带冒号的时区，先压成 "+0800"
    fixed = re.sub(r"([+-]\d{2}):(\d{2})$", r"\1\2", normalized)
    iso_formats = (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    )
    for fmt in iso_formats:
        for candidate in (fixed, text):
            try:
                dt = datetime.strptime(candidate, fmt)
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ)
            return dt.astimezone(TZ)
    return None


def days_left(expiry: Optional[datetime]) -> Optional[float]:
    if not expiry:
        return None
    return round((expiry - now_local()).total_seconds() / 86400.0, 1)


def pick(dct: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = dct.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def nested(source: Optional[Dict[str, Any]], *keys: str) -> Dict[str, Any]:
    value: Any = source or {}
    for key in keys:
        if not isinstance(value, dict):
            return {}
        value = value.get(key) or {}
    return value if isinstance(value, dict) else {}


def jwt_claims(token: Any) -> Dict[str, Any]:
    """解 JWT 的 payload 拿展示字段（nickname / preferred_username / sub）。
    不校验签名 —— 这里的值只用来在界面上显示账号名，不参与任何鉴权判断。"""
    if not isinstance(token, str) or token.count(".") != 2:
        return {}
    segment = token.split(".")[1]
    segment += "=" * (-len(segment) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(segment.encode("ascii")).decode("utf-8"))
    except Exception:
        return {}
    return claims if isinstance(claims, dict) else {}


def credential_meta(path: Path, product: str) -> Dict[str, Any]:
    data = read_json(path) or {}
    portable = nested(data, "_portable")
    account = nested(data, "account")
    auth = nested(data, "auth")
    auth_info = nested(data, "authInfo")
    auth_info_account = nested(data, "authInfo", "account")
    claims = jwt_claims(pick(auth, "accessToken", "token") or pick(auth_info, "token"))

    # display_name 保持原有取值顺序（TRAE 的 account.accountName → 文件名），
    # 好让这次改动是「纯新增」：卡片标题不会因为多了 accountName 就换了个样子。
    # 顺带修掉一个潜伏 bug —— 原来这里写 pick(auth_info, "account", ...)，
    # 而 authInfo.account 是个 dict，会被 pick 原样返回，再 str() 成一大串字典文本。
    display_name = (
        pick(account, "accountName", "username", "name")
        or pick(auth_info_account, "username", "name")
        or path.stem
    )

    # 账号 ID 与 accountName 单独拎出来：卡片上要能一眼看到「这是谁、ID 是多少」。
    # 两款产品的字段名不一致，按实际凭据结构取值：
    #   TRAE      account.userId(16 位) + account.accountName
    #   WorkBuddy account.uid(36 位 UUID) + JWT 里的 nickname / preferred_username
    account_id = (
        pick(account, "userId", "uid", "accountId", "id")
        or pick(auth_info, "userId")
        or pick(auth_info_account, "userId")
        or pick(portable, "accountId", "userId")
        or pick(claims, "sub")
    )
    account_name = (
        pick(account, "accountName", "nickname", "displayName", "username")
        or pick(claims, "nickname", "preferred_username", "name")
        or pick(auth_info_account, "username", "organization", "email")
        or pick(auth_info, "accountName")
        or pick(portable, "accountName")
    )
    expiry = (
        parse_time(pick(portable, "expiresAt", "expiredAt"))
        or parse_time(pick(auth, "expiresAt", "expiredAt"))
        or parse_time(pick(auth_info, "expiredAt", "expiresAt"))
    )
    refresh_expiry = (
        parse_time(pick(portable, "refreshExpiresAt", "refreshExpiredAt"))
        or parse_time(pick(auth, "refreshExpiresAt", "refreshExpiredAt"))
        or parse_time(pick(auth_info, "refreshExpiredAt", "refreshExpiresAt"))
    )

    raw_days = pick(portable, "daysLeft")
    left = days_left(expiry)
    if left is None and isinstance(raw_days, (int, float)):
        left = round(float(raw_days), 1)

    result = {
        "product": product,
        "name": path.stem,
        "display_name": str(display_name),
        "account_id": str(account_id) if account_id else "",
        "account_name": str(account_name) if account_name else "",
        "credential_path": rel(path),
        "credential_exists": True,
        "expires_at": iso(expiry),
        "days_left": left,
        "refresh_expires_at": iso(refresh_expiry),
        "refresh_days_left": days_left(refresh_expiry),
    }

    # TRAE 特有字段
    if product == "TRAE":
        aha_device_id = pick(auth, "ahaDeviceId") or pick(auth_info, "ahaDeviceId")
        if aha_device_id:
            result["ahaDeviceId"] = str(aha_device_id)

    return result


def classify_record(record: Dict[str, Any], ts: Optional[datetime]) -> Dict[str, Any]:
    result = str(record.get("result") or "").upper()
    checked = record.get("checked_in")
    needs_attention = bool(record.get("needs_attention")) or result in ERROR_RESULTS
    signed = False
    severity = "neutral"
    label = result or "UNKNOWN"

    if needs_attention:
        severity = "danger"
        label = {
            "AUTH_ERROR": "认证被拒",
            "AUTH_REJECTED": "认证被拒",
            "FORBIDDEN": "权限拒绝",
            "NO_AUTH": "无凭据",
            "NO_SESSION": "无会话",
            "NETWORK": "网络异常",
            "TIMEOUT": "执行超时",
            "ERROR": "执行异常",
            "UNKNOWN": "未知状态",
        }.get(result, label)
    elif result == "CLAIMED":
        severity = "success"
        signed = True
        label = "已领取"
    elif result == "ALREADY":
        severity = "success"
        signed = True
        label = "今日已签"
    elif result == "STATUS":
        if checked is True:
            severity = "success"
            signed = True
            label = "今日已签"
        elif checked is False:
            severity = "warning"
            label = "今日尚未签到"
        else:
            severity = "neutral"
            label = "状态正常"
    elif result == "AUTH_READY":
        severity = "success"
        label = "凭据有效"
    elif result == "INACTIVE":
        severity = "neutral"
        label = "活动未开启"
    elif result == "GROWTH":
        severity = "success"
        label = "成长中心"
    elif result in QUIET_RESULTS:
        severity = "neutral"
        label = result

    if ts and ts.astimezone(TZ).date() != now_local().date() and signed:
        signed = False
        label = "非今日记录"
        severity = "neutral"

    note = record.get("report") or record.get("note") or label
    credit = pick(record, "credit", "today_credit", "credits_gained")
    if isinstance(credit, str) and credit.isdigit():
        credit = int(credit)
    if not isinstance(credit, (int, float)):
        credit = None
    if credit is not None and not (signed or result in {"CLAIMED", "GROWTH"}):
        credit = None

    return {
        "result": result,
        "label": label,
        "severity": severity,
        "signed_today": bool(signed),
        "needs_attention": bool(needs_attention),
        "note": str(note),
        "credit": credit,
        "streak_days": pick(record, "streak_days"),
        "total_credits": pick(record, "total_credits"),
    }


def parse_log(path: Path, product: str, account: str) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for line in tail_text(path).splitlines():
        body = None
        ts = None
        match = LOG_RE.match(line)
        if match:
            ts = parse_time(match.group("ts"))
            body = match.group("body")
        else:
            pos = line.find("{")
            if pos >= 0:
                body = line[pos:]
                ts = parse_time(line[:pos].strip(" []"))
        if not body:
            continue
        try:
            record = json.loads(body)
        except Exception:
            continue
        if not isinstance(record, dict):
            continue
        if ts is None:
            ts = parse_time(record.get("ts")) or datetime.fromtimestamp(path.stat().st_mtime, TZ)
        status = classify_record(record, ts)
        records.append({
            "product": product,
            "account": account,
            "time": iso(ts),
            "timestamp": ts.timestamp() if ts else 0,
            "log_path": rel(path),
            "raw_result": record.get("result"),
            **status,
        })
    return records


def account_health(account: Dict[str, Any]) -> Tuple[str, str]:
    if account.get("enabled") is False:
        return "neutral", "已停用"
    latest = account.get("latest") or {}
    if latest.get("needs_attention"):
        return "danger", latest.get("label") or "需关注"

    left = account.get("days_left")
    if isinstance(left, (int, float)):
        if left < 0:
            return "danger", "凭据已过期"
        if left <= 3:
            return "danger", "即将过期"
        if left <= 10:
            return "warning", "临近过期"
        return "success", "有效"

    if not account.get("credential_exists"):
        return "warning", "未发现凭据"
    return "neutral", "未记录有效期"


def discover_product(
    product: str,
    base: Optional[Path] = None,
    sessions_dir: Optional[str] = None,
    log_dir: Optional[str] = None,
    dirs: Optional[Dict[str, Path]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    accounts: Dict[str, Dict[str, Any]] = {}
    records: List[Dict[str, Any]] = []
    dirs = dirs or product_dirs(product)
    base = base or dirs["base"]
    sessions_path = dirs.get("sessions") or (base / (sessions_dir or "sessions"))
    logs_path = dirs.get("logs") or (base / (log_dir or "logs"))

    for path in sorted(sessions_path.glob("*.json")) if sessions_path.is_dir() else []:
        if path.name.startswith(".") or path.name.endswith(".example.json"):
            continue
        meta = credential_meta(path, product)
        meta["enabled"] = True
        accounts[path.stem] = meta

    disabled_dir = dirs["disabled"]
    for path in sorted(disabled_dir.glob("*.json")) if disabled_dir.is_dir() else []:
        if path.name.startswith(".") or path.name.endswith(".example.json"):
            continue
        meta = credential_meta(path, product)
        meta["enabled"] = False
        meta["disabled_path"] = rel(path)
        accounts.setdefault(path.stem, meta)

    single_session = base / "session.json"
    if not user_workspace_path(sessions_path) and single_session.is_file() and "default" not in accounts:
        meta = credential_meta(single_session, product)
        meta["name"] = "default"
        meta["enabled"] = True
        accounts["default"] = meta

    candidate_logs: List[Path] = []
    if logs_path.is_dir():
        candidate_logs.extend(sorted(logs_path.glob("*.log")))
    for path in dict.fromkeys([dirs.get("multi_log"), base / "signin.log", base / "multi.log", base / "trae-multi.log"]):
        if path and path.is_file():
            candidate_logs.append(path)

    seen_logs = set()
    for log_path in candidate_logs:
        if log_path in seen_logs:
            continue
        seen_logs.add(log_path)
        if log_path.name in {"multi.log", "trae-multi.log", "notify.log"} or log_path.name.endswith(".renew.log"):
            continue
        account = log_path.stem if log_path.stem != "signin" else "default"
        if account not in accounts:
            accounts[account] = {
                "product": product,
                "name": account,
                "display_name": account,
                # 只在日志里出现过的账号没有凭据文件，自然也就没有账号 ID / accountName
                "account_id": "",
                "account_name": "",
                "credential_path": "",
                "credential_exists": False,
                "expires_at": None,
                "days_left": None,
                "refresh_expires_at": None,
                "refresh_days_left": None,
                "enabled": False,
            }
        parsed = parse_log(log_path, product, account)
        records.extend(parsed)
        if parsed:
            latest = sorted(parsed, key=lambda item: item["timestamp"], reverse=True)[0]
            accounts[account]["latest"] = latest
            accounts[account]["log_path"] = rel(log_path)

    out = []
    for account in accounts.values():
        latest = account.get("latest") or {
            "label": "暂无记录",
            "severity": "neutral",
            "signed_today": False,
            "needs_attention": False,
            "note": "暂无日志",
            "time": None,
            "credit": None,
        }
        account["latest"] = latest
        severity, label = account_health(account)
        account["health_severity"] = severity
        account["health_label"] = label
        out.append(account)
    out.sort(key=lambda item: (item["product"], item["name"]))
    return out, records


def product_stats(product: str, accounts: List[Dict[str, Any]]) -> Dict[str, Any]:
    active = [item for item in accounts if item.get("enabled") is not False]
    total = len(active)
    signed = sum(1 for item in active if (item.get("latest") or {}).get("signed_today"))
    attention = sum(1 for item in active if (item.get("latest") or {}).get("needs_attention"))
    pending = max(total - signed - attention, 0)
    return {
        "product": product,
        "total": total,
        "managed": len(accounts),
        "signed_today": signed,
        "pending": pending,
        "attention": attention,
        "completion": round((signed / total) * 100) if total else 0,
    }


def notification_config(
    config: Optional[Dict[str, Any]] = None,
    user: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    items = []
    config = config or load_user_config(user)
    for product, values in (config.get("notifications") or {}).items():
        if product not in PRODUCTS:
            continue
        path = product_dirs_for(product, user)["notify"]
        items.append({
            "product": product,
            "channel": values.get("channel") or "none",
            "group": values.get("group") or product,
            "on": values.get("on") or "daily",
            "key": mask_secret(values.get("key")),
            "url": mask_secret(values.get("url")),
            "path": rel(path),
            "example": False,
            "enabled": bool(values.get("enabled")),
        })
    candidates = [
        ("WorkBuddy", product_base("WorkBuddy") / "config" / "notify.example.json"),
        ("TRAE", product_base("TRAE") / "config" / "notify.example.json"),
    ]
    if not user:
        candidates.extend([
            ("WorkBuddy", product_base("WorkBuddy") / "config" / "notify.json"),
            ("TRAE", product_base("TRAE") / "config" / "notify.json"),
        ])
    for product, path in candidates:
        data = read_json(path)
        if not data:
            continue
        key = str(data.get("key") or "")
        if path.name.endswith(".example.json"):
            masked = "示例"
        elif key and len(key) > 8:
            masked = key[:3] + "..." + key[-4:]
        elif key:
            masked = "已配置"
        else:
            masked = "未配置"
        items.append({
            "product": product,
            "channel": data.get("channel") or "unknown",
            "group": data.get("group") or product,
            "on": data.get("on") or "daily",
            "key": masked,
            "path": rel(path),
            "example": path.name.endswith(".example.json"),
            "enabled": not path.name.endswith(".example.json"),
        })
    unique = {}
    for item in items:
        key = (item["product"], item["path"], item["example"])
        unique.setdefault(key, item)
    return list(unique.values())


def schedule_payload(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for product in ("WorkBuddy", "TRAE"):
        values = ((config.get("schedules") or {}).get(product) or {})
        daily_time = parse_clock(values.get("daily_time")) or DEFAULT_CONFIG["schedules"][product]["daily_time"]
        poll_times = normalize_times(values.get("poll_times")) or DEFAULT_CONFIG["schedules"][product]["poll_times"]
        out.append({
            "product": product,
            "title": "%s 每日签到" % product,
            "daily_time": daily_time,
            "daily_mode": values.get("daily_mode") or "silent",
            "poll_times": poll_times,
            "poll_mode": values.get("poll_mode") or "silent-poll",
            "description": "每天 %s 签到" % daily_time,
            "poll_description": "轮询：%s" % " / ".join(poll_times),
            "enabled": bool(values.get("enabled")),
            "poll_enabled": bool(values.get("poll_enabled")),
            "directory_exists": product_base(product).exists(),
        })
    return out


def recent_tasks(user: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    with TASK_LOCK:
        tasks = list(TASKS.values())
    if user and user.get("id"):
        uid = int(user["id"])
        tasks = [item for item in tasks if int(item.get("user_id") or 0) == uid]
    tasks.sort(key=lambda item: item.get("started_at") or "", reverse=True)
    return tasks[:20]


def build_dashboard(user: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    config = load_user_config(user)
    if user:
        ensure_user_workspace(user)
        bootstrap_admin_workspace(user)

    workbuddy_accounts, workbuddy_records = discover_product("WorkBuddy", dirs=product_dirs_for("WorkBuddy", user))
    trae_accounts, trae_records = discover_product("TRAE", dirs=product_dirs_for("TRAE", user))

    accounts = workbuddy_accounts + trae_accounts
    records = sorted(workbuddy_records + trae_records, key=lambda item: item["timestamp"], reverse=True)
    today_prefix = now_local().strftime("%Y-%m-%d")
    total_credit = sum(
        (item.get("credit") or 0)
        for item in records
        if str(item.get("time") or "").startswith(today_prefix) and item.get("signed_today")
    )
    streak_values = [
        (item.get("latest") or {}).get("streak_days")
        for item in accounts
        if isinstance((item.get("latest") or {}).get("streak_days"), (int, float))
    ]

    products = [
        product_stats("WorkBuddy", workbuddy_accounts),
        product_stats("TRAE", trae_accounts),
    ]
    total_accounts = len(accounts)
    signed_today = sum(item["signed_today"] for item in products)
    attention = sum(item["attention"] for item in products)
    pending = max(total_accounts - signed_today - attention, 0)

    return {
        "generated_at": iso(now_local()),
        "timezone": TIMEZONE_NAME,
        "project_root": str(PROJECT_ROOT),
        "summary": {
            "total_accounts": total_accounts,
            "signed_today": signed_today,
            "pending": pending,
            "attention": attention,
            "total_credit_today": total_credit,
            "longest_streak": max(streak_values) if streak_values else None,
        },
        "products": products,
        "accounts": accounts,
        # 归档是软删除：不把清单交给前端，界面上就再也找不回来了。
        "archived": list_archived(user),
        "recent": records[:80],
        "schedules": schedule_payload(config),
        "notifications": notification_config(config, user),
        "tasks": recent_tasks(user),
        # 调度器状态：界面上「调度器运行中」和「下次执行」都读这三项。
        # --check 模式下不启动调度器，因此 scheduler_active 为 False 属正常。
        "scheduler_active": (SCHEDULER_LOCK_HANDLE is not None) or SCHEDULER_DELEGATED,
        "next_run_at": next_run_at(config),
        "schedule_status": today_schedule_status(config, user),
        "config": {
            "schedules": config.get("schedules") or {},
            "notifications": {
                product: {
                    **values,
                    "key": mask_secret(values.get("key")),
                    "url": mask_secret(values.get("url")),
                    "has_key": bool(values.get("key")),
                    "has_url": bool(values.get("url")),
                }
                for product, values in (config.get("notifications") or {}).items()
            },
        },
        "auth": {
            "authenticated": bool(user),
            "username": (user or {}).get("username") or "",
            "role": (user or {}).get("role") or "",
            "status": (user or {}).get("status") or "",
            "actions_enabled": True,
            "allowed_modes": ["status", "silent", "silent-poll", "doctor"],
        },
        "admin": {
            "users": list_user_accounts(),
            "accounts": admin_account_overview(),
            "notifications": admin_notification_overview(),
        } if user and user.get("role") == "admin" else None,
    }


# ---------------------------------------------------------------------------
# build_dashboard 结果缓存：build_dashboard 每次都要 discover_product 扫磁盘，
# 轮询高频时开销大。这里按「用户名|角色」缓存最多 _STATUS_CACHE_TTL 秒。
# 变更接口都会直接返回各自的完整 payload（不经缓存），所以无需显式失效，
# 轮询最多滞后 TTL 秒即自动刷新，可接受。
# ---------------------------------------------------------------------------
_STATUS_CACHE_TTL = 3.0
_dashboard_cache: Dict[str, Dict[str, Any]] = {}
_DASHBOARD_CACHE_LOCK = threading.Lock()


def dashboard_cache_key(user: Optional[Dict[str, Any]]) -> str:
    role = (user or {}).get("role") or ""
    name = (user or {}).get("username") or "<anon>"
    return f"{name}|{role}"


def build_dashboard_cached(user: Optional[Dict[str, Any]] = None, force: bool = False) -> Dict[str, Any]:
    key = dashboard_cache_key(user)
    now = time.monotonic()
    with _DASHBOARD_CACHE_LOCK:
        entry = _dashboard_cache.get(key)
        if not force and entry and (now - entry["t"]) < _STATUS_CACHE_TTL:
            return entry["payload"]
        if len(_dashboard_cache) > 256:  # 顺手回收长期不用的条目防止内存膨胀
            for k, e in list(_dashboard_cache.items()):
                if now - e["t"] > 120:
                    _dashboard_cache.pop(k, None)
        payload = build_dashboard(user)
        _dashboard_cache[key] = {"t": now, "payload": payload}
        return payload


def json_response(
    handler: BaseHTTPRequestHandler,
    payload: Any,
    status: int = 200,
    headers: Optional[Dict[str, str]] = None,
) -> None:
    raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    if getattr(handler, "_resp_crypto", False):
        enc = build_envelope(raw, envelope_aad(getattr(handler, "command", "GET"), getattr(handler, "path", "/")))
        if enc is not None:
            raw = enc
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(raw)))
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(raw)


def text_response(handler: BaseHTTPRequestHandler, text: str, status: int = 200) -> None:
    raw = text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def base64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


# ---------------------------------------------------------------------------
# HTTP 请求 / 响应体加解密（纯标准库，无第三方依赖）
# 密钥存 workbench.json 的 app_secret（base64url 32 字节），首启自动生成。
# 封包格式：{"v":2,"n":<b64 nonce>,"c":<b64 密文>,"t":<b64 tag>}
#   方案 = HMAC-SHA256 计数器模式流加密 + 加密后 HMAC 认证（encrypt-then-MAC）。
#   AAD = "<HTTP方法> <路径>"，绑定请求指向，防止密文被串用到其它接口。
# 说明：这是应用层防御纵深（体加密 + 明文不可读），真正的传输机密性仍需
#       走 HTTPS；纯 HTTP 下本方案与明文同信道，故强烈建议同时上 TLS。
# ---------------------------------------------------------------------------
_CRYPTO_LOCK = threading.Lock()
_CRYPTO_CACHE: Dict[str, Any] = {"key": None, "enabled": True}


# ---------------------------------------------------------------------------
# IP 滑动窗口限流（防爬 / 防爆破）
#   每个 (bucket, ip) 一个单调时钟时间戳队列；窗口内超限返回 429。
#   配置：workbench.json 里 rate_limit.enabled / api / auth / proxy
#     默认 auth 8 次/分、api 180 次/分、静态不限制。
# ---------------------------------------------------------------------------
_RATE = threading.Lock()
_RATE_WINDOWS: Dict[str, Any] = {}
_RATE_PRUNE_TICKS = 0


def _rate_bucket(bucket: str, ip: str) -> str:
    return f"{bucket}|{ip}"


def _rate_allow(bucket: str, ip: str, limit: int, window: float) -> Tuple[bool, int]:
    """返回 (是否放行, 需要等待秒数)。"""
    global _RATE_WINDOWS, _RATE_PRUNE_TICKS
    now = time.monotonic()
    key = _rate_bucket(bucket, ip)
    with _RATE:
        dq = _RATE_WINDOWS.get(key)
        if dq is None:
            dq = _RATE_WINDOWS[key] = deque()
        while dq and dq[0] <= now - window:
            dq.popleft()
        if len(dq) >= limit:
            retry = int(window - (now - dq[0])) + 1
            return False, max(1, retry)
        dq.append(now)
        # 轻量回收：每 512 次清一次空桶，防长时间积累过多 IP
        _RATE_PRUNE_TICKS += 1
        if _RATE_PRUNE_TICKS % 512 == 0:
            _RATE_WINDOWS = {k: v for k, v in _RATE_WINDOWS.items() if v}
        return True, 0


def crypto_config() -> Dict[str, Any]:
    key = _CRYPTO_CACHE.get("key")
    if key is not None:
        return _CRYPTO_CACHE
    with _CRYPTO_LOCK:
        if _CRYPTO_CACHE.get("key") is not None:
            return _CRYPTO_CACHE
        cfg = load_config()
        enabled = bool((cfg.get("crypto") or {}).get("enabled", True))
        secret = None
        stored = cfg.get("app_secret")
        if stored:
            try:
                secret = base64url_decode(str(stored))
            except Exception:
                secret = None
        if not secret or len(secret) != 32:
            secret = secrets.token_bytes(32)
            cfg["app_secret"] = base64url_encode(secret)
            save_config(cfg)
        _CRYPTO_CACHE["key"] = secret
        _CRYPTO_CACHE["enabled"] = enabled
        _CRYPTO_CACHE["realms"] = {}
        return _CRYPTO_CACHE


def crypto_secret() -> bytes:
    return crypto_config()["key"]


def envelope_aad(method: str, request_path: str) -> str:
    plain = request_path.split("?", 1)[0]
    return f"{method} {plain}"


def _msg_key(secret: bytes, nonce: bytes, label: bytes = b"k") -> bytes:
    return hashlib.pbkdf2_hmac("sha256", label + b"\x00" + secret, nonce, 1, 32)


def _keystream(msg_key: bytes, length: int) -> bytes:
    """HMAC-SHA256 计数器模式 -> 长度可变的伪随机密钥流（对流密码）。"""
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(msg_key, counter.to_bytes(4, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def build_envelope(raw: bytes, aad: str) -> bytes:
    """raw -> json 封包(v:2)。stdonly，失败返回 None。"""
    try:
        secret = crypto_secret()
        nonce = secrets.token_bytes(16)
        key = _msg_key(secret, nonce)
        ct = bytes(b ^ k for b, k in zip(raw, _keystream(key, len(raw))))
        tag = hmac.new(_msg_key(secret, nonce, b"a"), nonce + ct + aad.encode("utf-8"), hashlib.sha256).digest()
    except Exception:
        return None
    body = {
        "v": 2,
        "n": base64url_encode(nonce),
        "c": base64url_encode(ct),
        "t": base64url_encode(tag),
    }
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def open_envelope(raw: bytes, aad: str) -> Optional[bytes]:
    """若 raw 是合法封包(v:2)则校验并返回明文，否则 None（维持明文/旧客户端兼容）。"""
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(obj, dict) or obj.get("v") != 2 or not all(k in obj for k in ("n", "c", "t")):
        return None
    try:
        secret = crypto_secret()
        nonce = base64url_decode(str(obj["n"]))
        ct = base64url_decode(str(obj["c"]))
        tag = base64url_decode(str(obj["t"]))
        expect = hmac.new(_msg_key(secret, nonce, b"a"), nonce + ct + aad.encode("utf-8"), hashlib.sha256).digest()
        if not hmac.compare_digest(expect, tag):
            return None
        return bytes(b ^ k for b, k in zip(ct, _keystream(_msg_key(secret, nonce), len(ct))))
    except Exception:
        return None


def auth_config() -> Dict[str, Any]:
    return load_config().get("auth") or {}


def password_hash(password: str) -> str:
    salt = secrets.token_urlsafe(18)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        str(password or "").encode("utf-8"),
        salt.encode("utf-8"),
        PASSWORD_ITERATIONS,
    )
    return "pbkdf2_sha256$%s$%s$%s" % (
        PASSWORD_ITERATIONS,
        salt,
        base64url_encode(digest),
    )


def password_matches(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, digest = str(stored or "").split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            str(password or "").encode("utf-8"),
            salt.encode("utf-8"),
            int(iterations),
        )
        return hmac.compare_digest(base64url_encode(actual), digest)
    except Exception:
        return False


def db_connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def default_user_settings() -> Dict[str, Any]:
    return {
        "schedules": json.loads(json.dumps(DEFAULT_CONFIG["schedules"], ensure_ascii=False)),
        "notifications": json.loads(json.dumps(DEFAULT_CONFIG["notifications"], ensure_ascii=False)),
    }


def user_from_row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def public_user(user: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": user.get("id"),
        "username": user.get("username") or "",
        "role": user.get("role") or "user",
        "status": user.get("status") or "pending",
        "created_at": user.get("created_at"),
        "approved_at": user.get("approved_at"),
        "last_login_at": user.get("last_login_at"),
    }


def ensure_settings_row(conn: sqlite3.Connection, user_id: int) -> None:
    defaults = default_user_settings()
    now = iso(now_local())
    conn.execute(
        """
        INSERT OR IGNORE INTO user_settings
            (user_id, schedules_json, notifications_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            user_id,
            json.dumps(defaults["schedules"], ensure_ascii=False),
            json.dumps(defaults["notifications"], ensure_ascii=False),
            now,
            now,
        ),
    )


def seed_admin_settings_from_config(conn: sqlite3.Connection, admin_id: int, config: Dict[str, Any]) -> None:
    """首次升级到 SQLite 时，把旧 JSON 里的计划/通知带给管理员。"""
    ensure_settings_row(conn, admin_id)
    row = conn.execute(
        "SELECT schedules_json, notifications_json, created_at, updated_at FROM user_settings WHERE user_id=?",
        (admin_id,),
    ).fetchone()
    if not row:
        return
    if str(row["created_at"] or "") != str(row["updated_at"] or ""):
        return
    try:
        schedules = json.loads(row["schedules_json"] or "{}")
    except Exception:
        schedules = {}
    try:
        notifications = json.loads(row["notifications_json"] or "{}")
    except Exception:
        notifications = {}
    source_schedules = (config.get("schedules") or {})
    source_notifications = (config.get("notifications") or {})

    schedules_are_default = schedules == DEFAULT_CONFIG["schedules"]
    source_schedules_changed = source_schedules and source_schedules != DEFAULT_CONFIG["schedules"]
    notifications_empty = not any(
        (values or {}).get("enabled") or (values or {}).get("key") or (values or {}).get("url")
        for values in notifications.values()
        if isinstance(values, dict)
    )
    source_has_notifications = any(
        (values or {}).get("enabled") or (values or {}).get("key") or (values or {}).get("url")
        for values in source_notifications.values()
        if isinstance(values, dict)
    )
    if not ((schedules_are_default and source_schedules_changed) or (notifications_empty and source_has_notifications)):
        return

    next_schedules = source_schedules if schedules_are_default and source_schedules_changed else schedules
    next_notifications = source_notifications if notifications_empty and source_has_notifications else notifications
    conn.execute(
        """
        UPDATE user_settings
           SET schedules_json=?, notifications_json=?, updated_at=?
         WHERE user_id=?
        """,
        (
            json.dumps(next_schedules, ensure_ascii=False),
            json.dumps(next_notifications, ensure_ascii=False),
            iso(now_local()),
            admin_id,
        ),
    )


def init_database(config: Dict[str, Any]) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                approved_at TEXT,
                approved_by INTEGER,
                last_login_at TEXT,
                fp_hash TEXT
            )
            """
        )
        # 存量库迁移：老库没有 fp_hash 列就补上；同一浏览器特征只能注册一个账号。
        # NULL 在 SQLite 唯一索引里互不冲突，所以老用户(未录指纹)不受影响。
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "fp_hash" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN fp_hash TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_fp_hash ON users(fp_hash)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                schedules_json TEXT NOT NULL,
                notifications_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )

        auth = config.get("auth") or {}
        username = str(auth.get("username") or "admin").strip() or "admin"
        password = str(auth.get("password") or DEFAULT_CONFIG["auth"]["password"])
        now = iso(now_local())
        admin_count = conn.execute("SELECT COUNT(*) AS c FROM users WHERE role='admin'").fetchone()["c"]
        existing = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO users
                    (username, password_hash, role, status, created_at, approved_at)
                VALUES (?, ?, 'admin', 'approved', ?, ?)
                """,
                (username, password_hash(password), now, now),
            )
        elif not admin_count:
            conn.execute(
                """
                UPDATE users
                   SET role='admin', status='approved',
                       approved_at=COALESCE(approved_at, ?)
                 WHERE id=?
                """,
                (now, existing["id"]),
            )

        for row in conn.execute("SELECT id FROM users").fetchall():
            ensure_settings_row(conn, int(row["id"]))
        for row in conn.execute("SELECT id FROM users WHERE role='admin'").fetchall():
            seed_admin_settings_from_config(conn, int(row["id"]), config)
        conn.commit()


def get_user_by_id(user_id: Any) -> Optional[Dict[str, Any]]:
    try:
        uid = int(user_id)
    except Exception:
        return None
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return user_from_row(row)


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()
    return user_from_row(row)


def list_user_accounts() -> List[Dict[str, Any]]:
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT id, username, role, status, created_at, approved_at, last_login_at
              FROM users
             ORDER BY
               CASE status
                 WHEN 'pending' THEN 0
                 WHEN 'approved' THEN 1
                 WHEN 'disabled' THEN 2
                 ELSE 3
               END,
               created_at DESC
            """
        ).fetchall()
    return [public_user(user_from_row(row) or {}) for row in rows]


def list_all_users() -> List[Dict[str, Any]]:
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT *
              FROM users
             ORDER BY
               CASE status
                 WHEN 'pending' THEN 0
                 WHEN 'approved' THEN 1
                 WHEN 'disabled' THEN 2
                 ELSE 3
               END,
               created_at DESC
            """
        ).fetchall()
    return [user_from_row(row) or {} for row in rows]


def admin_account_overview() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for user in list_all_users():
        ensure_user_workspace(user)
        if user.get("role") == "admin":
            bootstrap_admin_workspace(user)
        owner = public_user(user)
        for product in PRODUCTS:
            accounts, _records = discover_product(product, dirs=product_dirs_for(product, user))
            for account in accounts:
                item = dict(account)
                item["owner"] = owner
                item["owner_id"] = owner.get("id")
                item["owner_username"] = owner.get("username") or ""
                item["owner_role"] = owner.get("role") or "user"
                item["owner_status"] = owner.get("status") or "pending"
                out.append(item)
    out.sort(key=lambda item: (
        str(item.get("owner_username") or ""),
        str(item.get("product") or ""),
        str(item.get("name") or ""),
    ))
    return out


def admin_notification_overview() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for user in list_all_users():
        ensure_user_workspace(user)
        owner = public_user(user)
        config = load_user_config(user)
        for product in PRODUCTS:
            values = ((config.get("notifications") or {}).get(product) or {})
            path = product_dirs_for(product, user)["notify"]
            out.append({
                "owner": owner,
                "owner_id": owner.get("id"),
                "owner_username": owner.get("username") or "",
                "owner_role": owner.get("role") or "user",
                "owner_status": owner.get("status") or "pending",
                "product": product,
                "enabled": bool(values.get("enabled")),
                "channel": values.get("channel") or "none",
                "group": values.get("group") or product,
                "on": values.get("on") or "daily",
                "key": "",
                "url": str(values.get("url") or ""),
                "key_masked": "已保存" if values.get("key") else "未配置",
                "url_masked": mask_secret(values.get("url")),
                "has_key": bool(values.get("key")),
                "has_url": bool(values.get("url")),
                "path": rel(path),
            })
    out.sort(key=lambda item: (
        str(item.get("owner_username") or ""),
        str(item.get("product") or ""),
    ))
    return out


def approved_admin_users() -> List[Dict[str, Any]]:
    """所有「已通过」的管理员账号（审核通知的接收方）。"""
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE role='admin' AND status='approved'"
        ).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        user = user_from_row(row)
        if user:
            out.append(user)
    return out


# ---------------------------------------------------------------------------
# 管理员首页统计页（全站聚合数据）
# 只面向 role=admin，路由见 /api/status/stats。聚合较贵（要扫全站各用户
# 的账号清单与签到日志），故复用 _STATS_CACHE_TTL 缓存，轮询周期同仪表盘。
# ---------------------------------------------------------------------------
_STATS_CACHE_TTL = 3.0
_admin_stats_cache: Dict[str, Any] = {"t": 0.0, "payload": None}
_ADMIN_STATS_CACHE_LOCK = threading.Lock()


def build_admin_stats_cached(force: bool = False) -> Dict[str, Any]:
    """带缓存的统计页数据（管理员专用，全站聚合）。"""
    now = time.monotonic()
    with _ADMIN_STATS_CACHE_LOCK:
        entry = _admin_stats_cache
        if not force and entry["payload"] is not None and (now - entry["t"]) < _STATS_CACHE_TTL:
            return entry["payload"]
    payload = build_admin_stats()
    with _ADMIN_STATS_CACHE_LOCK:
        _admin_stats_cache["t"] = now
        _admin_stats_cache["payload"] = payload
    return payload


def _date_from_string(value: Any) -> Optional[str]:
    """从 ISO 时间串里取 'YYYY-MM-DD'，取不到返回 None。"""
    if not value:
        return None
    s = str(value).strip()[:10]
    return s if re.match(r"^\d{4}-\d{2}-\d{2}$", s) else None


def build_admin_stats() -> Dict[str, Any]:
    """管理员首页统计页：总览 + 注册趋势 + 签到效果。"""
    users = list_all_users()
    now = now_local()

    # ---- 总览卡 ----
    all_accounts = admin_account_overview()
    overview = {
        "total_users": len(users),
        "pending_users": sum(1 for u in users if u.get("status") == "pending"),
        "managed_accounts": len(all_accounts),
        "enabled_accounts": sum(1 for a in all_accounts if a.get("enabled") is not False),
        "signed_today": sum(1 for a in all_accounts if (a.get("latest") or {}).get("signed_today")),
    }

    # ---- 注册趋势：近 30 天按注册日期分组 ----
    reg_days = 30
    reg_counts = {}
    for i in range(reg_days - 1, -1, -1):
        reg_counts[(now.date() - timedelta(days=i)).isoformat()] = 0
    for u in users:
        day = _date_from_string(u.get("created_at"))
        if day in reg_counts:
            reg_counts[day] += 1
    registration_trend = [{"date": d, "count": reg_counts[d]} for d in sorted(reg_counts)]

    # ---- 签到效果：近 7 天按结果分类（成功 / 失败 / 轮空） ----
    signin_days = 7
    day_keys = [(now.date() - timedelta(days=i)).isoformat() for i in range(signin_days - 1, -1, -1)]
    signin = {k: {"success": 0, "fail": 0, "skip": 0} for k in day_keys}
    for user in users:
        ensure_user_workspace(user)
        if user.get("role") == "admin":
            bootstrap_admin_workspace(user)
        for product in PRODUCTS:
            _accounts, records = discover_product(product, dirs=product_dirs_for(product, user))
            for rec in records:
                day = _date_from_string(rec.get("time"))
                if day not in signin:
                    continue
                if rec.get("signed_today") or (rec.get("raw_result") or "").upper() in {"CLAIMED", "ALREADY"}:
                    cat = "success"
                elif rec.get("needs_attention"):
                    cat = "fail"
                else:
                    cat = "skip"
                signin[day][cat] += 1
    signin_effect = [{"date": k, **signin[k]} for k in day_keys]

    return {
        "generated_at": iso(now),
        "timezone": TIMEZONE_NAME,
        "overview": overview,
        "registration_trend": {"days": reg_days, "points": registration_trend},
        "signin_effect": {"days": signin_days, "points": signin_effect},
    }


def notify_admins_approval(username: str) -> None:
    """新用户注册后，提醒管理员去审核。

    遍历每个已通过的管理员，取其配置里「已启用且有凭据」的通知渠道发一条
    「有新的注册申请待审核」。全程安静失败（打日志不抛错）——通知发不出
    绝不能让注册失败。
    """
    if not approved_admin_users():
        return
    lines = [
        "新用户：%s" % username,
        "时间：%s" % iso(now_local()),
        "请到「用户管理」→ 审核 或 拒绝",
    ]
    for admin in approved_admin_users():
        try:
            config = load_user_config(admin)
            for product in PRODUCTS:
                values = ((config.get("notifications") or {}).get(product) or {})
                if not values.get("enabled") or not (values.get("key") or values.get("url")):
                    continue
                dirs = product_dirs_for(product, admin)
                notify = dirs["base"] / "notify.py"
                if not notify.exists():
                    continue
                env = notification_env(product, config, dirs)
                cmd = [sys.executable, str(notify), "startup",
                       "--note", "有新的注册申请待审核"]
                for line in lines:
                    cmd.append("--extra")
                    cmd.append(line)
                subprocess.run(
                    cmd, cwd=str(dirs["base"]),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    timeout=60, env=env,
                )
                # 每个管理员只发一条（用他配置的投递成功的那一条，避免两份产品重复推送）
                break
        except Exception:
            continue


def validate_username(username: str) -> str:
    username = str(username or "").strip()
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("账号格式：3-16 位，以英文字母开头，仅可含英文、数字、下划线")
    return username


def create_pending_user(username: str, password: str, fp_hash: Optional[str] = None) -> Dict[str, Any]:
    username = validate_username(username)
    if len(str(password or "")) < 6:
        raise ValueError("密码至少需要 6 位")
    fp_hash = (str(fp_hash or "").strip() or None)
    now = iso(now_local())
    try:
        with db_connect() as conn:
            if fp_hash:
                used = conn.execute(
                    "SELECT COUNT(*) AS c FROM users WHERE fp_hash=?",
                    (fp_hash,),
                ).fetchone()["c"]
                if used:
                    raise ValueError("该设备已注册过账号，一个设备只能注册一个账号")
            cur = conn.execute(
                """
                INSERT INTO users (username, password_hash, role, status, created_at, fp_hash)
                VALUES (?, ?, 'user', 'pending', ?, ?)
                """,
                (username, password_hash(password), now, fp_hash),
            )
            ensure_settings_row(conn, int(cur.lastrowid))
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
    except sqlite3.IntegrityError:
        existing = get_user_by_username(username)
        if existing and existing.get("status") == "pending":
            raise ValueError("该账号已提交注册，正在等待管理员审核") from None
        if existing is not None:
            raise ValueError("该账号已存在") from None
        # 到这里 username 不冲突，多半是唯一索引 fp_hash 兜住了「同一设备再注册」
        if fp_hash:
            raise ValueError("该设备已注册过账号，一个设备只能注册一个账号") from None
        raise ValueError("该账号已存在") from None
    return public_user(user_from_row(row) or {})


def authenticate_user(username: str, password: str) -> Tuple[Optional[Dict[str, Any]], str, int]:
    user = get_user_by_username(str(username or "").strip())
    if not user or not password_matches(password, str(user.get("password_hash") or "")):
        return None, "账号或密码不正确", HTTPStatus.UNAUTHORIZED
    status = str(user.get("status") or "pending")
    if status == "pending":
        return None, "账号已注册，等待管理员审核后才能登录", HTTPStatus.FORBIDDEN
    if status == "rejected":
        return None, "该账号注册未通过，请联系管理员", HTTPStatus.FORBIDDEN
    if status != "approved":
        return None, "该账号已被停用，请联系管理员", HTTPStatus.FORBIDDEN
    with db_connect() as conn:
        conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (iso(now_local()), user["id"]))
        conn.commit()
    user["last_login_at"] = iso(now_local())
    ensure_user_workspace(user)
    bootstrap_admin_workspace(user)
    return user, "", HTTPStatus.OK


def load_user_config(user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not user or not user.get("id"):
        return load_config()
    defaults = default_user_settings()
    with db_connect() as conn:
        ensure_settings_row(conn, int(user["id"]))
        row = conn.execute(
            "SELECT schedules_json, notifications_json FROM user_settings WHERE user_id=?",
            (int(user["id"]),),
        ).fetchone()
        conn.commit()
    if not row:
        return defaults
    try:
        schedules = json.loads(row["schedules_json"] or "{}")
    except Exception:
        schedules = {}
    try:
        notifications = json.loads(row["notifications_json"] or "{}")
    except Exception:
        notifications = {}
    return {
        "schedules": merge_dict(defaults["schedules"], schedules),
        "notifications": merge_dict(defaults["notifications"], notifications),
    }


def save_user_config(user: Optional[Dict[str, Any]], config: Dict[str, Any]) -> None:
    if not user or not user.get("id"):
        save_config(config)
        return
    defaults = default_user_settings()
    schedules = merge_dict(defaults["schedules"], config.get("schedules") or {})
    notifications = merge_dict(defaults["notifications"], config.get("notifications") or {})
    now = iso(now_local())
    with db_connect() as conn:
        ensure_settings_row(conn, int(user["id"]))
        conn.execute(
            """
            UPDATE user_settings
               SET schedules_json=?, notifications_json=?, updated_at=?
             WHERE user_id=?
            """,
            (
                json.dumps(schedules, ensure_ascii=False),
                json.dumps(notifications, ensure_ascii=False),
                now,
                int(user["id"]),
            ),
        )
        conn.commit()


def update_managed_user(admin: Dict[str, Any], user_id: Any, action: str) -> Dict[str, Any]:
    try:
        target_id = int(user_id)
    except Exception:
        raise ValueError("缺少用户 ID")
    action = str(action or "").strip().lower()
    now = iso(now_local())
    with db_connect() as conn:
        target = conn.execute("SELECT * FROM users WHERE id=?", (target_id,)).fetchone()
        if target is None:
            raise ValueError("用户不存在")
        if action == "approve":
            conn.execute(
                "UPDATE users SET status='approved', approved_at=?, approved_by=? WHERE id=?",
                (now, admin.get("id"), target_id),
            )
        elif action == "reject":
            if str(target["role"]) == "admin":
                raise ValueError("不能拒绝管理员账号")
            conn.execute("UPDATE users SET status='rejected' WHERE id=?", (target_id,))
        elif action == "disable":
            if str(target["role"]) == "admin":
                admin_count = conn.execute(
                    "SELECT COUNT(*) AS c FROM users WHERE role='admin' AND status='approved'"
                ).fetchone()["c"]
                if admin_count <= 1:
                    raise ValueError("至少需要保留一个可登录管理员")
            conn.execute("UPDATE users SET status='disabled' WHERE id=?", (target_id,))
        elif action == "activate":
            conn.execute(
                """
                UPDATE users
                   SET status='approved', approved_at=COALESCE(approved_at, ?),
                       approved_by=COALESCE(approved_by, ?)
                 WHERE id=?
                """,
                (now, admin.get("id"), target_id),
            )
        elif action == "make_admin":
            conn.execute("UPDATE users SET role='admin', status='approved', approved_at=COALESCE(approved_at, ?) WHERE id=?",
                         (now, target_id))
        elif action == "make_user":
            admin_count = conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE role='admin' AND status='approved'"
            ).fetchone()["c"]
            if str(target["role"]) == "admin" and admin_count <= 1:
                raise ValueError("至少需要保留一个可登录管理员")
            conn.execute("UPDATE users SET role='user' WHERE id=?", (target_id,))
        elif action == "delete":
            if int(admin.get("id") or 0) == target_id:
                raise ValueError("不能删除当前登录账号")
            if str(target["role"]) == "admin":
                admin_count = conn.execute(
                    "SELECT COUNT(*) AS c FROM users WHERE role='admin' AND status='approved'"
                ).fetchone()["c"]
                if admin_count <= 1:
                    raise ValueError("至少需要保留一个可登录管理员")
            username = str(target["username"] or "")
            conn.execute("DELETE FROM users WHERE id=?", (target_id,))
            conn.commit()
            return {
                "id": target_id,
                "username": username,
                "role": str(target["role"] or "user"),
                "status": "deleted",
                "deleted": True,
            }
        else:
            raise ValueError("不支持的用户操作")
        ensure_settings_row(conn, target_id)
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE id=?", (target_id,)).fetchone()
    user = user_from_row(row) or {}
    ensure_user_workspace(user)
    return public_user(user)


def list_schedulable_users() -> List[Dict[str, Any]]:
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE status='approved' ORDER BY id"
        ).fetchall()
    users = [user_from_row(row) or {} for row in rows]
    for user in users:
        ensure_user_workspace(user)
        bootstrap_admin_workspace(user)
    return users


def make_session_token(user: Dict[str, Any]) -> str:
    auth = auth_config()
    secret = str(auth.get("secret") or "")
    payload = {
        "uid": int(user.get("id") or 0),
        "user": str(user.get("username") or ""),
        "iat": int(time.time()),
        "exp": int(time.time()) + SESSION_MAX_AGE,
    }
    body = base64url_encode(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return "%s.%s" % (body, base64url_encode(signature))


def verify_session_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        body, signature = token.split(".", 1)
        auth = auth_config()
        secret = str(auth.get("secret") or "")
        expected = base64url_encode(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(base64url_decode(body).decode("utf-8"))
        if int(payload.get("exp") or 0) < int(time.time()):
            return None
        user = get_user_by_id(payload.get("uid"))
        if not user:
            return None
        username = str(payload.get("user") or "")
        if not hmac.compare_digest(username, str(user.get("username") or "")):
            return None
        if str(user.get("status") or "") != "approved":
            return None
        ensure_user_workspace(user)
        return user
    except Exception:
        return None


def cookie_values(headers) -> Dict[str, str]:
    cookies: Dict[str, str] = {}
    for part in (headers.get("Cookie") or "").split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        cookies[key.strip()] = value.strip()
    return cookies


def session_user(headers) -> Optional[Dict[str, Any]]:
    token = cookie_values(headers).get(SESSION_COOKIE)
    return verify_session_token(token) if token else None


def session_cookie(user: Dict[str, Any]) -> str:
    return "%s=%s; Path=/; Max-Age=%s; HttpOnly; SameSite=Lax%s" % (
        SESSION_COOKIE,
        make_session_token(user),
        SESSION_MAX_AGE,
        "; Secure" if cookie_secure_enabled() else "",
    )


def expired_session_cookie() -> str:
    return "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax%s" % (
        SESSION_COOKIE,
        "; Secure" if cookie_secure_enabled() else "",
    )


def cookie_secure_enabled() -> bool:
    """仅在 HTTPS 部署下开启；本地 http 调试或未上反代(nginx/caddy)前保持关闭。

    设 DASHBOARD_COOKIE_SECURE=1 才会输出 Secure —— 否则非加密链路会直接丢弃该 cookie。
    """
    return os.environ.get("DASHBOARD_COOKIE_SECURE", "").lower() in {"1", "true", "yes", "on"}


def write_notify_config(product: str, values: Dict[str, Any]) -> None:
    path = product_dirs(product)["notify"]
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "channel": values.get("channel") or "none",
        "key": values.get("key") or "",
        "url": values.get("url") or "",
        "on": values.get("on") or "daily",
        "group": values.get("group") or product,
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def sync_notify_configs(config: Dict[str, Any]) -> None:
    for product in ("WorkBuddy", "TRAE"):
        values = dict(((config.get("notifications") or {}).get(product) or {}))
        if not values.get("enabled"):
            # 关闭通知也必须落盘 —— 否则 notify.json 里留着旧配置，
            # 脚本照旧发消息，面板上的「关闭」等于没关。
            # channel="none" 是 notify.py 认的关闭标记。
            values = {
                "channel": "none",
                "key": "",
                "url": "",
                "on": values.get("on") or "daily",
                "group": values.get("group") or product,
            }
        write_notify_config(product, values)


def apply_notification_payload(config: Dict[str, Any], product: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    notifications = config.setdefault("notifications", {})
    current = notifications.setdefault(product, {})
    # 留空 = 不改（保留已存的）；显式清除走 *_clear 标记。
    if payload.get("key_clear"):
        current["key"] = ""
    elif "key" in payload and str(payload.get("key") or "").strip():
        current["key"] = str(payload.get("key")).strip()
    if payload.get("url_clear"):
        current["url"] = ""
    elif "url" in payload and str(payload.get("url") or "").strip():
        current["url"] = str(payload.get("url")).strip()
    for field in ("channel", "on", "group"):
        if field in payload and str(payload.get(field) or "").strip():
            current[field] = str(payload.get(field)).strip()
    if "enabled" in payload:
        current["enabled"] = bool(payload.get("enabled"))
    return current


def credential_paths(product: str, name: str, user: Optional[Dict[str, Any]] = None) -> Tuple[Path, Path]:
    dirs = product_dirs_for(product, user)
    safe = sanitize_name(name)
    return dirs["sessions"] / ("%s.json" % safe), dirs["disabled"] / ("%s.json" % safe)


def safe_account_stem(name: str) -> str:
    """账号名只用来拼文件名，必须挡住路径穿越。"""
    raw = str(name or "").strip()
    if not raw or raw in (".", "..") or "/" in raw or "\\" in raw or "\x00" in raw:
        raise ValueError("账号名不合法：%s" % name)
    return raw


def resolve_credential_paths(
    product: str,
    name: str,
    user: Optional[Dict[str, Any]] = None,
) -> Tuple[Path, Path, str]:
    """把界面上的「账号名」映射到磁盘上的真实文件。

    不能拿 sanitize_name(name) 反推 —— 目录里的文件名是**既有事实**，
    而 sanitize_name 会改名字（去首尾的 `._`、把特殊字符换成 `_`、截到 80 字）。
    一旦两者不一致，就会出现「卡片上看得见账号、点按钮却说找不到凭据文件」。
    实测踩点：文件名 `_e2e测试.json` → sanitize 成 `e2e测试` → 三个按钮全废。

    顺序：原名精确匹配 → 净化名兜底 → 都没有就返回净化名（由调用方报「没有凭据」）。
    """
    stem = safe_account_stem(name)
    dirs = product_dirs_for(product, user)
    active_dir, disabled_dir = dirs["sessions"], dirs["disabled"]

    for candidate in dict.fromkeys([stem, sanitize_name(stem)]):
        active = active_dir / ("%s.json" % candidate)
        disabled = disabled_dir / ("%s.json" % candidate)
        if active.exists() or disabled.exists():
            return active, disabled, candidate

    fallback = sanitize_name(stem)
    return active_dir / ("%s.json" % fallback), disabled_dir / ("%s.json" % fallback), fallback


def active_account_count(product: str, user: Optional[Dict[str, Any]] = None) -> int:
    dirs = product_dirs_for(product, user)
    count = 0
    if dirs["sessions"].is_dir():
        count += len([p for p in dirs["sessions"].glob("*.json") if not p.name.startswith(".")])
    if not user and product == "WorkBuddy" and (dirs["base"] / "session.json").is_file():
        count += 1
    return count


def upload_credential(
    product: str,
    filename: str,
    content: bytes,
    account_name: Optional[str] = None,
    user: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if product not in ("WorkBuddy", "TRAE"):
        raise ValueError("未知产品")
    if len(content) > 2 * 1024 * 1024:
        raise ValueError("凭据文件过大")
    try:
        data = json.loads(content.decode("utf-8"))
    except Exception as exc:
        raise ValueError("凭据不是合法 JSON：%s" % exc) from None
    if not isinstance(data, dict):
        raise ValueError("凭据 JSON 顶层必须是对象")
    if account_name:
        # 「替换凭据」的场景：账号已存在，必须沿用磁盘上那个文件名，
        # 重新净化会改出第二个文件，看起来像替换成功、实际多出一个账号。
        active, _disabled, name = resolve_credential_paths(product, account_name, user)
    else:
        name = sanitize_name(filename)
        active, _disabled = credential_paths(product, name, user)
    active.parent.mkdir(parents=True, exist_ok=True)
    tmp = active.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    tmp.replace(active)
    try:
        os.chmod(active, 0o600)
    except OSError:
        pass
    return {"product": product, "name": name, "path": rel(active)}


def set_account_enabled(
    product: str,
    name: str,
    enabled: bool,
    user: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    active, disabled, stem = resolve_credential_paths(product, name, user)
    if enabled:
        source, target = disabled, active
    else:
        source, target = active, disabled
    if not source.exists():
        # 三种「源文件不在」要给出三种不同的说法，否则用户看到的都是同一句
        # 「找不到账号凭据」，根本分不清该重传还是该刷新。
        if target.exists():
            raise FileNotFoundError(
                "账号已经是%s状态：%s" % ("启用" if enabled else "停用", stem))
        raise FileNotFoundError(
            "账号 %s 没有凭据文件（只在日志里出现过），请先上传凭据再操作。" % stem)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError("目标位置已有同名文件：%s" % target.name)
    shutil.move(str(source), str(target))
    return {"product": product, "name": stem, "enabled": enabled, "path": rel(target)}


def archive_account(
    product: str,
    name: str,
    user: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    active, disabled, stem = resolve_credential_paths(product, name, user)
    source = active if active.exists() else disabled if disabled.exists() else None
    if source is None:
        raise FileNotFoundError("账号 %s 没有凭据文件，无需归档。" % stem)
    stamp = now_local().strftime("%Y%m%d-%H%M%S")
    target_dir = workspace_archive_dir(user) / product
    target_dir.mkdir(parents=True, exist_ok=True)
    # 用真实文件名入档 —— 恢复时才能原样还原，不会因为净化改名对不上。
    target = target_dir / ("%s-%s.json" % (stamp, stem))
    shutil.move(str(source), str(target))
    return {"product": product, "name": stem, "archived_to": rel(target)}


def delete_account(
    product: str,
    name: str,
    user: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """永久删除账号凭据和该账号日志。

    仅删除凭据会被日志重新发现成“仅日志账号”，所以这里同时清理
    该账号对应的日志文件。只删除明确命中的文件，不递归删目录。
    """
    active, disabled, stem = resolve_credential_paths(product, name, user)
    dirs = product_dirs_for(product, user)
    removed: List[str] = []
    candidates: List[Path] = [active, disabled]

    if not user and product == "WorkBuddy" and stem == "default":
        candidates.append(dirs["base"] / "session.json")

    for candidate in dict.fromkeys([stem, sanitize_name(stem)]):
        candidates.append(dirs["logs"] / ("%s.log" % candidate))
        if candidate == "default":
            candidates.append(dirs["logs"] / "signin.log")
            candidates.append(dirs["base"] / "signin.log")

    for path in dict.fromkeys(candidates):
        try:
            if path.is_file():
                path.unlink()
                removed.append(rel(path))
        except OSError as exc:
            raise OSError("删除失败：%s（%s）" % (rel(path), exc)) from None

    if not removed:
        raise FileNotFoundError("找不到可删除的账号文件：%s" % stem)
    return {"product": product, "name": stem, "removed": removed}


# 归档文件名 = <YYYYmmdd>-<HHMMSS>-<账号名>.json。
# 账号名本身允许含 "-"，所以只能按「前缀时间戳」切，不能按第一个 "-" 切。
ARCHIVE_NAME_RE = re.compile(r"^(\d{8})-(\d{6})-(.+)$")


def parse_archive_stem(stem: str) -> Tuple[str, str]:
    match = ARCHIVE_NAME_RE.match(stem or "")
    if not match:
        return "", stem or ""
    ymd, hms = match.group(1), match.group(2)
    at = "%s-%s-%s %s:%s:%s" % (ymd[:4], ymd[4:6], ymd[6:8], hms[:2], hms[2:4], hms[4:6])
    return at, match.group(3)


def list_archived(user: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """列出 archive/ 下所有归档账号，最新在前。

    归档是软删除 —— 不列出来就等于数据没了，所以这个清单必须存在。
    """
    out: List[Dict[str, Any]] = []
    for product in PRODUCTS:
        folder = workspace_archive_dir(user) / product
        if not folder.is_dir():
            continue
        for path in folder.glob("*.json"):
            try:
                at, name = parse_archive_stem(path.stem)
                out.append({
                    "product": product,
                    "name": name,
                    "file": rel(path),
                    "archived_at": at,
                    "size": path.stat().st_size,
                })
            except OSError:
                continue
    out.sort(key=lambda row: (row.get("archived_at") or "", row.get("name") or ""), reverse=True)
    return out


def restore_account(
    product: str,
    name: str,
    user: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """把归档账号搬回启用目录。归档必须可逆，否则点一下就把账号弄丢了。"""
    product = "TRAE" if str(product or "").lower() == "trae" else "WorkBuddy"
    stem = safe_account_stem(name)
    folder = workspace_archive_dir(user) / product

    # 归档文件名里存的是「当时真实的文件名」，所以先按原名比、再按净化名兜底。
    # 反过来用 sanitize_name(name) 单边比对，`_e2e测试` 这类名字永远匹配不上。
    wanted = dict.fromkeys([stem, sanitize_name(stem)])
    # 同一账号可能归档过多次 → 恢复最新那一份。
    # 用解析出来的名字精确比对，不用 glob 拼模式（名字里可能有特殊字符）。
    candidates: List[Path] = []
    if folder.is_dir():
        for path in folder.glob("*.json"):
            _at, archived_name = parse_archive_stem(path.stem)
            if archived_name in wanted:
                candidates.append(path)
    if not candidates:
        raise FileNotFoundError("归档里找不到账号：%s" % name)

    source = sorted(candidates)[-1]
    # 目标名 = 归档时记下的名字，原样还原，不重新净化 —— 否则恢复出来的文件名
    # 又和界面上显示的名字对不上，下一轮点击继续失灵。
    _at, archived_name = parse_archive_stem(source.stem)
    if not archived_name or "/" in archived_name or "\\" in archived_name:
        archived_name = stem
    target = product_dirs_for(product, user)["sessions"] / ("%s.json" % archived_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError("启用目录里已有同名账号：%s" % target.name)
    shutil.move(str(source), str(target))
    return {"product": product, "name": archived_name, "restored_to": rel(target)}


def parse_multipart(body: bytes, content_type: str) -> Tuple[Dict[str, str], List[Dict[str, Any]]]:
    match = re.search(r"boundary=(?P<boundary>[^;]+)", content_type or "")
    if not match:
        raise ValueError("缺少 multipart boundary")
    boundary = match.group("boundary").strip().strip('"').encode("utf-8")
    fields: Dict[str, str] = {}
    files: List[Dict[str, Any]] = []
    marker = b"--" + boundary
    for part in body.split(marker):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if part.endswith(b"--"):
            part = part[:-2].rstrip(b"\r\n")
        if b"\r\n\r\n" not in part:
            continue
        raw_headers, content = part.split(b"\r\n\r\n", 1)
        headers = raw_headers.decode("utf-8", "replace")
        disposition = ""
        for line in headers.splitlines():
            if line.lower().startswith("content-disposition:"):
                disposition = line
                break
        name_match = re.search(r'name="([^"]+)"', disposition)
        if not name_match:
            continue
        field_name = name_match.group(1)
        file_match = re.search(r'filename="([^"]*)"', disposition)
        if file_match:
            files.append({
                "field": field_name,
                "filename": file_match.group(1) or "session.json",
                "content": content,
            })
        else:
            fields[field_name] = content.decode("utf-8", "replace")
    return fields, files


def command_for(
    product: str,
    mode: str,
    user: Optional[Dict[str, Any]] = None,
) -> Tuple[Path, List[str], Dict[str, str]]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    dirs = product_dirs_for(product, user)
    if product == "WorkBuddy":
        base = product_base("WorkBuddy")
        run_sh = base / "run.sh"
        if run_sh.exists() and not user:
            return base, [str(run_sh), mode], env
        sessions = dirs["sessions"]
        if sessions.is_dir():
            return base, [
                sys.executable, str(base / "multi_run.py"), mode,
                "--dir", str(sessions),
                "--log-dir", str(dirs["logs"]),
                "--multi-log", str(dirs["multi_log"]),
                "--lock", str(dirs["lock"]),
            ], env
        return base, [sys.executable, str(base / "signin.py"), mode], env
    if product == "TRAE":
        base = product_base("TRAE")
        run_sh = base / "run.sh"
        if run_sh.exists() and not user:
            return base, [str(run_sh), mode], env
        return base, [
            sys.executable, str(base / "trae_signin.py"), mode,
            "--dir", str(dirs["sessions"]),
            "--log-dir", str(dirs["logs"]),
            "--multi-log", str(dirs["multi_log"]),
        ], env
    raise ValueError("unknown product")


def single_account_command(
    product: str,
    mode: str,
    account: str,
    user: Optional[Dict[str, Any]] = None,
) -> Tuple[Path, List[str], Dict[str, str]]:
    """只跑某一个账号的命令。

    run.sh / multi_run.py 都是「扫目录跑全部」，没有账号维度 ——
    所以要真正只跑一个，必须绕开它们，把凭据单独指给单账号脚本：
      · TRAE      trae_signin.py --file <凭据>   （load_accounts 认 --file / TRAE_SESSION_FILE）
      · WorkBuddy signin.py + WORKBUDDY_AUTH_FILE（multi_run.py 没有单账号入口）
    日志落点也要对齐，否则面板的「执行记录」看不到这次运行。
    """
    base = product_base(product)
    cred, _disabled = credential_paths(product, account, user)
    if not cred.is_file():
        raise FileNotFoundError(str(cred))

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    logs = product_dirs_for(product, user)["logs"]

    if product == "TRAE":
        return base, [sys.executable, str(base / "trae_signin.py"), mode,
                      "--file", str(cred), "--log-dir", str(logs)], env

    # signin.py 默认写脚本同目录的 signin.log；指到 logs/<账号>.log 才能被面板认到
    env["WORKBUDDY_AUTH_FILE"] = str(cred)
    env["WORKBUDDY_SIGNIN_LOG"] = str(logs / ("%s.log" % sanitize_name(account)))
    return base, [sys.executable, str(base / "signin.py"), mode], env


def notification_env(
    product: str,
    config: Dict[str, Any],
    dirs: Dict[str, Path],
) -> Dict[str, str]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    values = dict(((config.get("notifications") or {}).get(product) or {}))
    if not values.get("enabled"):
        values = {"channel": "none", "key": "", "url": "", "on": values.get("on") or "daily",
                  "group": values.get("group") or product}
    env["WB_NOTIFY_CHANNEL"] = str(values.get("channel") or "none")
    env["WB_NOTIFY_KEY"] = str(values.get("key") or "")
    env["WB_NOTIFY_URL"] = str(values.get("url") or "")
    env["WB_NOTIFY_ON"] = str(values.get("on") or "daily")
    env["WB_NOTIFY_GROUP"] = str(values.get("group") or product)
    env["WB_NOTIFY_MULTI_LOG"] = str(dirs["multi_log"])
    env["WB_NOTIFY_STATE"] = str(dirs.get("notify_state") or (dirs["logs"] / "notify_state.json"))
    return env


def run_notify(
    product: str,
    user: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> None:
    dirs = product_dirs_for(product, user)
    notify = dirs["base"] / "notify.py"
    if not notify.exists():
        return
    cmd = [sys.executable, str(notify), "check"]
    if dirs["logs"].is_dir():
        cmd.extend(["--log-dir", str(dirs["logs"])])
    if dirs["multi_log"].exists():
        cmd.extend(["--multi-log", str(dirs["multi_log"])])
    env = notification_env(product, config or load_user_config(user), dirs)
    try:
        subprocess.run(cmd, cwd=str(dirs["base"]), stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=60, env=env)
    except Exception:
        pass


def run_notify_test(
    product: str,
    config: Dict[str, Any],
    user: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    dirs = product_dirs_for(product, user)
    notify = dirs["base"] / "notify.py"
    if not notify.exists():
        raise FileNotFoundError("未找到通知脚本：%s" % rel(notify))
    env = notification_env(product, config, dirs)
    proc = subprocess.run(
        [sys.executable, str(notify), "test"],
        cwd=str(dirs["base"]),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=90,
    )
    output = proc.stdout.decode("utf-8", "replace").strip()
    if len(output) > 2000:
        output = output[-2000:]
    return {
        "product": product,
        "code": proc.returncode,
        "ok": proc.returncode == 0,
        "output": output,
    }


def task_worker(
    task_id: str,
    product: str,
    mode: str,
    out_path: Path,
    cwd: Path,
    cmd: List[str],
    env: Dict[str, str],
    user_id: Optional[int] = None,
) -> None:
    with TASK_LOCK:
        TASKS[task_id]["status"] = "running"
    code = -1
    try:
        with out_path.open("ab") as handle:
            handle.write(("[%s] start %s %s\n" % (now_local().strftime("%Y-%m-%d %H:%M:%S"), product, mode)).encode("utf-8"))
            proc = subprocess.run(cmd, cwd=str(cwd), env=env, stdout=handle,
                                  stderr=subprocess.STDOUT, timeout=30 * 60)
            code = proc.returncode
            handle.write(("[%s] exit %s\n" % (now_local().strftime("%Y-%m-%d %H:%M:%S"), code)).encode("utf-8"))
        if mode not in {"status", "doctor"}:
            run_notify(product, get_user_by_id(user_id) if user_id else None)
        status = "finished" if code == 0 else "failed"
    except Exception as exc:
        status = "failed"
        try:
            with out_path.open("ab") as handle:
                handle.write(("[%s] error %s\n" % (now_local().strftime("%Y-%m-%d %H:%M:%S"), exc)).encode("utf-8", "replace"))
        except Exception:
            pass
    with TASK_LOCK:
        TASKS[task_id].update({
            "status": status,
            "code": code,
            "finished_at": iso(now_local()),
        })


def start_task(
    product: str,
    mode: str,
    reason: str = "manual",
    account: Optional[str] = None,
    user: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if user:
        ensure_user_workspace(user)
    if account:
        # 单账号：只校验这一个账号的凭据在不在，不看整个产品的账号数
        cred, _disabled = credential_paths(product, account, user)
        if not cred.is_file():
            return {
                "id": uuid.uuid4().hex[:12],
                "product": product,
                "account": account,
                "user_id": (user or {}).get("id"),
                "username": (user or {}).get("username") or "",
                "mode": mode,
                "reason": reason,
                "status": "skipped",
                "code": 0,
                "output": "",
                "started_at": iso(now_local()),
                "finished_at": iso(now_local()),
                "note": "凭据文件不存在，已跳过",
            }
    elif mode not in {"status", "doctor"} and active_account_count(product, user) == 0:
        return {
            "id": uuid.uuid4().hex[:12],
            "product": product,
            "user_id": (user or {}).get("id"),
            "username": (user or {}).get("username") or "",
            "mode": mode,
            "reason": reason,
            "status": "skipped",
            "code": 0,
            "output": "",
            "started_at": iso(now_local()),
            "finished_at": iso(now_local()),
            "note": "没有启用账号，已跳过",
        }
    cwd, cmd, env = single_account_command(product, mode, account, user) if account \
        else command_for(product, mode, user)
    if not cwd.exists():
        raise FileNotFoundError(str(cwd))
    runtime_dir = workspace_runtime_dir(user)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_local().strftime("%Y%m%d-%H%M%S")
    task_id = uuid.uuid4().hex[:12]
    out_path = runtime_dir / f"{stamp}-{product.lower()}-{mode}-{task_id}.log"
    payload = {
        "id": task_id,
        "product": product,
        "account": account or "",
        "user_id": (user or {}).get("id"),
        "username": (user or {}).get("username") or "",
        "mode": mode,
        "reason": reason,
        "status": "queued",
        "code": None,
        "output": rel(out_path),
        "started_at": iso(now_local()),
    }
    with TASK_LOCK:
        TASKS[task_id] = dict(payload)
    thread = threading.Thread(
        target=task_worker,
        args=(task_id, product, mode, out_path, cwd, cmd, env, (user or {}).get("id")),
        daemon=True,
    )
    thread.start()
    return payload


def _try_lock_file(path: Path) -> Any:
    """尝试对 path 加「不阻塞文件锁」。成功返回句柄（须保持不关闭），失败返回 None。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(str(path), "a+")
    except Exception:
        return None
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        try:
            handle.close()
        except Exception:
            pass
        return None
    return handle


def acquire_scheduler_lock() -> bool:
    """抢调度锁。抢不到说明已有实例在跑定时，本进程只提供 HTTP 服务。

    返回 True 时才进入调度循环；句柄挂在全局变量上保持不关闭 ——
    一旦关闭，操作系统会立刻释放这把锁，别的实例就能抢走。
    """
    global SCHEDULER_LOCK_HANDLE
    handle = _try_lock_file(SCHEDULER_LOCK_PATH)
    if handle is None:
        sys.stderr.write("[scheduler] 锁已被占用/打不开，本次不启用定时。\n")
        return False
    SCHEDULER_LOCK_HANDLE = handle
    try:
        handle.seek(0)
        handle.truncate()
        handle.write("pid=%s started=%s\n" % (os.getpid(), iso(now_local())))
        handle.flush()
    except Exception:
        pass
    return True


def acquire_cleanup_lock() -> bool:
    """抢「无凭据账号清理」锁，保证多实例时只有一个在跑清理。"""
    global CLEANUP_LOCK_HANDLE
    handle = _try_lock_file(CLEANUP_LOCK_PATH)
    if handle is None:
        sys.stderr.write("[purge] 清理锁已被占用，本进程不参与账号清理。\n")
        return False
    CLEANUP_LOCK_HANDLE = handle
    return True


def claim_slot(slot_key: str) -> bool:
    """跨进程原子占位：同一个调度点只允许一个进程真正执行。

    O_CREAT|O_EXCL 是操作系统级原子操作 —— 多实例同一分钟同时到点，
    只有一个能创建成功，其余拿到 FileExistsError 直接让路。
    """
    try:
        slot_dir = RUNTIME_DIR / SLOT_DIRNAME
        slot_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^0-9A-Za-z_.-]", "_", slot_key)
        marker = slot_dir / (safe + ".fired")
        fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except Exception as exc:
        sys.stderr.write("[scheduler] 占位文件创建失败，本次不执行：%s\n" % exc)
        return False
    try:
        os.write(fd, ("pid=%s at=%s\n" % (os.getpid(), iso(now_local()))).encode("utf-8"))
    except Exception:
        pass
    finally:
        os.close(fd)
    return True


def prune_slot_markers(today: str) -> None:
    """清掉非今天的占位文件，避免 slots/ 无限累积。"""
    slot_dir = RUNTIME_DIR / SLOT_DIRNAME
    if not slot_dir.is_dir():
        return
    for path in slot_dir.glob("*.fired"):
        if not path.name.startswith(today):
            try:
                path.unlink()
            except Exception:
                pass


def schedule_slots(item: Dict[str, Any]) -> List[Tuple[str, str]]:
    """把一个产品的配置摊平成 [(时刻, 类型)]，类型为 daily / poll。"""
    slots: List[Tuple[str, str]] = []
    if item.get("enabled") and item.get("daily_time"):
        slots.append((str(item["daily_time"]), "daily"))
    if item.get("enabled") and item.get("poll_enabled"):
        for clock in (item.get("poll_times") or []):
            slots.append((str(clock), "poll"))
    return slots


def load_fired_keys() -> set:
    if not FIRED_KEYS_PATH.is_file():
        return set()
    try:
        raw = json.loads(FIRED_KEYS_PATH.read_text("utf-8") or "[]")
    except Exception:
        return set()
    return {str(item) for item in raw} if isinstance(raw, list) else set()


def save_fired_keys(keys: set) -> None:
    """合并写：别的进程记过的触发点不能被本进程内存里的集合覆盖掉。"""
    try:
        merged = load_fired_keys() | {str(item) for item in keys}
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        tmp = FIRED_KEYS_PATH.with_name(FIRED_KEYS_PATH.name + ".tmp")
        tmp.write_text(json.dumps(sorted(merged), ensure_ascii=False), "utf-8")
        tmp.replace(FIRED_KEYS_PATH)
    except Exception as exc:
        sys.stderr.write("[scheduler] 触发记录写入失败：%s\n" % exc)


def next_run_at(config: Dict[str, Any]) -> Optional[str]:
    """算了下一个将要触发的调度点，给界面和 --check 用。"""
    now = now_local()
    best: Optional[datetime] = None
    for item in schedule_payload(config):
        for clock, _kind in schedule_slots(item):
            try:
                hour, minute = (int(part) for part in clock.split(":"))
            except Exception:
                continue
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target = target + timedelta(days=1)
            if best is None or target < best:
                best = target
    return iso(best) if best else None


def schedule_scope(user: Optional[Dict[str, Any]]) -> str:
    return "u%s" % int(user["id"]) if user and user.get("id") else "global"


def today_schedule_status(config: Dict[str, Any], user: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """今天每个调度点跑过没有 —— 用来回答「到点了为什么没动静」。"""
    today = now_local().strftime("%Y-%m-%d")
    fired = load_fired_keys()
    scope = schedule_scope(user)
    out: List[Dict[str, Any]] = []
    for item in schedule_payload(config):
        product = item["product"]
        for clock, kind in schedule_slots(item):
            key = "%s %s|%s|%s|%s" % (today, clock, scope, product, kind)
            out.append({
                "product": product,
                "clock": clock,
                "kind": kind,
                "label": "每日签到" if kind == "daily" else "轮询补签",
                "fired": key in fired,
                "mode": item.get("daily_mode") if kind == "daily" else item.get("poll_mode"),
            })
    out.sort(key=lambda row: (row["clock"], row["product"]))
    return out


def scheduler_loop() -> None:
    global FIRED_SCHEDULE_KEYS, SCHEDULER_DELEGATED
    if not acquire_scheduler_lock():
        SCHEDULER_DELEGATED = True
        sys.stderr.write(
            "[scheduler] 已有实例接管定时任务，本进程只提供 HTTP 服务。\n"
            "            （这是防止同一时间点被重复执行的保护）\n"
        )
        return
    FIRED_SCHEDULE_KEYS |= load_fired_keys()
    sys.stderr.write("[scheduler] 定时任务已接管，每 20 秒比对一次时间点。\n")
    while not SCHEDULER_STOP.is_set():
        now = now_local()
        minute_key = now.strftime("%Y-%m-%d %H:%M")
        current = now.strftime("%H:%M")
        try:
            fired_any = False
            for user in list_schedulable_users():
                config = load_user_config(user)
                scope = schedule_scope(user)
                for item in schedule_payload(config):
                    product = item["product"]
                    for clock, kind in schedule_slots(item):
                        if clock != current:
                            continue
                        key = "%s|%s|%s|%s" % (minute_key, scope, product, kind)
                        if key in FIRED_SCHEDULE_KEYS:
                            continue
                        # 双保险：进程内记忆挡住同进程的重复循环，
                        # 原子占位挡住「另一个实例也在同一分钟到点」。
                        if not claim_slot(key):
                            FIRED_SCHEDULE_KEYS.add(key)
                            sys.stderr.write(
                                "[%s] 调度点 %s 已被其他实例抢占，本进程让路。\n" % (minute_key, key)
                            )
                            continue
                        FIRED_SCHEDULE_KEYS.add(key)
                        fired_any = True
                        mode = item.get("daily_mode") if kind == "daily" else item.get("poll_mode")
                        sys.stderr.write(
                            "[%s] 触发定时任务 %s/%s %s（%s）\n" %
                            (minute_key, user.get("username") or ("u%s" % user.get("id")), product,
                             mode or "silent", kind)
                        )
                        start_task(product, mode or "silent", reason="schedule", user=user)
            if fired_any:
                save_fired_keys(FIRED_SCHEDULE_KEYS)
            if len(FIRED_SCHEDULE_KEYS) > 5000:
                today = now.strftime("%Y-%m-%d")
                FIRED_SCHEDULE_KEYS = {k for k in FIRED_SCHEDULE_KEYS if k.startswith(today)}
                save_fired_keys(FIRED_SCHEDULE_KEYS)
                prune_slot_markers(today)
        except Exception as exc:
            sys.stderr.write("[scheduler error] %s\n" % exc)
        SCHEDULER_STOP.wait(20)


# ---------------------------------------------------------------------------
# 无凭据账号的自动清理：注册超过 grace、期间从未上传过任何凭据的普通账号，
# 自动删除（连同其工作区），避免长期堆积空账号占用空间。
# ---------------------------------------------------------------------------
def user_uploaded_any_credential(user: Dict[str, Any]) -> bool:
    try:
        for product in PRODUCTS:
            if active_account_count(product, user) > 0:
                return True
    except Exception:
        pass
    return False


def remove_user_row(user: Dict[str, Any]) -> None:
    with db_connect() as conn:
        conn.execute("DELETE FROM users WHERE id=?", (int(user["id"]),))
        conn.commit()


def remove_user_workspace(user: Dict[str, Any]) -> None:
    root = user_workspace_root(user)
    if not root:
        return
    try:
        if root.exists() or root.is_symlink():
            shutil.rmtree(str(root))
    except Exception as exc:
        sys.stderr.write("[purge] 删除工作区失败 %s：%s\n" % (root, exc))


def purge_no_credential_users() -> int:
    """执行一轮清理，返回删除的账号数。只删：非管理员 + 状态为 pending/approved
    + 注册已超过 grace 小时 + 从未上传过任何凭据。"""
    now = now_local()
    grace = datetime.timedelta(hours=PURGE_NO_CREDENTIAL_HOURS)
    removed = 0
    for user in list_all_users():
        if str(user.get("role") or "") == "admin":
            continue
        if str(user.get("status") or "") not in ("pending", "approved"):
            continue
        created = parse_time(user.get("created_at"))
        if created is None or (now - created) < grace:
            continue
        if user_uploaded_any_credential(user):
            continue
        sys.stderr.write(
            "[purge] 删除从未上传凭据的账号 %s（注册于 %s，超 %s 天）\n"
            % (str(user.get("username") or ""), user.get("created_at"),
               PURGE_NO_CREDENTIAL_HOURS / 24.0)
        )
        remove_user_row(user)
        remove_user_workspace(user)
        removed += 1
    return removed


def purge_loop() -> None:
    """后台清理线程：首次延迟 60s 等服务就绪，之后按周期扫一轮。"""
    try:
        if not acquire_cleanup_lock():
            return
        sys.stderr.write("[purge] 无凭据账号清理已接管，每 %s 秒运行一次。\n" % PURGE_INTERVAL_SECONDS)
        while not SCHEDULER_STOP.is_set():
            try:
                removed = purge_no_credential_users()
                if removed:
                    sys.stderr.write("[purge] 本轮清理 %s 个账号。\n" % removed)
            except Exception as exc:
                sys.stderr.write("[purge error] %s\n" % exc)
            SCHEDULER_STOP.wait(PURGE_INTERVAL_SECONDS)
    except Exception as exc:
        sys.stderr.write("[purge error] %s\n" % exc)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "SigninDashboard/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[%s] %s\n" % (now_local().strftime("%Y-%m-%d %H:%M:%S"), fmt % args))

    def current_user(self) -> Optional[Dict[str, Any]]:
        return session_user(self.headers)

    def dashboard_payload(self) -> Dict[str, Any]:
        return build_dashboard(self.current_user())

    def require_login(self) -> Optional[Dict[str, Any]]:
        user = self.current_user()
        if not user:
            json_response(self, {"error": "请先登录", "authenticated": False}, HTTPStatus.UNAUTHORIZED)
            return None
        return user

    def require_admin(self) -> Optional[Dict[str, Any]]:
        user = self.require_login()
        if not user:
            return None
        if user.get("role") != "admin":
            json_response(self, {"error": "需要管理员权限"}, HTTPStatus.FORBIDDEN)
            return None
        return user

    def client_ip(self) -> str:
        cfg = load_config().get("rate_limit") or {}
        if cfg.get("proxy"):
            xff = self.headers.get("X-Forwarded-For")
            if xff:
                return xff.split(",")[0].strip()
        return self.client_address[0]

    def check_rate_limit(self) -> bool:
        """IP 滑动窗口限流；被拦截时返回 429 并返回 False。"""
        path = self.path.split("?", 1)[0]
        cfg = load_config().get("rate_limit") or {}
        if not cfg.get("enabled", True):
            return True
        if path in ("/api/auth/login", "/api/auth/register"):
            bucket = "auth"
            rule = cfg.get("auth") or {}
            limit = int(rule.get("limit", 8))
            window = float(rule.get("window", 60))
        elif path.startswith("/api/"):
            bucket = "api"
            rule = cfg.get("api") or {}
            limit = int(rule.get("limit", 180))
            window = float(rule.get("window", 60))
        else:
            return True  # 静态资源不参与限流，避免首次整页加载误伤
        allowed, retry = _rate_allow(bucket, self.client_ip(), limit, window)
        if not allowed:
            json_response(
                self,
                {"error": "请求过于频繁，请稍后再试", "retry_after": retry},
                HTTPStatus.TOO_MANY_REQUESTS,
                headers={"Retry-After": str(retry)},
            )
            return False
        return True

    def read_json_body(self, max_bytes: int = 10 * 1024 * 1024) -> Dict[str, Any]:
        body = self.read_raw_body(max_bytes)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except Exception:
            raise ValueError("请求体不是合法 JSON") from None
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return payload

    def read_raw_body(self, max_bytes: int = 10 * 1024 * 1024) -> bytes:
        """读原始请求体；若是加密封包则解出明文，否则原样返回（兼容明文客户端）。"""
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(length, max_bytes))
        dec = open_envelope(raw, envelope_aad(self.command, self.path))
        return dec if dec is not None else raw

    def do_GET(self) -> None:  # noqa: N802
        self._resp_crypto = (self.headers.get("X-Crypto") or "") == "1"
        if not self.check_rate_limit():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/crypto/key":
            # 引导密钥：明文返回（此时客户端尚无密钥，故不加密）。
            json_response(self, {"key": base64url_encode(crypto_secret())})
            return
        if parsed.path == "/api/auth/session":
            user = self.current_user()
            json_response(self, {
                "authenticated": bool(user),
                "username": (user or {}).get("username") or "",
                "role": (user or {}).get("role") or "",
                "status": (user or {}).get("status") or "",
            })
            return
        if parsed.path == "/api/admin/users":
            if not self.require_admin():
                return
            json_response(self, {"users": list_user_accounts()})
            return
        if parsed.path == "/api/status" or parsed.path.startswith("/api/status/"):
            if not self.require_login():
                return
            user = self.current_user()
            payload = build_dashboard_cached(user)
            path = parsed.path.rstrip("/")
            if path == "/api/status":
                json_response(self, payload)
            elif path == "/api/status/overview":
                json_response(self, {
                    "generated_at": payload["generated_at"],
                    "timezone": payload["timezone"],
                    "summary": payload["summary"],
                    "products": payload["products"],
                    "scheduler_active": payload["scheduler_active"],
                    "next_run_at": payload["next_run_at"],
                    "schedule_status": payload["schedule_status"],
                    "auth": payload["auth"],
                })
            elif path == "/api/status/accounts":
                json_response(self, {
                    "summary": payload["summary"],
                    "accounts": payload["accounts"],
                    "archived": payload["archived"],
                })
            elif path == "/api/status/recent":
                query = parse_qs(parsed.query)
                try:
                    limit = int((query.get("limit") or ["100"])[0])
                except ValueError:
                    limit = 100
                json_response(self, {"recent": payload["recent"][: max(1, min(limit, 500))]})
            elif path == "/api/status/schedules":
                json_response(self, {
                    "schedules": payload["schedules"],
                    "next_run_at": payload["next_run_at"],
                    "schedule_status": payload["schedule_status"],
                    "config_schedules": (payload.get("config") or {}).get("schedules") or {},
                })
            elif path == "/api/status/notifications":
                json_response(self, {
                    "notifications": payload["notifications"],
                    "config_notifications": (payload.get("config") or {}).get("notifications") or {},
                })
            elif path == "/api/status/tasks":
                json_response(self, {"tasks": payload["tasks"]})
            elif path == "/api/status/auth":
                json_response(self, payload["auth"])
            elif path == "/api/status/admin":
                if not self.require_admin():
                    return
                json_response(self, payload.get("admin"))
            elif path == "/api/status/stats":
                if not self.require_admin():
                    return
                json_response(self, build_admin_stats_cached())
            else:
                json_response(self, payload)
            return
        if parsed.path == "/api/logs":
            if not self.require_login():
                return
            query = parse_qs(parsed.query)
            try:
                limit = int((query.get("limit") or ["200"])[0])
            except ValueError:
                limit = 200
            payload = build_dashboard_cached(self.current_user())
            json_response(self, {"recent": payload["recent"][: max(1, min(limit, 500))]})
            return
        if parsed.path == "/api/log":
            if not self.require_login():
                return
            query = parse_qs(parsed.query)
            rel_path = (query.get("path") or [""])[0]
            try:
                lines = int((query.get("lines") or ["120"])[0])
            except ValueError:
                lines = 120
            json_response(self, self.read_log_tail(rel_path, lines))
            return
        if parsed.path == "/healthz":
            text_response(self, "ok\n")
            return
        self.serve_static(parsed.path)

    def do_POST(self) -> None:  # noqa: N802
        self._resp_crypto = (self.headers.get("X-Crypto") or "") == "1"
        if not self.check_rate_limit():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/auth/login":
            self.handle_login()
            return
        if parsed.path == "/api/auth/register":
            self.handle_register()
            return
        if parsed.path == "/api/auth/logout":
            json_response(
                self,
                {"authenticated": False},
                headers={"Set-Cookie": expired_session_cookie()},
            )
            return
        if parsed.path not in {
            "/api/run",
            "/api/accounts/upload",
            "/api/accounts/toggle",
            "/api/accounts/archive",
            "/api/accounts/restore",
            "/api/accounts/delete",
            "/api/schedules",
            "/api/notifications",
            "/api/notifications/test",
            "/api/admin/users/update",
        }:
            json_response(self, {"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        user = self.require_login()
        if not user:
            return
        if parsed.path == "/api/admin/users/update":
            admin = self.require_admin()
            if not admin:
                return
            try:
                payload = self.read_json_body(max_bytes=64 * 1024)
                updated = update_managed_user(admin, payload.get("user_id"), payload.get("action"))
            except Exception as exc:
                json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            json_response(self, {"user": updated, "users": list_user_accounts(), "status": self.dashboard_payload()})
            return
        body = self.read_raw_body(10 * 1024 * 1024)
        if parsed.path == "/api/accounts/upload":
            self.handle_upload(body, user)
            return
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except Exception:
            json_response(self, {"error": "请求体不是合法 JSON"}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/accounts/toggle":
            self.handle_toggle(payload, user)
            return
        if parsed.path == "/api/accounts/archive":
            self.handle_archive(payload, user)
            return
        if parsed.path == "/api/accounts/restore":
            self.handle_restore(payload, user)
            return
        if parsed.path == "/api/accounts/delete":
            self.handle_delete(payload, user)
            return
        if parsed.path == "/api/schedules":
            self.handle_schedules(payload, user)
            return
        if parsed.path == "/api/notifications/test":
            self.handle_notification_test(payload, user)
            return
        if parsed.path == "/api/notifications":
            self.handle_notifications(payload, user)
            return
        product = str(payload.get("product") or "all")
        mode = str(payload.get("mode") or "silent")
        if mode not in {"status", "silent", "silent-poll", "doctor"}:
            json_response(self, {"error": "不支持的模式"}, HTTPStatus.BAD_REQUEST)
            return
        # account 非空 = 「只跑这个账号」：必须落到具体产品，
        # 且账号要真实存在于该产品的凭据目录 —— 不让请求体决定文件路径。
        account = str(payload.get("account") or "").strip()
        if account and product.lower() == "all":
            json_response(self, {"error": "指定账号时必须同时指定 product"}, HTTPStatus.BAD_REQUEST)
            return
        products = ["WorkBuddy", "TRAE"] if product.lower() == "all" else [product]
        started = []
        try:
            for item in products:
                normalized = "TRAE" if item.lower() == "trae" else "WorkBuddy" if item.lower() == "workbuddy" else item
                if account:
                    cred, _disabled = credential_paths(normalized, account, user)
                    if not cred.is_file():
                        json_response(self, {"error": "账号不存在或凭据缺失：%s" % account},
                                      HTTPStatus.BAD_REQUEST)
                        return
                started.append(start_task(normalized, mode, reason="manual",
                                          account=account or None, user=user))
        except Exception as exc:
            json_response(self, {"error": str(exc), "started": started}, HTTPStatus.BAD_REQUEST)
            return
        json_response(self, {"started": started})

    def handle_login(self) -> None:
        try:
            payload = self.read_json_body(max_bytes=64 * 1024)
            username = str(payload.get("username") or "")
            password = str(payload.get("password") or "")
        except Exception as exc:
            json_response(self, {"error": str(exc), "authenticated": False}, HTTPStatus.BAD_REQUEST)
            return
        user, message, status = authenticate_user(username, password)
        if not user:
            json_response(self, {"error": message or "账号或密码不正确", "authenticated": False}, status)
            return
        json_response(
            self,
            {
                "authenticated": True,
                "username": user.get("username") or "",
                "role": user.get("role") or "user",
                "status": user.get("status") or "approved",
            },
            headers={"Set-Cookie": session_cookie(user)},
        )

    def handle_register(self) -> None:
        try:
            payload = self.read_json_body(max_bytes=64 * 1024)
            username = str(payload.get("username") or "")
            password = str(payload.get("password") or "")
            confirm = str(payload.get("confirm") or payload.get("password_confirm") or "")
            if confirm and not hmac.compare_digest(password, confirm):
                raise ValueError("两次输入的密码不一致")
            user = create_pending_user(
                username,
                password,
                fp_hash=str(payload.get("fingerprint") or "").strip(),
            )
        except Exception as exc:
            json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        # 提醒管理员去审核；安静失败，不阻塞注册结果返回。
        try:
            notify_admins_approval(user.get("username") or username)
        except Exception:
            pass
        json_response(self, {
            "registered": True,
            "user": user,
            "message": "注册已提交，请等待管理员审核",
        })

    def handle_upload(self, body: bytes, user: Dict[str, Any]) -> None:
        try:
            fields, files = parse_multipart(body, self.headers.get("Content-Type") or "")
            product = "TRAE" if fields.get("product", "").lower() == "trae" else "WorkBuddy"
            if not files:
                raise ValueError("没有收到凭据文件")
            uploaded = [
                upload_credential(product, file["filename"], file["content"], fields.get("account_name") or None, user)
                for file in files
            ]
        except Exception as exc:
            json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        json_response(self, {"uploaded": uploaded, "status": self.dashboard_payload()})

    def handle_toggle(self, payload: Dict[str, Any], user: Dict[str, Any]) -> None:
        try:
            product = "TRAE" if str(payload.get("product", "")).lower() == "trae" else "WorkBuddy"
            result = set_account_enabled(product, str(payload.get("name") or ""), bool(payload.get("enabled")), user)
        except Exception as exc:
            json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        json_response(self, {"account": result, "status": self.dashboard_payload()})

    def handle_archive(self, payload: Dict[str, Any], user: Dict[str, Any]) -> None:
        try:
            product = "TRAE" if str(payload.get("product", "")).lower() == "trae" else "WorkBuddy"
            result = archive_account(product, str(payload.get("name") or ""), user)
        except Exception as exc:
            json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        json_response(self, {"account": result, "status": self.dashboard_payload()})

    def handle_restore(self, payload: Dict[str, Any], user: Dict[str, Any]) -> None:
        # 恢复必须认「产品 + 账号名」两个字段；只给名字会在两个产品同名时搬错。
        try:
            product = "TRAE" if str(payload.get("product", "")).lower() == "trae" else "WorkBuddy"
            result = restore_account(product, str(payload.get("name") or ""), user)
        except Exception as exc:
            json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        json_response(self, {"account": result, "status": self.dashboard_payload()})

    def handle_delete(self, payload: Dict[str, Any], user: Dict[str, Any]) -> None:
        try:
            product = "TRAE" if str(payload.get("product", "")).lower() == "trae" else "WorkBuddy"
            result = delete_account(product, str(payload.get("name") or ""), user)
        except Exception as exc:
            json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        json_response(self, {"account": result, "status": self.dashboard_payload()})

    def handle_schedules(self, payload: Dict[str, Any], user: Dict[str, Any]) -> None:
        try:
            product = "TRAE" if str(payload.get("product", "")).lower() == "trae" else "WorkBuddy"
            config = load_user_config(user)
            schedules = config.setdefault("schedules", {})
            current = schedules.setdefault(product, {})
            daily_time = parse_clock(payload.get("daily_time"))
            if not daily_time:
                raise ValueError("日签时间格式应为 HH:MM")
            poll_times = normalize_times(payload.get("poll_times"))
            if not poll_times:
                raise ValueError("至少需要一个轮询时间")
            # 前端不一定会带模式字段 —— 没带就保留原值，别把它重置成默认。
            current.update({
                "enabled": bool(payload.get("enabled")),
                "daily_time": daily_time,
                "daily_mode": str(payload.get("daily_mode") or current.get("daily_mode") or "silent"),
                "poll_enabled": bool(payload.get("poll_enabled")),
                "poll_times": poll_times,
                "poll_mode": str(payload.get("poll_mode") or current.get("poll_mode") or "silent-poll"),
            })
            save_user_config(user, config)
        except Exception as exc:
            json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        json_response(self, {"schedules": schedule_payload(config), "status": self.dashboard_payload()})

    def handle_notifications(self, payload: Dict[str, Any], user: Dict[str, Any]) -> None:
        try:
            product = "TRAE" if str(payload.get("product", "")).lower() == "trae" else "WorkBuddy"
            config = load_user_config(user)
            apply_notification_payload(config, product, payload)
            save_user_config(user, config)
        except Exception as exc:
            json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        json_response(self, {"notifications": notification_config(config, user), "status": self.dashboard_payload()})

    def handle_notification_test(self, payload: Dict[str, Any], user: Dict[str, Any]) -> None:
        try:
            product = "TRAE" if str(payload.get("product", "")).lower() == "trae" else "WorkBuddy"
            config = load_user_config(user)
            values = apply_notification_payload(config, product, payload)
            if not values.get("enabled"):
                raise ValueError("请先启用通知，再发送测试消息")
            if (values.get("channel") or "none") == "none":
                raise ValueError("请选择一个通知渠道")
            save_user_config(user, config)
            result = run_notify_test(product, config, user)
        except subprocess.TimeoutExpired:
            json_response(self, {"error": "测试消息发送超时"}, HTTPStatus.BAD_GATEWAY)
            return
        except Exception as exc:
            json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if not result.get("ok"):
            json_response(self, {"error": result.get("output") or "测试消息发送失败", "test": result},
                          HTTPStatus.BAD_GATEWAY)
            return
        json_response(self, {
            "test": result,
            "notifications": notification_config(config, user),
            "status": self.dashboard_payload(),
        })

    def read_log_tail(self, rel_path: str, lines: int) -> Dict[str, Any]:
        """按相对路径读日志尾部（执行记录的「查看详情」用它）。

        路径必须落在项目根目录内 —— 否则一个构造过的 path 就能读到系统文件。
        """
        safe_rel = unquote(str(rel_path or "")).replace("\\", "/").lstrip("/")
        if not safe_rel:
            return {"exists": False, "error": "缺少 path 参数"}
        try:
            root = PROJECT_ROOT.resolve()
            target = (PROJECT_ROOT / safe_rel).resolve()
            target.relative_to(root)
        except ValueError:
            return {"exists": False, "error": "路径越界，已拒绝"}
        except Exception as exc:
            return {"exists": False, "error": str(exc)}
        if not target.is_file():
            return {"exists": False, "path": safe_rel, "error": "日志文件不存在"}
        text = tail_text(target, limit_bytes=512 * 1024)
        rows = text.splitlines()
        keep = max(1, min(int(lines or 120), 2000))
        try:
            size = target.stat().st_size
        except Exception:
            size = None
        return {
            "exists": True,
            "path": safe_rel,
            "size": size,
            "total_lines": len(rows),
            "lines": rows[-keep:],
        }

    def serve_static(self, request_path: str) -> None:
        path = unquote(request_path.split("?", 1)[0])
        if path in {"", "/"}:
            path = "/index.html"
        target = (STATIC_DIR / path.lstrip("/")).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            json_response(self, {"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            return
        if not target.is_file():
            json_response(self, {"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        content = target.read_bytes()
        mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.suffix == ".js":
            mime = "text/javascript"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime + ("; charset=utf-8" if mime.startswith("text/") or mime in {"text/javascript", "application/json"} else ""))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def main() -> int:
    parser = argparse.ArgumentParser(description="签到工作台静态服务")
    parser.add_argument("--host", default=os.environ.get("DASHBOARD_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", "8000")))
    parser.add_argument("--check", action="store_true", help="只输出一次状态 JSON，不启动服务")
    args = parser.parse_args()

    config = ensure_runtime_config()
    init_database(config)

    if args.check:
        print(json.dumps(build_dashboard(), ensure_ascii=False, indent=2, default=str))
        return 0

    # 启动时对齐一次通知配置：面板上关掉的渠道，不该因为脚本里残留旧配置继续发消息。
    sync_notify_configs(config)

    httpd = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    scheduler = threading.Thread(target=scheduler_loop, daemon=True)
    scheduler.start()
    purge = threading.Thread(target=purge_loop, daemon=True)
    purge.start()
    auth = config.get("auth") or {}
    print("签到工作台已启动：http://%s:%s" % (args.host, args.port))
    print("工作台根目录：%s" % PROJECT_ROOT)
    print("内置脚本目录：%s" % APPS_DIR)
    print("默认管理员：%s（数据库：%s）" % (auth.get("username") or "admin", DB_PATH))
    if auth.get("password") == DEFAULT_CONFIG["auth"]["password"]:
        print("安全提醒：当前仍是默认密码，请部署前修改 workbench.json")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        SCHEDULER_STOP.set()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
