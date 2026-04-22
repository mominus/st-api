# Changelog

All notable changes to this project will be documented in this file.

## v1.4.0 - 2026-04-22

- 相对 `9b9c7efefad8077413e83085095f6a8f684ddb91`，重构 Anthropic / Claude Code 主链路：`protocol_bridge` 保留 `anthropic_beta`、`thinking`、`metadata` 与原始 `messages`，`gateway_runtime` 支持按 `input_mapping` 透传 `system_prompt`、`chat_history`、`max_tokens`、`temperature`、`tool_choice`、`thinking`、`metadata`、`anthropic_beta`，Anthropic router 会按请求自动选择普通流、thinking 流或工具流，并在 `claude-code-20250219` 下启用工具优先早停。
- 重构工具调用与 Anthropic 响应重组：补齐 `tool_result` 结构校验、declared-only 工具约束、fenced JSON / XML / bracket / plain JSON 四类工具识别、增量 `input_json_delta` 输出、thinking block 重建与流式 `event: error`，减少 Claude Code 工具状态机误判。
- 新增 Gateway 侧历史预算与上下文压缩：OpenAI / Anthropic / Gemini 入口统一接入 `history_budget`，可按 token 预算裁剪旧消息，尽量保留最近轮次及 `tool_use` / `tool_result` 配对，并补充 `GATEWAY_HISTORY_BUDGET_*` 环境变量。
- 新增 Claude Code 能力矩阵：`model_groups` 支持 `capability_overrides`，管理后台模型组列表与 `GET /v1/key/info` 响应增加 `capability_matrix`，用 `native` / `simulated` / `unsupported` 标注能力边界，后台页面同步展示。
- 重构数据库与连接配置：抽出 `_build_async_engine_kwargs`，SQLite 不再误带 queue pool 参数，PostgreSQL 支持连接 / 命令 / statement / lock timeout 与 `application_name` 配置，并补上 `model_groups.capability_overrides` 迁移。
- 新增发布前验证工具链：增加真实 Claude CLI smoke、统一 regression gate、生成产物清理脚本，并让 `scripts/release.py` 支持 `--run-claude-code-gate` 与 `--gate-*` 参数，在改写版本文件前先跑验收。
- 补充回归测试与文档：新增 Anthropic stream progress、capability matrix、history budget、release helper、database engine 配置测试，并更新 README、`.env.example` 与相关运维说明。

## v1.3.0 - 2026-03-31

- 重构高并发账号路由与选号逻辑：优先选择额度充足、`daily_used` 更低、当前 `inflight` 更低的活跃账号，并减少继续轮询到已耗尽账号的情况
- PostgreSQL 账号选择链路升级为并发安全实现，支持 `FOR UPDATE SKIP LOCKED`、账号请求占位租约、暂时性错误冷却与 failover 重试
- 新增高并发账号路由索引、日志查询索引和 PostgreSQL 连接预算检查脚本，优化几百到上千账号池下的 SQL 路径与连接配置
- 请求并发、流式并发、数据库并发拆分为独立闸门与统计维度，增加请求/流式/DB 排队时延、拒绝数、P95/P99 等性能指标
- 新增异步使用量聚合与后台日志异步落库，降低 `request_logs`、`call_logs`、`system_stats` 对主请求路径的阻塞
- 后台新增性能监控关键指标展示：排队与限流、上游 HTTP 连接池、usage aggregation、background logs，并补充手机端和平板端响应式适配
- 新增 API Key 成本重算能力，支持单个 Key 和全部 Key 的历史调用成本追溯重算
- 改进上游错误映射：上游 `402` 明确映射为 `quota_exceeded`，上游 `5xx` / 超时 / 连接错误统一收口为更清晰的服务不可用语义
- 默认隐藏用户侧错误 `details`，同时保留后台结构化日志；新增上游域名、邮箱、工具参数、流式事件的统一脱敏，避免向用户暴露上游站点信息
- 增强 Claude Code / Tool Use 压测与回归能力，新增多轮工具调用压测脚本、真实 Claude CLI 并发压测脚本和多组高并发回归测试
- 调整 Claude Code 对话压测默认参数：客户端超时提升到 `180s`，默认 `max_steps=24`，减少客户端假超时导致的误判
- 重整 `.env.example` 与 README，按“最小必填 / PostgreSQL 47 连接起步值 / 并发 / 上游 HTTP / 异步落库 / Tool Use”重新组织配置

## v1.2.0 - 2026-03-26

- cc内置工具的全面支持
- cli工具调用增加 bracket 风格
- Cherry Studio MCP支持

## v1.1.1 - 2026-03-24

- 单账号多模型路由(与v1.1.0版本一致)
- 之前未提交功能代码，仅发布release

## v1.1.0 - 2026-03-24

- 新增“单账号 + 多模型 + 模型注入”
- 新增映射字段，传入request.model进行模型路由选择

## v1.0.11 - 2026-03-24

- 修改Token计算，直接获取真实的Token数据，新增统一Token解析器。优先：后端返回或流式 chunk 中的真实 usage；其次：analytics run_id usage；最后：估算 fallback（不再固定 1:2 拆分）

## v1.0.1 - 2026-03-24

- 批量导入支持无[] JSON及llm_models自动创建模型组
- 新增Claude llm_models规范化并去除Anthropic前缀
- 新增CHANGELOG/VERSION和发布脚本

## v1.0.0 - 2026-03-24

- Initial release baseline.
