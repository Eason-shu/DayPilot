#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 EasonShu
"""WorkBuddy 签到通知器 —— 把签到结果按配置投递到外部渠道。

输入是签到脚本写下的日志（单账号日志与 multi.log 汇总），输出是一条消息。
本脚本不参与签到，只做「读日志 → 判等级 → 渲染 → 投递 → 记状态」，
因此可以在签到结束后单独重跑，也可以离线预览。

子命令：
  check     扫描新增日志，按规则决定推不推、推哪几条（定时任务用这个）
  startup   发送「服务已启动」提醒
  notice    发送业务通知（如新用户注册），标题由 --title 指定
  test      发送一条测试消息，用于验证渠道配置
  status    打印当前渠道配置与投递状态

渠道与凭据来源（优先级从高到低）：命令行参数 → 环境变量 → config/notify.json。
环境变量统一用 WB_NOTIFY_ 前缀：KEY / URL / CHANNEL / ON / MULTI_LOG / STATE。
支持 Server酱、飞书、企业微信、Bark 与通用 Webhook，渠道差异见 references/notification.md。

关键设计：投递进度记在 notify_state.json —— 每个日志文件记一条「已消费字节
偏移」，只读偏移之后的新增内容。因此本脚本可以随意重跑而不会重复推送；文件被
清空或轮转（当前大小 < 偏移）时偏移归零重读，新增账号的日志从 0 开始读，已有
账号继续沿用旧偏移（否则会把当天历史记录重新汇总一次）。
"""

import argparse
import datetime
import glob
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
TIMEOUT = 15
LINE_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+(\{.*\})\s*$")

# 结果分类。ERROR_RESULTS 与 signin.py emit() 里 needs_attention 的集合保持一致。
ERROR_RESULTS = {
    "ERROR", "UNKNOWN", "NETWORK", "TIMEOUT", "NO_AUTH", "NO_SESSION",
    "AUTH_ERROR", "AUTH_REJECTED", "FORBIDDEN",
}
SUCCESS_RESULTS = {"CLAIMED", "SUCCESS"}
# 这些结果本身不代表"有事发生"，不主动推送（除非 WB_NOTIFY_ON=all）
QUIET_RESULTS = {"ALREADY", "INACTIVE", "AUTH_READY", "GROWTH"}

LEVEL_TEXT = {
    "success": "签到成功",
    "already": "今日已签",
    "error": "签到异常",
    "gain": "成长中心有收获",
    "info": "运行记录",
    "startup": "服务已启动",
    "test": "测试消息",
    "notice": "通知",
    "digest": "本轮签到汇总",
}
LEVEL_COLOR = {
    "success": "green", "already": "blue", "error": "red", "gain": "turquoise",
    "info": "grey", "startup": "blue", "test": "blue", "notice": "blue",
    "digest": "green",
}
# 状态字形：微信 / iOS 通知栏里唯一能一眼区分结果的东西。WB_NOTIFY_GLYPH=0 可关掉。
LEVEL_GLYPH = {
    "success": "✅", "already": "☑️", "error": "⚠️", "gain": "🎁",
    "info": "ℹ️", "startup": "🚀", "test": "🧪", "notice": "📢", "digest": "✅",
}
MARK_OK = "✅"
MARK_BAD = "⚠️"

# 结果码 → 中文短标签。多账号汇总里用它替换裸代码，异常时保留原码便于排查。
RESULT_LABEL = {
    "CLAIMED": "已领取", "SUCCESS": "成功", "ALREADY": "今日已签",
    "INACTIVE": "活动未开始", "AUTH_READY": "待领取", "GROWTH": "成长中心",
    "DIGEST": "本轮汇总", "STARTUP": "启动", "TEST": "测试消息", "NOTICE": "通知",
    "ERROR": "执行出错", "UNKNOWN": "未知结果", "NETWORK": "网络异常",
    "TIMEOUT": "请求超时", "NO_AUTH": "未找到凭据", "NO_SESSION": "凭据无效",
    "AUTH_ERROR": "认证失败", "AUTH_REJECTED": "认证被拒", "FORBIDDEN": "无权限",
}

# 出错时给一句能直接照做的建议 —— 通知里最缺的就是这个。
ERROR_HINT = {
    "AUTH_REJECTED": "令牌已过期，回本机重跑 export_session.py，再把新的 session.json 传上去",
    "AUTH_ERROR": "令牌已过期，回本机重跑 export_session.py，再把新的 session.json 传上去",
    "NO_AUTH": "凭据文件不见了，重新上传 session.json（或 sessions/ 目录）",
    "NO_SESSION": "凭据内容不合法，回本机重新导出一次",
    "FORBIDDEN": "账号无签到权限，或活动已下线 —— 去客户端里确认一下",
    "NETWORK": "服务器出网异常，先在服务器上 curl -sI https://copilot.tencent.com 试一下",
    "TIMEOUT": "请求超时，多半是出网抖动；若连续几轮都超时再排查",
    "ERROR": "看日志定位：tail -n 20 /opt/workbuddy-signin/signin.log",
}

# 多账号汇总日志（multi.log）里，只有这些模式才是"真正跑过签到"的轮次
MULTI_MUTATING_MODES = {
    "silent", "auto", "all", "growth", "claim", "silent-poll", "silent-growth",
}


# ---- 配置 -------------------------------------------------------------------

# 配置文件键名兼容：短名（config/notify.json 里推荐写这种）与长名等价
_CFG_KEY_ALIASES = {
    "channel": "notify_channel",
    "url": "notify_url",
    "webhook": "notify_url",
    "key": "notify_key",
    "sendkey": "notify_key",
    "send_key": "notify_key",
    "sckey": "notify_key",
    "on": "notify_on",
    "mode": "notify_on",
    "group": "notify_group",
    "name": "notify_group",
}


def _load_cfg_file():
    """读通知配置。后遍历的覆盖先遍历的 —— 即 `config/notify.json` 优先于同目录 `notify.json`。

    最推荐把配置放 `config/notify.json`：数据与代码分离，改密钥不用动脚本、不用重装。
    键名短写（channel / key / on / group）或长写（notify_channel / …）都认。
    """
    merged = {}
    for name in ("notify.json", "notify.config.json",
                 "config/notify.json", "config/notify.config.json"):
        p = os.path.join(HERE, *name.split("/"))
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for k, v in data.items():
            if v in (None, ""):
                continue
            merged[_CFG_KEY_ALIASES.get(str(k).strip().lower(), k)] = v
    return merged


_CFG = _load_cfg_file()

# 环境变量别名：让「只给一个 SendKey」这种最省事的写法也能直接生效。
# 顺序即优先级，第一个非空值胜出；WB_ 前缀始终最高。
ENV_ALIASES = {
    "notify_key": ("WB_NOTIFY_KEY", "SENDKEY", "SEND_KEY", "SERVERCHAN_KEY",
                   "SERVERCHAN_SENDKEY", "SCKEY"),
    "notify_url": ("WB_NOTIFY_URL", "NOTIFY_URL", "WEBHOOK_URL"),
    "notify_channel": ("WB_NOTIFY_CHANNEL", "NOTIFY_CHANNEL"),
    "notify_on": ("WB_NOTIFY_ON", "NOTIFY_ON"),
}


def cfg(key, default=None):
    """环境变量优先（WB_ 前缀 + 常见别名），其次 notify.json，最后默认值。"""
    for env_key in ENV_ALIASES.get(key, ("WB_" + key.upper(),)):
        val = os.environ.get(env_key)
        if val:
            return val
    if key in _CFG and _CFG[key] not in (None, ""):
        return _CFG[key]
    # 兼容小写 / 带下划线的写法
    lower = key.lower()
    if lower in _CFG and _CFG[lower] not in (None, ""):
        return _CFG[lower]
    return default


def resolve_log_path(explicit=None):
    if explicit:
        return os.path.abspath(explicit)
    for env in ("WB_NOTIFY_LOG", "WORKBUDDY_SIGNIN_LOG"):
        if os.environ.get(env):
            return os.path.abspath(os.environ[env])
    return os.path.join(HERE, "signin.log")


def resolve_log_paths(args):
    """返回要扫描的签到日志列表（去重、保序）。

    --log-dir 优先（多账号：目录下所有 *.log，排除 *.renew.log）；
    其次显式 --file（可重复）；最后回落到单个默认日志。
    """
    paths = []
    if getattr(args, "log_dir", None):
        base = os.path.abspath(args.log_dir)
        for p in sorted(glob.glob(os.path.join(base, "*.log"))):
            name = os.path.basename(p)
            if name.endswith(".renew.log") or name.startswith("."):
                continue
            paths.append(os.path.abspath(p))
        return paths
    for f in (getattr(args, "file", None) or []):
        if f:
            paths.append(os.path.abspath(f))
    if paths:
        return paths
    return [resolve_log_path(None)]


def resolve_multi_log(explicit=None):
    if explicit:
        return os.path.abspath(explicit)
    if os.environ.get("WB_NOTIFY_MULTI_LOG"):
        return os.path.abspath(os.environ["WB_NOTIFY_MULTI_LOG"])
    v = cfg("notify_multi_log")
    return os.path.abspath(v) if v else None


def resolve_state_path(explicit=None, log_path=None):
    if explicit:
        return os.path.abspath(explicit)
    if os.environ.get("WB_NOTIFY_STATE"):
        return os.path.abspath(os.environ["WB_NOTIFY_STATE"])
    base = os.path.dirname(log_path or resolve_log_path())
    return os.path.join(base, "notify_state.json")


def resolve_channel():
    """渠道解析：显式配置优先；**没配但给了密钥/地址就按最省事的默认推断**。

    —— 只填一个 Server酱 SendKey 就等于选了 serverchan（推荐用法）；
    只填一个 webhook 地址就等于通用 webhook。显式写 none 才是关闭。
    """
    ch = (cfg("notify_channel", "") or "").strip().lower()
    if ch in ("off", "none", "no"):
        return "none"
    if ch:
        return ch
    if (cfg("notify_key") or "").strip():
        return "serverchan"
    if (cfg("notify_url") or "").strip():
        return "webhook"
    return "none"


# ---- 状态 -------------------------------------------------------------------

def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_state(path, state):
    try:
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


# ---- 日志解析 ---------------------------------------------------------------

def parse_line(line):
    m = LINE_RE.match(line.strip())
    if not m:
        return None
    ts, payload = m.group(1), m.group(2)
    try:
        rec = json.loads(payload)
    except Exception:
        return None
    if not isinstance(rec, dict):
        return None
    rec["_ts"] = ts
    return rec


def read_new_records(log_path, offset, max_age_hours=None, now=None):
    """从 offset 读到文件末尾，返回 (记录列表, 新 offset)。

    文件被清空/轮转（当前大小 < offset）时自动重置 offset。
    """
    if not os.path.exists(log_path):
        return [], 0
    size = os.path.getsize(log_path)
    if size < offset:
        offset = 0
    try:
        with open(log_path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
            new_offset = f.tell()
    except Exception:
        return [], offset

    text = chunk.decode("utf-8", "replace")
    records = []
    for raw in text.splitlines():
        rec = parse_line(raw)
        if rec:
            records.append(rec)

    if max_age_hours is not None and records:
        now = now or datetime.datetime.now()
        cutoff = now - datetime.timedelta(hours=max_age_hours)
        kept = []
        for rec in records:
            try:
                t = datetime.datetime.strptime(rec["_ts"], "%Y-%m-%d %H:%M:%S")
            except Exception:
                kept.append(rec)
                continue
            if t >= cutoff:
                kept.append(rec)
        records = kept
    return records, new_offset


def read_multi_log(path, offset, max_age_hours=None, now=None):
    """读 multi.log 的自 offset 起新增行，返回 (汇总条目列表, 新 offset)。

    multi.log 是纯 JSON 行（无 [时间] 前缀），格式：
        {"ts": "...", "mode": "silent", "accounts": 3, "ok": 2,
         "failed": ["carol"], "needs_attention": true, ...}
    """
    if not path or not os.path.exists(path):
        return [], 0
    size = os.path.getsize(path)
    if size < offset:
        offset = 0
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
            new_offset = f.tell()
    except Exception:
        return [], offset

    items = []
    for raw in chunk.decode("utf-8", "replace").splitlines():
        raw = raw.strip()
        if not raw or not raw.startswith("{"):
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("mode") not in MULTI_MUTATING_MODES:
            continue
        items.append(obj)

    if max_age_hours is not None and items:
        now = now or datetime.datetime.now()
        cutoff = now - datetime.timedelta(hours=max_age_hours)
        kept = []
        for it in items:
            try:
                t = datetime.datetime.strptime(it.get("ts") or "", "%Y-%m-%d %H:%M:%S")
            except Exception:
                kept.append(it)
                continue
            if t >= cutoff:
                kept.append(it)
        items = kept
    return items, new_offset


# ---- 分类 -------------------------------------------------------------------

def classify(rec):
    """返回 (level, 是否值得推送的候选)。"""
    result = rec.get("result") or ""
    if rec.get("needs_attention") or result in ERROR_RESULTS:
        return "error", True
    if result == "DIGEST":
        return "digest", True
    if result == "NOTICE":
        # 工作台发来的通用业务通知（新用户注册等），与签到链路无关，永远推送。
        return "notice", True
    if result == "STARTUP":
        return "startup", True
    if result == "TEST":
        return "test", True
    if result in SUCCESS_RESULTS:
        return "success", True
    if result == "GROWTH":
        return "gain", True
    if result == "ALREADY":
        # 已签本身不推；但如果成长中心这次有动作（非 idle），signin.py 才会写这行，
        # 说明有实际收益，按"收获"处理。
        growth = rec.get("growth") or ""
        if growth and "旅行中" not in growth:
            return "gain", True
        return "already", False
    if result in QUIET_RESULTS:
        return "info", False
    # 未知结果：保守当 info，看模式决定
    return "info", True


def pick_records(records, mode):
    """按模式筛选本轮要推送的记录。"""
    if not records:
        return []
    if mode == "all":
        picked = [r for r in records if classify(r)[0] != "info" or r.get("report")]
    elif mode == "error":
        picked = [r for r in records if classify(r)[0] == "error"]
    else:  # daily
        picked = [r for r in records if classify(r)[1]]
    return picked


def make_digest(records, failures=None):
    """把多账号的多条记录合成一条"本轮汇总"伪记录。

    records : 本轮新读到的账号记录（每条应带 _account）
    failures: multi.log 里报告的失败账号名列表（可能为空/None）
    """
    rows = []
    seen = set()
    latest_by_account = {}
    for rec in records:
        name = rec.get("_account") or "-"
        old = latest_by_account.get(name)
        if old is None or (rec.get("_ts") or "") >= (old.get("_ts") or ""):
            latest_by_account[name] = rec

    for rec in latest_by_account.values():
        name = rec.get("_account") or "-"
        seen.add(name)
        level = classify(rec)[0]
        credit = rec_credit(rec)
        rows.append({
            "name": name,
            "result": rec.get("result") or "-",
            "note": rec.get("note") or "",
            "ok": level not in ("error",),
            "level": level,
            "credit": credit if isinstance(credit, (int, float)) else None,
            "streak_days": rec.get("streak_days"),
        })
    for name in (failures or []):
        if name in seen:
            continue
        rows.append({"name": name, "result": "-", "note": "无日志（超时或崩溃）",
                     "ok": False, "level": "error", "credit": None, "streak_days": None})

    if not rows:
        return None

    bad = [r for r in rows if not r["ok"]]
    credits = sum(r["credit"] for r in rows if isinstance(r.get("credit"), (int, float)))
    if bad:
        report = "%d 个账号 · %d 成功 · %d 异常" % (len(rows), len(rows) - len(bad), len(bad))
    else:
        report = "%d 个账号全部签到成功" % len(rows)
    if credits:
        report += "，共 +%g 积分" % credits

    ts = ""
    for rec in records:
        t = rec.get("_ts") or ""
        if t > ts:
            ts = t
    if not ts:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return {
        "_ts": ts, "result": "DIGEST", "report": report, "digest": rows,
        "credits_total": credits or None,
        "needs_attention": bool(bad),
    }


# ---- 渲染 -------------------------------------------------------------------

def _title_prefix():
    return cfg("notify_group", "WorkBuddy") or "WorkBuddy"


def _short_date(rec):
    return (rec.get("_ts") or "")[5:10]  # MM-DD


def _short_dt(rec):
    ts = rec.get("_ts") or ""
    return ts[5:16] if len(ts) >= 16 else ts   # MM-DD HH:MM


def _clip(text, limit):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _glyphs_enabled():
    """WB_NOTIFY_GLYPH=0 可关掉状态字形，退回纯文字（排版不变）。"""
    val = (cfg("notify_glyph", "1") or "1").strip().lower()
    return val not in ("0", "off", "no", "false", "none")


def _glyph(level):
    return LEVEL_GLYPH.get(level, "") if _glyphs_enabled() else ""


def _mark(ok):
    if not _glyphs_enabled():
        return "OK" if ok else "FAIL"
    return MARK_OK if ok else MARK_BAD


# 只有"确实发生了领取"的结果码，才允许把 today_credit 当收益。
GAIN_RESULTS = {"CLAIMED", "SUCCESS", "GROWTH"}


def rec_credit(rec):
    """本次实际获得的积分；没有真的发生领取就返回 None。

    `credit` 是签到脚本明确给出的"本次获得"，最可信。`today_credit` 只是账户
    额度，仅当结果码表明这次真的领了（CLAIMED / SUCCESS）才采信它。
    """
    explicit = rec.get("credit")
    if isinstance(explicit, (int, float)) and explicit:
        return explicit
    if (rec.get("result") or "") in GAIN_RESULTS:
        fallback = rec.get("today_credit")
        if isinstance(fallback, (int, float)) and fallback:
            return fallback
    return None


def _gain_bits(rec):
    """收益片段，例如 ["+100", "连签 12 天"]。"""
    bits = []
    credit = rec_credit(rec)
    if credit:
        bits.append("+%g" % credit)
    streak = rec.get("streak_days")
    if isinstance(streak, (int, float)) and streak:
        bits.append("连签 %g 天" % streak)
    return bits


def build_headline(rec):
    """标题里的「事实」部分：一眼看懂，优先讲结果和收益，而不是日期。

    微信 / iOS 通知栏只显示标题这一行，所以日期让位给「+100」「连签 12 天」
    这类真正会变的信息 —— 日期反正每天都会变，正文页脚里有。
    汇总和启动提醒没有更短的来源，直接用 report。
    """
    level = classify(rec)[0]
    if level == "startup":
        return "服务已启动"
    if level == "notice":
        # 业务通知的标题由调用方给（--title），没给才回落到 report。
        return (rec.get("notice_title") or rec.get("report") or "通知").strip() or "通知"
    if rec.get("digest"):
        return rec.get("report") or LEVEL_TEXT.get(level, "本轮签到汇总")

    result = rec.get("result") or ""
    if level == "error":
        if result in RESULT_LABEL:
            head = "签到异常 · %s（%s）" % (result, RESULT_LABEL[result])
        else:
            head = "签到异常" + ((" · " + result) if result else "")
        note = (rec.get("note") or "").strip()
        return head + ((" · " + note) if note and note != "需关注" else "")

    if level == "success":
        head = "签到成功"
    elif level == "already":
        # 已签到 = 这一轮没动过账户（领取被跳过）。只写"今日已签"会让人以为
        # 刚又进账一份；收益数字也已经被 rec_credit() 挡掉，不会再出现 +100。
        head = ("今日已签 · 跳过领取" if "跳过" in (rec.get("note") or "")
                else "今日已签")
    elif level == "gain":
        head = "成长中心有收获"
    else:
        head = LEVEL_TEXT.get(level, result or "运行记录")
    bits = _gain_bits(rec)
    return "%s · %s" % (head, " · ".join(bits)) if bits else head


def build_title(level, rec):
    parts = [_title_prefix(), _glyph(level), build_headline(rec)]
    return _clip(" ".join([p for p in parts if p]), 48)


def _footer(rec):
    return "%s 自动签到 · %s" % (_title_prefix(), _short_dt(rec) or "刚刚")


def _footer_short(rec):
    """飞书卡片页脚用：卡头已经写了来源名，页脚不必再重复一遍。"""
    return "自动签到 · %s" % (_short_dt(rec) or "刚刚")


def _body_head(rec):
    """正文首行摘要；与标题文字完全相同时返回 None。

    飞书卡片和 Bark 的标题与正文是上下相邻显示的，同一个句子出现两遍
    是这类通知最常见的丑。Server酱 详情页不受影响 —— 标题在推送里、
    正文在网页里，正文必须自带上下文。
    """
    head = build_summary(rec) or build_headline(rec)
    return None if head == build_headline(rec) else head


def build_summary(rec):
    """正文首行（人话摘要）。有 report 就用它，否则回落到标题主语。"""
    if rec.get("report"):
        return rec["report"]
    return build_headline(rec)


def _hints_of(rows):
    """从失败行里提取去重后的处理建议。"""
    hints = []
    for row in rows:
        if row.get("ok"):
            continue
        hint = ERROR_HINT.get(row.get("result") or "")
        if hint and hint not in hints:
            hints.append(hint)
    return hints


def build_detail_lines(rec):
    """明细行（markdown），Markdown 正文 / 飞书卡片 / 纯文本三处共用。

    - 多账号汇总：逐账号一行，状态用字形打头，结果码翻成中文；
    - 单账号：连签 / 累计 / 成长中心收益；
    - 只要出错，末尾补一条能直接照做的「处理」建议。
    """
    lines = []
    rows = rec.get("digest") or []

    for row in rows:
        name = row.get("name") or "-"
        raw = row.get("result") or "-"
        ok = bool(row.get("ok"))
        note = (row.get("note") or "").strip()
        if raw == "-":
            # 连签到日志都没写出来（超时 / 崩溃），note 本身就是原因，别重复拼
            label = note or "无日志"
            note = ""
        else:
            label = RESULT_LABEL.get(raw, raw)
            if not ok and label != raw:
                label = "%s（%s）" % (raw, label)   # 保留原码，方便对着日志排查
            if note == "需关注":
                note = ""
        tail = []
        credit = row.get("credit")
        if isinstance(credit, (int, float)) and credit:
            tail.append("+%g" % credit)
        if note:
            tail.append(note)
        line = "- %s **%s** · %s" % (_mark(ok), name, label)
        if tail:
            line += "（%s）" % " · ".join(tail)
        lines.append(line)

    if rows:
        for hint in _hints_of(rows):
            lines.append("**处理**：%s" % hint)
        return lines

    # 启动提醒等自定义补充行
    for extra in (rec.get("extra") or []):
        if extra:
            lines.append("- %s" % extra)

    bits = []
    streak = rec.get("streak_days")
    total = rec.get("total_credits")
    if isinstance(streak, (int, float)) and streak:
        bits.append("连续签到 **%g** 天" % streak)
    if isinstance(total, (int, float)) and total:
        bits.append("累计 **%g** 积分" % total)
    if bits and not rec.get("report"):
        lines.append("- " + " · ".join(bits))

    gained = rec.get("credits_gained")
    if isinstance(gained, (int, float)) and gained:
        lines.append("- 成长中心 **+%g** 积分" % gained)

    growth = rec.get("growth")
    if growth:
        lines.append("- %s" % growth)

    if classify(rec)[0] == "already":
        # "今日已签"如果正文只留标题那五个字就太空了 —— note 才是这条记录的
        # 全部内容（为什么没领、跳过的是什么），必须落到明细里。
        note = (rec.get("note") or "").strip()
        if note and note != "需关注":
            lines.append("- %s" % note)

    if classify(rec)[0] == "error":
        hint = ERROR_HINT.get(rec.get("result") or "")
        if hint:
            lines.append("**处理**：%s" % hint)
    return lines


def _plain_lines(rec):
    """把 markdown 明细行去掉加粗语法，得到纯文本行。"""
    return [re.sub(r"\*\*", "", line) for line in build_detail_lines(rec)]


def render_markdown(rec, footer_style="md"):
    """Markdown 版正文：Server酱详情页 / 企业微信 markdown 共用。

    footer_style：md = 分隔线 + 普通页脚；wecom = 企业微信的灰色小字。
    """
    level = classify(rec)[0]
    glyph = _glyph(level)
    head = build_summary(rec) or build_headline(rec)
    body = "**%s**" % (("%s %s" % (glyph, head)) if glyph else head)
    detail = build_detail_lines(rec)
    # 「处理」建议渲染成 Markdown 引用块：Server酱与网页端会把 `> **处理**…`
    # 画成左边竖线 + 浅底，一眼能扫到"要我做什么"，而不是混在账号明细里。
    detail = ["> %s" % ln if ln.startswith("**处理**") else ln
              for ln in detail]
    if detail:
        body += "\n\n" + "\n".join(detail)
    foot = _footer(rec)
    if footer_style == "wecom":
        body += "\n" + '<font color="comment">%s</font>' % foot
    else:
        body += "\n\n---\n\n**%s**" % foot
    return body


def render_text(rec):
    """纯文本版：Bark 之外的兜底、webhook 的 text 字段、终端调试。"""
    level = classify(rec)[0]
    lines = [build_title(level, rec)]
    head = _body_head(rec)
    if head:
        lines.append(head)
    lines.extend(_plain_lines(rec))
    lines.append(_footer(rec))
    return "\n".join([ln for ln in lines if ln])


def build_bark_body(rec):
    """Bark / 桌面通知的正文：摘要 + 最多 4 条明细，控制在通知可读长度内。"""
    lines = [_body_head(rec)]
    lines.extend(_plain_lines(rec)[:4])
    lines.append(_footer(rec))
    return "\n".join([ln for ln in lines if ln])


def render_feishu(rec):
    """飞书交互卡片：标题带状态字形，明细合并进一个 div，卡片更短更好扫。"""
    level = classify(rec)[0]
    glyph = _glyph(level)
    head = _body_head(rec)
    elements = []
    if head:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                         "content": "**%s**" % (("%s %s" % (glyph, head)) if glyph else head)}})
    detail = build_detail_lines(rec)
    if detail:
        if elements:
            elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(detail)}})
    elements.append({"tag": "note", "elements": [
        {"tag": "plain_text", "content": _footer_short(rec)}
    ]})
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": LEVEL_COLOR.get(level, "grey"),
                "title": {"tag": "plain_text", "content": build_title(level, rec)},
            },
            "elements": elements,
        },
    }


def render_wecom(rec):
    return {"msgtype": "markdown",
            "markdown": {"content": render_markdown(rec, footer_style="wecom")}}


# ---- 发送 -------------------------------------------------------------------

def _http_post(url, payload, form=False):
    if form:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        ctype = "application/x-www-form-urlencoded"
    else:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ctype = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": ctype, "User-Agent": "wb-notify/1.0"})
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, body[:500]
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode("utf-8", "replace")[:500] if hasattr(e, "read") else str(e))
    except Exception as e:
        return 0, "%s: %s" % (type(e).__name__, e)


def _http_get(url):
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "wb-notify/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")[:500]
    except Exception as e:
        return 0, "%s: %s" % (type(e).__name__, e)


# Server酱有两代产品，端点不同、SendKey 不通用 —— 必须按前缀路由。用错端点的
# 特征是接口返回「成功」但消息根本不投递，成因与排查见 references/notification.md。
SC_TURBO_URL = "https://sctapi.ftqq.com/%s.send"          # Server酱 Turbo，SCT 开头，可推微信
SC3_URL = "https://%s.push.ft07.com/send/%s.send"          # Server酱³，sctp 开头，只推 SC3 App
SC3_RE = re.compile(r"^sctp(\d+)t", re.I)


def serverchan_url(key):
    """把用户填的东西归一化成 Server酱 推送地址，按 SendKey 前缀自动选端点。

    - `SCT…`  → Server酱 Turbo：https://sctapi.ftqq.com/<key>.send （微信/企微/钉钉群…）
    - `sctp…` → Server酱³      ：https://<uid>.push.ft07.com/send/<key>.send （只有 App）
      其中 uid 取自 `sctp` 与 `t` 之间的数字：sctp<uid>txxx → <uid>

    也接受带 `.send` 后缀或整条地址的写法；notify.json 的 notify_url 可显式覆盖
    （含 {key} 占位符会被替换），方便走自建中转。
    """
    raw = (key or "").strip().strip("'\"")
    base = (cfg("notify_url") or "").strip()
    if base:
        if "{key}" in base:
            return base.replace("{key}", urllib.parse.quote(raw, safe=""))
        return base
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    if raw.lower().endswith(".send"):
        raw = raw[:-5]
    m = SC3_RE.match(raw)
    if m:
        return SC3_URL % (m.group(1), urllib.parse.quote(raw, safe=""))
    return SC_TURBO_URL % urllib.parse.quote(raw, safe="")


def _plain(s):
    """去掉 Server酱 错误信息里夹带的 HTML 标签，压成一行。"""
    s = re.sub(r"<[^>]+>", " ", str(s or ""))
    return re.sub(r"\s+", " ", s).strip()


def serverchan_result(code, body):
    """解析 Server酱 返回值。

    注意：**出错时它常常也返回 HTTP 200**，真正的判据是 JSON 里的 code：
      成功 code=0；SendKey 不存在 code=10003 / 40001；超配额 code=10001。
    错误文字可能放在 message / info / error 任一字段，得挨个看。
    """
    try:
        j = json.loads(body)
    except Exception:
        return (200 <= code < 300), "HTTP %s %s" % (code, (body or "")[:200])

    sc = j.get("code")
    if str(sc) == "0":
        data = j.get("data") if isinstance(j.get("data"), dict) else {}
        note = _plain(j.get("message") or j.get("info") or data.get("error")) or "OK"
        pid = data.get("pushid") or j.get("pushid")
        return True, "HTTP %s %s%s" % (code, note, ("  pushid=%s" % pid) if pid else "")

    msg = _plain(j.get("message") or j.get("info") or j.get("error") or body)
    low = msg.lower()
    try:
        sci = int(sc)
    except (TypeError, ValueError):
        sci = None
    hint = ""
    if sci in (40001, 10003) or "sendkey" in low or "错误的key" in msg:
        hint = "  —— SendKey 不对：去 sct.ftqq.com 后台复制最新那串（SCT / sctp 开头）"
    elif sci == 10001 or "上限" in msg or "quota" in low:
        hint = "  —— 今日推送条数已达上限（免费版每天 5 条），明天再试"
    return False, "HTTP %s %s%s" % (code, msg[:160], hint)


def send(channel, rec, dry_run=False):
    """返回 (ok: bool, detail: str)。dry_run 时不实际发送。"""
    title = build_title(classify(rec)[0], rec)
    text = render_text(rec)

    if channel == "none":
        return False, "未配置通知渠道（WB_NOTIFY_CHANNEL），本次跳过"

    if channel == "feishu":
        url = cfg("notify_url")
        if not url:
            return False, "缺少 WB_NOTIFY_URL"
        payload = render_feishu(rec)
        if dry_run:
            return True, "[dry-run] " + json.dumps(payload, ensure_ascii=False)
        code, body = _http_post(url, payload)
        # 飞书成功返回 {"code":0,...} 或 {"StatusCode":0,...}
        try:
            j = json.loads(body)
            ok = (j.get("code", j.get("StatusCode", 0)) == 0)
        except Exception:
            ok = 200 <= code < 300
        return ok, "HTTP %s %s" % (code, body[:200])

    if channel == "wecom":
        url = cfg("notify_url")
        if not url:
            return False, "缺少 WB_NOTIFY_URL"
        payload = render_wecom(rec)
        if dry_run:
            return True, "[dry-run] " + json.dumps(payload, ensure_ascii=False)
        code, body = _http_post(url, payload)
        try:
            j = json.loads(body)
            ok = j.get("errcode", 0) == 0
        except Exception:
            ok = 200 <= code < 300
        return ok, "HTTP %s %s" % (code, body[:200])

    if channel == "bark":
        key = cfg("notify_key")
        base = (cfg("notify_url") or "https://api.day.app").rstrip("/")
        if not key:
            return False, "缺少 WB_NOTIFY_KEY"
        # 正文走 body 而不是 URL —— 多账号汇总正文挺长，走 URL 会被截断。
        # 老版本自建服务没有 /push，下方会退回 GET。
        level_name = "timeSensitive" if classify(rec)[0] == "error" else "active"
        body_text = build_bark_body(rec)
        group = cfg("notify_group", "WorkBuddy")
        payload = {"device_key": key, "title": title, "body": body_text,
                   "group": group, "level": level_name}
        if dry_run:
            return True, "[dry-run] POST %s/push %s" % (base, json.dumps(payload, ensure_ascii=False)[:200])
        code, resp = _http_post(base + "/push", payload)
        if code and 200 <= code < 300:
            return True, "HTTP %s %s" % (code, resp[:200])
        url = "%s/%s/%s/%s?group=%s" % (base, urllib.parse.quote(key),
                                        urllib.parse.quote(title),
                                        urllib.parse.quote(body_text),
                                        urllib.parse.quote(group))
        code, resp = _http_get(url)
        return 200 <= code < 300, "HTTP %s %s" % (code, resp[:200])

    if channel == "serverchan":
        key = (cfg("notify_key") or "").strip()
        if not key:
            return False, ("缺少 SendKey —— 环境变量 SENDKEY（或 WB_NOTIFY_KEY），"
                           "或 notify.json 的 notify_key")
        url = serverchan_url(key)
        # desp 用 Markdown：Server酱详情页会把 markdown 渲染成 HTML，
        # 纯文本塞进去就是一坨没有层级的字，加粗/列表/分隔线全丢。
        desp = render_markdown(rec)
        if dry_run:
            # 把 markdown 原样打出来 —— 调样式时不用真发一条就能看效果
            return True, "[dry-run] POST %s\n标题：%s\n%s" % (url, title, desp)
        code, body = _http_post(url, {"title": title, "desp": desp}, form=True)
        return serverchan_result(code, body)

    if channel == "webhook":
        url = cfg("notify_url")
        if not url:
            return False, "缺少 WB_NOTIFY_URL"
        payload = {"title": title, "text": text, "markdown": render_markdown(rec),
                   "record": rec, "level": classify(rec)[0], "source": _title_prefix()}
        if dry_run:
            return True, "[dry-run] " + json.dumps(payload, ensure_ascii=False)
        code, body = _http_post(url, payload)
        return 200 <= code < 300, "HTTP %s %s" % (code, body[:200])

    return False, "未知渠道：%s" % channel


# ---- 命令 -------------------------------------------------------------------

def _collect_offsets(state, paths):
    """返回 {路径: offset}，兼容旧版单 offset 的 state。"""
    sig = sorted(paths)
    files = state.get("files")
    if not isinstance(files, dict):
        return {p: 0 for p in paths}, False
    # 日志集合变化时，只让新出现的文件从 0 开始读；已有文件继续沿用旧 offset。
    # 否则增删账号后，旧账号今天的多条历史日志会被重新扫进一次汇总。
    return {p: int(files.get(p) or 0) for p in paths}, state.get("filesList") != sig


def cmd_check(args):
    paths = resolve_log_paths(args)
    state_path = resolve_state_path(args.state, paths[0] if paths else None)
    channel = resolve_channel()
    mode = (cfg("notify_on", "daily") or "daily").strip().lower()

    state = load_state(state_path)
    offsets, reset = _collect_offsets(state, paths)
    if getattr(args, "from_start", False):
        offsets = {p: 0 for p in paths}      # 手动忽略基线，从头扫（仍受 max-age 限制）
    max_age = args.max_age if args.max_age is not None else 24
    multi_log = resolve_multi_log(getattr(args, "multi_log", None))

    all_records = []
    new_offsets = {}
    for p in paths:
        recs, new_off = read_new_records(p, offsets.get(p, 0), max_age_hours=max_age)
        new_offsets[p] = new_off
        account = os.path.basename(p)
        if account.endswith(".log"):
            account = account[:-4]
        for rec in recs:
            rec["_account"] = account
            all_records.append(rec)

    # multi.log：补上超时/崩溃这类"没写签到日志"的账号
    failures = []
    multi_off = int(state.get("multiOffset") or 0)
    new_multi_off = multi_off
    if multi_log:
        items, new_multi_off = read_multi_log(multi_log, multi_off, max_age_hours=max_age)
        for it in items:
            for name in (it.get("failed") or []):
                if name not in failures:
                    failures.append(name)

    all_records.sort(key=lambda r: r.get("_ts") or "")

    multi_accounts = len(paths) > 1

    if not all_records and not failures:
        state["files"] = new_offsets
        state["filesList"] = sorted(paths)
        if multi_log:
            state["multiOffset"] = new_multi_off
        state["checkedAt"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_state(state_path, state)
        print("没有新记录（%d 个日志，max-age %sh）" % (len(paths), max_age))
        return 0

    if multi_accounts or failures:
        picked = pick_records(all_records, mode)
        if failures:
            # 失败的账号必须提醒，即使 daily 模式当天已推过
            pass
        digest = make_digest(picked, failures)
        targets = [digest] if digest else []
    else:
        picked = pick_records(all_records, mode)
        targets = picked

    # daily 模式：同一天只推一条"非错误"记录；错误永远推
    if mode == "daily" and targets:
        final = []
        seen_dates = set()
        last_pushed = state.get("lastPushedDate")
        for rec in targets:
            level = classify(rec)[0]
            if level == "error":
                final.append(rec)
                continue
            d = (rec.get("_ts") or "")[:10]
            if d == last_pushed or d in seen_dates:
                continue
            seen_dates.add(d)
            final.append(rec)
        targets = final

    if not targets:
        state["files"] = new_offsets
        state["filesList"] = sorted(paths)
        if multi_log:
            state["multiOffset"] = new_multi_off
        state["checkedAt"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_state(state_path, state)
        print("有 %d 条新记录，但按模式 %s 均无需推送" % (len(all_records), mode))
        return 0

    ok_all = True
    pushed_dates = []
    for rec in targets:
        level = classify(rec)[0]
        ok, detail = send(channel, rec, dry_run=args.dry_run)
        ok_all = ok_all and (ok or channel == "none")
        tag = "OK " if ok else "FAIL"
        print("[%s] %s -> %s" % (tag, build_title(level, rec), detail))
        if ok and level != "error":
            pushed_dates.append((rec.get("_ts") or "")[:10])

    state["files"] = new_offsets
    state["filesList"] = sorted(paths)
    if multi_log:
        state["multiOffset"] = new_multi_off
    state["checkedAt"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if pushed_dates:
        state["lastPushedDate"] = max(pushed_dates)
    state["lastResult"] = (all_records[-1].get("result") if all_records else "FAILED_ACCOUNTS")
    state["channel"] = channel
    save_state(state_path, state)

    if channel == "none":
        print("提示：未配置 WB_NOTIFY_CHANNEL，本次未真正发送。配置后即可生效。")
        return 0
    return 0 if ok_all else 1


def cmd_startup(args):
    """发一条"服务已启动 / 部署完成"提醒。"""
    channel = resolve_channel()
    rec = {
        "_ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "result": "STARTUP",
        "report": args.note or "签到服务已启动，今日起自动运行",
        "needs_attention": False,
        "extra": list(args.extra or []),
    }
    if channel == "none":
        print("未配置通知渠道，启动提醒未发送（配好 SENDKEY 或 WB_NOTIFY_CHANNEL 即可）。")
        return 0
    ok, detail = send(channel, rec, dry_run=args.dry_run)
    print("[%s] %s -> %s" % ("OK" if ok else "FAIL", build_title("startup", rec), detail))
    return 0 if ok else 1


def cmd_notice(args):
    """发一条通用业务通知（新用户注册、管理员提醒等），与签到结果无关。

    和 startup 的区别：标题由 --title 指定，不会顶着一句「服务已启动」——
    工作台拿它来告诉管理员"有人注册了"。正文摘要走 --note，其余信息走
    可重复的 --extra，渲染管线（markdown / 飞书 / 企微 / Bark / webhook）
    全部复用，不需要为它单独写模板。
    """
    channel = resolve_channel()
    rec = {
        "_ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "result": "NOTICE",
        "notice_title": (args.title or "").strip() or "通知",
        "report": (args.note or "").strip(),
        "needs_attention": False,
        "extra": list(args.extra or []),
    }
    if channel == "none":
        print("未配置通知渠道，业务通知未发送（配好 SENDKEY 或 WB_NOTIFY_CHANNEL 即可）。")
        return 0
    ok, detail = send(channel, rec, dry_run=args.dry_run)
    print("[%s] %s -> %s" % ("OK" if ok else "FAIL", build_title("notice", rec), detail))
    return 0 if ok else 1


def cmd_test(args):
    channel = resolve_channel()
    demo = {
        "result": "TEST",
        "report": "测试消息已送达，通知通道工作正常",
        "needs_attention": False,
        "extra": [
            "来源：%s" % _title_prefix(),
            "渠道：%s" % channel,
            "下一次签到完成后，会按当前推送策略发送正式提醒",
        ],
        "_ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if channel == "none":
        print("未配置通知渠道，没有可发送的通道。")
        print("\n推荐（Server酱，微信收推送，只差一个 SendKey）：")
        print('  export SENDKEY="SCT123456Txxxxxxxx"     # 从 sct.ftqq.com 复制')
        print("  python3 notify.py test")
        print("\n其它通道：feishu / wecom 用 WB_NOTIFY_URL，bark 用 WB_NOTIFY_KEY，webhook 用 WB_NOTIFY_URL。")
        return 2
    ok, detail = send(channel, demo, dry_run=args.dry_run)
    print("[%s] 渠道=%s %s" % ("OK" if ok else "FAIL", channel, detail))
    return 0 if ok else 1


def cmd_status(args):
    paths = resolve_log_paths(args)
    state_path = resolve_state_path(args.state, paths[0] if paths else None)
    channel = resolve_channel()
    url = cfg("notify_url") or ""
    key = cfg("notify_key") or ""
    multi_log = resolve_multi_log(getattr(args, "multi_log", None))

    def mask(v):
        if not v:
            return "(未设置)"
        return v[:12] + "…" + v[-4:] if len(v) > 20 else "***"

    print("渠道        : %s" % channel)
    print("推送模式    : %s" % (cfg("notify_on", "daily")))
    if channel == "serverchan" and key:
        print("Sender      : Server酱 → %s" % mask(serverchan_url(key)))
    else:
        print("webhook     : %s" % mask(url))
        print("key         : %s" % mask(key))
    print("来源名      : %s" % _title_prefix())
    print("日志文件    : %d 个" % len(paths))
    for p in paths:
        print("              - %s%s" % (p, "" if os.path.exists(p) else "  (不存在)"))
    if multi_log:
        print("汇总日志    : %s%s" % (multi_log, "" if os.path.exists(multi_log) else "  (不存在)"))
    print("状态文件    : %s" % state_path)
    st = load_state(state_path)
    if st:
        print("  lastPushedDate : %s" % st.get("lastPushedDate"))
        print("  lastResult     : %s" % st.get("lastResult"))
        print("  checkedAt      : %s" % st.get("checkedAt"))
        files = st.get("files") or {}
        if files:
            print("  offset         : %d 个日志已记录" % len(files))
    else:
        print("  （尚未运行过 check）")
    return 0


def build_parser():
    ap = argparse.ArgumentParser(description="WorkBuddy 签到通知器（启动提醒 + 签到结果提醒 + 业务通知）")
    sub = ap.add_subparsers(dest="cmd")

    def add_common(p):
        p.add_argument("--file", action="append", help="签到日志路径（可重复）")
        p.add_argument("--log-dir", help="日志目录：扫描其下所有 *.log（多账号用）")
        p.add_argument("--state", help="状态文件路径")
        p.add_argument("--dry-run", action="store_true", help="只渲染不发送")

    p_check = sub.add_parser("check", help="检查新增日志并按规则推送")
    add_common(p_check)
    p_check.add_argument("--from-start", action="store_true", help="忽略历史基线，从头扫描")
    p_check.add_argument("--max-age", type=float, default=None,
                         help="只处理最近 N 小时内的记录（默认 24）")
    p_check.add_argument("--multi-log", help="多账号汇总日志（补上超时/崩溃的账号）")

    p_start = sub.add_parser("startup", help="发送一条「服务已启动」提醒")
    add_common(p_start)
    p_start.add_argument("--note", help="正文摘要，例如「多账号 3 个」")
    p_start.add_argument("--extra", action="append", help="附加信息行（可重复）")

    p_notice = sub.add_parser("notice", help="发送一条业务通知（新用户注册等）")
    add_common(p_notice)
    p_notice.add_argument("--title", help="通知标题，例如「新用户注册」")
    p_notice.add_argument("--note", help="正文摘要")
    p_notice.add_argument("--extra", action="append", help="附加信息行（可重复）")

    p_test = sub.add_parser("test", help="发一条测试通知")
    add_common(p_test)

    p_status = sub.add_parser("status", help="显示配置与状态")
    p_status.add_argument("--file", action="append", help="签到日志路径（可重复）")
    p_status.add_argument("--log-dir", help="日志目录")
    p_status.add_argument("--multi-log", help="多账号汇总日志")
    p_status.add_argument("--state", help="状态文件路径")

    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    cmd = args.cmd or "check"
    if cmd == "check":
        return cmd_check(args)
    if cmd == "startup":
        return cmd_startup(args)
    if cmd == "notice":
        return cmd_notice(args)
    if cmd == "test":
        return cmd_test(args)
    if cmd == "status":
        return cmd_status(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
