# 部署说明

**完整部署手册（推荐先看这个）：[`../系统部署说明.html`](../系统部署说明.html)**

包含前置条件、五步部署流程、定时与通知配置、运维与排错。

---

## 快速开始

```bash
# 在 outputs/ 目录里执行（需要 root）
sudo sh deploy/install.sh
```

可用环境变量覆盖默认值：

| 变量 | 作用 | 默认 |
|---|---|---|
| `APP_DIR` | 安装目录 | `/opt/checkinops` |
| `APP_USER` | 运行用户（不存在会自动创建） | `checkin` |
| `PY` | 解释器绝对路径 | `/usr/bin/python3` |
| `DASHBOARD_PORT` | 服务监听端口 | `8000` |

> ⚠️ 源码目录不能与安装目录相同。请把 `outputs/` 传到别处（例如 `/opt/checkinops-src`）再执行安装。

## 安装之后

- 访问 `http://服务器IP:8000`，默认账号 `admin / admin123456` —— **必须立即修改**
- 改密码：管理员密码在**首次启动时**从 `data/workbench.json` 的 `auth.password` 播种进
  `data/dailyhub.sqlite3`。所以要么在首次启动**前**改好 json；已启动过的部署目前没有改密界面，
  重置 = 删掉 `data/dailyhub.sqlite3` 后改 json 再重启（**会清掉所有已注册用户**）
- 放行端口：`firewall-cmd --add-port=8000/tcp --permanent && firewall-cmd --reload`
- 看日志：`journalctl -u checkinops -f`
- 注册与准入：登录页可自助注册，**默认注册后直接可用、无需审核**。防多开靠两层：
  服务端从请求头算出的设备指纹（同一设备只能有一个可用账号）+ 同 IP 注册配额
  （默认 24 小时内 3 个）。命中任一条会**降级成待审核**，管理员在顶栏**「用户管理」**
  里通过后才能登录；每次注册都会给管理员推一条消息。配额与开关在
  `data/workbench.json` 的 `registration` 段（`auto_approve` / `ip_quota` /
  `ip_window_hours` / `server_fingerprint`）。
- 删除用户：管理员点「删除」是**彻底删除**，会一并移除 `data/users/u<id>/`
  整个工作空间（该公司下已上传的凭据、日志、归档）以及该用户的定时任务和当天
  的调度触发记录，不可恢复。若该用户正有签到任务在跑，会拒绝删除并提示等任务结束
  —— 避免删出半截目录。停用/拒绝则只改状态，目录原样保留。
- 旧版本「删除用户」只删库不删盘，可能残留孤儿目录。启动日志会提示，执行
  `python server.py --purge-orphans` 可清理（数据库为空时会拒绝执行以防误删）。

## 两点必须知道

1. **定时任务由服务内部调度，不需要配置 crontab。** 如果服务器上还留着旧的
   `/etc/cron.d/trae-signin` 或 `workbuddy-signin`，请删掉，否则会重复执行。
2. **调度器跟着进程走。** 服务停了定时就停了，这是设计使然。单元里已配置
   `Restart=always` 自动拉起。

## 数据保护

重复执行 `install.sh` 是安全的 —— 它会保留以下「服务器上攒出来」的数据：

```
data/workbench.json     首次启动播种管理员用的配置（时间表、通知的兜底默认值）
data/dailyhub.sqlite3   用户库：账号、角色、审核状态、密码哈希
data/users/             各注册用户的独立工作区（凭据/日志/时间表/通知）
apps/trae/trae-sessions TRAE 凭据（旧单用户目录，管理员首次登录会自动继承）
apps/workbuddy/sessions WorkBuddy 凭据（同上）
archive/                归档账号
runtime/                任务历史与日志
```
