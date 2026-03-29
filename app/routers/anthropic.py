"""Anthropic-compatible router (rewritten with unified protocol/runtime core)."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_session
from app.services.error_handler import APIError, get_error_handler
from app.services.gateway_runtime import GatewayAuthError, get_gateway_runtime
from app.services.protocol_bridge import CanonicalRequest, UsageNumbers, get_protocol_bridge
from app.services.response_transformer import get_response_transformer
from app.services.tool_parser import ToolParser
from app.services.web_search_fallback import (
    extract_legacy_web_search_query,
    get_web_search_fallback_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["Anthropic Compatible"])


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


def _anthropic_error_response(error_handler, error: APIError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content=_anthropic_error_payload(error_handler, error),
    )


def _anthropic_event(event: str, payload: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


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
    body_json = await http_request.json()
    canonical = bridge.parse_anthropic_messages(body_json)
    return {
        "model": canonical.model,
        "stream": canonical.stream,
        "system_prompt": canonical.system_prompt,
        "messages": [m.__dict__ for m in canonical.messages],
        "tools": [t.__dict__ for t in canonical.tools],
        "rendered_prompt": bridge.render_prompt(canonical),
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

    try:
        body_json = await http_request.json()
        CountTokensRequest(**body_json)
        canonical = bridge.parse_anthropic_messages(body_json)
    except Exception as exc:
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

    valid, error_msg, _api_key_obj = await runtime.api_key_service.validate_key(
        session,
        raw_key,
        canonical.model,
    )
    if not valid:
        error = _map_validation_error(error_handler, error_msg or "Invalid API key")
        return _anthropic_error_response(error_handler, error)

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
    request_id = uuid.uuid4().hex[:24]
    start_time = time.time()

    bridge = get_protocol_bridge()
    runtime = get_gateway_runtime()
    response_transformer = get_response_transformer()
    error_handler = get_error_handler()

    try:
        body_json = await http_request.json()
        MessagesRequest(**body_json)
        canonical = bridge.parse_anthropic_messages(body_json)
    except Exception as exc:
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
                                        "input_tokens": 0,
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
        )

        if canonical.stream:
            stream_gen = await runtime.run_stream(resolved=resolved, payload=payload)

            async def generate_stream():
                raw_tokens: List[str] = []
                state = {"best_usage": None, "run_id": None}

                async def tracked_backend_stream():
                    async for raw_chunk in stream_gen:
                        token, usage_candidate, run_candidate = runtime.parse_stream_chunk(raw_chunk)
                        if run_candidate and not state["run_id"]:
                            state["run_id"] = run_candidate
                        state["best_usage"] = runtime.merge_stream_usage(
                            state["best_usage"],
                            usage_candidate,
                        )
                        if token:
                            raw_tokens.append(token)
                        yield raw_chunk

                try:
                    if canonical.tools:
                        tool_parser = ToolParser(registry=None)
                        transform_gen = response_transformer.transform_backend_sse_to_anthropic_with_tools(
                            tracked_backend_stream(),
                            canonical.model,
                            request_id,
                            tool_parser=tool_parser,
                        )
                    else:
                        transform_gen = response_transformer.transform_backend_sse_to_anthropic(
                            tracked_backend_stream(),
                            canonical.model,
                            request_id,
                        )

                    async for event in transform_gen:
                        yield event

                    raw_output = "".join(raw_tokens)
                    parsed = bridge.parse_model_output(raw_output)
                    usage = runtime.finalize_usage(
                        preferred_usage=state["best_usage"],
                        prompt_text=prompt_text,
                        output_text=raw_output,
                        fallback_source="estimate.anthropic_stream",
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

        backend_response = await runtime.run_sync(resolved=resolved, payload=payload)
        raw_output = runtime.extract_content(backend_response)
        parsed = bridge.parse_model_output(raw_output)
        usage = runtime.usage_from_sync(
            backend_response=backend_response,
            prompt_text=prompt_text,
            output_text=raw_output,
        )

        if canonical.tools:
            tool_parser = ToolParser(registry=None)
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
        return _anthropic_error_response(error_handler, exc.error)
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
        )
        return _anthropic_error_response(error_handler, api_error)
