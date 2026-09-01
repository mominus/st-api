# 通用工具调用兼容说明

st-api 将客户端协议标准化后，通过文本工具协议驱动不具备原生工具调用能力的
StackAI 工作流。工具由客户端执行；网关不会执行客户端声明的命令或访问文件。

## 支持的入口

| 协议 | 入口 | 工具定义 | 工具结果 |
| --- | --- | --- | --- |
| Anthropic Messages | `POST /v1/messages` | `tools[].input_schema` | 用户消息中的 `tool_result` 块 |
| OpenAI Chat Completions | `POST /v1/chat/completions` | `tools[].function.parameters` | `role: tool` 消息 |
| OpenAI Responses | `POST /v1/responses` | 扁平 `type: function` 工具 | `function_call_output` 输入项 |

Responses API 同时接受 `function_call` 历史项。Chat Completions 兼容旧式
`function_call` assistant 历史，但新集成应使用 `tool_calls`。

## 选择策略

`auto`、`none`、`required`/`any` 和具名工具选择会统一为内部策略。具名选择必须
引用当前请求声明的工具；`required`/`any` 至少需要声明一个工具。由于 StackAI
上游不提供原生约束，这些策略最终以严格提示约束模型，网关不能保证模型一定遵守。

## 工具协议

发送给上游的工具调用采用以下无歧义文本格式：

```text
[tool_call id=call_unique name=read_file]
{"path":"README.md"}
```

工具结果采用：

```text
[tool_result id=call_unique name=read_file]
result text
```

网关只将当前请求中声明的工具名提升为结构化调用，避免将普通 JSON 或示例代码误判
为工具执行。调用 ID 在每个响应中唯一，并在三种输出协议中映射为各自标准字段。

## 限制

- 单个请求最多 128 个工具，工具名最长 128 字符且只能使用字母、数字及 `_.:/-`。
- 输入 schema 顶层必须是 JSON object；网关不执行完整 JSON Schema 参数校验。
- 上游输出是文本，因此流式参数在完整工具对象可解析后才作为结构化事件发送。
- 图片、托管搜索、MCP 等提供商内置工具会降级为能力提示或合成工具，并非原生执行。
- Anthropic `tool_result` 必须紧跟对应 assistant `tool_use`，结果块必须位于消息开头，
  且 ID 集合必须完整匹配；结果后可跟普通文本。

## Agent 客户端

Cherry Studio、Claude Code、Codex、Zed/Zcode、DSH、Hermes 等客户端应选择其原生
OpenAI 或 Anthropic provider，并把 base URL 指向本服务。优先使用 Responses API
或 Anthropic Messages；旧客户端可使用 Chat Completions。
