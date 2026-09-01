
# st-api API 网关

一个统一的 API 网关服务，提供与 OpenAI、Anthropic、Gemini 兼容的接口。

## 功能特性

- 支持多种 API 协议格式（OpenAI、Anthropic、Gemini）
- 支持流式响应（SSE）
- API Key 管理
- 用量跟踪与额度控制
- 管理后台

## 接口列表

- `POST /v1/chat/completions`：OpenAI 兼容接口
- `POST /v1/responses`：OpenAI Responses API 兼容接口
- `POST /v1/messages`：Anthropic 兼容接口
- `POST /v1beta/models/{model}:generateContent`：Gemini 兼容接口
- `GET /v1/key/info`：查询当前 API Key 可用模型、状态、用量和剩余额度
- `GET /health`：健康检查

三套主流 Agent 协议均支持函数/工具定义、强制工具选择、并行工具调用结果和多轮
`tool_call` / `tool_result` 上下文。详细兼容格式与限制见
[工具调用兼容说明](./docs/TOOL_CALLING.md)。安全部署前请阅读 [安全策略](./SECURITY.md)。
并发扩容与是否迁移 Go 的技术决策见 [架构说明](./docs/ARCHITECTURE.md)。

## Claude Code 兼容性

为了尽可能贴近 Claude Code / Anthropic 的行为，推荐使用 model-group 的 `input_mapping`，让结构化请求上下文保持分离，而不是把所有内容压平成一个 prompt 字段。

推荐映射结构：

```json
{
  "user_input": "in-0",
  "system_prompt": "in-1",
  "chat_history": "in-2",
  "model_id": "in-3",
  "max_tokens": "in-4",
  "temperature": "in-5",
  "tool_choice": "in-6",
  "thinking": "in-7",
  "metadata": "in-8",
  "anthropic_beta": "in-9"
}
```

运行说明：

- `user_input` 是唯一的硬性必填项。
- 对 Claude Code 场景，强烈建议同时提供 `system_prompt` 和 `chat_history`，这样在退化成纯文本渲染之前，Anthropic 的工具轮次和更早的 assistant 上下文能保留得更久。
- `max_tokens`、`temperature`、`tool_choice`、`thinking`、`metadata`、`anthropic_beta` 都是可选透传字段，只有当上游工作流暴露了对应输入时才需要映射。
- 当 `anthropic-beta` 包含 `claude-code-20250219` 时，网关会优先走 Claude Code 工具流式路径。
- 在 Anthropic 工具路径上，网关只会把当前请求里显式声明过工具名的 JSON / XML / 方括号工具调用升级为 `tool_use`；未声明的“像工具调用的文本”会保留为普通文本，不会被错误发成 `tool_use`。
- `tool_choice` 目前以“尽力而为”的提示词引导方式生效（`required`、具名工具、`none`、`auto`），而不是上游原生的硬约束。
- `thinking` 通过 `<thinking>...</thinking>` 这类文本标签做尽力重建，并不是 Anthropic 原生的 reasoning 支持。
- 当 `tools` 和 `thinking` 同时存在时，当前实现会优先保证工具状态机行为正确。
- 网关现在会在渲染上游 prompt 之前先应用服务端历史预算；较早的对话轮次可能会在请求发出前被压缩掉。
- 历史预算相关环境变量：
  - `GATEWAY_HISTORY_BUDGET_ENABLED=true|false`
  - `GATEWAY_HISTORY_BUDGET_TOKENS=120000`
  - `GATEWAY_HISTORY_COMPACT_MAX_CHARS=1200`
  - `GATEWAY_HISTORY_COMPACT_RECENT_MESSAGES=4`
- 管理后台的 model-group 响应以及 `GET /v1/key/info` 现在都会返回一个面向 Claude Code 场景推导出的 `capability_matrix`。
- `capability_matrix` 使用以下稳定状态值：
  - `native`：通过 `input_mapping` 直接透传到上游工作流输入
  - `simulated`：由网关重建行为，或通过提示词进行能力引导
  - `unsupported`：当前 StackAI 路径未暴露为受支持的 Anthropic 兼容能力
- 管理后台的 model-group 创建 / 更新接口现在也接受可选的 `capability_overrides` JSON，用于极少数特殊工作流。
  只有当上游工作流确实具备比默认网关假设更强的原生支持时才应使用。
  不要把仅依赖提示词模拟或文本重建的行为标记为 `native`。

真实 Claude CLI 冒烟测试：

- 使用 [run_local_real_claude_cli_smoke.sh](./scripts/run_local_real_claude_cli_smoke.sh) 验证完整的 `claude` CLI -> `st-api` -> Anthropic `/v1/messages` 链路，提示词设计为应在单轮内收敛。
- 当 `ST_API_KEY` 未设置时，脚本会先启动网关，再通过管理 API 创建一个临时 API Key，执行一次真实 `claude` 请求，校验 JSON 结果载荷，最后删除这个临时 key。
- 成功时会打印 `gate_status=PASS`；失败时会打印 `gate_status=FAIL`，并附带失败阶段与原因。
- 脚本还会把结构化 gate 报告写入 `data/stress_reports/real_claude_cli_smoke_report_*.json`。
- 默认要求回答中同时包含 `Vue` 和 `Vite`。如果你故意修改了 smoke prompt，可通过 `EXPECTED_SUBSTRINGS=...` 覆盖。
- 默认前置条件：
  - 已安装 `claude` CLI，且在 `PATH` 中可用
  - 管理后台凭据可通过 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 提供
  - 已存在模型组 `claude-opus-4-6`，否则请通过 `MODEL=...` 覆盖

示例：

```bash
bash scripts/run_local_real_claude_cli_smoke.sh
```

常用覆盖参数：

```bash
MODEL=claude-opus-4-6 \
PROMPT='@BookmarkVault 仅用最少工具，告诉我这个项目主要使用什么前端框架和构建工具。回答控制在3行内，不要继续扩展。' \
EXPECTED_SUBSTRINGS='Vue,Vite' \
CLAUDE_TIMEOUT_SECONDS=180 \
bash scripts/run_local_real_claude_cli_smoke.sh
```

统一 Claude Code 回归闸门：

- 使用 [run_claude_code_regression_gate.sh](./scripts/run_claude_code_regression_gate.sh) 执行一条命令的验收。
- 该脚本会先运行聚焦的 Claude Code 兼容性 pytest 测试集，然后再运行真实 Claude CLI smoke gate。
- 顶层 gate 报告会写入 `data/stress_reports/claude_code_regression_gate_*.json`。
- 如果只想执行协议 / 单元回归部分，可设置 `SKIP_REAL_CLI_SMOKE=1`。
- 如需在 gate 成功后清理旧的 Claude Code 生成产物，可设置 `AUTO_CLEANUP=1`。
- 使用 `AUTO_CLEANUP_KEEP_LATEST=N` 控制保留数量，使用 `AUTO_CLEANUP_DRY_RUN=1` 先预览而不删除。

示例：

```bash
bash scripts/run_claude_code_regression_gate.sh
```

```bash
AUTO_CLEANUP=1 AUTO_CLEANUP_KEEP_LATEST=2 bash scripts/run_claude_code_regression_gate.sh
```

```bash
SKIP_REAL_CLI_SMOKE=1 AUTO_CLEANUP=1 AUTO_CLEANUP_DRY_RUN=1 \
bash scripts/run_claude_code_regression_gate.sh
```

生成产物清理：

- 使用 [cleanup_claude_code_artifacts.sh](./scripts/cleanup_claude_code_artifacts.sh) 只清理自动生成的 Claude Code 校验产物。
- 它不会触碰已经纳入仓库管理的历史压测基线文件，例如 `stress_report_*`、`stress_details_*`、`session_stats_*`。
- 默认行为是每一类生成产物保留最新的 `3` 个文件。

示例：

```bash
bash scripts/cleanup_claude_code_artifacts.sh --dry-run
```

```bash
bash scripts/cleanup_claude_code_artifacts.sh --keep-latest 2
```

`capability_overrides` 示例：

```json
{
  "image_input": {
    "status": "native",
    "detail": "该工作流通过自定义上游适配器接受 Anthropic 风格的图片块。"
  },
  "tool_use": {
    "status": "unsupported",
    "detail": "该工作流是纯文本流程，不应声明工具重建能力。"
  }
}
```

## 会话隔离（重要）

当多个终端用户共享同一个 API Key 时，客户端应该传入一个稳定的会话身份。  
否则网关会退回到请求级隔离模式（安全，但不会保留跨请求记忆）。

支持的传递方式：

- 请求头：`X-ST-Session-ID: <tenant_or_user_session_id>`（推荐）
- 请求头：`X-Session-ID: <tenant_or_user_session_id>`
- OpenAI 请求体：`user`
- 任意协议请求体：`metadata.user_id`（或 `metadata.user`）

示例：

```bash
curl -X POST "https://api.example.com/v1/chat/completions" \
  -H "Authorization: Bearer sk-xxx" \
  -H "Content-Type: application/json" \
  -H "X-ST-Session-ID: tenantA:user42" \
  -d '{
    "model":"claude-opus-4-6",
    "messages":[{"role":"user","content":"hello"}]
  }'
```

```bash
curl -X POST "https://api.example.com/v1/messages" \
  -H "x-api-key: sk-xxx" \
  -H "Content-Type: application/json" \
  -d '{
    "model":"claude-opus-4-6",
    "messages":[{"role":"user","content":"hello"}],
    "metadata":{"user_id":"tenantA:user42"}
  }'
```

## API Key 模型信息

通过 `GET /v1/key/info` 可以查询当前 key 可用的模型列表，以及每个模型的当前状态、不可用原因、已用 token 和可用 token。

支持的鉴权方式：

- `Authorization: Bearer sk-xxx`
- `x-api-key: sk-xxx`
- 浏览器查询参数：`/v1/key/info?key=sk-xxx`

示例请求：

```bash
BASE_URL="https://<your-deployment-url>"
curl "${BASE_URL}/v1/key/info?key=sk-xxx"
```

示例响应：

```json
{
  "models": [
    {
      "model": "gpt-5.4",
      "status": "active",
      "unavailable_reasons": [],
      "current_usage": "1.2K tokens",
      "available_tokens": "1.5M tokens",
      "capability_matrix": {
        "profile": "claude_code",
        "source": "derived_from_model_group_input_mapping_and_gateway_defaults",
        "summary": {
          "native": 5,
          "simulated": 4,
          "unsupported": 8
        }
      }
    },
    {
      "model": "claude-opus-4-1",
      "status": "exhausted",
      "unavailable_reasons": ["accounts_exhausted"],
      "current_usage": "210 tokens",
      "available_tokens": "0 tokens"
    }
  ]
}
```

## 环境变量快速开始

从 [`.env.example`](./.env.example) 开始。这个文件已经按照通常配置服务的顺序排好：

1. 最小必填项
2. 常用生产配置
3. 网关并发与排队
4. 上游 HTTP 连接池、重试与账号切换
5. 异步持久化与日志降载
6. Tool Use

优先修改这些变量：

- `BACKEND_API_URL`
- `DATABASE_URL`
- `JWT_SECRET_KEY`
- `ENCRYPTION_KEY`
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`

常见的生产环境可选变量：

- `ADMIN_PATH`
- `PROXY_SHARED_SECRET`
- `CORS_ORIGINS`
- `UVICORN_WORKERS`
- `LOG_LEVEL`
- `LOG_FILE`

说明：

- `.env.example` 里的值主要是面向生产环境的起步配置，不一定等于代码内置默认值。
- 对工具调用较重的 Claude Code 场景，调优时应重点关注请求并发、HTTP 连接池、SQLite 写入竞争以及单账号飞行中请求保护。

## SQLite 配置建议

项目当前仅支持 SQLite。默认数据库地址是：

```env
DATABASE_URL=sqlite+aiosqlite:///./data/api_service.db
```

部署建议：

- 单机部署直接使用默认路径即可。
- Docker、Hugging Face Spaces 等容器环境请把数据库文件放到持久化目录。
- Hugging Face Spaces 推荐使用 `/data/api_service.db`，并开启 Persistent Storage。
- 默认会启用 `journal_mode=WAL` 和 `busy_timeout=30s`；管理后台性能面板会直接展示这两个运行参数。
- `MAX_CONCURRENT_DB_OPS` 建议从 `10` 起步，再按机器磁盘性能和写入竞争情况调整。

容器部署示例：

```bash
docker run -d \
  --name st-api \
  -p 8000:7860 \
  -v st-api-data:/data \
  -e DATABASE_URL=sqlite+aiosqlite:////data/api_service.db \
  -e LOG_FILE=/data/api_service.log \
  -e JWT_SECRET_KEY=replace-with-a-random-jwt-secret \
  -e ENCRYPTION_KEY=replace-with-a-random-fernet-key \
  -e ADMIN_PASSWORD=replace-with-a-strong-admin-password \
  your-image:latest
```

## 发布管理

项目内置了 changelog + 版本发布工作流：

- `VERSION`：发布版本的单一事实来源
- `CHANGELOG.md`：记录每次发布的变更
- `scripts/release.py`：发布辅助脚本

### 准备一条发布记录

```bash
python scripts/release.py --version 1.0.1 \
  --note "变更说明1" \
  --note "变更说明2"
```

### 运行 Claude Code gate 后再准备发布记录

```bash
python scripts/release.py --version 1.0.1 \
  --note "变更说明1" \
  --note "变更说明2" \
  --run-claude-code-gate
```

如果暂时还没有可用的 `claude` CLI，也可以先只执行聚焦的 pytest gate：

```bash
python scripts/release.py --version 1.0.1 \
  --note "变更说明1" \
  --run-claude-code-gate \
  --gate-skip-real-cli-smoke
```

### 一条命令发布到 Hugging Face

```bash
python scripts/release.py --version 1.0.1 \
  --note "变更说明1" \
  --note "变更说明2" \
  --run-claude-code-gate \
  --commit --tag --push --remote origin --branch main
```

常用的 gate 参数：

- `--gate-python-bin /path/to/python`：覆盖 `run_claude_code_regression_gate.sh` 使用的 Python 解释器
- `--gate-skip-real-cli-smoke`：在修改发布内容前只运行聚焦的 pytest 测试集
- `--gate-auto-cleanup`：gate 成功后清理较旧的 Claude Code 生成产物
- `--gate-auto-cleanup-keep-latest N`：设置生成产物保留数量
- `--gate-auto-cleanup-dry-run`：只预览清理结果，不实际删除

### 下载指定版本

```bash
git clone --branch v1.0.1 https://huggingface.co/spaces/<username>/st-api
```

列出已发布版本：

```bash
git ls-remote --tags https://huggingface.co/spaces/<username>/st-api
```

或使用 `huggingface_hub`：

```python
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id="<username>/st-api",
    repo_type="space",
    revision="v1.0.1",
)
```

## 许可证

MIT
