# Security Policy

## 部署基线

- 必须替换 `JWT_SECRET_KEY`、`ENCRYPTION_KEY`、`ADMIN_PASSWORD` 和 `ADMIN_PATH`。
- 生产环境将 `CORS_ORIGINS` 配置为明确的 HTTPS 来源，不要使用 `*`。
- 建议在反向代理启用 TLS、请求体大小限制和速率限制，并配置
  `PROXY_SHARED_SECRET` 防止绕过受信任代理。
- 保持 `ALLOW_LOCAL_LOGIN_LOCKOUT_BYPASS=false`；反向代理可能让所有请求看起来来自本机。
- API key 优先通过 `Authorization: Bearer` 或 `x-api-key` 传递。查询参数可能进入
  浏览器历史、代理日志和监控系统，仅为 Gemini/浏览器兼容保留。
- 不要启用生产环境请求调试捕获；调试文件可能包含模型名、工具名和会话结构摘要。

## 工具调用信任边界

本服务只生成结构化工具调用，不执行工具。Agent 客户端必须在执行文件、Shell、网络
或 MCP 工具前自行进行权限检查、路径隔离和用户确认。工具描述、工具结果和上游模型
输出都应视为不可信输入。

## 报告漏洞

请通过 GitHub Security Advisory 私下报告，并附上受影响版本、复现步骤和影响范围。
请勿在公开 issue 中提交 API key、StackAI 凭据、数据库或请求日志。
