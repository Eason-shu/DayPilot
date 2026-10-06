#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 作者：EasonShu
"""WorkBuddy 自动签到 · 多账号调度器（一套代码 + 一个凭据目录）

"""

import argparse
import datetime
import glob
import json
import os
import subprocess
import sys
import time
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
SIGNIN = os.path.join(HERE, "signin.py")
RENEW = os.path.join(HERE, "renew_session.py")
DEFAULT_SESS_DIR = os.path.join(HERE, "sessions")
DEFAULT_LOCK = os.path.join(HERE, ".multi.lock")
DEFAULT_MULTI_LOG = os.path.join(HERE, "multi.log")

LOCK_STALE_SECONDS = 45 * 60        # 锁文件超过这个时长视为上一轮残留
TIMEOUT_DEFAULT = 300.0             # 单个账号的子进程超时（秒）

# 只读模式：不间隔、不走续期、不改任何状态
READ_ONLY_MODES = ("status", "doctor")

# 有副作用的模式 → 账号之间的默认间隔（秒）。签到类间隔大，轮询类间隔小。
MUTATING_GAP = {
    "silent": 60.0,
    "auto": 60.0,
    "all": 60.0,
    "growth": 60.0,
    "claim": 60.0,
    "silent-poll": 30.0,
    "silent-growth": 30.0,
}
VALID_MODES = READ_ONLY_MODES + tuple(sorted(MUTATING_GAP))


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def disp_width(text):
    """终端显示宽度：中文按 2 列算。"""
    total = 0
    for ch in text:
        total += 2 if unicodedata.east_asian_width(ch) in ("F", "W", "A") else 1
    return total


def pad(text, width):
    return text + " " * max(0, width - disp_width(text))


def stamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clock():
    return datetime.datetime.now().strftime("%H:%M:%S")


def discover(sess_dir):
    """列出账号：返回 [(账号名, 凭据路径), ...]，按文件名排序。"""
    found = []
    for path in sorted(glob.glob(os.path.join(sess_dir, "*.json"))):
        name = os.path.basename(path)
        if name.startswith(".") or name.endswith(".example.json"):
            continue
        found.append((name[:-5], path))
    return found


def read_new(path, offset):
    """读取文件自 offset 起的新内容，返回 (文本, 新 offset)。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            data = handle.read()
            return data, handle.tell()
    except (IOError, OSError):
        return "", offset


def last_result(text):
    """从日志片段里取出最后一条 {"result": ...} 记录。"""
    for line in reversed(text.strip().splitlines()):
        start = line.find("{")
        if start < 0:
            continue
        try:
            obj = json.loads(line[start:])
        except ValueError:
            continue
        if isinstance(obj, dict) and "result" in obj:
            return obj
    return None


def append_json(path, obj):
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except (IOError, OSError):
        pass


# --------------------------------------------------------------------------- #
# 运行锁：防止上一轮还没跑完、下一轮 cron 又启动（多账号耗时长，重叠概率不低）
# --------------------------------------------------------------------------- #
class RunLock(object):
    def __init__(self, path, stale=LOCK_STALE_SECONDS):
        self.path = path
        self.stale = stale
        self.acquired = False

    def acquire(self):
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(self.path)
            except OSError:
                age = 0.0
            if age > self.stale:
                try:
                    os.unlink(self.path)
                except OSError:
                    pass
                return self.acquire()
            return False
        except OSError:
            # 拿不到锁文件（只读文件系统等）时放行，别把签到拦死
            return True
        try:
            os.write(fd, ("%d %s\n" % (os.getpid(), stamp())).encode("utf-8"))
        finally:
            os.close(fd)
        self.acquired = True
        return True

    def release(self):
        if self.acquired:
            try:
                os.unlink(self.path)
            except OSError:
                pass
            self.acquired = False


# --------------------------------------------------------------------------- #
# 单个账号
# --------------------------------------------------------------------------- #
def renew_account(name, auth_path, log_dir, timeout):
    """先续期（仅在剩余 <10 天才真发请求，失败不阻断）。"""
    if not os.path.isfile(RENEW):
        return None
    log_path = os.path.join(log_dir, "%s.renew.log" % name)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    before = os.path.getsize(log_path) if os.path.exists(log_path) else 0
    try:
        subprocess.run(
            [sys.executable, RENEW, "--file", auth_path, "--log", log_path],
            env=env, cwd=HERE, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except Exception:                      # noqa: BLE001 —— fail-open，续期不该连累签到
        return None
    text, _ = read_new(log_path, before)
    return last_result(text)


def run_account(name, auth_path, log_dir, mode, timeout):
    """跑一个账号，返回结果字典。"""
    if not os.path.isdir(log_dir):
        try:
            os.makedirs(log_dir)
        except OSError:
            pass
    log_path = os.path.join(log_dir, "%s.log" % name)

    env = os.environ.copy()
    env["WORKBUDDY_AUTH_FILE"] = auth_path
    env["WORKBUDDY_SIGNIN_LOG"] = log_path
    env["PYTHONIOENCODING"] = "utf-8"

    before = os.path.getsize(log_path) if os.path.exists(log_path) else 0
    started = time.time()
    outcome = {"name": name, "code": None, "result": None, "note": "", "elapsed": 0.0,
               "started": stamp(), "started_clock": clock()}

    if mode not in READ_ONLY_MODES:
        renewed = renew_account(name, auth_path, log_dir, timeout)
        if renewed and renewed.get("result") == "RENEWED":
            outcome["note"] = "已续期"

    try:
        proc = subprocess.run(
            [sys.executable, SIGNIN, mode],
            env=env, cwd=HERE, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        outcome["code"] = proc.returncode
        # 交互命令走 stdout，silent 系列写日志文件
        blob = proc.stdout.decode("utf-8", "replace")
        tail, _ = read_new(log_path, before)
        record = last_result(tail) or last_result(blob)
        if record:
            outcome["result"] = record.get("result")
            if record.get("needs_attention"):
                outcome["note"] = (outcome["note"] + " 需关注").strip()
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            if err and not outcome["note"]:
                outcome["note"] = err[-1][:70]
    except subprocess.TimeoutExpired:
        outcome["code"] = -1
        outcome["note"] = "超时（>%ds）" % int(timeout)
    except Exception as exc:               # noqa: BLE001
        outcome["code"] = -2
        outcome["note"] = str(exc)[:70]

    outcome["elapsed"] = time.time() - started
    outcome["finished"] = clock()
    outcome["ok"] = (outcome["code"] == 0)
    return outcome


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description="WorkBuddy 多账号调度器（一套代码 + 一个凭据目录）")
    parser.add_argument("mode", help="运行模式：%s" % " / ".join(VALID_MODES))
    parser.add_argument("--dir", default=DEFAULT_SESS_DIR, help="凭据目录，默认 <脚本目录>/sessions")
    parser.add_argument("--gap", type=float, default=None,
                        help="账号之间的间隔秒数（默认：签到类 60，轮询类 30，只读 0）")
    parser.add_argument("--timeout", type=float, default=TIMEOUT_DEFAULT, help="单账号超时秒数")
    parser.add_argument("--lock", default=DEFAULT_LOCK, help="运行锁文件路径")
    parser.add_argument("--log-dir", default=os.path.join(HERE, "logs"), help="单账号日志目录")
    parser.add_argument("--multi-log", default=DEFAULT_MULTI_LOG, help="多账号汇总日志")
    parser.add_argument("--dry-run", action="store_true", help="只打印账号与时间表，不执行")
    parser.add_argument("--no-lock", action="store_true", help="跳过运行锁（手动排查时用）")
    args = parser.parse_args()

    mode = args.mode
    if mode not in VALID_MODES:
        sys.stderr.write("未知模式 %s；可用：%s\n" % (mode, " / ".join(VALID_MODES)))
        return 2

    sess_dir = os.path.abspath(args.dir)
    log_dir = os.path.abspath(args.log_dir)
    if not os.path.isdir(sess_dir):
        sys.stderr.write(
            "凭据目录不存在：%s\n"
            "把每个账号的凭据（export_session.py 导出的 session.json）放进去，一个账号一个 .json。\n"
            % sess_dir)
        return 2

    accounts = discover(sess_dir)
    if not accounts:
        sys.stderr.write("凭据目录里没有可用的 *.json：%s\n" % sess_dir)
        return 2

    gap = args.gap
    if gap is None:
        env_gap = os.environ.get("WORKBUDDY_ACCOUNT_GAP")
        if env_gap:
            try:
                gap = float(env_gap)
            except ValueError:
                gap = None
    if gap is None:
        gap = 0.0 if mode in READ_ONLY_MODES else MUTATING_GAP.get(mode, 60.0)

    # 找得到 signin.py 才继续（代码目录被拆散时给个明确报错）
    if not os.path.isfile(SIGNIN):
        sys.stderr.write("找不到 signin.py（应与本脚本同目录）：%s\n" % SIGNIN)
        return 2

    width = max([disp_width(name) for name, _ in accounts] + [6])

    if args.dry_run:
        print("==> 演练（不执行）：%d 个账号 · 模式 %s · 间隔 %s 秒" % (len(accounts), mode, gap))
        for index, (name, path) in enumerate(accounts, 1):
            offset = (index - 1) * gap
            print("  [%d/%d] %s  第 %s 秒  ← %s" % (index, len(accounts), pad(name, width),
                                                    int(offset), path))
        return 0

    lock = RunLock(args.lock)
    if not args.no_lock:
        if not lock.acquire():
            # 上一轮还在跑：直接退出，不报错（cron 不该因此告警）
            print("上一轮仍在执行，本次跳过（锁：%s）" % args.lock)
            return 0

    started_at = time.time()
    print("==> 多账号：%d 个账号 · 模式 %s · 间隔 %s 秒 · 起始 %s"
          % (len(accounts), mode, gap, stamp()))
    print("    凭据目录：%s" % sess_dir)

    outcomes = []
    try:
        for index, (name, path) in enumerate(accounts, 1):
            if index > 1 and gap > 0:
                time.sleep(gap)
            shown = "%s/%s" % (index, len(accounts))
            print("    [%s] %s 启动于 %s ..." % (shown, pad(name, width), clock()))
            sys.stdout.flush()
            outcome = run_account(name, path, log_dir, mode, args.timeout)
            outcomes.append(outcome)
            print("    [%s] %s %s  %s → %s (%ds)%s"
                  % (shown, pad(name, width),
                     "OK  " if outcome["ok"] else "FAIL",
                     outcome["started_clock"], outcome["finished"],
                     int(round(outcome["elapsed"])),
                     ("  " + outcome["note"]) if outcome["note"] else ""))
    except KeyboardInterrupt:
        print("\n被中断，已完成的账号不受影响。")
    finally:
        lock.release()

    ok = [o for o in outcomes if o["ok"]]
    failed = [o for o in outcomes if not o["ok"]]
    elapsed = time.time() - started_at

    print("==> 汇总：%d 成功 · %d 失败 · 总耗时 %d 秒" % (len(ok), len(failed), int(round(elapsed))))
    for item in failed:
        print("    失败：%s  result=%s  %s" % (item["name"], item["result"] or "-", item["note"]))
    for item in ok:
        if item["result"]:
            print("    %s：%s%s" % (pad(item["name"], width), item["result"],
                                    ("（%s）" % item["note"]) if item["note"] else ""))

    append_json(args.multi_log, {
        "ts": stamp(), "mode": mode, "accounts": len(accounts), "ok": len(ok),
        "failed": [o["name"] for o in failed], "gap": gap,
        "elapsed": round(elapsed, 1), "needs_attention": bool(failed),
    })

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
