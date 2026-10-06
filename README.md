# DayPilot

![DayPilot 宣传封面](outputs/static/assets/daypilot-promo-cover-v1.png)

DayPilot 是一个用于技术学习与研究的自动签到工作台。它把本地脚本、凭据托管、定时执行、消息通知、用户审核和运行日志收进一个可部署的 Web 控制台里，让原本分散的自动签到流程更容易维护。

> 重要声明：本项目仅用于技术学习、研究与交流。请遵守相关平台协议、服务条款和法律法规，不要用于绕过平台规则、批量滥用、侵权或任何未授权用途。凭据托管存在账号安全与风控风险，部署与使用后果由使用者自行承担。

## 功能特性

- **账号托管**：支持 WorkBuddy / TRAE 凭据导入、启用、停用、归档、删除。
- **定时执行**：内置 Scheduler，不依赖 crontab，可在网页配置每日签到和轮询时间。
- **消息通知**：支持 Server酱、飞书、企业微信、Bark、Webhook 等渠道，并提供测试消息。
- **多用户管理**：支持注册、管理员审核、用户数据隔离。
- **管理员总览**：管理员可查看所有用户的托管账号和通知配置状态。
- **运行日志**：支持查看执行历史和 `.log` 原始日志。
- **零 Python 第三方运行依赖**：服务端基于 Python 标准库实现。

## 项目结构

```text
.
├── README.md                 # GitHub 首页说明
├── LICENSE                   # 开源许可证
├── CONTRIBUTING.md           # 贡献指南
├── SECURITY.md               # 安全策略
├── docs/
│   └── GITHUB_RELEASE_CHECKLIST.md
└── outputs/                  # 完整可部署项目
    ├── server.py             # HTTP API、认证、调度器、静态文件服务
    ├── static/               # 前端页面与静态资源
    ├── apps/                 # WorkBuddy / TRAE 业务脚本
    ├── deploy/               # systemd 部署脚本与服务模板
    ├── data/                 # 运行数据，禁止提交真实数据
    ├── runtime/              # 运行时锁、历史、任务输出
    └── README.md             # 更详细的部署与配置说明
```

## 快速开始

本仓库真正的应用目录是 `outputs/`。

```bash
cd outputs
python server.py --host 127.0.0.1 --port 8000
```

浏览器访问：

```text
http://127.0.0.1:8000
```

默认管理员账号：

```text
账号：admin
密码：admin123456
```

首次部署到公网前务必修改默认密码，并启用 HTTPS。

## 服务器部署

在 systemd Linux 服务器上进入 `outputs/` 目录执行：

```bash
sudo sh deploy/install.sh
```

常用命令：

```bash
systemctl restart checkinops
journalctl -u checkinops -f
journalctl -u checkinops -n 50 --no-pager
```

更完整的部署说明见：

- [outputs/README.md](outputs/README.md)
- [outputs/deploy/README.md](outputs/deploy/README.md)
- [outputs/系统部署说明.html](outputs/系统部署说明.html)

## GitHub 发布前请检查

本项目会处理本地凭据、通知 Key、SQLite 用户库和运行日志。发布前请务必确认没有把真实运行数据提交到 GitHub。

重点不要提交：

- `outputs/data/`
- `outputs/runtime/`
- `outputs/archive/`
- `outputs/apps/**/sessions*/`
- `outputs/apps/**/logs/`
- `outputs/apps/**/config/notify.json`
- `outputs/static/downloads/*.exe`

发布前清单见 [docs/GITHUB_RELEASE_CHECKLIST.md](docs/GITHUB_RELEASE_CHECKLIST.md)。

## 安全说明

- 公网部署请使用 Nginx/Caddy 反代并开启 HTTPS。
- 不要在公开仓库提交真实账号凭据、通知密钥、数据库文件或日志。
- 默认密码仅用于本地首次启动，公网部署前必须修改。
- TRAE 凭据与机器码环境有关，请仅在可信机器上导出。

更多安全策略见 [SECURITY.md](SECURITY.md)。

## 贡献

欢迎提交 Issue、PR 或改进建议。请先阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

本项目使用 [MIT License](LICENSE) 开源。第三方资源与依赖的许可证请以其原项目为准。

