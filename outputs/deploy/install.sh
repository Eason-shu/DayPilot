#!/bin/sh
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 EasonShu
# =========================================================================
# DayPilot 自动签到工作台 · systemd 部署脚本
#
# 用法（在 outputs/ 目录里执行）：
#     sudo sh deploy/install.sh
#
# 可用环境变量覆盖默认值：
#     APP_DIR=/opt/checkinops   安装目录
#     APP_USER=checkin          运行用户
#     PY=/usr/bin/python3       解释器路径
#
# 重复执行是安全的：配置(data/workbench.json)、凭据(sessions/)、
# 归档(archive/)、任务历史(runtime/) 都会被保留，只更新代码。
# =========================================================================
set -eu

APP_NAME=checkinops
APP_DIR=${APP_DIR:-/opt/checkinops}
APP_USER=${APP_USER:-checkin}
PY=${PY:-/usr/bin/python3}
PORT=${DASHBOARD_PORT:-8000}
SERVICE=/etc/systemd/system/${APP_NAME}.service

info() { echo "[*] $*"; }
warn() { echo "[!] $*" >&2; }
die()  { echo "[x] $*" >&2; exit 1; }

# ---------------------------------------------------------------- 前置检查
[ "$(id -u)" = "0" ] || die "需要 root 权限，请用 sudo 运行"

command -v systemctl >/dev/null 2>&1 || die "没有 systemctl，本脚本只支持 systemd 发行版"

[ -x "$PY" ] || die "找不到解释器 $PY（可用 PY=/usr/bin/python3.8 指定）"

# Python 3.6 是下限。这里不做花哨解析，只取主次版本号比对。
PYVER=$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
case "$PYVER" in
  3.6|3.7|3.8|3.9|3.10|3.11|3.12|3.13|3.14) : ;;
  *) die "Python 版本为 $PYVER，需要 3.6 以上" ;;
esac
info "Python $PYVER"

# 源码根目录 = 本脚本所在目录的上一级
SRC=$(cd "$(dirname "$0")/.." && pwd)
[ -f "$SRC/server.py" ] || die "在 $SRC 里找不到 server.py，请确认从 outputs/ 目录运行"

if [ "$SRC" = "$APP_DIR" ]; then
  die "源码目录与安装目录相同（$SRC），请把 outputs/ 放到别处再执行"
fi

# ---------------------------------------------------------------- 运行用户
if ! id "$APP_USER" >/dev/null 2>&1; then
  info "创建系统用户 $APP_USER"
  if command -v useradd >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$APP_USER" 2>/dev/null \
      || useradd -r -s /sbin/nologin "$APP_USER"
  else
    adduser --system --no-create-home --disabled-login "$APP_USER"
  fi
fi

# ------------------------------------------------- 保护运行期数据（重要）
# 这些是"服务器上攒出来的"，重新部署不能冲掉：用户库、密码、上传的凭据、归档、任务历史。
KEEP_BACKUP=$(mktemp -d)
trap 'rm -rf "$KEEP_BACKUP"' EXIT INT TERM

KEEP_LIST="data/workbench.json data/dailyhub.sqlite3 data/users archive runtime apps/trae/trae-sessions apps/workbuddy/sessions"
for rel in $KEEP_LIST; do
  if [ -e "$APP_DIR/$rel" ]; then
    mkdir -p "$KEEP_BACKUP/$(dirname "$rel")"
    cp -R "$APP_DIR/$rel" "$KEEP_BACKUP/$rel" 2>/dev/null || true
  fi
done

# ---------------------------------------------------------------- 同步代码
info "安装到 $APP_DIR"
mkdir -p "$APP_DIR"
cp -R "$SRC/." "$APP_DIR/"

# promo/ 是产品短片的工程目录（HyperFrames 合成 + 音乐 + 字体 + 缩略图），
# 跟签到服务运行时一点关系都没有，体积却有 11MB —— 别让它跟着部署上服务器。
if [ -d "$APP_DIR/promo" ]; then
  rm -rf "$APP_DIR/promo"
  info "已跳过 promo/（产品短片工程，不随服务部署）"
fi

# 还原被保护的数据
for rel in $KEEP_LIST; do
  if [ -e "$KEEP_BACKUP/$rel" ]; then
    rm -rf "$APP_DIR/$rel"
    mkdir -p "$APP_DIR/$(dirname "$rel")"
    cp -R "$KEEP_BACKUP/$rel" "$APP_DIR/$rel"
    info "保留原有 $rel"
  fi
done

# 首次安装（服务器上原本没有用户库）时，别把源码目录里附带的开发库带过去：
if [ ! -e "$KEEP_BACKUP/data/dailyhub.sqlite3" ]; then
  rm -f "$APP_DIR/data/dailyhub.sqlite3"
  rm -rf "$APP_DIR/data/users"
  info "首次安装：已清除源码附带的用户库，首次启动会按 workbench.json 播种管理员"
fi

# 清理编译产物，别让不同机器的 .pyc 混进去
find "$APP_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
rm -f "$APP_DIR/runtime/scheduler.lock" 2>/dev/null || true

chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 700 "$APP_DIR/data" "$APP_DIR/runtime" 2>/dev/null || true

# ---------------------------------------------------------------- systemd
info "安装 systemd 单元 → $SERVICE"
sed -e "s#/opt/checkinops#$APP_DIR#g" \
    -e "s#^User=.*#User=$APP_USER#" \
    -e "s#^Group=.*#Group=$APP_USER#" \
    -e "s#^ExecStart=.*#ExecStart=$PY $APP_DIR/server.py#" \
    "$SRC/deploy/${APP_NAME}.service" > "$SERVICE"
chmod 644 "$SERVICE"

systemctl daemon-reload
systemctl enable "$APP_NAME" >/dev/null 2>&1 || true
systemctl restart "$APP_NAME"

sleep 2
if systemctl is-active --quiet "$APP_NAME"; then
  info "服务已启动"
else
  warn "服务未能启动，看日志：journalctl -u $APP_NAME -n 50 --no-pager"
fi

# ---------------------------------------------------------------- 收尾提示
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -n "${IP:-}" ] || IP="<服务器IP>"

echo
echo "================================================================"
echo " DayPilot 已部署"
echo "   安装目录 : $APP_DIR"
echo "   运行用户 : $APP_USER"
echo "   访问地址 : http://$IP:$PORT"
echo "   默认账号 : admin / workbench.json 里的密码"
echo
echo " 下一步："
echo "   1. 改默认密码：管理员密码在首次启动时由 workbench.json 的 auth.password"
echo "      播种进用户库。所以要么在首次启动【前】改好 json，要么现在删掉"
echo "      $APP_DIR/data/dailyhub.sqlite3 后改 json 再重启（会清掉已注册用户）"
echo "   2. 放行端口：firewall-cmd --add-port=$PORT/tcp --permanent && firewall-cmd --reload"
echo "      （或 ufw allow $PORT/tcp / 云厂商安全组放行）"
echo "   3. 看日志：journalctl -u $APP_NAME -f"
echo
echo " 提醒：定时任务由本服务内部调度，不需要配 crontab。"
echo "================================================================"
