import json
import asyncio

from app.services.protocol_bridge import UsageNumbers, get_protocol_bridge
from app.services.response_transformer import get_response_transformer
from app.services.tool_parser import ToolParser


bridge = get_protocol_bridge()


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
