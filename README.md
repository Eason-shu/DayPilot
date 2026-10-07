![DayPilot —— 自托管自动签到工作台](outputs/static/assets/daypilot-promo-cover-v1.png)

# 🛰️ DayPilot

**把零散的自动签到脚本，收进一个可部署的 Web 控制台**

![License: MIT](https://img.shields.io/badge/License-MIT-green.svg) ![Python](https://img.shields.io/badge/Python-3.6%2B-blue.svg) ![Dependencies](https://img.shields.io/badge/dependencies-0-success.svg) ![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20Windows%20%7C%20macOS-lightgrey.svg) ![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg) ![Stars](https://img.shields.io/github/stars/Eason-shu/DayPilot?label=Stars)

> DayPilot 是一个**自托管**的自动签到工作台：脚本负责签到，服务端负责凭据托管、定时调度、消息通知、多用户准入和运行日志。  
> 每个用户有独立工作区，管理员在同一个面板里看全部账号、调度和通知状态。  
> 服务端只用 Python 标准库，**零第三方运行依赖**，部署就是一个目录 + 一个 systemd 服务。
>
> 👤 作者：Eason-shu · 📦 仓库：github.com/Eason-shu/DayPilot · 🌐 演示环境：<http://111.228.56.37:8000>

> [!TIP]  
> **⭐ 顺手点个 Star 再往下看。** 签到接口是从各产品桌面端逆向得到的，上游改一版脚本就可能失效；凭据托管又涉及账号安全。点一下 Star，等哪天连签莫名其妙断了，你能一秒翻回这个仓库。

---

## ✨ 特性

- 🧩 **两套业务开箱即用** —— WorkBuddy（每日签到 + 成长中心全套）与 TRAE（每日签到 + 积分续期），各自独立配置，互不干扰
- ⏰ **内置调度器，不依赖 crontab** —— 每日签到 + 每轮补签（轮询）都在网页上配，进程内 20 秒一次对表；调度跟进程走，服务停了定时才停
- 👥 **多用户 + 独立工作区** —— 每个用户一个 `data/users/u<id>/`，凭据、日志、时间表、通知配置全部隔离；管理员视角是只读看板
- 🚪 **注册准入** —— 默认免审核但**不是无门槛**：服务端指纹 + 同 IP 注册配额挡多开，疑似同设备重复注册自动**降级为人工审核**，每次注册都推消息给管理员
- 📣 **五种通知渠道** —— Server酱 / 飞书 / 企业微信 / Bark / Webhook，支持「每日汇总」「只在有收获时」「出错才提醒」，带一键测试消息
- 🗂️ **账号全生命周期** —— 上传、启用、停用、归档、恢复、删除；删除是**彻底删除**（连工作空间与定时任务一起清）
- 🔐 **凭据不出本机** —— 导出工具在你自己的机器上读取登录态；服务端只保存脚本所需的会话文件，密文落盘
- 🧾 **运行记录可追溯** —— 面板看执行历史与结果码，也可直接翻 `.log` 原始日志
- 📦 **零第三方运行依赖** —— Python 标准库 + SQLite，不用 `pip install`，源码就是运行时
- 🪶 **轻量加固** —— 自带 systemd 单元，`ProtectSystem` / `NoNewPrivileges` / `PrivateTmp` 都开了

---

## 📋 前置条件

- ✅ 一台能长期开机的机器（本机试用也行，服务器推荐 1 核 1G 起）
- ✅ **Python 3.6+**（`deploy/install.sh` 会校验；3.8 以下自动走 `zoneinfo` 兜底分支）
- ✅ 想托管的账号本身已在对应产品里登录过（凭据靠导出工具从**本机**读取）
- ⬜ 可选：`systemd` 发行版 —— 想用一键部署脚本才需要；不用它也能 `python server.py` 直接跑
- ⬜ 可选：Nginx / Caddy —— 公网部署时用来终结 HTTPS

> [!IMPORTANT]  
> 本项目会处理**真实凭据**、通知密钥和用户库。请先读 [安全说明](#-安全与隐私) 和 [发布到 GitHub 之前](#-发布到-github-之前)，再决定把它部署到哪里。

---

## 🚀 快速开始（本机试用）

本仓库真正的应用目录是 `outputs/`。

```bash
git clone https://github.com/Eason-shu/DayPilot.git
cd DayPilot/outputs
python server.py --host 127.0.0.1 --port 8000
```

浏览器打开 `http://127.0.0.1:8000`，用首次启动播种的管理员账号登录：

```text
账号：admin
密码：admin123456
```

> [!WARNING]  
> `admin123456` 只是**首次启动**的默认值，会从 `data/workbench.json` 的 `auth.password` 播种进 `data/dailyhub.sqlite3`。**播种只发生一次**——数据库里已经有这个管理员之后，再改 json 就不会生效了。

只想确认服务能不能起来、不想开端口，用自检模式打印一次状态 JSON 就退出：

```bash
python server.py --check
```

---

## 🖥️ 部署到服务器

在 `outputs/` 目录里执行一条命令（需要 root）：

```bash
sudo sh deploy/install.sh
```

它会创建运行用户、同步代码、写 systemd 单元并启动服务。可用环境变量覆盖默认值：

| 变量               | 作用                  | 默认                 |
| ---------------- | ------------------- | ------------------ |
| `APP_DIR`        | 安装目录                | `/opt/checkinops`  |
| `APP_USER`       | 运行用户（不存在会自动创建为系统用户） | `checkin`          |
| `PY`             | 解释器绝对路径             | `/usr/bin/python3` |
| `DASHBOARD_PORT` | 服务监听端口              | `8000`             |

部署后常用命令：

```bash
systemctl restart checkinops                 # 重启
systemctl status checkinops                  # 状态
journalctl -u checkinops -f                  # 实时日志
journalctl -u checkinops -n 50 --no-pager    # 最近 50 行
```

> [!NOTE]  
> **重复执行是安全的。** 安装脚本会把「服务器上攒出来的」数据先备份再还原：`data/workbench.json`、`data/dailyhub.sqlite3`、`data/users/`、`archive/`、`runtime/`、两边的凭据目录（`apps/*/sessions`、`apps/trae/trae-sessions`）。它只更新代码。  
> 另外两个细节：源码目录不能与安装目录相同（脚本会直接报错拦住你）；首次安装时会清掉源码里附带的开发库，避免把你的测试账号带上服务器。

### 还有两步

1. **放行端口**：`firewall-cmd --add-port=8000/tcp --permanent && firewall-cmd --reload`，或 `ufw allow 8000/tcp`，云厂商还要在安全组里放行。
2. **改掉默认密码**：目前**没有改密界面**，重置流程是「删库 → 改 json → 重启」，会清掉所有已注册用户：

```bash
# 在安装目录（默认 /opt/checkinops）里执行
systemctl stop checkinops
rm -f data/dailyhub.sqlite3 && rm -rf data/users
# 编辑 data/workbench.json，改 auth.password（并建议把 auth.secret 留空让它自动生成）
systemctl start checkinops
```

> [!TIP]  
> **想让默认密码一开始就安全**：在**首次启动之前**就把 `data/workbench.json` 的 `auth.password` 改掉，再启动服务 —— 这样播种进去的直接就是你的密码，不用删库。

### 公网部署请务必做

- **HTTPS 反代**（Nginx / Caddy），并在 systemd 单元里打开 `Environment=DASHBOARD_COOKIE_SECURE=1`  
  —— 否则浏览器会在 https 页面丢弃不带 `Secure` 的会话 Cookie，表现为「登录成功但立刻掉线」。
- 把 `data/`、`runtime/` 的权限收紧（安装脚本已 `chmod 700`），别用 root 跑服务。
- 完整手册见 [`outputs/系统部署说明.html`](outputs/系统部署说明.html)（含前置条件、五步流程、定时与通知配置、运维排错）。

---

## 🧭 使用流程（4 步）

登录页的「使用指南」就是这四步，这里给上每步的实际操作：

**1️⃣ 注册账号** —— 登录页点「注册账号」。默认注册后**直接可用**，不用等审核（见 [注册准入](#-多用户与注册准入)）。

**2️⃣ 导出凭据**（在你登录过该产品的**本机**上跑）

从 [Releases](https://github.com/Eason-shu/DayPilot/releases) 下载对应导出器，或者直接用仓库里的脚本：

```bash
# WorkBuddy：按「当前登录的是谁」自动命名，避免多账号互相覆盖
python apps/workbuddy/export_auto.py

# TRAE：导出桌面端凭据（含续期所需的机器码信息）
python apps/trae/trae_export.py
```

两个导出器都支持 `--who` 先看「当前登录的是谁」再决定导不导；`--rename` 改名、`--as-new` 重名时另存不覆盖。

**3️⃣ 上传账号** —— 面板「账号」页 → 添加账号 → 选择刚导出的凭据文件，选好产品（WorkBuddy / TRAE）。

**4️⃣ 配置自动化** —— 在「设置」里安排每日签到时间、轮询时间，以及通知渠道（配好可以点「发送测试消息」验证）。

> [!NOTE]  
> 上传后若显示「待上传凭据」，说明这个账号还没跑过一次有效签到。**注册后 24 小时内没有任何凭据的账号会被自动清理**（节省空间，也顺手回收误注册），这是刻意设计。

---

## ⏰ 定时执行：不需要 crontab

DayPilot 的调度**完全在服务进程内部**：`scheduler_loop` 每 20 秒对一次表，命中时间点就按用户配置拉起对应脚本。默认排期（可按用户在面板上改）：

| 产品        | 每日签到              | 轮询（补签 + 成长中心）                              |
| --------- | ----------------- | ------------------------------------------ |
| WorkBuddy | `00:05`（`silent`） | `05:00` / `12:00` / `20:00`（`silent-poll`） |
| TRAE      | `00:35`（`silent`） | `05:30` / `12:30` / `20:30`（`silent-poll`） |

三件事必须知道：

1. **不需要也不应该配 crontab。** 如果服务器上还留着旧的 `/etc/cron.d/trae-signin` 或 `workbuddy-signin`，请删掉，否则会重复执行。
2. **调度器跟着进程走。** 服务停了，定时就停了 —— 这是设计使然。systemd 单元里已配置 `Restart=always` 自动拉起。
3. **轮询兼做补签兜底。** 每轮先查一次签到状态，**未签才签**（接口幂等，不会重复领取）。所以「00:05 那次撞上关机或睡眠」不再等于当天断签，一天有四次机会。

> [!IMPORTANT]  
> **同一个 `outputs/` 目录下不要跑多个 server.py。** 多个实例会各起一份调度循环，靠 `runtime/scheduler.lock` 文件锁互斥；虽然做了防重（触发记录 + `O_CREAT|O_EXCL` 占位文件双重去重），但没必要给自己找麻烦。

---

## 👥 多用户与注册准入

默认策略是「**自动开通 + 异常降级人工审核**」，三层机制各管一件事：

| 机制          | 管什么                | 默认值        |
| ----------- | ------------------ | ---------- |
| 服务端设备指纹     | 同一台设备只能有一个**可用**账号 | 开          |
| 同 IP 注册配额   | 同网络批量注册            | 24 小时内 3 个 |
| 24h 无凭据自动清理 | 误注册 / 占位注册         | 24 小时      |

细节：服务端从**请求头**算出设备指纹（HMAC 后存 `users.fp_hash`，唯一索引），前端上报的指纹只做参考、不作数——所以改前端字段绕过不了。命中指纹或配额时**不直接拒绝**，而是落成 `pending` 走人工审核（避免公司/校园/CGNAT 这类共享出口被误伤）；但同一设备已有待审申请时会直接拒绝，防止待审记录被刷屏。**每次注册都会给管理员推一条消息**，管理员在顶栏「用户管理」里处理。

配置项在 `data/workbench.json` 的 `registration` 段：`auto_approve` / `ip_quota` / `ip_window_hours` / `server_fingerprint`。

### 删除用户 = 彻底删除

管理员点「删除」会一并移除：

- `data/users/u<id>/` 整个工作空间（已上传的凭据、日志、归档）
- 该用户的**全部定时任务**（`user_settings.schedules_json`，随外键 `ON DELETE CASCADE`）
- 该用户在调度器里的**触发残留**（`runtime/scheduler-fired.json` 与 `runtime/slots/` 下按账号分桶的键）

**不可恢复**，前端确认框会把要删的东西列清楚。若该用户正有签到任务在跑，会**拒绝删除**并提示等任务结束 —— 否则 Windows 删不动、Linux 删出半截目录，脚本还会继续写并向已删账号发通知。

「停用 / 拒绝」则只改状态，目录原样保留。

> [!NOTE]  
> 旧版本的「删除用户」只删库不删盘，可能残留孤儿目录。启动日志会提示有多少个，执行 `python server.py --purge-orphans` 可清理。安全阀：数据库里一个账号都没有时**拒绝执行**，防止换库后误删整盘。

---

## 🧩 内置的业务脚本

脚本也都能脱离服务端单独跑，方便调试。

<details>

<summary><b>WorkBuddy · <code>apps/workbuddy/signin.py</code></b>（点开看全部命令）</summary>

```bash
python apps/workbuddy/signin.py auto           # 签到 + 成长中心（默认命令）
python apps/workbuddy/signin.py silent         # 同 auto，但结果写日志文件而非 stdout
python apps/workbuddy/signin.py growth         # 只跑成长中心，不签到
python apps/workbuddy/signin.py silent-poll    # 轮询：未签才补签 + 成长中心，空跑不落盘
python apps/workbuddy/signin.py silent-growth  # silent-poll 的旧名，行为相同
python apps/workbuddy/signin.py status         # 只查签到状态（调试）
python apps/workbuddy/signin.py claim          # 只领取签到（调试，幂等）
python apps/workbuddy/signin.py all            # 查状态 + 领取
python apps/workbuddy/signin.py doctor         # 离线检查凭据格式与运行时能力，不解密、不联网
```

成长中心包含：旅行礼物、派 Buddy、领取新任务、领任务奖励、断登自动补登、连登奖励兑换、开盲盒、能量开 Buddy 盲盒。  
服务端跑多账号时用的是 `multi_run.py`（扫目录逐账号执行 + 独立锁 + 逐账号超时），单账号会被单独指给 `signin.py`。

</details>

<details>

<summary><b>TRAE · <code>apps/trae/trae_signin.py</code></b>（点开看全部命令与参数）</summary>

```bash
python apps/trae/trae_signin.py silent          # 默认命令：签到 + 续期，写日志
python apps/trae/trae_signin.py silent-poll     # 轮询：未签才补签
python apps/trae/trae_signin.py status          # 只查状态
python apps/trae/trae_signin.py doctor          # 离线自检
```

常用参数：`--dir`（凭据目录）· `--file`（单个凭据）· `--log-dir` · `--multi-log` · `--order`（账号顺序）· `--gap`（账号间隔秒）· `--timeout` · `--retry` / `--retry-delay` · `--dry-run`（不发签到请求）。

TRAE 侧额外包含 `trae_crypto.py`（纯 Python 实现的 AES-128 解密，用来读桌面端凭据）与 `trae_ecdsa.py`（续期请求的 ECDSA 签名），都只依赖标准库。

</details>

---

## ⚙️ 工作原理

**一个进程，三条线程**（`outputs/server.py`）：

```
HTTP 服务      ThreadingHTTPServer → DashboardHandler（API + 静态文件）
调度器         scheduler_loop：每 20 秒对表，命中就拉起脚本；文件锁 + 占位文件双重去重
账号清理       purge_loop：每小时扫一次，清理注册后从未上传凭据的空壳账号
```

**一次签到的完整链路**：

1. 调度器命中时间点 → 汇总该用户的账号、时间表、通知配置
2. 通过 `subprocess` 拉起 `apps/<产品>/` 下的脚本，注入该用户工作区的路径与该用户的通知环境变量
3. 脚本读写**该用户自己**的凭据与日志（`data/users/u<id>/`），结果以一行 JSON 汇报
4. 服务端解析日志、更新面板状态，并按用户通知配置决定这一条要不要推、推给谁

**存储布局**：

```
outputs/
├── server.py                 HTTP API、认证、调度器、静态文件服务
├── static/                   前端页面与静态资源
├── apps/                     WorkBuddy / TRAE 业务脚本与导出器
├── deploy/                   systemd 单元与一键安装脚本
├── data/                     workbench.json（配置）+ dailyhub.sqlite3（用户库）+ users/（各用户工作区）
├── runtime/                  调度锁、触发记录、任务历史
├── archive/                  归档账号
└── 系统部署说明.html          更详细的部署与配置说明
```

**认证与传输**：密码用 PBKDF2-SHA256（260,000 次迭代）存哈希；会话是 HMAC 签名的 Cookie（`HttpOnly` / `SameSite=Lax`，有效期 7 天，`Secure` 由 `DASHBOARD_COOKIE_SECURE` 控制）。前端与后端之间还有一层自建的信封加密（`v2`：16 字节 nonce + HMAC-SHA256 计数器密钥流 + 带 AAD 的 HMAC tag，AAD 为 `"METHOD path"`），密钥在握手时下发，会话后由浏览器 WebCrypto 在内存里使用。

> [!NOTE]  
> **上游接口是逆向来的。** 两套业务都直接打官方客户端用的同一个 endpoint，接口随时可能变；项目本身不碰平台规则，只做「本地脚本 + 定时执行」这一层。请自行评估风险。

---

## 🔧 配置

### 环境变量

| 变量                          | 作用                                      | 默认                |
| --------------------------- | --------------------------------------- | ----------------- |
| `DASHBOARD_HOST`            | 监听地址                                    | `0.0.0.0`         |
| `DASHBOARD_PORT`            | 监听端口                                    | `8000`            |
| `DASHBOARD_TZ`              | 调度与展示时区                                 | `Asia/Shanghai`   |
| `DASHBOARD_COOKIE_SECURE`   | 置 `1` 让会话 Cookie 带 `Secure`（HTTPS 部署时开） | 关                 |
| `SIGNIN_ROOT`               | 业务脚本根目录（改这里就能把 `apps/` 放到别处）            | 与 `server.py` 同目录 |
| `PURGE_NO_CREDENTIAL_HOURS` | 注册后多少小时无凭据就清理                           | `24`              |
| `PURGE_INTERVAL_SECONDS`    | 清理线程运行周期（秒）                             | `3600`            |
| `PYTHONUNBUFFERED`          | 置 `1` 让日志实时进 journald                   | 单元里已设             |


> [!WARNING]  
> **不要在 systemd 单元里设 `NOTIFY_CHANNEL` / `WB_NOTIFY_CHANNEL` 之类的通知变量。**  
> 通知配置的优先级是「环境变量 > 配置文件」，一旦设了，面板上的通知开关就再也管不住了（改了也不生效）。要配通知请走面板，或直接改 `apps/*/config/notify.json`。

### 命令行参数

```bash
python server.py --host 0.0.0.0 --port 8000   # 覆盖监听地址与端口
python server.py --check                      # 只输出一次状态 JSON，不启动服务
python server.py --purge-orphans              # 清理「库里已无账号」的孤儿工作空间目录
```

### `data/workbench.json`

配置的兜底默认值（`auth` 之外，每个用户的实际配置存在用户库的 `user_settings` 表里）：

| 段               | 关键字段                                                                                        |
| --------------- | ------------------------------------------------------------------------------------------- |
| `auth`          | `username` / `password`（仅首次启动播种）/ `secret`（留空则自动生成）                                         |
| `schedules`     | 每个产品的 `enabled` / `daily_time` / `daily_mode` / `poll_enabled` / `poll_times` / `poll_mode` |
| `notifications` | 每个产品的 `channel` / `key` / `url` / `on` / `group` / `enabled`                                |
| `registration`  | `auto_approve` / `ip_quota` / `ip_window_hours` / `server_fingerprint`                      |
| `crypto`        | `enabled`：是否启用前后端信封加密                                                                       |
| `app_secret`    | 信封加密密钥，首次使用时自动生成并写回本文件                                                                      |

---

## 🧪 排错

| 现象                                    | 处理                                                                                    |
| ------------------------------------- | ------------------------------------------------------------------------------------- |
| 打不开面板 / 连接被拒                          | 服务没起来或端口没放行：`systemctl status checkinops`、`journalctl -u checkinops -n 50`，再确认防火墙与安全组 |
| 登录成功但立刻掉线                             | HTTPS 页面下 Cookie 缺 `Secure` 被浏览器丢弃。设 `Environment=DASHBOARD_COOKIE_SECURE=1` 后重启      |
| 改了 `workbench.json` 的密码却不生效           | 播种只在首次启动发生。按 [部署章节](#还有两步) 的流程删库重来（会清用户）                                              |
| 定时任务到点没跑                              | ① 服务是否在跑（调度跟进程走）② 面板上的时间表是否启用 ③ `journalctl` 里有没有 `scheduler` 相关行                     |
| 服务器上同时装了旧 crontab                     | 删掉 `/etc/cron.d/trae-signin`、`workbuddy-signin`，否则重复执行                                |
| 面板看不到某次轮询记录                           | 空跑（已签过 / 名额用完）默认不落盘，属正常，避免淹没有效记录                                                      |
| 账号一直显示「待上传凭据」                         | 该账号还没跑过有效签到；若注册后超过 24 小时仍未上传，账号会被自动清理，重新注册即可                                          |
| 注册后提示需要审核                             | 命中了设备指纹或同 IP 配额（共享网络常见）。等管理员在「用户管理」里通过；每次注册都会通知管理员                                    |
| 面板查不到某个账号                             | 账号被归档了，在「账号」页切到归档清单恢复                                                                 |
| 删除用户失败                                | 该用户当前有签到任务在跑，脚本会拒绝删除以免删出半截目录；等任务结束再删                                                  |
| 启动日志提示孤儿工作空间目录                        | 旧版本只删库不删盘的遗留。`python server.py --purge-orphans` 清理（库里没账号时会被安全阀拦住）                     |
| 通知发了但收不到                              | 到「设置 → 通知」点「发送测试消息」定位；不同渠道的 Key 不通用（例如 Server酱两代产品端点与 SendKey 都不同），按前缀选对渠道            |
| TRAE 凭据导入后失效                          | TRAE 凭据与机器码环境相关，请在**导出它的那台机器**上使用；换机器需要重新导出                                           |
| 命令退出码非 0、结果里带 `needs_attention: true` | 该账号需要人工处理（凭据失效 / 被限流等），原始返回可直接看 `.log`                                                |

---

## 🔐 安全与隐私

- 凭据由**导出工具在本机读取**，服务端只保存脚本运行所需的会话文件；`data/`、`runtime/`、账号工作区默认不进版本库（见 `.gitignore`）
- 密码只存 PBKDF2-SHA256 哈希（260,000 次迭代），会话 Cookie 带 `HttpOnly` + `SameSite=Lax`
- 用户之间严格隔离：每个用户只读写 `data/users/u<id>/`，管理员看板是只读视图
- **凭据托管本身就是风险**：把会话凭据交给一台服务器，就多了一个被攻破的点。请只在**自己的、可信的**机器上部署，别把面板裸露在公网而不做 HTTPS 与访问控制
- 前后端那层信封加密防的是**被动旁观与日志留痕**（明文不出现在抓包与访问日志里）；它不防能够直接调用 API 的主动攻击者 —— 真正的边界是 HTTPS、Cookie 与面板本身的访问控制

> [!WARNING]  
> **部署前请检查这三件事**：① 默认管理员密码是否已改；② 是否已用 HTTPS 反代；③ 是否误把真实凭据 / 用户库 / 日志提交进了公开仓库（下一节有清单）。

更多安全策略与漏洞上报方式见 [SECURITY.md](SECURITY.md)。

---

## ⚠️ 免责声明

> [!CAUTION]  
> 本项目为**非官方**工具，与腾讯、WorkBuddy、TRAE 及其关联方**没有任何隶属或合作关系**。项目名称仅用于说明接入场景。  
> 签到接口系从各产品桌面端**逆向**得到，可能随时变动且不另行通知。  
> 本项目**仅用于技术学习、研究与交流**，请遵守相关平台协议、服务条款与法律法规，**不要**用于绕过平台规则、批量滥用、侵权或任何未授权用途。  
> 凭据托管存在账号安全与风控风险，**部署与使用后果由使用者自行承担**。

---

## 📦 发布到 GitHub 之前

DayPilot 会处理本地凭据、通知 Key、SQLite 用户库和运行日志。**推送之前**请确认没有把真实运行数据带上去。

`.gitignore` 默认忽略，但仍请人工确认以下几类**不要**提交：

- `outputs/data/dailyhub.sqlite3` —— 用户库（含密码哈希）
- `outputs/data/users/` —— 各用户工作区（**含凭据**）
- `outputs/runtime/` —— 任务历史与日志
- `outputs/archive/` —— 归档账号
- `outputs/apps/**/sessions*/`、`outputs/apps/**/logs/` —— 凭据与日志
- `outputs/static/downloads/*.exe` —— 本地打包的二进制（建议挂 Releases）

仓库需要保留的**样例文件只有三个**，且必须是**空密钥**的脱敏内容：

- `outputs/data/workbench.json`（`auth.password` 与 `app_secret` 都要清干净）
- `outputs/apps/workbuddy/config/notify.json`
- `outputs/apps/trae/config/notify.json`

> [!CAUTION]  
> `data/workbench.json` 是**被 git 跟踪**的（`.gitignore` 用 `!` 显式放行了它），而它在首次启动时会写回自动生成的 `app_secret`。改过默认密码再启动过服务的话，**这个文件里就躺着你的真实密码** —— 提交前逐行看一眼。

发布前清单见 [docs/GITHUB\_RELEASE\_CHECKLIST.md](docs/GITHUB_RELEASE_CHECKLIST.md)。

---

## 🤝 贡献

欢迎 Issue、PR 和改进建议：

- 提交前请先读 [CONTRIBUTING.md](CONTRIBUTING.md)
- Bug 与需求请用 [Issue 模板](.github/ISSUE_TEMPLATE)，附上复现步骤、系统环境与相关日志（**记得把凭据与通知 Key 涂掉**）
- 新增或修改业务脚本时，请保持「标准库优先」——本项目的零依赖是刻意选择

## 📚 文档索引

| 文档                                                                     | 内容                          |
| ---------------------------------------------------------------------- | --------------------------- |
| [`outputs/系统部署说明.html`](outputs/系统部署说明.html)                           | 完整部署手册：前置条件、五步流程、定时与通知、运维排错 |
| [`outputs/deploy/README.md`](outputs/deploy/README.md)                 | 部署脚本用法、数据保护清单、安装后必做项        |
| [`CONTRIBUTING.md`](CONTRIBUTING.md)                                   | 贡献流程与代码规范                   |
| [`SECURITY.md`](SECURITY.md)                                           | 安全策略与漏洞上报                   |
| [`docs/GITHUB_RELEASE_CHECKLIST.md`](docs/GITHUB_RELEASE_CHECKLIST.md) | 发布前检查清单                     |
| [`NOTICE.md`](NOTICE.md)                                               | 第三方资源与许可                    |

## 📄 协议

[MIT License](LICENSE) © 2026 Eason-shu。第三方资源（Pico.css、Tailwind CSS browser build）的许可详见 [NOTICE.md](NOTICE.md)。

---

## ☕ 请作者喝杯咖啡

DayPilot 以 MIT 协议开源，所有功能都在这个仓库里，没有需要付费才能解锁的部分。**如果它对你确实有用，可以请作者喝杯咖啡** —— 完全自愿，不打赏也照常更新。

<table>
  <tr>
    <td align="center">
      <img src="outputs/static/assets/donate-wechat-v1.png" height="320" alt="微信收款码"><br>
      <sub>微信支付</sub>
    </td>
    <td align="center">
      <img src="outputs/static/assets/donate-alipay-v1.jpg" height="320" alt="支付宝收款码"><br>
      <sub>支付宝</sub>
    </td>
  </tr>
</table>

> [!TIP]  
> 打赏并不是支持项目最有效的方式 —— **点个 Star、提个 Issue、把踩到的坑写清楚**，对项目的帮助往往更大。

---

## 📊 Star History

![Star History Chart](https://api.star-history.com/svg?repos=Eason-shu/DayPilot\&type=Date)

**如果这个项目帮你把签到这件事彻底忘了 —— 点个 ⭐ Star 吧。**

一秒的事，却能在上游接口哪天变了、脚本悄悄失灵时，让你还找得到回来的路。
