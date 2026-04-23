import asyncio
import json
from types import SimpleNamespace

from app.routers import anthropic as anthropic_router
from app.services.protocol_bridge import UsageNumbers, get_protocol_bridge
from app.services.response_transformer import get_response_transformer


class _FakeClient:
    host = "127.0.0.1"


class _FakeRequest:
    def __init__(self, payload):
        self._payload = payload
        self.client = _FakeClient()
        self.headers = {}

    async def json(self):
        return self._payload


class _FakeCanonical:
    def __init__(
        self,
        *,
        stream=True,
        tools=None,
        user_text="analyze repo",
        response_language="en",
        thinking=None,
        anthropic_beta=None,
    ):
        self.model = "claude-opus-4-6"
        self.stream = stream
        self.tools = list(tools or [])
        self.messages = [SimpleNamespace(role="user", content=user_text)]
        self.response_language = response_language
        self.thinking = thinking
        self.anthropic_beta = list(anthropic_beta or [])
        self.metadata = {}

    def input_preview(self):
        return self.messages[-1].content


class _FakeBridge:
    def __init__(self, canonical):
        self._canonical = canonical
        self._real = get_protocol_bridge()

    def parse_anthropic_messages(self, _payload, *, anthropic_beta_header=None):
        return self._canonical

    def extract_api_key(self, **_kwargs):
        return "sk-test"

    def render_prompt(self, _canonical):
        return "prompt text"

    def parse_model_output(self, output):
        return self._real.parse_model_output(output)


class _FakeRuntime:
    def __init__(self, *, stream_chunks=None, backend_response=None, final_usage=None):
        self._stream_chunks = list(stream_chunks or [])
        self._backend_response = backend_response or {"outputs": {"out-0": "done"}}
        self._final_usage = final_usage or UsageNumbers(input_tokens=7, output_tokens=11, total_tokens=18)
        self.token_counter = SimpleNamespace(count=lambda text: len(text or ""))

    @staticmethod
    def resolve_request_id(*, headers):
        return "req_test_stream"

    @staticmethod
    def resolve_client_ip(*, headers, fallback_client_ip):
        return fallback_client_ip

    @staticmethod
    def resolve_session_hint(*, headers, payload=None, allow_user_field=True):
        return None

    @staticmethod
    def resolve_backend_user_id(
        *,
        resolved,
        request_id,
        session_hint,
        client_ip,
        user_agent,
    ):
        return f"api:{resolved.api_key.key_prefix}:{request_id}"

    async def resolve_request(self, _session, *, raw_key, model, request_id=None):
        assert raw_key == "sk-test"
        return SimpleNamespace(
            request_id=request_id or "req_1",
            model=model,
            api_key=SimpleNamespace(key_prefix="test"),
            account=SimpleNamespace(id=1),
        )

    def build_backend_payload(self, **_kwargs):
        return {"in-0": "prompt"}

    async def run_stream(self, **_kwargs):
        async def _gen():
            for chunk in self._stream_chunks:
                yield chunk

        return _gen()

    async def run_sync(self, **_kwargs):
        return self._backend_response

    def parse_stream_chunk(self, raw_chunk):
        payload = json.loads(raw_chunk[5:].strip()) if raw_chunk.startswith("data:") else {}
        token = ""
        if isinstance(payload, dict):
            outputs = payload.get("outputs") or {}
            if isinstance(outputs, dict):
                token = str(outputs.get("out-0") or "")
        return token, None, None

    @staticmethod
    def merge_stream_usage(current, candidate):
        return candidate if candidate is not None else current

    def finalize_usage(self, *, output_text, **_kwargs):
        if output_text:
            return self._final_usage
        return UsageNumbers(
            input_tokens=self._final_usage.input_tokens,
            output_tokens=0,
            total_tokens=self._final_usage.input_tokens,
        )

    def usage_from_sync(self, **_kwargs):
        return self._final_usage

    @staticmethod
    def extract_content(backend_response):
        outputs = backend_response.get("outputs") or {}
        if isinstance(outputs, dict):
            return str(outputs.get("out-0") or "")
        return ""

    @staticmethod
    async def persist_success(*_args, **_kwargs):
        return None

    @staticmethod
    async def persist_error(*_args, **_kwargs):
        return None

    @staticmethod
    def elapsed_ms(_start_time):
        return 1

    def map_backend_exception(self, exc):
        raise AssertionError(f"unexpected exception: {exc}")


class _FailingStreamRuntime(_FakeRuntime):
    async def run_stream(self, **_kwargs):
        async def _gen():
            yield 'data: {"outputs":{"out-0":"partial"}}'
            raise RuntimeError("stream exploded")

        return _gen()

    def map_backend_exception(self, exc):
        assert str(exc) == "stream exploded"
        return anthropic_router.get_error_handler().create_service_unavailable_error(
            "Upstream service unavailable"
        )


class _FakeWebSearchService:
    async def search(self, query):
        assert query == "latest st-api news"
        return "1. Result - https://example.com"


class _CapturingResponseTransformer:
    def __init__(self):
        self._real = get_response_transformer()
        self.stream_mode = None
        self.stop_after_first_tool_call = None
        self.thinking_budget = None
        self.sync_thinking_enabled = None

    async def transform_backend_sse_to_anthropic(self, *args, **kwargs):
        self.stream_mode = "default"
        async for event in self._real.transform_backend_sse_to_anthropic(*args, **kwargs):
            yield event

    async def transform_backend_sse_to_anthropic_with_tools(self, *args, **kwargs):
        self.stream_mode = "tools"
        self.stop_after_first_tool_call = kwargs.get("stop_after_first_tool_call")
        async for event in self._real.transform_backend_sse_to_anthropic_with_tools(*args, **kwargs):
            yield event

    async def transform_backend_sse_to_anthropic_with_thinking(self, *args, **kwargs):
        self.stream_mode = "thinking"
        self.thinking_budget = kwargs.get("thinking_budget")
        async for event in self._real.transform_backend_sse_to_anthropic_with_thinking(*args, **kwargs):
            yield event

    def to_anthropic_response(self, *args, **kwargs):
        self.sync_thinking_enabled = kwargs.get("thinking_enabled")
        return self._real.to_anthropic_response(*args, **kwargs)

    def to_anthropic_response_with_tools(self, *args, **kwargs):
        return self._real.to_anthropic_response_with_tools(*args, **kwargs)


async def _collect_stream_body(response) -> str:
    parts = []
    async for chunk in response.body_iterator:
        if isinstance(chunk, bytes):
            parts.append(chunk.decode("utf-8", errors="replace"))
        else:
            parts.append(str(chunk))
    return "".join(parts)


async def _invoke_create_message(request):
    response = await anthropic_router.create_message(
        request,
        authorization="Bearer sk-test",
        x_api_key=None,
        anthropic_version=None,
        anthropic_beta=None,
        session=object(),
    )
    return await _collect_stream_body(response)


async def _invoke_create_message_json(request):
    response = await anthropic_router.create_message(
        request,
        authorization="Bearer sk-test",
        x_api_key=None,
        anthropic_version=None,
        anthropic_beta=None,
        session=object(),
    )
    return json.loads(response.body)


def _parse_anthropic_events(stream_body: str):
    parsed = []
    for block in stream_body.split("\n\n"):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        event_name = ""
        payload = None
        for line in lines:
            if line.startswith("event: "):
                event_name = line[7:].strip()
            elif line.startswith("data: "):
                payload = json.loads(line[6:])
        if event_name and payload is not None:
            parsed.append((event_name, payload))
    return parsed


def test_create_message_stream_with_tools_uses_runtime_usage(monkeypatch):
    canonical = _FakeCanonical(tools=[{"name": "Read"}])
    bridge = _FakeBridge(canonical)
    runtime = _FakeRuntime(
        stream_chunks=[
            'data: {"outputs":{"out-0":"I will inspect the repo. "}}',
            'data: {"outputs":{"out-0":"```json\\n{\\"tool\\":\\"Read\\",\\"arguments\\":{\\"file_path\\":\\"README.md\\"}}\\n```"}}',
        ],
        final_usage=UsageNumbers(input_tokens=9, output_tokens=14, total_tokens=23),
    )

    monkeypatch.setattr(anthropic_router, "get_protocol_bridge", lambda: bridge)
    monkeypatch.setattr(anthropic_router, "get_gateway_runtime", lambda: runtime)

    request = _FakeRequest(
        {
            "model": "claude-opus-4-6",
            "stream": True,
            "messages": [{"role": "user", "content": "analyze repo"}],
            "tools": [{"name": "Read"}],
        }
    )

    body = asyncio.run(_invoke_create_message(request))
    events = _parse_anthropic_events(body)

    message_start = next(payload for event, payload in events if event == "message_start")
    message_delta = next(payload for event, payload in reversed(events) if event == "message_delta")

    assert message_start["message"]["usage"]["input_tokens"] == 9
    assert message_delta["usage"]["output_tokens"] == 14
    assert message_delta["delta"]["stop_reason"] == "tool_use"


def test_create_message_legacy_search_stream_uses_runtime_usage(monkeypatch):
    canonical = _FakeCanonical(user_text="Perform a web search for the query: latest st-api news")
    bridge = _FakeBridge(canonical)
    runtime = _FakeRuntime(final_usage=UsageNumbers(input_tokens=6, output_tokens=10, total_tokens=16))

    monkeypatch.setattr(anthropic_router, "get_protocol_bridge", lambda: bridge)
    monkeypatch.setattr(anthropic_router, "get_gateway_runtime", lambda: runtime)
    monkeypatch.setattr(
        anthropic_router,
        "get_web_search_fallback_service",
        lambda: _FakeWebSearchService(),
    )

    request = _FakeRequest(
        {
            "model": "claude-opus-4-6",
            "stream": True,
            "messages": [
                {
                    "role": "user",
                    "content": "Perform a web search for the query: latest st-api news",
                }
            ],
        }
    )

    body = asyncio.run(_invoke_create_message(request))
    events = _parse_anthropic_events(body)

    message_start = next(payload for event, payload in events if event == "message_start")
    message_delta = next(payload for event, payload in reversed(events) if event == "message_delta")

    assert message_start["message"]["usage"]["input_tokens"] == 6
    assert message_start["message"]["usage"]["server_tool_use"]["web_search_requests"] == 1
    assert message_delta["usage"]["output_tokens"] == 10
    assert message_delta["usage"]["server_tool_use"]["web_search_requests"] == 1


def test_create_message_stream_with_thinking_tags_selects_thinking_transformer(monkeypatch):
    canonical = _FakeCanonical(
        thinking={"type": "enabled", "budget_tokens": 2048},
    )
    bridge = _FakeBridge(canonical)
    runtime = _FakeRuntime(
        stream_chunks=[
            'data: {"outputs":{"out-0":"<thinking>先看入口文件</thinking>最终结论"}}',
        ],
        final_usage=UsageNumbers(input_tokens=8, output_tokens=12, total_tokens=20),
    )
    transformer = _CapturingResponseTransformer()

    monkeypatch.setattr(anthropic_router, "get_protocol_bridge", lambda: bridge)
    monkeypatch.setattr(anthropic_router, "get_gateway_runtime", lambda: runtime)
    monkeypatch.setattr(anthropic_router, "get_response_transformer", lambda: transformer)

    request = _FakeRequest(
        {
            "model": "claude-opus-4-6",
            "stream": True,
            "messages": [{"role": "user", "content": "analyze repo"}],
            "thinking": {"type": "enabled", "budget_tokens": 2048},
        }
    )

    body = asyncio.run(_invoke_create_message(request))
    events = _parse_anthropic_events(body)
    content_block_types = [
        payload["content_block"]["type"]
        for event, payload in events
        if event == "content_block_start"
    ]

    assert transformer.stream_mode == "thinking"
    assert transformer.thinking_budget == 2048
    assert content_block_types[:2] == ["thinking", "text"]


def test_create_message_stream_with_thinking_hint_but_no_tags_falls_back_to_default(monkeypatch):
    canonical = _FakeCanonical(
        thinking={"type": "adaptive"},
    )
    bridge = _FakeBridge(canonical)
    runtime = _FakeRuntime(
        stream_chunks=[
            'data: {"outputs":{"out-0":"这里没有思维标签，直接给结论。"}}',
        ],
    )
    transformer = _CapturingResponseTransformer()

    monkeypatch.setattr(anthropic_router, "get_protocol_bridge", lambda: bridge)
    monkeypatch.setattr(anthropic_router, "get_gateway_runtime", lambda: runtime)
    monkeypatch.setattr(anthropic_router, "get_response_transformer", lambda: transformer)

    request = _FakeRequest(
        {
            "model": "claude-opus-4-6",
            "stream": True,
            "messages": [{"role": "user", "content": "analyze repo"}],
            "thinking": {"type": "adaptive"},
        }
    )

    asyncio.run(_invoke_create_message(request))

    assert transformer.stream_mode == "default"


def test_create_message_stream_claude_code_beta_keeps_tool_transformer_priority(monkeypatch):
    canonical = _FakeCanonical(
        tools=[{"name": "Read"}],
        thinking={"type": "adaptive"},
        anthropic_beta=["claude-code-20250219"],
    )
    bridge = _FakeBridge(canonical)
    runtime = _FakeRuntime(
        stream_chunks=[
            'data: {"outputs":{"out-0":"```json\\n{\\"tool\\":\\"Read\\",\\"arguments\\":{\\"file_path\\":\\"README.md\\"}}\\n```"}}',
        ],
    )
    transformer = _CapturingResponseTransformer()

    monkeypatch.setattr(anthropic_router, "get_protocol_bridge", lambda: bridge)
    monkeypatch.setattr(anthropic_router, "get_gateway_runtime", lambda: runtime)
    monkeypatch.setattr(anthropic_router, "get_response_transformer", lambda: transformer)

    request = _FakeRequest(
        {
            "model": "claude-opus-4-6",
            "stream": True,
            "messages": [{"role": "user", "content": "analyze repo"}],
            "tools": [{"name": "Read"}],
            "thinking": {"type": "adaptive"},
        }
    )

    body = asyncio.run(_invoke_create_message(request))
    events = _parse_anthropic_events(body)
    message_delta = next(payload for event, payload in reversed(events) if event == "message_delta")

    assert transformer.stream_mode == "tools"
    assert transformer.stop_after_first_tool_call is True
    assert message_delta["delta"]["stop_reason"] == "tool_use"


def test_create_message_stream_without_claude_code_beta_disables_tool_early_stop(monkeypatch):
    canonical = _FakeCanonical(
        tools=[{"name": "Read"}],
        anthropic_beta=[],
    )
    bridge = _FakeBridge(canonical)
    runtime = _FakeRuntime(
        stream_chunks=[
            'data: {"outputs":{"out-0":"```json\\n{\\"tool\\":\\"Read\\",\\"arguments\\":{\\"file_path\\":\\"README.md\\"}}\\n```"}}',
        ],
    )
    transformer = _CapturingResponseTransformer()

    monkeypatch.setattr(anthropic_router, "get_protocol_bridge", lambda: bridge)
    monkeypatch.setattr(anthropic_router, "get_gateway_runtime", lambda: runtime)
    monkeypatch.setattr(anthropic_router, "get_response_transformer", lambda: transformer)

    request = _FakeRequest(
        {
            "model": "claude-opus-4-6",
            "stream": True,
            "messages": [{"role": "user", "content": "analyze repo"}],
            "tools": [{"name": "Read"}],
        }
    )

    asyncio.run(_invoke_create_message(request))

    assert transformer.stream_mode == "tools"
    assert transformer.stop_after_first_tool_call is False


def test_create_message_sync_with_thinking_hint_emits_thinking_block(monkeypatch):
    canonical = _FakeCanonical(
        stream=False,
        thinking={"type": "adaptive"},
    )
    bridge = _FakeBridge(canonical)
    runtime = _FakeRuntime(
        backend_response={
            "outputs": {
                "out-0": "<thinking>先检查 README 和 tests</thinking>这是最终答复"
            }
        },
        final_usage=UsageNumbers(input_tokens=5, output_tokens=9, total_tokens=14),
    )
    transformer = _CapturingResponseTransformer()

    monkeypatch.setattr(anthropic_router, "get_protocol_bridge", lambda: bridge)
    monkeypatch.setattr(anthropic_router, "get_gateway_runtime", lambda: runtime)
    monkeypatch.setattr(anthropic_router, "get_response_transformer", lambda: transformer)

    request = _FakeRequest(
        {
            "model": "claude-opus-4-6",
            "stream": False,
            "messages": [{"role": "user", "content": "analyze repo"}],
            "thinking": {"type": "adaptive"},
        }
    )

    response = asyncio.run(_invoke_create_message_json(request))

    assert transformer.sync_thinking_enabled is True
    assert [block["type"] for block in response["content"]] == ["thinking", "text"]
    assert response["content"][0]["thinking"] == "先检查 README 和 tests"


def test_create_message_invalid_tool_result_sequence_returns_anthropic_invalid_request(monkeypatch):
    runtime = _FakeRuntime()
    monkeypatch.setattr(anthropic_router, "get_gateway_runtime", lambda: runtime)

    request = _FakeRequest(
        {
            "model": "claude-opus-4-6",
            "stream": False,
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_expected",
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
                            "tool_use_id": "toolu_mismatch",
                            "content": "README content",
                        }
                    ],
                },
            ],
        }
    )

    response = asyncio.run(_invoke_create_message_json(request))

    assert response["type"] == "error"
    assert response["error"]["type"] == "invalid_request_error"
    assert "do not match" in response["error"]["message"]


def test_create_message_stream_failure_emits_anthropic_error_event(monkeypatch):
    canonical = _FakeCanonical()
    bridge = _FakeBridge(canonical)
    runtime = _FailingStreamRuntime()

    monkeypatch.setattr(anthropic_router, "get_protocol_bridge", lambda: bridge)
    monkeypatch.setattr(anthropic_router, "get_gateway_runtime", lambda: runtime)

    request = _FakeRequest(
        {
            "model": "claude-opus-4-6",
            "stream": True,
            "messages": [{"role": "user", "content": "analyze repo"}],
        }
    )

    body = asyncio.run(_invoke_create_message(request))
    events = _parse_anthropic_events(body)
    error_payload = next(payload for event, payload in events if event == "error")

    assert error_payload == {
        "type": "error",
        "error": {
            "type": "overloaded_error",
            "message": "Upstream service unavailable",
        },
    }
