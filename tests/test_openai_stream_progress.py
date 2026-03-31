import asyncio
import json
from types import SimpleNamespace

from app.routers import openai as openai_router
from app.services.protocol_bridge import UsageNumbers, get_protocol_bridge


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
    def __init__(self):
        self.model = "claude-opus-4-6"
        self.stream = True
        self.tools = [{"name": "Read"}]
        self.messages = [SimpleNamespace(role="user", content="list files")]

    def input_preview(self):
        return "list files"


class _FakeBridge:
    def __init__(self, canonical):
        self._canonical = canonical
        self._real = get_protocol_bridge()

    def parse_openai_chat(self, _payload):
        return self._canonical

    def parse_openai_responses(self, _payload):
        return self._canonical

    def extract_api_key(self, **_kwargs):
        return "sk-test"

    def render_prompt(self, _canonical):
        return "prompt"

    def parse_model_output(self, output):
        return self._real.parse_model_output(output)

    def to_openai_responses_response(self, **kwargs):
        return self._real.to_openai_responses_response(**kwargs)


class _FakeRuntime:
    def __init__(self, stream_tokens):
        self._stream_tokens = stream_tokens

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
            for token in self._stream_tokens:
                yield token

        return _gen()

    def parse_stream_chunk(self, raw_chunk):
        return raw_chunk, None, None

    @staticmethod
    def merge_stream_usage(current, candidate):
        return candidate if candidate is not None else current

    @staticmethod
    def finalize_usage(**_kwargs):
        return UsageNumbers(input_tokens=3, output_tokens=5, total_tokens=8)

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


async def _collect_stream_body(response) -> str:
    parts = []
    async for chunk in response.body_iterator:
        if isinstance(chunk, bytes):
            parts.append(chunk.decode("utf-8", errors="replace"))
        else:
            parts.append(str(chunk))
    return "".join(parts)


def _parse_openai_data_events(stream_body: str):
    events = []
    for line in stream_body.splitlines():
        if not line.startswith("data: "):
            continue
        data = line[6:].strip()
        if not data or data == "[DONE]":
            continue
        events.append(json.loads(data))
    return events


def _parse_responses_events(stream_body: str):
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


def test_chat_completions_stream_with_tools_emits_incremental_content(monkeypatch):
    canonical = _FakeCanonical()
    bridge = _FakeBridge(canonical)
    runtime = _FakeRuntime(
        [
            "hello ",
            "world ",
            "```json\n{\"tool\":\"Read\",\"arguments\":{\"file_path\":\"README.md\"}}\n```",
        ]
    )

    monkeypatch.setattr(openai_router, "get_protocol_bridge", lambda: bridge)
    monkeypatch.setattr(openai_router, "get_gateway_runtime", lambda: runtime)

    request = _FakeRequest(
        {
            "model": "claude-opus-4-6",
            "stream": True,
            "messages": [{"role": "user", "content": "list files"}],
            "tools": [{"type": "function", "function": {"name": "Read"}}],
        }
    )

    response = asyncio.run(
        openai_router.chat_completions(
            request,
            authorization="Bearer sk-test",
            x_api_key=None,
            session=object(),
        )
    )
    body = asyncio.run(_collect_stream_body(response))
    events = _parse_openai_data_events(body)

    content_deltas = []
    finish_reasons = []
    for event in events:
        choice = event["choices"][0]
        delta = choice.get("delta") or {}
        if "content" in delta:
            content_deltas.append(delta["content"])
        finish_reasons.append(choice.get("finish_reason"))

    merged = "".join(content_deltas)
    assert merged.startswith("hello world ")
    assert '"tool":"Read"' not in merged
    assert "tool_calls" in finish_reasons


def test_responses_stream_with_tools_emits_incremental_output_text(monkeypatch):
    canonical = _FakeCanonical()
    bridge = _FakeBridge(canonical)
    runtime = _FakeRuntime(
        [
            "step-1 ",
            "step-2 ",
            "```json\n{\"tool\":\"Read\",\"arguments\":{\"file_path\":\"README.md\"}}\n```",
        ]
    )

    monkeypatch.setattr(openai_router, "get_protocol_bridge", lambda: bridge)
    monkeypatch.setattr(openai_router, "get_gateway_runtime", lambda: runtime)

    request = _FakeRequest(
        {
            "model": "claude-opus-4-6",
            "stream": True,
            "input": "list files",
            "tools": [{"type": "function", "function": {"name": "Read"}}],
        }
    )

    response = asyncio.run(
        openai_router.create_response(
            request,
            authorization="Bearer sk-test",
            x_api_key=None,
            session=object(),
        )
    )
    body = asyncio.run(_collect_stream_body(response))
    events = _parse_responses_events(body)

    deltas = [payload["delta"] for name, payload in events if name == "response.output_text.delta"]
    event_names = [name for name, _payload in events]

    merged = "".join(deltas)
    assert merged.startswith("step-1 step-2 ")
    assert '"tool":"Read"' not in merged
    assert "response.function_call_arguments.done" in event_names


def test_chat_completions_stream_with_tools_but_no_tool_call_still_streams(monkeypatch):
    canonical = _FakeCanonical()
    bridge = _FakeBridge(canonical)
    runtime = _FakeRuntime(["plain ", "text ", "answer"])

    monkeypatch.setattr(openai_router, "get_protocol_bridge", lambda: bridge)
    monkeypatch.setattr(openai_router, "get_gateway_runtime", lambda: runtime)

    request = _FakeRequest(
        {
            "model": "claude-opus-4-6",
            "stream": True,
            "messages": [{"role": "user", "content": "list files"}],
            "tools": [{"type": "function", "function": {"name": "Read"}}],
        }
    )

    response = asyncio.run(
        openai_router.chat_completions(
            request,
            authorization="Bearer sk-test",
            x_api_key=None,
            session=object(),
        )
    )
    body = asyncio.run(_collect_stream_body(response))
    events = _parse_openai_data_events(body)

    content_deltas = []
    finish_reasons = []
    for event in events:
        choice = event["choices"][0]
        delta = choice.get("delta") or {}
        if "content" in delta:
            content_deltas.append(delta["content"])
        finish_reasons.append(choice.get("finish_reason"))

    assert "".join(content_deltas) == "plain text answer"
    assert "stop" in finish_reasons
