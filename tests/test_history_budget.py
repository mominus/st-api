from types import SimpleNamespace

from app.services.gateway_runtime import GatewayRuntime
from app.services.history_budget import HistoryBudgetService
from app.services.protocol_bridge import CanonicalMessage, CanonicalRequest, ProtocolBridge


class _CharTokenCounter:
    def count(self, text: str) -> int:
        return len(text or "")


def test_history_budget_compacts_oldest_messages():
    bridge = ProtocolBridge()
    service = HistoryBudgetService(
        enabled=True,
        max_input_tokens=520,
        compact_max_chars=260,
        compact_recent_messages=2,
    )

    request = CanonicalRequest(
        source="openai_chat",
        model="claude-opus-4-6",
        system_prompt="You are a coding assistant.",
        messages=[
            CanonicalMessage(role="user", content="old context A " * 18),
            CanonicalMessage(role="assistant", content="old context B " * 18),
            CanonicalMessage(role="user", content="old context C " * 18),
            CanonicalMessage(role="assistant", content="old context D " * 18),
            CanonicalMessage(role="user", content="keep this recent question"),
        ],
    )

    result = service.compact_request(bridge, request, _CharTokenCounter())

    assert result.applied is True
    assert result.dropped_messages >= 2
    assert result.compacted_tokens < result.original_tokens
    assert result.compacted_tokens <= service.max_input_tokens
    assert result.request.messages[-1].content == "keep this recent question"
    assert all("old context A" not in msg.content for msg in result.request.messages)


def test_history_budget_preserves_anthropic_tool_use_and_result_pair():
    bridge = ProtocolBridge()
    service = HistoryBudgetService(
        enabled=True,
        max_input_tokens=480,
        compact_max_chars=220,
        compact_recent_messages=2,
    )

    request = CanonicalRequest(
        source="anthropic_messages",
        model="claude-opus-4-6",
        system_prompt="你是代码助手",
        messages=[
            CanonicalMessage(role="user", content="早期背景 " * 30),
            CanonicalMessage(
                role="assistant",
                content='[tool_call id=toolu_1 name=Read]\n{"file_path":"README.md"}',
            ),
            CanonicalMessage(role="user", content="[tool_result id=toolu_1]\nREADME 内容 " * 8),
            CanonicalMessage(role="user", content="继续分析 tests"),
        ],
        raw_messages=[
            {"role": "user", "content": [{"type": "text", "text": "早期背景 " * 30}]},
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
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "README 内容 " * 8,
                    }
                ],
            },
            {"role": "user", "content": [{"type": "text", "text": "继续分析 tests"}]},
        ],
    )

    result = service.compact_request(bridge, request, _CharTokenCounter())

    assert result.applied is True
    assert result.request.raw_messages[0]["role"] == "assistant"
    assert result.request.raw_messages[1]["role"] == "user"
    assert result.request.raw_messages[0]["content"][0]["type"] == "tool_use"
    assert result.request.raw_messages[1]["content"][0]["type"] == "tool_result"
    assert result.request.messages[0].role == "assistant"
    assert "[tool_call id=toolu_1 name=Read]" in result.request.messages[0].content
    assert result.request.messages[-1].content == "继续分析 tests"


def test_history_budget_keeps_notice_when_headroom_allows_it():
    bridge = ProtocolBridge()
    service = HistoryBudgetService(
        enabled=True,
        max_input_tokens=400,
        compact_max_chars=220,
        compact_recent_messages=2,
    )

    request = CanonicalRequest(
        source="anthropic_messages",
        model="claude-opus-4-6",
        system_prompt="你是代码助手",
        messages=[
            CanonicalMessage(role="user", content="早期背景 " * 30),
            CanonicalMessage(
                role="assistant",
                content='[tool_call id=toolu_1 name=Read]\n{"file_path":"README.md"}',
            ),
            CanonicalMessage(role="user", content="[tool_result id=toolu_1]\nREADME 内容 " * 8),
            CanonicalMessage(role="user", content="继续分析 tests"),
        ],
        raw_messages=[
            {"role": "user", "content": [{"type": "text", "text": "早期背景 " * 30}]},
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
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "README 内容 " * 8,
                    }
                ],
            },
            {"role": "user", "content": [{"type": "text", "text": "继续分析 tests"}]},
        ],
    )

    result = service.compact_request(bridge, request, _CharTokenCounter())

    assert result.applied is True
    assert "Gateway compacted history" in result.request.system_prompt
    assert result.compacted_tokens <= service.max_input_tokens


def test_history_budget_keeps_backend_payload_structured_context_consistent():
    bridge = ProtocolBridge()
    service = HistoryBudgetService(
        enabled=True,
        max_input_tokens=480,
        compact_max_chars=220,
        compact_recent_messages=2,
    )
    runtime = GatewayRuntime(token_counter=_CharTokenCounter())

    request = CanonicalRequest(
        source="anthropic_messages",
        model="claude-opus-4-6",
        system_prompt="你是代码助手",
        messages=[
            CanonicalMessage(role="user", content="早期背景 " * 30),
            CanonicalMessage(
                role="assistant",
                content='[tool_call id=toolu_1 name=Read]\n{"file_path":"README.md"}',
            ),
            CanonicalMessage(role="user", content="[tool_result id=toolu_1]\nREADME 内容 " * 8),
            CanonicalMessage(role="user", content="继续分析 tests"),
        ],
        raw_messages=[
            {"role": "user", "content": [{"type": "text", "text": "早期背景 " * 30}]},
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
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "README 内容 " * 8,
                    }
                ],
            },
            {"role": "user", "content": [{"type": "text", "text": "继续分析 tests"}]},
        ],
    )
    compacted = service.compact_request(bridge, request, _CharTokenCounter()).request

    payload = runtime.build_backend_payload(
        resolved=SimpleNamespace(
            input_mapping={
                "user_input": "in-0",
                "system_prompt": "in-1",
                "chat_history": "in-2",
                "model_id": "in-3",
            },
            model="claude-opus-4-6",
        ),
        prompt_text=bridge.render_prompt(compacted),
        user_id="api:key",
        canonical=compacted,
    )

    assert payload["in-0"] == "继续分析 tests"
    assert payload["in-1"] == "你是代码助手"
    assert "早期背景" not in payload["in-2"]
    assert "[tool_call id=toolu_1 name=Read]" in payload["in-2"]
    assert "[tool_result id=toolu_1]" in payload["in-2"]
    assert payload["in-3"] == "claude-opus-4-6"
