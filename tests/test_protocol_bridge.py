import json
import asyncio

from app.services.protocol_bridge import CanonicalMessage, CanonicalRequest, UsageNumbers, get_protocol_bridge
from app.services.response_transformer import get_response_transformer
from app.services.tool_parser import ToolParser
from app.services.tool_registry import ToolRegistry, ToolSchema
from app.services.web_search_fallback import (
    SearchResult,
    WebSearchFallbackService,
    extract_legacy_web_search_query,
)


bridge = get_protocol_bridge()

MALFORMED_EDIT_TOOL_JSON = """{"tool":"Edit","arguments":{"replace_all":false,"file_path":"/home/ww/Project/BlogDemo/index.html","old_string":"          <p class=\\"details hidden\\">\\n            如果你只需要快速上线，一个静态页面就足够；如果你需要后台管理和内容检索，再考虑引入框架与数据库。建议先做最小可用版本，持续迭代，而不是一开始就追求"完美架构"。\\n          </p>","new_string":"          <div class=\\"details-wrapper\\">\\n            <p class=\\"details\\">\\n              如果你只需要快速上线，一个静态页面就足够；如果你需要后台管理和内容检索，再考虑引入框架与数据库。建议先做最小可用版本，持续迭代，而不是一开始就追求"完美架构"。\\n            </p>\\n          </div>"}}"""
TASKCREATE_TOOL_ARRAY_JSON = """[
  {"tool":"TaskCreate","arguments":{"subject":"添加新关卡和生存竞技场模式","description":"添加第9关配置，实现生存竞技场模式的持续生成逻辑、UI和排行榜","activeForm":"添加新关卡和模式"}},
  {"tool":"TaskCreate","arguments":{"subject":"实现天赋树系统","description":"定义天赋、实现UI页面、天赋效果应用、持久化存储和重置功能","activeForm":"实现天赋树系统"}},
  {"tool":"TaskCreate","arguments":{"subject":"添加新成就和集成测试","description":"添加与新内容相关的4个成就，确保所有新功能正常工作","activeForm":"添加新成就"}}
]"""
WRITE_TOOL_JSON_WITH_INNER_FENCES = """现在我已经充分了解了代码库。让我编写最终实施计划。

```json
{"tool":"Write","arguments":{"file_path":"/home/ww/.claude/plans/demo-plan.md","content":"# Demo Plan\\n\\n## Rust\\n\\n```toml\\nrust = [\\"tree-sitter-rust>=0.23\\"]\\n```\\n\\n## Benchmarks\\n\\n```text\\nbenchmarks/\\n  metrics.py\\n```"}}
```
"""


def _parse_sse_event(raw_event):
    event_name = None
    payload = None
    for line in raw_event.splitlines():
        if line.startswith("event: "):
            event_name = line[7:].strip()
        elif line.startswith("data: "):
            payload = json.loads(line[6:])
    return event_name, payload


def _collect_tool_stream_events(chunks, *, request_id):
    transformer = get_response_transformer()

    async def backend_stream():
        for chunk in chunks:
            yield f"data: {json.dumps(chunk)}"

    async def collect_events():
        events = []
        async for event in transformer.transform_backend_sse_to_anthropic_with_tools(
            backend_stream(),
            model="claude-opus-4-6",
            request_id=request_id,
            tool_parser=ToolParser(registry=None),
        ):
            events.append(event)
        return events

    return asyncio.run(collect_events())


def _build_tool_parser(tool_names, *, allow_unknown_tools=True):
    registry = ToolRegistry()
    for tool_name in tool_names:
        registry.register_tool(
            ToolSchema(
                name=tool_name,
                description=f"{tool_name} tool",
                input_schema={"type": "object", "properties": {}},
            )
        )
    return ToolParser(registry=registry, allow_unknown_tools=allow_unknown_tools)


def test_parse_openai_chat_and_tool_call_roundtrip():
    payload = {
        "model": "claude-opus-4-6",
        "messages": [
            {"role": "system", "content": "you are a coding assistant"},
            {"role": "user", "content": "list files"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "LS",
                    "description": "list files",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
            }
        ],
    }

    canonical = bridge.parse_openai_chat(payload)
    prompt = bridge.render_prompt(canonical)
    assert canonical.model == "claude-opus-4-6"
    assert canonical.tools and canonical.tools[0].name == "LS"
    assert "[Tooling]" in prompt

    output = """I will use a tool\n```json
{"tool":"LS","arguments":{"path":"."}}
```"""
    parsed = bridge.parse_model_output(output)
    assert parsed.has_tool_calls is True
    assert parsed.tool_calls[0].name == "LS"
    assert parsed.tool_calls[0].arguments["path"] == "."



def test_openai_chat_response_with_tool_calls():
    parsed = bridge.parse_model_output(
        """```json
{"tool":"Read","arguments":{"file_path":"README.md"}}
```"""
    )

    response = bridge.to_openai_chat_response(
        model="gpt-5.4",
        request_id="abc123",
        parsed=parsed,
        usage=UsageNumbers(input_tokens=10, output_tokens=5, total_tokens=15),
    )

    choice = response["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    tool_call = choice["message"]["tool_calls"][0]
    assert tool_call["function"]["name"] == "Read"
    json.loads(tool_call["function"]["arguments"])



def test_parse_bracket_tool_call_roundtrip():
    parsed = bridge.parse_model_output(
        """[tool_call id=toolu_123 name=Read]
{"file_path":"README.md"}"""
    )

    assert parsed.has_tool_calls is True
    assert parsed.tool_calls[0].name == "Read"
    assert parsed.tool_calls[0].arguments["file_path"] == "README.md"
    assert parsed.text == ""


def test_parse_plain_json_tool_call_roundtrip():
    parsed = bridge.parse_model_output(
        '{"tool":"Write","arguments":{"file_path":"/tmp/plan.md","content":"# Plan"}}'
    )

    assert parsed.has_tool_calls is True
    assert parsed.tool_calls[0].name == "Write"
    assert parsed.tool_calls[0].arguments["file_path"] == "/tmp/plan.md"
    assert parsed.tool_calls[0].arguments["content"] == "# Plan"
    assert parsed.text == ""


def test_parse_malformed_edit_tool_call_roundtrip():
    parsed = bridge.parse_model_output(
        f"```json\n{MALFORMED_EDIT_TOOL_JSON}\n```"
    )

    assert parsed.has_tool_calls is True
    assert parsed.tool_calls[0].name == "Edit"
    assert parsed.tool_calls[0].arguments["file_path"] == "/home/ww/Project/BlogDemo/index.html"
    assert '追求"完美架构"' in parsed.tool_calls[0].arguments["old_string"]
    assert 'details-wrapper' in parsed.tool_calls[0].arguments["new_string"]
    assert parsed.text == ""


def test_parse_multiple_bracket_tool_calls():
    parsed = bridge.parse_model_output(
        """让我读取核心源码文件。

[tool_call id=toolu_1 name=Read]
{"file_path":"src/a.js"}

[tool_call id=toolu_2 name=Read]
{"file_path":"src/b.js"}

[tool_call id=toolu_3 name=Read]
{"file_path":"src/c.js"}"""
    )

    assert parsed.has_tool_calls is True
    assert len(parsed.tool_calls) == 3
    assert [c.name for c in parsed.tool_calls] == ["Read", "Read", "Read"]
    assert parsed.tool_calls[0].arguments["file_path"] == "src/a.js"
    assert parsed.tool_calls[1].arguments["file_path"] == "src/b.js"
    assert parsed.tool_calls[2].arguments["file_path"] == "src/c.js"
    assert "让我读取核心源码文件" in parsed.text


def test_parse_legacy_websearch_function_call_roundtrip():
    parsed = bridge.parse_model_output(
        'WebSearch("今日新闻 2026年3月27日")'
    )

    assert parsed.has_tool_calls is True
    assert len(parsed.tool_calls) == 1
    assert parsed.tool_calls[0].name == "WebSearch"
    assert parsed.tool_calls[0].arguments["query"] == "今日新闻 2026年3月27日"
    assert parsed.text == ""


def test_parse_legacy_websearch_phrase_roundtrip():
    parsed = bridge.parse_model_output(
        "Perform a web search for the query: top news today March 27 2026"
    )

    assert parsed.has_tool_calls is True
    assert len(parsed.tool_calls) == 1
    assert parsed.tool_calls[0].name == "WebSearch"
    assert parsed.tool_calls[0].arguments["query"] == "top news today March 27 2026"
    assert parsed.text == ""


def test_parse_bracket_websearch_with_legacy_phrase_arguments():
    parsed = bridge.parse_model_output(
        "[tool_call id=toolu_legacy_ws name=WebSearch]\n"
        "Perform a web search for the query: 今日新闻 2026年3月27日"
    )

    assert parsed.has_tool_calls is True
    assert len(parsed.tool_calls) == 1
    assert parsed.tool_calls[0].name == "WebSearch"
    assert parsed.tool_calls[0].arguments["query"] == "今日新闻 2026年3月27日"
    assert parsed.text == ""


def test_anthropic_response_with_tool_use_blocks():
    parsed = bridge.parse_model_output(
        """```json
{"tool":"Bash","arguments":{"command":"pwd"}}
```"""
    )
    response = bridge.to_anthropic_response(
        model="claude-sonnet-4-5",
        request_id="msg1",
        parsed=parsed,
        usage=UsageNumbers(input_tokens=3, output_tokens=7, total_tokens=10),
    )

    assert response["stop_reason"] == "tool_use"
    assert any(block["type"] == "tool_use" for block in response["content"])



def test_gemini_response_with_function_call_parts():
    parsed = bridge.parse_model_output(
        """```json
{"tool":"search_docs","arguments":{"query":"stackai"}}
```"""
    )
    response = bridge.to_gemini_response(
        model="gemini-2.5-pro",
        parsed=parsed,
        usage=UsageNumbers(input_tokens=4, output_tokens=6, total_tokens=10),
    )

    parts = response["candidates"][0]["content"]["parts"]
    assert any("functionCall" in part for part in parts)
    fn = [part["functionCall"] for part in parts if "functionCall" in part][0]
    assert fn["name"] == "search_docs"


def test_parse_model_output_preserves_success_text_verbatim():
    parsed = bridge.parse_model_output("Open https://api.stack-ai.com/docs for details")
    assert parsed.text == "Open https://api.stack-ai.com/docs for details"


def test_parse_model_output_preserves_tool_arguments_verbatim():
    parsed = bridge.parse_model_output(
        """```json
{"tool":"OpenURL","arguments":{"url":"https://api.stack-ai.com/docs"}}
```"""
    )
    assert parsed.has_tool_calls is True
    assert parsed.tool_calls[0].arguments["url"] == "https://api.stack-ai.com/docs"


def test_parse_model_output_supports_plain_json_tool_call_array():
    parsed = bridge.parse_model_output(TASKCREATE_TOOL_ARRAY_JSON)

    assert parsed.has_tool_calls is True
    assert parsed.text == ""
    assert [call.name for call in parsed.tool_calls] == [
        "TaskCreate",
        "TaskCreate",
        "TaskCreate",
    ]
    assert parsed.tool_calls[0].arguments["subject"] == "添加新关卡和生存竞技场模式"
    assert parsed.tool_calls[2].arguments["activeForm"] == "添加新成就"


def test_response_transformer_preserves_non_tool_text_response_verbatim():
    transformer = get_response_transformer()
    response = transformer.to_anthropic_response(
        {"outputs": {"out-0": "Visit https://api.stack-ai.com/help"}},
        model="claude-opus-4-6",
        request_id="msg_sanitized",
    )
    text_blocks = [block for block in response["content"] if block["type"] == "text"]
    assert text_blocks
    assert text_blocks[0]["text"] == "Visit https://api.stack-ai.com/help"


def test_response_transformer_preserves_tool_use_input_verbatim():
    transformer = get_response_transformer()
    response = transformer.to_anthropic_response_with_tools(
        {"outputs": {"out-0": '```json\n{"tool":"Read","arguments":{"file_path":"README.md"}}\n```'}},
        model="claude-opus-4-6",
        request_id="msg_tool_input",
        tool_parser=ToolParser(registry=None),
    )

    tool_blocks = [block for block in response["content"] if block["type"] == "tool_use"]
    assert tool_blocks
    assert tool_blocks[0]["input"] == {"file_path": "README.md"}


def test_response_transformer_plain_json_write_becomes_tool_use():
    transformer = get_response_transformer()
    response = transformer.to_anthropic_response_with_tools(
        {
            "outputs": {
                "out-0": '{"tool":"Write","arguments":{"file_path":"/home/ww/.claude/plans/demo.md","content":"# Demo"}}'
            }
        },
        model="claude-opus-4-6",
        request_id="msg_plain_write",
        tool_parser=ToolParser(registry=None),
    )

    tool_blocks = [block for block in response["content"] if block["type"] == "tool_use"]
    assert tool_blocks
    assert tool_blocks[0]["name"] == "Write"
    assert tool_blocks[0]["input"]["file_path"] == "/home/ww/.claude/plans/demo.md"
    assert tool_blocks[0]["input"]["content"] == "# Demo"


def test_response_transformer_plain_json_tool_array_becomes_multiple_tool_use_blocks():
    transformer = get_response_transformer()
    response = transformer.to_anthropic_response_with_tools(
        {"outputs": {"out-0": TASKCREATE_TOOL_ARRAY_JSON}},
        model="claude-opus-4-6",
        request_id="msg_taskcreate_array",
        tool_parser=ToolParser(registry=None),
    )

    tool_blocks = [block for block in response["content"] if block["type"] == "tool_use"]
    assert len(tool_blocks) == 3
    assert [block["name"] for block in tool_blocks] == ["TaskCreate", "TaskCreate", "TaskCreate"]
    assert tool_blocks[0]["input"]["subject"] == "添加新关卡和生存竞技场模式"
    assert tool_blocks[1]["input"]["activeForm"] == "实现天赋树系统"
    assert tool_blocks[2]["input"]["description"] == "添加与新内容相关的4个成就，确保所有新功能正常工作"
    assert not any(block["type"] == "text" and '"tool":"TaskCreate"' in block["text"] for block in response["content"])
    assert response["stop_reason"] == "tool_use"


def test_response_transformer_malformed_edit_becomes_tool_use():
    transformer = get_response_transformer()
    response = transformer.to_anthropic_response_with_tools(
        {"outputs": {"out-0": MALFORMED_EDIT_TOOL_JSON}},
        model="claude-opus-4-6",
        request_id="msg_malformed_edit",
        tool_parser=ToolParser(registry=None),
    )

    tool_blocks = [block for block in response["content"] if block["type"] == "tool_use"]
    assert tool_blocks
    assert tool_blocks[0]["name"] == "Edit"
    assert tool_blocks[0]["input"]["replace_all"] is False
    assert '追求"完美架构"' in tool_blocks[0]["input"]["old_string"]


def test_response_transformer_preserves_anthropic_stream_whitespace():
    transformer = get_response_transformer()
    event = transformer.to_anthropic_stream_event(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "  line1\n    code"},
        },
    )

    assert '"text": "  line1\\n    code"' in event


def test_response_transformer_preserves_openai_chunk_whitespace():
    transformer = get_response_transformer()
    chunk = transformer.to_openai_stream_chunk(
        "\n  - item",
        "claude-opus-4-6",
        "chunk_whitespace",
        is_final=False,
    )

    assert '"content": "\\n  - item"' in chunk


def test_transform_backend_sse_to_anthropic_preserves_whitespace():
    transformer = get_response_transformer()

    chunks = [
        {"outputs": {"out-0": "  line1"}},
        {"outputs": {"out-0": "\n    line2"}},
    ]

    async def backend_stream():
        for chunk in chunks:
            yield f"data: {json.dumps(chunk)}"

    async def collect_events():
        events = []
        async for event in transformer.transform_backend_sse_to_anthropic(
            backend_stream(),
            model="claude-opus-4-6",
            request_id="stream_ws",
        ):
            events.append(event)
        return events

    events = asyncio.run(collect_events())
    merged = "\n".join(events)
    assert '"text": "  line1"' in merged
    assert '"text": "\\n    line2"' in merged


def test_anthropic_stream_events_with_tool_use_are_incremental():
    transformer = get_response_transformer()

    chunks = [
        {"outputs": {"out-0": "hello "}},
        {"outputs": {"out-0": "world "}},
        {"outputs": {"out-0": "```json\n{\"tool\":\"Read\",\"arguments\":{\"file_path\":\"README.md\"}}\n```"}},
    ]

    async def backend_stream():
        for chunk in chunks:
            yield f"data: {json.dumps(chunk)}"

    async def collect_events():
        events = []
        async for event in transformer.transform_backend_sse_to_anthropic_with_tools(
            backend_stream(),
            model="claude-opus-4-6",
            request_id="stream1",
            tool_parser=ToolParser(registry=None),
        ):
            events.append(event)
        return events

    events = asyncio.run(collect_events())
    assert any("event: content_block_delta" in event and "\"text_delta\"" in event for event in events)
    assert any("\"type\": \"tool_use\"" in event for event in events)
    assert any("\"stop_reason\": \"tool_use\"" in event for event in events)


def test_anthropic_stream_with_bracket_tool_call_emits_tool_use():
    transformer = get_response_transformer()

    chunks = [
        {"outputs": {"out-0": "[tool_call id=toolu_abc name=Read]\n{\"file_path\":\"README.md\"}"}},
    ]

    async def backend_stream():
        for chunk in chunks:
            yield f"data: {json.dumps(chunk)}"

    async def collect_events():
        events = []
        async for event in transformer.transform_backend_sse_to_anthropic_with_tools(
            backend_stream(),
            model="claude-opus-4-6",
            request_id="stream_bracket1",
            tool_parser=ToolParser(registry=None),
        ):
            events.append(event)
        return events

    events = asyncio.run(collect_events())
    merged = "\n".join(events)
    assert "\"type\": \"tool_use\"" in merged
    assert "\"name\": \"Read\"" in merged
    assert "[tool_call id=" not in merged
    assert "\"stop_reason\": \"tool_use\"" in merged


def test_anthropic_stream_with_plain_json_write_emits_tool_use_without_leak():
    transformer = get_response_transformer()

    chunks = [
        {
            "outputs": {
                "out-0": '{"tool":"Write","arguments":{"file_path":"/home/ww/.claude/plans/demo.md","content":"# Demo"}}'
            }
        },
    ]

    async def backend_stream():
        for chunk in chunks:
            yield f"data: {json.dumps(chunk)}"

    async def collect_events():
        events = []
        async for event in transformer.transform_backend_sse_to_anthropic_with_tools(
            backend_stream(),
            model="claude-opus-4-6",
            request_id="stream_plain_write",
            tool_parser=ToolParser(registry=None),
        ):
            events.append(event)
        return events

    events = asyncio.run(collect_events())
    merged = "\n".join(events)
    assert '"type": "tool_use"' in merged
    assert '"name": "Write"' in merged
    assert '"tool":"Write"' not in merged
    assert '"stop_reason": "tool_use"' in merged


def test_anthropic_stream_with_malformed_plain_json_edit_emits_tool_use_without_leak():
    transformer = get_response_transformer()

    chunks = [
        {"outputs": {"out-0": MALFORMED_EDIT_TOOL_JSON}},
    ]

    async def backend_stream():
        for chunk in chunks:
            yield f"data: {json.dumps(chunk)}"

    async def collect_events():
        events = []
        async for event in transformer.transform_backend_sse_to_anthropic_with_tools(
            backend_stream(),
            model="claude-opus-4-6",
            request_id="stream_malformed_plain_edit",
            tool_parser=ToolParser(registry=None),
        ):
            events.append(event)
        return events

    events = asyncio.run(collect_events())
    merged = "\n".join(events)
    assert '"type": "tool_use"' in merged
    assert '"name": "Edit"' in merged
    assert '"tool":"Edit"' not in merged
    assert '"stop_reason": "tool_use"' in merged


def test_anthropic_stream_with_malformed_fenced_json_edit_emits_tool_use_without_leak():
    transformer = get_response_transformer()

    chunks = [
        {"outputs": {"out-0": f"```json\n{MALFORMED_EDIT_TOOL_JSON}\n```"}},
    ]

    async def backend_stream():
        for chunk in chunks:
            yield f"data: {json.dumps(chunk)}"

    async def collect_events():
        events = []
        async for event in transformer.transform_backend_sse_to_anthropic_with_tools(
            backend_stream(),
            model="claude-opus-4-6",
            request_id="stream_malformed_fenced_edit",
            tool_parser=ToolParser(registry=None),
        ):
            events.append(event)
        return events

    events = asyncio.run(collect_events())
    merged = "\n".join(events)
    assert '"type": "tool_use"' in merged
    assert '"name": "Edit"' in merged
    assert '"tool":"Edit"' not in merged
    assert '"stop_reason": "tool_use"' in merged


def test_anthropic_stream_with_plain_json_todowrite_emits_tool_use_without_leak():
    chunks = [
        {
            "outputs": {
                "out-0": '{"tool":"TodoWrite","arguments":{"todos":[{"content":"Task A","status":"pending","activeForm":"Doing task A"}]}}'
            }
        },
    ]

    events = _collect_tool_stream_events(chunks, request_id="stream_plain_todo")
    merged = "\n".join(events)
    assert '"type": "tool_use"' in merged
    assert '"name": "TodoWrite"' in merged
    assert '"tool":"TodoWrite"' not in merged
    assert '"stop_reason": "tool_use"' in merged


def test_anthropic_stream_with_plain_json_tool_array_emits_multiple_tool_use_without_leak():
    chunks = [
        {
            "outputs": {
                "out-0": TASKCREATE_TOOL_ARRAY_JSON
            }
        },
    ]

    events = _collect_tool_stream_events(chunks, request_id="stream_plain_taskcreate_array")
    merged = "\n".join(events)
    assert merged.count('"type": "tool_use"') == 3
    assert merged.count('"name": "TaskCreate"') == 3
    assert '"tool":"TaskCreate"' not in merged
    assert '"stop_reason": "tool_use"' in merged

    partial_by_index = {}
    for raw_event in events:
        event_name, payload = _parse_sse_event(raw_event)
        if event_name != "content_block_delta":
            continue
        index = payload["index"]
        partial_by_index.setdefault(index, "")
        partial_by_index[index] += payload["delta"]["partial_json"]

    decoded = [json.loads(partial_by_index[idx]) for idx in sorted(partial_by_index)]
    assert [item["subject"] for item in decoded] == [
        "添加新关卡和生存竞技场模式",
        "实现天赋树系统",
        "添加新成就和集成测试",
    ]
    assert decoded[2]["activeForm"] == "添加新成就"


def test_anthropic_stream_with_fenced_write_containing_inner_code_fences_emits_tool_use_without_leak():
    chunks = [
        {
            "outputs": {
                "out-0": WRITE_TOOL_JSON_WITH_INNER_FENCES
            }
        },
    ]

    events = _collect_tool_stream_events(chunks, request_id="stream_fenced_write_inner_fences")
    merged = "\n".join(events)
    assert '"type": "tool_use"' in merged
    assert '"name": "Write"' in merged
    assert '"tool":"Write"' not in merged
    assert '"stop_reason": "tool_use"' in merged

    parsed_events = [_parse_sse_event(event) for event in events]
    partial_json = "".join(
        payload["delta"]["partial_json"]
        for event_name, payload in parsed_events
        if event_name == "content_block_delta"
        and isinstance(payload, dict)
        and payload.get("delta", {}).get("type") == "input_json_delta"
    )
    decoded = json.loads(partial_json)
    assert decoded["file_path"] == "/home/ww/.claude/plans/demo-plan.md"
    assert "```toml" in decoded["content"]
    assert "```text" in decoded["content"]


def test_anthropic_stream_tool_input_json_is_chunked_into_multiple_deltas():
    chunks = [
        {
            "outputs": {
                "out-0": (
                    '```json\n'
                    '{"tool":"Write","arguments":{"file_path":"/tmp/demo.md","content":"第一行\\n第二行\\n第三行\\n第四行\\n第五行"}}\n'
                    '```'
                )
            }
        },
    ]

    events = _collect_tool_stream_events(chunks, request_id="stream_chunked_json")
    parsed_events = [_parse_sse_event(event) for event in events]

    input_json_deltas = [
        payload["delta"]["partial_json"]
        for event_name, payload in parsed_events
        if event_name == "content_block_delta"
        and isinstance(payload, dict)
        and payload.get("delta", {}).get("type") == "input_json_delta"
    ]

    assert len(input_json_deltas) >= 2
    assert "".join(input_json_deltas) == (
        '{"file_path":"/tmp/demo.md","content":"第一行\\n第二行\\n第三行\\n第四行\\n第五行"}'
    )


def test_anthropic_stream_with_xml_tool_use_emits_tool_use_without_leak():
    chunks = [
        {
            "outputs": {
                "out-0": '<tool_use id="toolu_xml_1" name="Read">{"file_path":"README.md","offset":120}</tool_use>'
            }
        },
    ]

    events = _collect_tool_stream_events(chunks, request_id="stream_xml_tool")
    merged = "\n".join(events)
    parsed_events = [_parse_sse_event(event) for event in events]

    tool_start = next(
        payload
        for event_name, payload in parsed_events
        if event_name == "content_block_start"
        and isinstance(payload, dict)
        and payload.get("content_block", {}).get("type") == "tool_use"
    )
    input_json_deltas = [
        payload["delta"]["partial_json"]
        for event_name, payload in parsed_events
        if event_name == "content_block_delta"
        and isinstance(payload, dict)
        and payload.get("delta", {}).get("type") == "input_json_delta"
    ]

    assert tool_start["content_block"]["id"] == "toolu_xml_1"
    assert tool_start["content_block"]["name"] == "Read"
    assert "".join(input_json_deltas) == '{"file_path":"README.md","offset":120}'
    assert '<tool_use id="toolu_xml_1"' not in merged
    assert '"stop_reason": "tool_use"' in merged


def test_anthropic_stream_with_invalid_fenced_tool_text_falls_back_to_text():
    chunks = [
        {
            "outputs": {
                "out-0": '```json\n{"arguments":{"file_path":"README.md"}}\n```'
            }
        },
    ]

    events = _collect_tool_stream_events(chunks, request_id="stream_invalid_tool_text")
    merged = "\n".join(events)
    parsed_events = [_parse_sse_event(event) for event in events]

    text_deltas = [
        payload["delta"]["text"]
        for event_name, payload in parsed_events
        if event_name == "content_block_delta"
        and isinstance(payload, dict)
        and payload.get("delta", {}).get("type") == "text_delta"
    ]
    message_delta = next(
        payload
        for event_name, payload in parsed_events
        if event_name == "message_delta" and isinstance(payload, dict)
    )

    assert not any('"type": "tool_use"' in event for event in events)
    assert "".join(text_deltas) == '```json\n{"arguments":{"file_path":"README.md"}}\n```'
    assert message_delta["delta"]["stop_reason"] == "end_turn"
    assert '"type": "tool_use"' not in merged


def test_anthropic_stream_with_undeclared_tool_name_falls_back_to_text():
    transformer = get_response_transformer()

    chunks = [
        {
            "outputs": {
                "out-0": '```json\n{"tool":"Write","arguments":{"file_path":"README.md","content":"x"}}\n```'
            }
        },
    ]

    async def backend_stream():
        for chunk in chunks:
            yield f"data: {json.dumps(chunk)}"

    async def collect_events():
        events = []
        async for event in transformer.transform_backend_sse_to_anthropic_with_tools(
            backend_stream(),
            model="claude-opus-4-6",
            request_id="stream_undeclared_tool",
            tool_parser=_build_tool_parser(["Read"], allow_unknown_tools=False),
        ):
            events.append(event)
        return events

    events = asyncio.run(collect_events())
    merged = "\n".join(events)
    parsed_events = [_parse_sse_event(event) for event in events]

    text_deltas = [
        payload["delta"]["text"]
        for event_name, payload in parsed_events
        if event_name == "content_block_delta"
        and isinstance(payload, dict)
        and payload.get("delta", {}).get("type") == "text_delta"
    ]
    message_delta = next(
        payload
        for event_name, payload in parsed_events
        if event_name == "message_delta" and isinstance(payload, dict)
    )

    assert not any('"type": "tool_use"' in event for event in events)
    assert "".join(text_deltas) == '```json\n{"tool":"Write","arguments":{"file_path":"README.md","content":"x"}}\n```'
    assert message_delta["delta"]["stop_reason"] == "end_turn"
    assert '"type": "tool_use"' not in merged


def test_anthropic_stream_with_tools_suppresses_text_after_first_tool_call():
    transformer = get_response_transformer()

    chunks = [
        {"outputs": {"out-0": "前置说明。"}},
        {"outputs": {"out-0": "```json\n{\"tool\":\"Read\",\"arguments\":{\"file_path\":\"README.md\"}}\n```"}},
        {"outputs": {"out-0": "THIS_TEXT_SHOULD_BE_HIDDEN_AFTER_TOOL"}},
    ]

    async def backend_stream():
        for chunk in chunks:
            yield f"data: {json.dumps(chunk)}"

    async def collect_events():
        events = []
        async for event in transformer.transform_backend_sse_to_anthropic_with_tools(
            backend_stream(),
            model="claude-opus-4-6",
            request_id="stream2",
            tool_parser=ToolParser(registry=None),
        ):
            events.append(event)
        return events

    events = asyncio.run(collect_events())
    merged = "\n".join(events)
    assert "\"type\": \"tool_use\"" in merged
    assert "THIS_TEXT_SHOULD_BE_HIDDEN_AFTER_TOOL" not in merged
    assert "\"stop_reason\": \"tool_use\"" in merged


def test_anthropic_stream_with_tools_stops_early_after_first_tool_call():
    transformer = get_response_transformer()
    consumed = {"count": 0}

    chunks = [
        {"outputs": {"out-0": "先看一下。"}},
        {"outputs": {"out-0": "```json\n{\"tool\":\"Read\",\"arguments\":{\"file_path\":\"README.md\"}}\n```"}},
        {"outputs": {"out-0": "TRAILING_SHOULD_NOT_BE_READ"}},
    ]

    async def backend_stream():
        for chunk in chunks:
            consumed["count"] += 1
            yield f"data: {json.dumps(chunk)}"

    async def collect_events():
        events = []
        async for event in transformer.transform_backend_sse_to_anthropic_with_tools(
            backend_stream(),
            model="claude-opus-4-6",
            request_id="stream3",
            tool_parser=ToolParser(registry=None),
        ):
            events.append(event)
        return events

    events = asyncio.run(collect_events())
    merged = "\n".join(events)
    assert consumed["count"] == 2
    assert "\"type\": \"tool_use\"" in merged
    assert "TRAILING_SHOULD_NOT_BE_READ" not in merged
    assert "\"stop_reason\": \"tool_use\"" in merged


def test_render_prompt_prefers_chinese_for_chinese_user():
    payload = {
        "model": "claude-opus-4-6",
        "messages": [
            {"role": "user", "content": "请分析这个项目结构"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "Read",
                    "description": "read file",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    }
    canonical = bridge.parse_openai_chat(payload)
    prompt = bridge.render_prompt(canonical)
    assert canonical.response_language == "zh"
    assert "默认使用简体中文回答" in prompt


def test_detect_language_ignores_english_tool_result_tail_openai():
    payload = {
        "model": "claude-opus-4-6",
        "messages": [
            {"role": "user", "content": "请分析该项目"},
            {
                "role": "assistant",
                "content": "```json\n{\"tool\":\"LS\",\"arguments\":{\"path\":\"BookmarkVault\"}}\n```",
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "LS",
                "content": "Listed directory BookmarkVault/",
            },
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "LS",
                    "description": "list files",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    }
    canonical = bridge.parse_openai_chat(payload)
    assert canonical.response_language == "zh"


def test_detect_language_ignores_english_tool_result_tail_anthropic():
    payload = {
        "model": "claude-opus-4-6",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "请分析该项目"}]},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "LS",
                        "input": {"path": "BookmarkVault"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "Listed directory BookmarkVault/",
                    }
                ],
            },
        ],
        "tools": [
            {
                "name": "LS",
                "description": "list files",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
    }
    canonical = bridge.parse_anthropic_messages(payload)
    assert canonical.response_language == "zh"


def test_parse_anthropic_messages_preserves_beta_thinking_metadata_and_raw_messages():
    payload = {
        "model": "claude-opus-4-6",
        "thinking": {"type": "adaptive"},
        "metadata": {"user_id": "session-123", "trace": "abc"},
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "请分析这个项目"}]},
        ],
    }

    canonical = bridge.parse_anthropic_messages(
        payload,
        anthropic_beta_header="claude-code-20250219,interleaved-thinking-2025-05-14",
    )

    assert canonical.thinking == {"type": "adaptive"}
    assert canonical.metadata == {"user_id": "session-123", "trace": "abc"}
    assert canonical.anthropic_beta == [
        "claude-code-20250219",
        "interleaved-thinking-2025-05-14",
    ]
    assert canonical.raw_messages == payload["messages"]


def test_render_prompt_prefers_raw_anthropic_messages_for_tool_context():
    canonical = CanonicalRequest(
        source="anthropic_messages",
        model="claude-opus-4-6",
        messages=[
            CanonicalMessage(role="user", content="STALE_USER_MESSAGE"),
        ],
        raw_messages=[
            {"role": "user", "content": [{"type": "text", "text": "请先读取 README"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "这段前置草稿不应进入 prompt"},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Read",
                        "input": {"file_path": "README.md"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": [{"type": "text", "text": "README content"}],
                    }
                ],
            },
        ],
    )

    prompt = bridge.render_prompt(canonical)

    assert "STALE_USER_MESSAGE" not in prompt
    assert "[Human]\n请先读取 README" in prompt
    assert "[Assistant]\n[tool_call id=toolu_1 name=Read]" in prompt
    assert '{"file_path":"README.md"}' in prompt
    assert "[Human]\n[tool_result id=toolu_1]\nREADME content" in prompt
    assert "这段前置草稿不应进入 prompt" not in prompt


def test_parse_anthropic_messages_preserves_structured_dict_tool_result_content():
    structured_payload = {
        "stdout": "README content",
        "exit_code": 0,
        "artifacts": ["README.md"],
    }
    payload = {
        "model": "claude-opus-4-6",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_structured",
                        "name": "Read",
                        "input": {"file_path": "README.md"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_structured",
                        "content": structured_payload,
                    }
                ],
            },
        ],
    }

    canonical = bridge.parse_anthropic_messages(payload)
    serialized_payload = json.dumps(structured_payload, ensure_ascii=False, separators=(",", ":"))

    assert canonical.messages[-1].content == (
        f"[tool_result id=toolu_structured]\n{serialized_payload}"
    )


def test_render_prompt_preserves_non_text_tool_result_blocks():
    image_block = {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": "abc123",
        },
    }
    document_block = {
        "type": "document",
        "source": {
            "type": "text",
            "media_type": "text/plain",
            "data": "traceback details",
        },
    }
    payload = {
        "model": "claude-opus-4-6",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_error",
                        "name": "Bash",
                        "input": {"command": "cat missing.txt"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_error",
                        "is_error": True,
                        "content": [
                            {"type": "text", "text": "命令执行失败"},
                            image_block,
                            document_block,
                        ],
                    }
                ],
            },
        ],
    }

    canonical = bridge.parse_anthropic_messages(payload)
    prompt = bridge.render_prompt(canonical)
    serialized_image = json.dumps(image_block, ensure_ascii=False, separators=(",", ":"))
    serialized_document = json.dumps(document_block, ensure_ascii=False, separators=(",", ":"))

    assert "[tool_result id=toolu_error error=true]" in canonical.messages[-1].content
    assert "命令执行失败" in canonical.messages[-1].content
    assert serialized_image in canonical.messages[-1].content
    assert serialized_document in canonical.messages[-1].content
    assert "[Human]\n[tool_result id=toolu_error error=true]" in prompt
    assert "命令执行失败" in prompt
    assert serialized_image in prompt
    assert serialized_document in prompt


def test_parse_anthropic_messages_accepts_valid_tool_result_pair():
    payload = {
        "model": "claude-opus-4-6",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Read",
                        "input": {"file_path": "README.md"},
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_2",
                        "name": "Read",
                        "input": {"file_path": "pyproject.toml"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"},
                    {"type": "tool_result", "tool_use_id": "toolu_2", "content": "ok"},
                ],
            },
        ],
    }

    canonical = bridge.parse_anthropic_messages(payload)
    assert canonical.raw_messages == payload["messages"]
    assert canonical.messages[-1].role == "user"
    assert "[tool_result id=toolu_1]" in canonical.messages[-1].content
    assert "[tool_result id=toolu_2]" in canonical.messages[-1].content


def test_parse_anthropic_messages_accepts_single_tool_result_pair():
    payload = {
        "model": "claude-opus-4-6",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_single",
                        "name": "Read",
                        "input": {"file_path": "README.md"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_single", "content": "ok"},
                ],
            },
        ],
    }

    canonical = bridge.parse_anthropic_messages(payload)
    assert canonical.raw_messages == payload["messages"]
    assert canonical.messages[-1].content.startswith("[tool_result id=toolu_single]")


def test_parse_anthropic_messages_accepts_standard_tool_result_then_text():
    payload = {
        "model": "claude-opus-4-6",
        "messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}}
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"},
                {"type": "text", "text": "continue"},
            ]},
        ],
    }

    canonical = bridge.parse_anthropic_messages(payload)

    assert "[tool_result id=toolu_1" in canonical.messages[-1].content
    assert "continue" in canonical.messages[-1].content


def test_parse_anthropic_messages_accepts_claude_code_mixed_tool_result_and_text_tail():
    payload = {
        "model": "claude-opus-4-6",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Read",
                        "input": {"file_path": "README.md"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"},
                    {"type": "text", "text": "额外文本"},
                ],
            },
        ],
    }

    canonical = bridge.parse_anthropic_messages(
        payload,
        anthropic_beta_header="claude-code-20250219",
    )

    assert canonical.anthropic_beta == ["claude-code-20250219"]
    assert canonical.raw_messages[-1]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_1",
            "content": "ok\n\n额外文本",
        }
    ]
    assert canonical.messages[-1].content == "[tool_result id=toolu_1]\nok\n\n额外文本"


def test_parse_anthropic_messages_accepts_claude_code_tool_reference_result_with_text_tail():
    tool_references = [
        {"type": "tool_reference", "uri": "tool://search/1", "title": "result-1"},
        {"type": "tool_reference", "uri": "tool://search/2", "title": "result-2"},
    ]
    payload = {
        "model": "claude-opus-4-6",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "我先检索一下。"},
                    {
                        "type": "tool_use",
                        "id": "toolu_search_1",
                        "name": "ToolSearch",
                        "input": {"query": "st-api anthropic beta"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_search_1",
                        "content": tool_references,
                    },
                    {"type": "text", "text": "继续处理"},
                ],
            },
        ],
    }

    canonical = bridge.parse_anthropic_messages(
        payload,
        anthropic_beta_header="claude-code-20250219,advanced-tool-use-2025-11-20",
    )

    assert canonical.anthropic_beta == [
        "claude-code-20250219",
        "advanced-tool-use-2025-11-20",
    ]
    assert canonical.raw_messages[-1]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_search_1",
            "content": [
                {"type": "tool_reference", "uri": "tool://search/1", "title": "result-1"},
                {"type": "tool_reference", "uri": "tool://search/2", "title": "result-2"},
                {"type": "text", "text": "继续处理"},
            ],
        }
    ]
    assert canonical.messages[-1].content.startswith("[tool_result id=toolu_search_1]")
    assert "继续处理" in canonical.messages[-1].content


def test_parse_anthropic_messages_rejects_tool_result_without_previous_tool_use():
    payload = {
        "model": "claude-opus-4-6",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"},
                ],
            },
        ],
    }

    try:
        bridge.parse_anthropic_messages(payload)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "not matching any tool_use" in str(exc)


def test_parse_anthropic_messages_rejects_mismatched_tool_result_ids():
    payload = {
        "model": "claude-opus-4-6",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Read",
                        "input": {"file_path": "README.md"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_2", "content": "ok"},
                ],
            },
        ],
    }

    try:
        bridge.parse_anthropic_messages(payload)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "do not match" in str(exc)


def test_render_prompt_with_tools_mentions_task_status_updates():
    payload = {
        "model": "claude-opus-4-6",
        "messages": [
            {"role": "user", "content": "请继续修改并维护任务状态"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "TodoWrite",
                    "description": "update todo list",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    }
    canonical = bridge.parse_openai_chat(payload)
    prompt = bridge.render_prompt(canonical)
    assert "必须在每次 tool_result 后立即更新完成状态与计数" in prompt


def test_render_prompt_prefers_explore_over_many_read_calls():
    payload = {
        "model": "claude-opus-4-6",
        "messages": [
            {"role": "user", "content": "请分析整个项目"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "Explore",
                    "description": "Explore and batch-read source files",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "Read",
                    "description": "Read a single file",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ],
    }
    canonical = bridge.parse_openai_chat(payload)
    prompt = bridge.render_prompt(canonical)
    assert "优先先发起一次 Explore" in prompt
    assert "避免在同一轮输出大量独立 Read 调用" in prompt


def test_render_prompt_with_required_named_tool_choice_adds_strict_guidance():
    payload = {
        "model": "claude-opus-4-6",
        "messages": [
            {"role": "user", "content": "请先读取 README"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "Read",
                    "description": "read file",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "Write",
                    "description": "write file",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ],
        "tool_choice": {"type": "function", "function": {"name": "Read"}},
    }
    canonical = bridge.parse_openai_chat(payload)
    prompt = bridge.render_prompt(canonical)

    assert "只能调用上面列出的工具；不要发明未声明的工具名" in prompt
    assert "本轮首个工具调用必须使用 `Read`" in prompt
    assert "在调用 `Read` 之前，不要先调用其他工具" in prompt


def test_render_prompt_with_required_any_tool_choice_forces_tool_call():
    payload = {
        "model": "claude-opus-4-6",
        "messages": [
            {"role": "user", "content": "请检查项目文件"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "Read",
                    "description": "read file",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ],
        "tool_choice": "required",
    }
    canonical = bridge.parse_openai_chat(payload)
    prompt = bridge.render_prompt(canonical)

    assert "本轮必须至少发起一次工具调用" in prompt


def test_parse_anthropic_mixed_tool_message_drops_verbose_preface():
    payload = {
        "model": "claude-opus-4-6",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "这是一个很长的初步分析，不应进入下一轮上下文。"},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Explore",
                        "input": {"path": "ipfs-file-manager-improved"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "done"},
                ],
            },
        ],
    }
    canonical = bridge.parse_anthropic_messages(payload)
    assistant_texts = [m.content for m in canonical.messages if m.role == "assistant"]
    assert assistant_texts
    assert "初步分析" not in assistant_texts[0]
    assert "[tool_call id=toolu_1 name=Explore]" in assistant_texts[0]


def test_extract_legacy_web_search_query():
    text = "Perform a web search for the query: 今日新闻 2026年3月27日"
    assert extract_legacy_web_search_query(text) == "今日新闻 2026年3月27日"


def test_extract_legacy_web_search_query_with_quotes():
    text = 'Perform a web search for the query: "top news today"'
    assert extract_legacy_web_search_query(text) == "top news today"


def test_extract_legacy_web_search_query_invalid():
    assert extract_legacy_web_search_query("Please help me search web") is None


def test_web_search_fallback_formats_results(monkeypatch):
    service = WebSearchFallbackService()

    async def fake_google(query: str, max_results: int):
        return [
            SearchResult(title="Result A", url="https://a.example.com", source="A News"),
            SearchResult(title="Result B", url="https://b.example.com", source="B News"),
        ]

    async def fake_duck(query: str, max_results: int):
        return []

    monkeypatch.setattr(service, "_search_google_news_rss", fake_google)
    monkeypatch.setattr(service, "_search_duckduckgo_instant", fake_duck)

    text = asyncio.run(service.search("test query", max_results=5))
    assert "1. Result A (A News) - https://a.example.com" in text
    assert "2. Result B (B News) - https://b.example.com" in text


def test_web_search_fallback_uses_duckduckgo_when_google_empty(monkeypatch):
    service = WebSearchFallbackService()

    async def fake_google(query: str, max_results: int):
        return []

    async def fake_duck(query: str, max_results: int):
        return [SearchResult(title="Duck Result", url="https://duck.example.com", source="DuckDuckGo")]

    monkeypatch.setattr(service, "_search_google_news_rss", fake_google)
    monkeypatch.setattr(service, "_search_duckduckgo_instant", fake_duck)

    text = asyncio.run(service.search("test query", max_results=5))
    assert "Duck Result" in text
    assert "https://duck.example.com" in text


def test_non_tool_json_block_does_not_create_tool_call():
    parsed = bridge.parse_model_output(
        """```json
{"title":"使用WebSearch获取今日新闻"}
```"""
    )
    assert parsed.has_tool_calls is False


def test_responses_function_call_history_is_preserved():
    canonical = bridge.parse_openai_responses({
        "model": "gpt-5",
        "input": [
            {"type": "function_call", "call_id": "call_1", "name": "read_file", "arguments": "{\"path\":\"a.txt\"}"},
            {"type": "function_call_output", "call_id": "call_1", "output": "hello"},
        ],
        "tools": [{"type": "function", "name": "read_file", "description": "Read", "parameters": {"type": "object"}}],
    })

    assert canonical.normalized_tool_choice.mode == "auto"
    assert "[tool_call id=call_1 name=read_file]" in canonical.messages[0].content
    assert "[tool_result id=call_1" in canonical.messages[1].content


def test_tool_choice_normalizes_all_public_protocol_shapes():
    chat = bridge.parse_openai_chat({
        "model": "gpt-5", "messages": [{"role": "user", "content": "read"}],
        "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}],
        "tool_choice": {"type": "function", "function": {"name": "read_file"}},
    })
    anthropic = bridge.parse_anthropic_messages({
        "model": "claude", "messages": [{"role": "user", "content": "read"}],
        "tools": [{"name": "read_file", "input_schema": {"type": "object"}}],
        "tool_choice": {"type": "any"},
    })

    assert (chat.normalized_tool_choice.mode, chat.normalized_tool_choice.name) == ("tool", "read_file")
    assert anthropic.normalized_tool_choice.mode == "any"


def test_named_tool_choice_rejects_undeclared_tool():
    try:
        bridge.parse_openai_chat({
            "model": "gpt-5", "messages": [], "tools": [],
            "tool_choice": {"type": "function", "function": {"name": "missing"}},
        })
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "undeclared tool" in str(exc)


def test_anthropic_validates_tool_results_in_earlier_turns():
    try:
        bridge.parse_anthropic_messages({
            "model": "claude",
            "messages": [
                {"role": "assistant", "content": [{"type": "tool_use", "id": "one", "name": "x", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "wrong", "content": "x"}]},
                {"role": "assistant", "content": "done"},
                {"role": "user", "content": "next"},
            ],
        })
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "do not match" in str(exc)


def test_parse_model_output_preserves_safe_upstream_call_id():
    parsed = bridge.parse_model_output(
        '[tool_call id=call_stable_123 name=Read]\n{"file_path":"README.md"}',
        allowed_tool_names={"Read"},
    )

    assert parsed.tool_calls[0].call_id == "call_stable_123"


def test_parse_model_output_does_not_promote_undeclared_tool():
    raw = '```json\n{"tool":"Bash","arguments":{"command":"rm -rf /"}}\n```'
    parsed = bridge.parse_model_output(raw, allowed_tool_names={"Read"})

    assert parsed.has_tool_calls is False
    assert parsed.text == raw
