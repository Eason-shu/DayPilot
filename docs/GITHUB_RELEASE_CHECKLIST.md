# GitHub 发布前清单

这份清单用于把 DayPilot 发布到 GitHub 前做最后检查，重点是避免泄露真实凭据、数据库、通知密钥和运行日志。

## 1. 清理敏感数据

确认以下路径没有被提交：

- `outputs/data/dailyhub.sqlite3`
- `outputs/data/users/`
- `outputs/runtime/`
- `outputs/archive/`
- `outputs/apps/**/sessions*/`
- `outputs/apps/**/trae-sessions*/`
- `outputs/apps/**/logs/`
- `outputs/static/downloads/*.exe`

推荐检查：

```bash
git status --short
git check-ignore -v outputs/data/dailyhub.sqlite3
git check-ignore -v outputs/apps/workbuddy/sessions/shu.json
git diff -- outputs/data/workbench.json outputs/apps/workbuddy/config/notify.json outputs/apps/trae/config/notify.json
```

## 2. 检查默认配置

- `outputs/data/workbench.json` 只作为示例配置提交。
- 不要提交真实管理员账号密码。
- 不要提交真实 `app_secret`、`auth.secret`。
- 不要提交真实通知 Key、Webhook URL。
- 不要提交真实用户数据。

`server.py` 会在首次启动时补齐空的运行密钥。公开仓库只保留空秘钥示例，不带真实运行数据。

## 3. 检查文档

- 根目录 `README.md` 能说明项目是什么、怎么运行、风险是什么。
- `outputs/README.md` 能说明部署和配置。
- `SECURITY.md` 说明敏感数据和安全报告方式。
- `CONTRIBUTING.md` 说明如何本地开发和提交 PR。
- `LICENSE` 已经确认是你想使用的开源许可证。

## 4. 检查程序

```bash
cd outputs
python -m py_compile server.py
python server.py --check
```

如果你改过前端，请手动验证：

- 登录 / 注册 / 退出登录
- 账号导入、详情、删除、归档
- 时间计划保存
- 通知配置保存与测试消息
- 执行记录与 `.log` 原始日志查看
- 管理员用户审核

## 5. 发布建议

- 仓库只提交源码、静态资源和文档。
- 导出工具 `.exe` 更适合作为 GitHub Release 附件发布。
- 真实服务器部署请启用 HTTPS。
- 在 README 顶部放置项目封面图：`outputs/static/assets/daypilot-promo-cover-v1.png`。
