"""Anthropic-compatible router (rewritten with unified protocol/runtime core)."""

from __future__ import annotations

import copy
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_session
from app.services.error_handler import APIError, get_error_handler
from app.services.gateway_runtime import GatewayAuthError, get_gateway_runtime
from app.services.history_budget import get_history_budget_service
from app.services.protocol_bridge import CanonicalRequest, UsageNumbers, get_protocol_bridge
from app.services.response_transformer import get_response_transformer
from app.services.tool_parser import ToolParser
from app.services.tool_registry import ToolRegistry, ToolSchema
from app.services.web_search_fallback import (
    extract_legacy_web_search_query,
    get_web_search_fallback_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["Anthropic Compatible"])

_CLAUDE_CODE_CORE_BETA = "claude-code-20250219"


class MessagesRequest(BaseModel):
    model_config = {"extra": "allow"}

    model: str = Field(...)
    messages: List[Dict[str, Any]] = Field(...)
    stream: bool = Field(default=False)


class CountTokensRequest(BaseModel):
    model_config = {"extra": "allow"}

    model: str = Field(...)
    messages: List[Dict[str, Any]] = Field(...)


def _anthropic_error_payload(error_handler, error: APIError) -> Dict[str, Any]:
    return error_handler.to_anthropic_error(error)


def _anthropic_error_response(
    error_handler,
    error: APIError,
    *,
    request_id: Optional[str] = None,
) -> JSONResponse:
    headers = {"X-Request-ID": request_id} if request_id else None
    return JSONResponse(
        status_code=error.status_code,
        content=_anthropic_error_payload(error_handler, error),
        headers=headers,
    )


def _anthropic_event(event: str, payload: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _invalid_request_capture_enabled() -> bool:
    for name in ("ST_DEBUG_INVALID_ANTHROPIC_REQUESTS", "DEBUG"):
        value = str(os.getenv(name, "") or "").strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return True
    return False


def _all_request_capture_enabled() -> bool:
    for name in ("ST_DEBUG_CAPTURE_ALL_ANTHROPIC_REQUESTS", "DEBUG"):
        value = str(os.getenv(name, "") or "").strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return True
    return False


def _redact_invalid_request_value(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: Dict[str, Any] = {}
        for key, inner_value in value.items():
            lowered = str(key or "").strip().lower()
            if lowered in {"authorization", "x-api-key", "api_key", "cookie"}:
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact_invalid_request_value(inner_value)
        return redacted
    if isinstance(value, list):
        return [_redact_invalid_request_value(item) for item in value]
    return value


def _summarize_anthropic_block(block: Any) -> Dict[str, Any]:
    if not isinstance(block, dict):
        return {"python_type": type(block).__name__}

    block_type = str(block.get("type") or "").strip() or "<missing>"
    summary: Dict[str, Any] = {"type": block_type}

    if block_type == "tool_use":
        tool_name = str(block.get("name") or "").strip()
        tool_id = str(block.get("id") or "").strip()
        if tool_name:
            summary["name"] = tool_name
        if tool_id:
            summary["id"] = tool_id
        return summary

    if block_type == "tool_result":
        tool_use_id = str(block.get("tool_use_id") or "").strip()
        if tool_use_id:
            summary["tool_use_id"] = tool_use_id
        content = block.get("content")
        if isinstance(content, list):
            summary["inner_count"] = len(content)
            summary["inner_types"] = [
                str(item.get("type") or "").strip() if isinstance(item, dict) else type(item).__name__
                for item in content[:8]
            ]
        elif isinstance(content, str):
            summary["inner_kind"] = "string"
            summary["inner_text_len"] = len(content)
        elif content is None:
            summary["inner_kind"] = "none"
        else:
            summary["inner_kind"] = type(content).__name__
        return summary

    if block_type == "text":
        text = str(block.get("text") or "")
        summary["text_len"] = len(text)
        return summary

    return summary


def _summarize_anthropic_message(message: Any) -> Dict[str, Any]:
    if not isinstance(message, dict):
        return {"python_type": type(message).__name__}

    content = message.get("content")
    if isinstance(content, list):
        blocks = content
    elif content is None:
        blocks = []
    else:
        blocks = [content]

    return {
        "role": str(message.get("role") or "").strip() or "<missing>",
        "block_count": len(blocks),
        "block_types": [
            str(block.get("type") or "").strip() if isinstance(block, dict) else type(block).__name__
            for block in blocks[:12]
        ],
        "blocks": [_summarize_anthropic_block(block) for block in blocks[:12]],
    }


def _capture_invalid_anthropic_request(
    *,
    http_request: Request,
    body_json: Any,
    exc: Exception,
    route_name: str,
    request_id: Optional[str] = None,
    anthropic_beta: Optional[str] = None,
) -> None:
    if not _invalid_request_capture_enabled():
        return

    _capture_anthropic_request(
        http_request=http_request,
        body_json=body_json,
        route_name=route_name,
        request_id=request_id,
        anthropic_beta=anthropic_beta,
        capture_kind="invalid",
        error=str(exc),
    )


def _capture_anthropic_request(
    *,
    http_request: Request,
    body_json: Any,
    route_name: str,
    request_id: Optional[str] = None,
    anthropic_beta: Optional[str] = None,
    capture_kind: str = "debug",
    error: Optional[str] = None,
) -> None:
    try:
        messages = body_json.get("messages") if isinstance(body_json, dict) else None
        last_message = messages[-1] if isinstance(messages, list) and messages else None
        previous_message = messages[-2] if isinstance(messages, list) and len(messages) >= 2 else None

        summary = {
            "route": route_name,
            "request_id": request_id,
            "capture_kind": capture_kind,
            "error": error,
            "model": body_json.get("model") if isinstance(body_json, dict) else None,
            "message_count": len(messages) if isinstance(messages, list) else None,
            "last_message": _summarize_anthropic_message(last_message),
            "previous_message": _summarize_anthropic_message(previous_message),
            "user_agent": http_request.headers.get("user-agent"),
            "anthropic_beta": anthropic_beta,
        }

        capture_payload = {
            "captured_at": time.time(),
            "summary": summary,
            "headers": _redact_invalid_request_value(dict(http_request.headers)),
            "body": _redact_invalid_request_value(body_json),
        }

        suffix = request_id or str(int(time.time() * 1000))
        prefix = "st_api_invalid_anthropic" if capture_kind == "invalid" else "st_api_anthropic_request"
        capture_path = Path("/tmp") / f"{prefix}_{suffix}.json"
        capture_path.write_text(
            json.dumps(capture_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        logger.warning(
            "Captured anthropic request route=%s request_id=%s kind=%s dump=%s summary=%s",
            route_name,
            request_id,
            capture_kind,
            capture_path,
            json.dumps(summary, ensure_ascii=False),
        )
    except Exception:
        logger.exception("Failed to capture anthropic request")


def _tool_parser_for_request(canonical: CanonicalRequest) -> ToolParser:
    if not canonical.tools:
        return ToolParser(registry=None)

    registry = ToolRegistry()
    for tool in canonical.tools:
        if isinstance(tool, dict):
            tool_name = str(tool.get("name") or "").strip()
            tool_description = str(tool.get("description") or "")
            tool_input_schema = tool.get("input_schema") or tool.get("parameters") or {}
        else:
            tool_name = str(getattr(tool, "name", "") or "").strip()
            tool_description = str(getattr(tool, "description", "") or "")
            tool_input_schema = getattr(tool, "input_schema", None) or {}

        if not tool_name:
            continue

        registry.register_tool(
            ToolSchema(
                name=tool_name,
                description=tool_description,
                input_schema=copy.deepcopy(tool_input_schema if isinstance(tool_input_schema, dict) else {}),
            )
        )
    return ToolParser(registry=registry, allow_unknown_tools=False)


def _canonical_has_beta(canonical: CanonicalRequest, beta_name: str) -> bool:
    betas = getattr(canonical, "anthropic_beta", None)
    if not isinstance(betas, list):
        return False
    return beta_name in betas


def _is_claude_code_request(canonical: CanonicalRequest) -> bool:
    return _canonical_has_beta(canonical, _CLAUDE_CODE_CORE_BETA)


def _thinking_requested(canonical: CanonicalRequest) -> bool:
    thinking = getattr(canonical, "thinking", None)
    if not isinstance(thinking, dict) or not thinking:
        return False
    thinking_type = str(thinking.get("type") or "").strip().lower()
    return thinking_type != "disabled"


def _thinking_budget(canonical: CanonicalRequest) -> int:
    thinking = getattr(canonical, "thinking", None)
    if not isinstance(thinking, dict):
        return 10000
    try:
        budget = int(thinking.get("budget_tokens"))
    except Exception:
        return 10000
    return budget if budget > 0 else 10000


def _contains_thinking_marker(text: str) -> bool:
    if not isinstance(text, str) or not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in ("<thinking>", "<think>", "<thought>")) or any(
        marker in text for marker in ("【思考】", "[思考]")
    )


def _select_stream_transform_mode(
    canonical: CanonicalRequest,
    *,
    preview_text: str,
) -> str:
    if canonical.tools:
        return "tools"
    if _thinking_requested(canonical) and _contains_thinking_marker(preview_text):
        return "thinking"
    return "default"


def _parse_anthropic_stream_event(raw_event: str) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
    event_name: Optional[str] = None
    data_parts: List[str] = []

    for line in raw_event.splitlines():
        if line.startswith("event: "):
            event_name = line[7:].strip()
        elif line.startswith("data: "):
            data_parts.append(line[6:])

    if not event_name or not data_parts:
        return None, None

    try:
        payload = json.loads("\n".join(data_parts))
    except Exception:
        return event_name, None

    if not isinstance(payload, dict):
        return event_name, None
    return event_name, payload


def _inject_usage_into_anthropic_event(
    raw_event: str,
    *,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    server_tool_use: Optional[Dict[str, Any]] = None,
) -> str:
    event_name, payload = _parse_anthropic_stream_event(raw_event)
    if not event_name or payload is None:
        return raw_event

    if event_name == "message_start":
        message = payload.get("message")
        if not isinstance(message, dict):
            return raw_event
        usage = message.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        if input_tokens is not None:
            usage["input_tokens"] = max(0, int(input_tokens))
        if output_tokens is not None:
            usage["output_tokens"] = max(0, int(output_tokens))
        if server_tool_use is not None:
            usage["server_tool_use"] = copy.deepcopy(server_tool_use)
        message["usage"] = usage
        payload["message"] = message
        return _anthropic_event(event_name, payload)

    if event_name == "message_delta":
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        if output_tokens is not None:
            usage["output_tokens"] = max(0, int(output_tokens))
        if server_tool_use is not None:
            usage["server_tool_use"] = copy.deepcopy(server_tool_use)
        payload["usage"] = usage
        return _anthropic_event(event_name, payload)

    return raw_event


async def _persist_stream_success(
    runtime,
    session,
    *,
    resolved,
    canonical: CanonicalRequest,
    output_preview: str,
    usage: UsageNumbers,
    start_time: float,
    client_ip: Optional[str],
) -> None:
    await runtime.persist_success(
        session,
        resolved=resolved,
        api_type="anthropic",
        input_preview=canonical.input_preview(),
        output_preview=output_preview,
        usage=usage,
        response_time_ms=runtime.elapsed_ms(start_time),
        is_stream=True,
        client_ip=client_ip,
    )


def _map_validation_error(error_handler, message: str) -> APIError:
    lowered = (message or "").lower()
    if "not authorized" in lowered:
        return error_handler.create_permission_error(message)
    if "quota" in lowered:
        return error_handler.create_quota_exceeded_error(message)
    if "expired" in lowered:
        return error_handler.create_authentication_error(message)
    return error_handler.create_authentication_error(message)


def _compact_tool_preface(response_data: Dict[str, Any], response_language: str) -> Dict[str, Any]:
    content = response_data.get("content")
    if not isinstance(content, list) or not content:
        return response_data

    has_tool_use = any(
        isinstance(block, dict) and block.get("type") == "tool_use"
        for block in content
    )
    if not has_tool_use:
        return response_data

    text_blocks = [
        block for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    if not text_blocks:
        return response_data

    short_preface = "我先检查相关文件。" if response_language == "zh" else "I'll inspect the relevant files first."
    new_content: List[Dict[str, Any]] = []
    text_replaced = False
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            if not text_replaced:
                new_content.append({"type": "text", "text": short_preface})
                text_replaced = True
            continue
        new_content.append(block)

    response_data["content"] = new_content
    return response_data


def _collapse_redundant_revision(response_data: Dict[str, Any], response_language: str) -> Dict[str, Any]:
    content = response_data.get("content")
    if not isinstance(content, list) or not content:
        return response_data

    text_blocks = [
        block for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    if not text_blocks:
        return response_data

    full_text = "\n".join(str(block.get("text") or "") for block in text_blocks).strip()
    if len(full_text) < 300:
        return response_data

    markers_zh = [
        "根据深入探索的结果",
        "让我更新分析",
        "更新分析",
        "修正",
    ]
    markers_en = [
        "I need to correct my initial analysis",
        "Let me update",
        "Updated analysis",
        "Based on deeper exploration",
    ]
    markers = markers_zh if response_language == "zh" else markers_en

    split_at = -1
    for marker in markers:
        pos = full_text.rfind(marker)
        if pos > split_at:
            split_at = pos

    if split_at <= 120:
        return response_data

    compacted = full_text[split_at:].strip()
    if len(compacted) < 120:
        return response_data

    replaced = False
    new_content: List[Dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            if not replaced:
                new_content.append({"type": "text", "text": compacted})
                replaced = True
            continue
        new_content.append(block)

    response_data["content"] = new_content
    return response_data


@router.post("/messages/debug")
async def debug_message(
    http_request: Request,
    session: AsyncSession = Depends(get_session),
):
    bridge = get_protocol_bridge()
    runtime = get_gateway_runtime()
    history_budget = get_history_budget_service()
    body_json = await http_request.json()
    try:
        canonical = bridge.parse_anthropic_messages(body_json)
        budget_result = history_budget.compact_request(bridge, canonical, runtime.token_counter)
        canonical = budget_result.request
    except Exception as exc:
        _capture_invalid_anthropic_request(
            http_request=http_request,
            body_json=body_json,
            exc=exc,
            route_name="/v1/messages/debug",
        )
        raise
    return {
        "model": canonical.model,
        "stream": canonical.stream,
        "system_prompt": canonical.system_prompt,
        "messages": [m.__dict__ for m in canonical.messages],
        "tools": [t.__dict__ for t in canonical.tools],
        "rendered_prompt": bridge.render_prompt(canonical),
        "history_budget": {
            "applied": budget_result.applied,
            "original_tokens": budget_result.original_tokens,
            "compacted_tokens": budget_result.compacted_tokens,
            "dropped_messages": budget_result.dropped_messages,
        },
    }


@router.post("/messages/count_tokens")
async def count_tokens(
    http_request: Request,
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    authorization: Optional[str] = Header(None),
    anthropic_version: Optional[str] = Header(None, alias="anthropic-version"),
    anthropic_beta: Optional[str] = Header(None, alias="anthropic-beta"),
    session: AsyncSession = Depends(get_session),
):
    bridge = get_protocol_bridge()
    runtime = get_gateway_runtime()
    error_handler = get_error_handler()
    history_budget = get_history_budget_service()

    try:
        body_json = await http_request.json()
        CountTokensRequest(**body_json)
        canonical = bridge.parse_anthropic_messages(
            body_json,
            anthropic_beta_header=anthropic_beta,
        )
    except Exception as exc:
        _capture_invalid_anthropic_request(
            http_request=http_request,
            body_json=body_json if "body_json" in locals() else None,
            exc=exc,
            route_name="/v1/messages/count_tokens",
            anthropic_beta=anthropic_beta,
        )
        error = error_handler.create_invalid_request_error(str(exc))
        return _anthropic_error_response(error_handler, error)

    if anthropic_version:
        logger.debug("anthropic-version=%s", anthropic_version)
    if anthropic_beta:
        logger.debug("anthropic-beta=%s", anthropic_beta)

    raw_key = bridge.extract_api_key(
        authorization=authorization,
        x_api_key=x_api_key,
    )
    if not raw_key:
        error = error_handler.create_authentication_error("Missing API key")
        return _anthropic_error_response(error_handler, error)

    try:
        valid, error_msg, _api_key_obj = await runtime.run_db_guarded(
            session,
            lambda: runtime.api_key_service.validate_key(
                session,
                raw_key,
                canonical.model,
            ),
        )
    except GatewayAuthError as exc:
        return _anthropic_error_response(error_handler, exc.error)
    if not valid:
        error = _map_validation_error(error_handler, error_msg or "Invalid API key")
        return _anthropic_error_response(error_handler, error)

    budget_result = history_budget.compact_request(bridge, canonical, runtime.token_counter)
    canonical = budget_result.request
    prompt_text = bridge.render_prompt(canonical)
    token_count = runtime.token_counter.count(prompt_text)
    return {"input_tokens": int(max(0, token_count))}


@router.post("/messages")
async def create_message(
    http_request: Request,
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    authorization: Optional[str] = Header(None),
    anthropic_version: Optional[str] = Header(None, alias="anthropic-version"),
    anthropic_beta: Optional[str] = Header(None, alias="anthropic-beta"),
    session: AsyncSession = Depends(get_session),
):
    bridge = get_protocol_bridge()
    runtime = get_gateway_runtime()
    response_transformer = get_response_transformer()
    error_handler = get_error_handler()
    history_budget = get_history_budget_service()
    request_id = runtime.resolve_request_id(headers=http_request.headers)
    start_time = time.time()

    try:
        body_json = await http_request.json()
        if _all_request_capture_enabled():
            _capture_anthropic_request(
                http_request=http_request,
                body_json=body_json,
                route_name="/v1/messages",
                request_id=request_id,
                anthropic_beta=anthropic_beta,
                capture_kind="debug",
            )
        MessagesRequest(**body_json)
        canonical = bridge.parse_anthropic_messages(
            body_json,
            anthropic_beta_header=anthropic_beta,
        )
        budget_result = history_budget.compact_request(bridge, canonical, runtime.token_counter)
        if budget_result.applied:
            logger.debug(
                "Applied history budget request_id=%s source=%s original_tokens=%s compacted_tokens=%s dropped_messages=%s",
                request_id,
                canonical.source,
                budget_result.original_tokens,
                budget_result.compacted_tokens,
                budget_result.dropped_messages,
            )
        canonical = budget_result.request
    except Exception as exc:
        _capture_invalid_anthropic_request(
            http_request=http_request,
            body_json=body_json if "body_json" in locals() else None,
            exc=exc,
            route_name="/v1/messages",
            request_id=request_id,
            anthropic_beta=anthropic_beta,
        )
        error = error_handler.create_invalid_request_error(str(exc))
        return _anthropic_error_response(error_handler, error, request_id=request_id)

    if anthropic_version:
        logger.debug("anthropic-version=%s", anthropic_version)
    if anthropic_beta:
        logger.debug("anthropic-beta=%s", anthropic_beta)

    raw_key = bridge.extract_api_key(
        authorization=authorization,
        x_api_key=x_api_key,
    )

    fallback_client_ip = http_request.client.host if http_request.client else None
    client_ip = runtime.resolve_client_ip(
        headers=http_request.headers,
        fallback_client_ip=fallback_client_ip,
    )
    resolved = None

    try:
        resolved = await runtime.resolve_request(
            session,
            raw_key=raw_key,
            model=canonical.model,
            request_id=request_id,
        )

        latest_user_text = ""
        for msg in reversed(canonical.messages):
            if msg.role == "user" and (msg.content or "").strip():
                latest_user_text = msg.content.strip()
                break

        legacy_web_search_query = extract_legacy_web_search_query(latest_user_text)
        if legacy_web_search_query:
            web_search = get_web_search_fallback_service()
            search_text = await web_search.search(legacy_web_search_query)
            server_tool_use_usage = {
                "web_search_requests": 1,
                "web_fetch_requests": 0,
            }
            usage = runtime.finalize_usage(
                preferred_usage=None,
                prompt_text=latest_user_text,
                output_text=search_text,
                fallback_source="estimate.web_search_fallback",
            )

            if canonical.stream:
                async def generate_legacy_search_stream():
                    try:
                        yield _anthropic_event(
                            "message_start",
                            {
                                "type": "message_start",
                                "message": {
                                    "id": f"msg_{request_id}",
                                    "type": "message",
                                    "role": "assistant",
                                    "content": [],
                                    "model": canonical.model,
                                    "stop_reason": None,
                                    "stop_sequence": None,
                                    "usage": {
                                        "input_tokens": usage.input_tokens,
                                        "output_tokens": 0,
                                        "server_tool_use": server_tool_use_usage,
                                    },
                                },
                            },
                        )
                        yield _anthropic_event(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": 0,
                                "content_block": {"type": "text", "text": ""},
                            },
                        )
                        if search_text:
                            yield _anthropic_event(
                                "content_block_delta",
                                {
                                    "type": "content_block_delta",
                                    "index": 0,
                                    "delta": {"type": "text_delta", "text": search_text},
                                },
                            )
                        yield _anthropic_event(
                            "content_block_stop",
                            {"type": "content_block_stop", "index": 0},
                        )
                        yield _anthropic_event(
                            "message_delta",
                            {
                                "type": "message_delta",
                                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                "usage": {
                                    "output_tokens": usage.output_tokens,
                                    "server_tool_use": server_tool_use_usage,
                                },
                            },
                        )
                        yield _anthropic_event("message_stop", {"type": "message_stop"})

                        await _persist_stream_success(
                            runtime,
                            session,
                            resolved=resolved,
                            canonical=canonical,
                            output_preview=search_text,
                            usage=usage,
                            start_time=start_time,
                            client_ip=client_ip,
                        )
                    except Exception as exc:
                        logger.exception("Legacy web search stream failed")
                        api_error = runtime.map_backend_exception(exc)
                        yield _anthropic_event(
                            "error",
                            {
                                "type": "error",
                                "error": _anthropic_error_payload(error_handler, api_error)["error"],
                            },
                        )

                        await runtime.persist_error(
                            session,
                            resolved=resolved,
                            api_type="anthropic",
                            model=canonical.model,
                            input_preview=canonical.input_preview(),
                            response_time_ms=runtime.elapsed_ms(start_time),
                            client_ip=client_ip,
                            error_message=api_error.message,
                            defer_noncritical_logs=True,
                        )

                return StreamingResponse(
                    generate_legacy_search_stream(),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Request-ID": request_id,
                    },
                )

            response_data = {
                "id": f"msg_{request_id}",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": search_text}],
                "model": canonical.model,
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "server_tool_use": server_tool_use_usage,
                },
            }

            await runtime.persist_success(
                session,
                resolved=resolved,
                api_type="anthropic",
                input_preview=canonical.input_preview(),
                output_preview=search_text,
                usage=usage,
                response_time_ms=runtime.elapsed_ms(start_time),
                is_stream=False,
                client_ip=client_ip,
            )
            return JSONResponse(content=response_data, headers={"X-Request-ID": request_id})

        prompt_text = bridge.render_prompt(canonical)
        session_hint = runtime.resolve_session_hint(
            headers=http_request.headers,
            payload=body_json,
            allow_user_field=True,
        )
        user_agent = http_request.headers.get("user-agent")
        backend_user_id = runtime.resolve_backend_user_id(
            resolved=resolved,
            request_id=request_id,
            session_hint=session_hint,
            client_ip=client_ip,
            user_agent=user_agent,
        )

        payload = runtime.build_backend_payload(
            resolved=resolved,
            prompt_text=prompt_text,
            user_id=backend_user_id,
            canonical=canonical,
        )

        if canonical.stream:
            stream_gen = await runtime.run_stream(
                resolved=resolved,
                payload=payload,
                session=session,
            )

            try:
                first_raw_chunk = await anext(stream_gen)
            except StopAsyncIteration:
                first_raw_chunk = None
            except Exception as exc:
                logger.exception("Anthropic stream bootstrap failed")
                api_error = runtime.map_backend_exception(exc)

                await runtime.persist_error(
                    session,
                    resolved=resolved,
                    api_type="anthropic",
                    model=canonical.model,
                    input_preview=canonical.input_preview(),
                    response_time_ms=runtime.elapsed_ms(start_time),
                    client_ip=client_ip,
                    error_message=api_error.message,
                    api_error=api_error,
                )
                return _anthropic_error_response(error_handler, api_error, request_id=request_id)

            async def generate_stream():
                raw_tokens: List[str] = []
                state = {"best_usage": None, "run_id": None}
                deferred_message_delta_payload: Optional[Dict[str, Any]] = None
                deferred_message_stop_payload: Optional[Dict[str, Any]] = None
                bootstrap_chunks: List[str] = []
                bootstrap_preview_parts: List[str] = []

                def ingest_raw_chunk(raw_chunk: str) -> str:
                    token, usage_candidate, run_candidate = runtime.parse_stream_chunk(raw_chunk)
                    if run_candidate and not state["run_id"]:
                        state["run_id"] = run_candidate
                    state["best_usage"] = runtime.merge_stream_usage(
                        state["best_usage"],
                        usage_candidate,
                    )
                    if token:
                        raw_tokens.append(token)
                    return token or ""

                if first_raw_chunk is not None:
                    bootstrap_chunks.append(first_raw_chunk)
                    first_token = ingest_raw_chunk(first_raw_chunk)
                    if first_token:
                        bootstrap_preview_parts.append(first_token)

                if _thinking_requested(canonical):
                    max_bootstrap_chunks = 3
                    max_bootstrap_preview_chars = 768
                    while (
                        len(bootstrap_chunks) < max_bootstrap_chunks
                        and len("".join(bootstrap_preview_parts)) < max_bootstrap_preview_chars
                        and not _contains_thinking_marker("".join(bootstrap_preview_parts))
                    ):
                        try:
                            extra_chunk = await anext(stream_gen)
                        except StopAsyncIteration:
                            break
                        bootstrap_chunks.append(extra_chunk)
                        extra_token = ingest_raw_chunk(extra_chunk)
                        if extra_token:
                            bootstrap_preview_parts.append(extra_token)

                async def tracked_backend_stream():
                    for raw_chunk in bootstrap_chunks:
                        yield raw_chunk
                    async for raw_chunk in stream_gen:
                        ingest_raw_chunk(raw_chunk)
                        yield raw_chunk

                try:
                    stream_mode = _select_stream_transform_mode(
                        canonical,
                        preview_text="".join(bootstrap_preview_parts),
                    )

                    if stream_mode == "tools":
                        tool_parser = _tool_parser_for_request(canonical)
                        transform_gen = response_transformer.transform_backend_sse_to_anthropic_with_tools(
                            tracked_backend_stream(),
                            canonical.model,
                            request_id,
                            tool_parser=tool_parser,
                            stop_after_first_tool_call=_is_claude_code_request(canonical),
                        )
                    elif stream_mode == "thinking":
                        # Stack AI upstream is still text-only. Thinking support here is
                        # best-effort tag reconstruction, not Anthropic-native reasoning.
                        transform_gen = response_transformer.transform_backend_sse_to_anthropic_with_thinking(
                            tracked_backend_stream(),
                            canonical.model,
                            request_id,
                            thinking_budget=_thinking_budget(canonical),
                        )
                    else:
                        transform_gen = response_transformer.transform_backend_sse_to_anthropic(
                            tracked_backend_stream(),
                            canonical.model,
                            request_id,
                        )

                    async for event in transform_gen:
                        event_name, event_payload = _parse_anthropic_stream_event(event)
                        if event_name == "message_start":
                            start_usage = runtime.finalize_usage(
                                preferred_usage=state["best_usage"],
                                prompt_text=prompt_text,
                                output_text="",
                                fallback_source="estimate.anthropic_stream_start",
                            )
                            yield _inject_usage_into_anthropic_event(
                                event,
                                input_tokens=start_usage.input_tokens,
                                output_tokens=0,
                            )
                            continue
                        if event_name == "message_delta":
                            deferred_message_delta_payload = event_payload
                            continue
                        if event_name == "message_stop":
                            deferred_message_stop_payload = event_payload
                            continue
                        yield event

                    raw_output = "".join(raw_tokens)
                    parsed = bridge.parse_model_output(raw_output)
                    usage = runtime.finalize_usage(
                        preferred_usage=state["best_usage"],
                        prompt_text=prompt_text,
                        output_text=raw_output,
                        fallback_source="estimate.anthropic_stream",
                    )

                    final_message_delta = copy.deepcopy(
                        deferred_message_delta_payload
                        or {
                            "type": "message_delta",
                            "delta": {"stop_reason": parsed.stop_reason, "stop_sequence": None},
                            "usage": {},
                        }
                    )
                    final_usage = final_message_delta.get("usage")
                    if not isinstance(final_usage, dict):
                        final_usage = {}
                    final_usage["output_tokens"] = usage.output_tokens
                    final_message_delta["usage"] = final_usage

                    yield _anthropic_event("message_delta", final_message_delta)
                    yield _anthropic_event(
                        "message_stop",
                        deferred_message_stop_payload or {"type": "message_stop"},
                    )

                    await _persist_stream_success(
                        runtime,
                        session,
                        resolved=resolved,
                        canonical=canonical,
                        output_preview=parsed.text or raw_output,
                        usage=usage,
                        start_time=start_time,
                        client_ip=client_ip,
                    )
                except Exception as exc:
                    logger.exception("Anthropic stream failed")
                    api_error = runtime.map_backend_exception(exc)
                    yield _anthropic_event(
                        "error",
                        {
                            "type": "error",
                            "error": _anthropic_error_payload(error_handler, api_error)["error"],
                        },
                    )

                    await runtime.persist_error(
                        session,
                        resolved=resolved,
                        api_type="anthropic",
                        model=canonical.model,
                        input_preview=canonical.input_preview(),
                        response_time_ms=runtime.elapsed_ms(start_time),
                        client_ip=client_ip,
                        error_message=api_error.message,
                        defer_noncritical_logs=True,
                    )

            return StreamingResponse(
                generate_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Request-ID": request_id,
                },
            )

        backend_response = await runtime.run_sync(
            resolved=resolved,
            payload=payload,
            session=session,
        )
        raw_output = runtime.extract_content(backend_response)
        parsed = bridge.parse_model_output(raw_output)
        usage = runtime.usage_from_sync(
            backend_response=backend_response,
            prompt_text=prompt_text,
            output_text=raw_output,
        )

        if canonical.tools:
            tool_parser = _tool_parser_for_request(canonical)
            response_data = response_transformer.to_anthropic_response_with_tools(
                backend_response,
                canonical.model,
                request_id,
                tool_parser=tool_parser,
            )
            response_data = _compact_tool_preface(response_data, canonical.response_language)
        else:
            response_data = response_transformer.to_anthropic_response(
                backend_response,
                canonical.model,
                request_id,
                thinking_enabled=_thinking_requested(canonical),
            )

        # If this is a post-tool-result synthesis turn, avoid duplicate
        # draft+revision narrative and keep only the final consolidated answer.
        if any("[tool_result" in (m.content or "") for m in canonical.messages if m.role == "user"):
            response_data = _collapse_redundant_revision(response_data, canonical.response_language)

        # Use runtime-verified usage to keep accounting and API response aligned.
        response_data["usage"] = {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
        }

        await runtime.persist_success(
            session,
            resolved=resolved,
            api_type="anthropic",
            input_preview=canonical.input_preview(),
            output_preview=parsed.text or raw_output,
            usage=usage,
            response_time_ms=runtime.elapsed_ms(start_time),
            is_stream=False,
            client_ip=client_ip,
        )

        return JSONResponse(content=response_data, headers={"X-Request-ID": request_id})

    except GatewayAuthError as exc:
        return _anthropic_error_response(error_handler, exc.error, request_id=request_id)
    except Exception as exc:
        logger.exception("Anthropic request failed")
        api_error = runtime.map_backend_exception(exc)

        await runtime.persist_error(
            session,
            resolved=resolved,
            api_type="anthropic",
            model=canonical.model,
            input_preview=canonical.input_preview(),
            response_time_ms=runtime.elapsed_ms(start_time),
            client_ip=client_ip,
            error_message=api_error.message,
            api_error=api_error,
        )
        return _anthropic_error_response(error_handler, api_error, request_id=request_id)
