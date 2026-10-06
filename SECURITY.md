# 安全策略

DayPilot 会处理用户导出的登录凭据、通知渠道密钥、执行日志和本地数据库。请把它当作敏感系统部署。

## 支持范围

当前以 `outputs/` 内主线代码为支持范围。历史脚本、个人运行数据、已归档凭据和本地构建产物不属于公开支持范围。

## 敏感数据

请不要提交或公开：

- `outputs/data/dailyhub.sqlite3`
- `outputs/data/users/`
- `outputs/runtime/`
- `outputs/archive/`
- `outputs/apps/**/sessions*/`
- `outputs/apps/**/logs/`
- 任何 `.json` 凭据、refreshToken、Cookie、Webhook URL、Server酱 Key、飞书/企微/Bark Key

仓库中的 `outputs/data/workbench.json` 和 `outputs/apps/**/config/notify.json` 只能作为空秘钥示例存在。提交前请确认 `auth.secret`、`app_secret`、通知 Key、Webhook URL 均为空。

## 部署建议

- 公网部署必须启用 HTTPS。
- 推荐使用 Nginx 或 Caddy 反代到 `127.0.0.1:8000`。
- 修改默认管理员密码。
- 限制服务器文件权限，只让运行用户读写应用数据目录。
- 定期备份 `outputs/data/`，但不要把备份推送到公开仓库。
- 开启反代真实 IP 后，再配置限流读取 `X-Forwarded-For`。

## 报告安全问题

如果你发现安全问题，请不要在公开 Issue 中贴出真实凭据、密钥、数据库、日志或完整攻击细节。

建议报告内容：

- 影响范围
- 复现步骤
- 可能的风险
- 建议修复方向
- 已脱敏的日志或截图

## 免责声明

本项目仅用于技术学习与研究。使用者应自行确认其使用行为符合相关平台规则与法律法规。
