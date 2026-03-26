"""OpenAI-compatible router (rewritten with unified protocol/runtime core)."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_session
from app.services.api_key import get_api_key_service
from app.services.call_logger import get_call_logger_service
from app.services.error_handler import APIError, get_error_handler
from app.services.gateway_runtime import GatewayAuthError, get_gateway_runtime
from app.services.key_info import build_public_key_info_payload
from app.services.protocol_bridge import (
    CanonicalRequest,
    ParsedOutput,
    UsageNumbers,
    get_protocol_bridge,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["OpenAI Compatible"])


class ChatCompletionRequest(BaseModel):
    model_config = {"extra": "allow"}

    model: str = Field(...)
    messages: List[Dict[str, Any]] = Field(...)
    stream: bool = Field(default=False)


class ResponsesRequest(BaseModel):
    model_config = {"extra": "allow"}

    model: str = Field(...)
    input: Any = Field(...)
    stream: bool = Field(default=False)


class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int
    owned_by: str = "organization"


class ModelsResponse(BaseModel):
    object: str = "list"
    data: List[ModelInfo]


def _openai_error_payload(error_handler, error: APIError) -> Dict[str, Any]:
    return error_handler.to_openai_error(error)


def _openai_error_response(error_handler, error: APIError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content=_openai_error_payload(error_handler, error),
    )


def _sse_data(data: Dict[str, Any]) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _openai_chunk(
    *,
    request_id: str,
    model: str,
    delta: Dict[str, Any],
    finish_reason: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "id": f"chatcmpl-{request_id}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }


def _normalize_tool_turn_output(parsed: ParsedOutput, *, tools_declared: bool) -> ParsedOutput:
    """When tools are declared, suppress assistant prose once tool calls are present."""
    if tools_declared and parsed.tool_calls:
        return ParsedOutput(text="", tool_calls=parsed.tool_calls)
    return parsed


class _ToolAwareTextBuffer:
    """Incremental text parser that suppresses tool-call payload leakage."""

    JSON_BLOCK_START = "```json"
    JSON_BLOCK_END = "```"
    XML_TOOL_START = "<tool_use"
    XML_TOOL_END = "</tool_use>"
    BRACKET_TOOL_START = "[tool_call"

    STATE_TEXT = "text"
    STATE_JSON = "json"
    STATE_XML = "xml"
    STATE_BRACKET = "bracket"
    STATE_PLAIN_JSON = "plain_json"

    BRACKET_TOOL_CALL_HEADER_PATTERN = re.compile(
        r"\[tool_call\s+id=([^\s\]]+)\s+name=([^\]]+)\]\s*",
        re.IGNORECASE,
    )
    PLAIN_TOOL_JSON_START_PATTERN = re.compile(
        r"(^|\n)\s*(\{\s*\"tool\")",
        re.IGNORECASE,
    )

    def __init__(self, bridge, *, max_buffer_size: int = 65536) -> None:
        self._bridge = bridge
        self._max_buffer_size = max_buffer_size
        self._buffer = ""
        self._state = self.STATE_TEXT
        self._tool_detected = False

    @property
    def tool_detected(self) -> bool:
        return self._tool_detected

    def feed(self, token: str) -> List[str]:
        if not token:
            return []
        if self._tool_detected:
            return []
        self._buffer += token
        return self._drain(finalize=False)

    def finish(self) -> List[str]:
        return self._drain(finalize=True)

    def _drain(self, *, finalize: bool) -> List[str]:
        outputs: List[str] = []

        while self._buffer:
            if self._state == self.STATE_TEXT:
                if self._tool_detected:
                    self._buffer = ""
                    break

                start_pos, start_type = self._find_next_marker(self._buffer)
                if start_pos != -1:
                    if start_pos > 0:
                        text_before = self._buffer[:start_pos]
                        if text_before:
                            outputs.append(text_before)
                    self._buffer = self._buffer[start_pos:]
                    self._state = start_type
                    continue

                if finalize:
                    outputs.append(self._buffer)
                    self._buffer = ""
                    break

                tail_len = self._marker_tail_len()
                if len(self._buffer) <= tail_len:
                    break

                safe_len = len(self._buffer) - tail_len
                outputs.append(self._buffer[:safe_len])
                self._buffer = self._buffer[safe_len:]
                break

            consumed_and_valid: Optional[tuple[int, bool]]
            if self._state == self.STATE_JSON:
                consumed_and_valid = self._consume_json_block()
            elif self._state == self.STATE_XML:
                consumed_and_valid = self._consume_xml_block()
            elif self._state == self.STATE_BRACKET:
                consumed_and_valid = self._consume_bracket_call()
            elif self._state == self.STATE_PLAIN_JSON:
                consumed_and_valid = self._consume_plain_json_tool_call()
            else:
                consumed_and_valid = None

            if consumed_and_valid is None:
                if finalize or len(self._buffer) > self._max_buffer_size:
                    outputs.append(self._buffer)
                    self._buffer = ""
                    self._state = self.STATE_TEXT
                break

            consumed_len, is_valid_tool = consumed_and_valid
            segment = self._buffer[:consumed_len]
            self._buffer = self._buffer[consumed_len:]
            self._state = self.STATE_TEXT

            if is_valid_tool:
                self._tool_detected = True
                self._buffer = ""
                break

            if segment:
                outputs.append(segment)

        return outputs

    def _find_next_marker(self, text: str) -> tuple[int, str]:
        candidates: List[tuple[int, str]] = []

        json_pos = text.find(self.JSON_BLOCK_START)
        if json_pos != -1:
            candidates.append((json_pos, self.STATE_JSON))

        xml_pos = text.find(self.XML_TOOL_START)
        if xml_pos != -1:
            candidates.append((xml_pos, self.STATE_XML))

        bracket_pos = text.find(self.BRACKET_TOOL_START)
        if bracket_pos != -1:
            candidates.append((bracket_pos, self.STATE_BRACKET))

        plain_match = self.PLAIN_TOOL_JSON_START_PATTERN.search(text)
        if plain_match is not None:
            candidates.append((plain_match.start(2), self.STATE_PLAIN_JSON))

        if not candidates:
            return -1, self.STATE_TEXT
        return min(candidates, key=lambda x: x[0])

    def _marker_tail_len(self) -> int:
        return max(
            len(self.JSON_BLOCK_START),
            len(self.XML_TOOL_START),
            len(self.BRACKET_TOOL_START),
            len('{"tool"'),
        ) - 1

    def _consume_json_block(self) -> Optional[tuple[int, bool]]:
        search_start = len(self.JSON_BLOCK_START)
        end_pos = self._buffer.find(self.JSON_BLOCK_END, search_start)
        if end_pos == -1:
            return None
        consumed_len = end_pos + len(self.JSON_BLOCK_END)
        segment = self._buffer[:consumed_len]
        return consumed_len, self._is_valid_tool_segment(segment)

    def _consume_xml_block(self) -> Optional[tuple[int, bool]]:
        end_pos = self._buffer.find(self.XML_TOOL_END)
        if end_pos == -1:
            return None
        consumed_len = end_pos + len(self.XML_TOOL_END)
        segment = self._buffer[:consumed_len]
        return consumed_len, self._is_valid_tool_segment(segment)

    def _consume_bracket_call(self) -> Optional[tuple[int, bool]]:
        header_match = self.BRACKET_TOOL_CALL_HEADER_PATTERN.match(self._buffer)
        if not header_match:
            closing = self._buffer.find("]")
            if closing == -1:
                return None
            consumed_len = closing + 1
            segment = self._buffer[:consumed_len]
            return consumed_len, self._is_valid_tool_segment(segment)

        rest = self._buffer[header_match.end():]
        leading_ws = len(rest) - len(rest.lstrip())
        json_part = rest.lstrip()
        if not json_part:
            return None

        decoder = json.JSONDecoder()
        try:
            _parsed_obj, end_idx = decoder.raw_decode(json_part)
        except json.JSONDecodeError:
            return None

        consumed_len = header_match.end() + leading_ws + end_idx
        while consumed_len < len(self._buffer) and self._buffer[consumed_len] in " \t\r\n":
            consumed_len += 1

        segment = self._buffer[:consumed_len]
        return consumed_len, self._is_valid_tool_segment(segment)

    def _consume_plain_json_tool_call(self) -> Optional[tuple[int, bool]]:
        decoder = json.JSONDecoder()
        try:
            _parsed_obj, end_idx = decoder.raw_decode(self._buffer)
        except json.JSONDecodeError:
            return None

        consumed_len = end_idx
        while consumed_len < len(self._buffer) and self._buffer[consumed_len] in " \t\r\n":
            consumed_len += 1

        segment = self._buffer[:consumed_len]
        return consumed_len, self._is_valid_tool_segment(segment)

    def _is_valid_tool_segment(self, segment: str) -> bool:
        try:
            parsed = self._bridge.parse_model_output(segment)
        except Exception:
            return False
        return bool(parsed.tool_calls)


def _responses_event(event: str, payload: Dict[str, Any]) -> str:
    body = {"type": event, **payload}
    return f"event: {event}\ndata: {json.dumps(body, ensure_ascii=False)}\n\n"


async def _persist_stream_success(
    runtime,
    session,
    *,
    resolved,
    api_type: str,
    canonical: CanonicalRequest,
    output_preview: str,
    usage: UsageNumbers,
    start_time: float,
    client_ip: Optional[str],
) -> None:
    await runtime.persist_success(
        session,
        resolved=resolved,
        api_type=api_type,
        input_preview=canonical.input_preview(),
        output_preview=output_preview,
        usage=usage,
        response_time_ms=runtime.elapsed_ms(start_time),
        is_stream=True,
        client_ip=client_ip,
    )


@router.post("/chat/completions")
async def chat_completions(
    http_request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    session: AsyncSession = Depends(get_session),
):
    request_id = uuid.uuid4().hex[:24]
    start_time = time.time()

    bridge = get_protocol_bridge()
    runtime = get_gateway_runtime()
    error_handler = get_error_handler()

    try:
        body_json = await http_request.json()
        # Validation-only parse (keeps extra fields).
        ChatCompletionRequest(**body_json)
        canonical = bridge.parse_openai_chat(body_json)
    except Exception as exc:
        error = error_handler.create_invalid_request_error(str(exc))
        return _openai_error_response(error_handler, error)

    raw_key = bridge.extract_api_key(
        authorization=authorization,
        x_api_key=x_api_key,
    )

    client_ip = http_request.client.host if http_request.client else None
    resolved = None

    try:
        resolved = await runtime.resolve_request(
            session,
            raw_key=raw_key,
            model=canonical.model,
            request_id=request_id,
        )

        prompt_text = bridge.render_prompt(canonical)
        payload = runtime.build_backend_payload(
            resolved=resolved,
            prompt_text=prompt_text,
            user_id=f"api:{resolved.api_key.key_prefix}",
        )

        if canonical.stream:
            stream_gen = await runtime.run_stream(resolved=resolved, payload=payload)

            async def generate_stream():
                raw_tokens: List[str] = []
                best_usage = None
                run_id = None
                parsed = None

                try:
                    yield _sse_data(
                        _openai_chunk(
                            request_id=request_id,
                            model=canonical.model,
                            delta={"role": "assistant"},
                            finish_reason=None,
                        )
                    )

                    if canonical.tools:
                        visible_text_parts: List[str] = []
                        tool_aware = _ToolAwareTextBuffer(bridge)

                        async for raw_chunk in stream_gen:
                            token, usage_candidate, run_candidate = runtime.parse_stream_chunk(raw_chunk)
                            if run_candidate and not run_id:
                                run_id = run_candidate
                            best_usage = runtime.merge_stream_usage(best_usage, usage_candidate)
                            if not token:
                                continue
                            raw_tokens.append(token)
                            for visible_text in tool_aware.feed(token):
                                if not visible_text:
                                    continue
                                visible_text_parts.append(visible_text)
                                yield _sse_data(
                                    _openai_chunk(
                                        request_id=request_id,
                                        model=canonical.model,
                                        delta={"content": visible_text},
                                        finish_reason=None,
                                    )
                                )

                        for visible_text in tool_aware.finish():
                            if not visible_text:
                                continue
                            visible_text_parts.append(visible_text)
                            yield _sse_data(
                                _openai_chunk(
                                    request_id=request_id,
                                    model=canonical.model,
                                    delta={"content": visible_text},
                                    finish_reason=None,
                                )
                            )

                        raw_output = "".join(raw_tokens)
                        parsed = _normalize_tool_turn_output(
                            bridge.parse_model_output(raw_output),
                            tools_declared=True,
                        )

                        if parsed.tool_calls:
                            for idx, call in enumerate(parsed.tool_calls):
                                yield _sse_data(
                                    _openai_chunk(
                                        request_id=request_id,
                                        model=canonical.model,
                                        delta={
                                            "tool_calls": [
                                                {
                                                    "index": idx,
                                                    "id": call.call_id,
                                                    "type": "function",
                                                    "function": {
                                                        "name": call.name,
                                                        "arguments": call.arguments_json,
                                                    },
                                                }
                                            ]
                                        },
                                        finish_reason=None,
                                    )
                                )
                        elif tool_aware.tool_detected and parsed.text:
                            streamed_text = "".join(visible_text_parts)
                            if parsed.text.startswith(streamed_text):
                                remaining = parsed.text[len(streamed_text):]
                            else:
                                remaining = parsed.text
                            if remaining:
                                yield _sse_data(
                                    _openai_chunk(
                                        request_id=request_id,
                                        model=canonical.model,
                                        delta={"content": remaining},
                                        finish_reason=None,
                                    )
                                )
                        elif parsed.text and not visible_text_parts:
                            yield _sse_data(
                                _openai_chunk(
                                    request_id=request_id,
                                    model=canonical.model,
                                    delta={"content": parsed.text},
                                    finish_reason=None,
                                )
                            )

                        finish_reason = "tool_calls" if parsed.tool_calls else "stop"
                    else:
                        async for raw_chunk in stream_gen:
                            token, usage_candidate, run_candidate = runtime.parse_stream_chunk(raw_chunk)
                            if run_candidate and not run_id:
                                run_id = run_candidate
                            best_usage = runtime.merge_stream_usage(best_usage, usage_candidate)
                            if not token:
                                continue
                            raw_tokens.append(token)
                            yield _sse_data(
                                _openai_chunk(
                                    request_id=request_id,
                                    model=canonical.model,
                                    delta={"content": token},
                                    finish_reason=None,
                                )
                            )

                        parsed = bridge.parse_model_output("".join(raw_tokens))
                        finish_reason = "tool_calls" if parsed.tool_calls else "stop"
                        # If tool calls are unexpectedly returned without tool declaration,
                        # pass them in final chunk to keep downstream compatibility.
                        if parsed.tool_calls:
                            for idx, call in enumerate(parsed.tool_calls):
                                yield _sse_data(
                                    _openai_chunk(
                                        request_id=request_id,
                                        model=canonical.model,
                                        delta={
                                            "tool_calls": [
                                                {
                                                    "index": idx,
                                                    "id": call.call_id,
                                                    "type": "function",
                                                    "function": {
                                                        "name": call.name,
                                                        "arguments": call.arguments_json,
                                                    },
                                                }
                                            ]
                                        },
                                        finish_reason=None,
                                    )
                                )

                    yield _sse_data(
                        _openai_chunk(
                            request_id=request_id,
                            model=canonical.model,
                            delta={},
                            finish_reason=finish_reason,
                        )
                    )
                    yield "data: [DONE]\n\n"

                    raw_output = "".join(raw_tokens)
                    if parsed is None:
                        parsed = _normalize_tool_turn_output(
                            bridge.parse_model_output(raw_output),
                            tools_declared=bool(canonical.tools),
                        )
                    usage = runtime.finalize_usage(
                        preferred_usage=best_usage,
                        prompt_text=prompt_text,
                        output_text=raw_output,
                        fallback_source="estimate.chat_stream",
                    )

                    await _persist_stream_success(
                        runtime,
                        session,
                        resolved=resolved,
                        api_type="openai",
                        canonical=canonical,
                        output_preview=parsed.text or raw_output,
                        usage=usage,
                        start_time=start_time,
                        client_ip=client_ip,
                    )
                except Exception as exc:
                    logger.exception("OpenAI stream failed")
                    api_error = runtime.map_backend_exception(exc)
                    payload = _openai_error_payload(error_handler, api_error)
                    yield _sse_data({"error": payload["error"]})
                    yield "data: [DONE]\n\n"

                    await runtime.persist_error(
                        session,
                        resolved=resolved,
                        api_type="openai",
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
        parsed = _normalize_tool_turn_output(
            bridge.parse_model_output(raw_output),
            tools_declared=bool(canonical.tools),
        )
        usage = runtime.usage_from_sync(
            backend_response=backend_response,
            prompt_text=prompt_text,
            output_text=raw_output,
        )

        response_data = bridge.to_openai_chat_response(
            model=canonical.model,
            request_id=request_id,
            parsed=parsed,
            usage=usage,
        )

        await runtime.persist_success(
            session,
            resolved=resolved,
            api_type="openai",
            input_preview=canonical.input_preview(),
            output_preview=parsed.text or raw_output,
            usage=usage,
            response_time_ms=runtime.elapsed_ms(start_time),
            is_stream=False,
            client_ip=client_ip,
        )

        return JSONResponse(content=response_data, headers={"X-Request-ID": request_id})

    except GatewayAuthError as exc:
        return _openai_error_response(error_handler, exc.error)
    except Exception as exc:
        logger.exception("OpenAI chat request failed")
        api_error = runtime.map_backend_exception(exc)

        await runtime.persist_error(
            session,
            resolved=resolved,
            api_type="openai",
            model=canonical.model,
            input_preview=canonical.input_preview(),
            response_time_ms=runtime.elapsed_ms(start_time),
            client_ip=client_ip,
            error_message=api_error.message,
        )
        return _openai_error_response(error_handler, api_error)


@router.post("/responses")
async def create_response(
    http_request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    session: AsyncSession = Depends(get_session),
):
    request_id = uuid.uuid4().hex[:24]
    start_time = time.time()

    bridge = get_protocol_bridge()
    runtime = get_gateway_runtime()
    error_handler = get_error_handler()

    try:
        body_json = await http_request.json()
        ResponsesRequest(**body_json)
        canonical = bridge.parse_openai_responses(body_json)
    except Exception as exc:
        error = error_handler.create_invalid_request_error(str(exc))
        return _openai_error_response(error_handler, error)

    raw_key = bridge.extract_api_key(
        authorization=authorization,
        x_api_key=x_api_key,
    )

    client_ip = http_request.client.host if http_request.client else None
    resolved = None

    try:
        resolved = await runtime.resolve_request(
            session,
            raw_key=raw_key,
            model=canonical.model,
            request_id=request_id,
        )

        prompt_text = bridge.render_prompt(canonical)
        payload = runtime.build_backend_payload(
            resolved=resolved,
            prompt_text=prompt_text,
            user_id=f"api:{resolved.api_key.key_prefix}",
        )

        if canonical.stream:
            stream_gen = await runtime.run_stream(resolved=resolved, payload=payload)

            async def generate_stream():
                raw_tokens: List[str] = []
                best_usage = None
                run_id = None
                parsed = None

                message_id = f"msg_{request_id}"

                try:
                    yield _responses_event(
                        "response.created",
                        {
                            "response": {
                                "id": f"resp_{request_id}",
                                "object": "response",
                                "created_at": int(time.time()),
                                "status": "in_progress",
                                "model": canonical.model,
                                "output": [],
                            }
                        },
                    )

                    if canonical.tools:
                        text_output_index = 0
                        yield _responses_event(
                            "response.output_item.added",
                            {
                                "output_index": text_output_index,
                                "item": {
                                    "id": message_id,
                                    "type": "message",
                                    "role": "assistant",
                                    "content": [],
                                },
                            },
                        )
                        yield _responses_event(
                            "response.content_part.added",
                            {
                                "output_index": text_output_index,
                                "item_id": message_id,
                                "content_index": 0,
                                "part": {"type": "output_text", "text": ""},
                            },
                        )

                        visible_text_parts: List[str] = []
                        tool_aware = _ToolAwareTextBuffer(bridge)

                        async for raw_chunk in stream_gen:
                            token, usage_candidate, run_candidate = runtime.parse_stream_chunk(raw_chunk)
                            if run_candidate and not run_id:
                                run_id = run_candidate
                            best_usage = runtime.merge_stream_usage(best_usage, usage_candidate)
                            if not token:
                                continue
                            raw_tokens.append(token)
                            for visible_text in tool_aware.feed(token):
                                if not visible_text:
                                    continue
                                visible_text_parts.append(visible_text)
                                yield _responses_event(
                                    "response.output_text.delta",
                                    {
                                        "output_index": text_output_index,
                                        "item_id": message_id,
                                        "content_index": 0,
                                        "delta": visible_text,
                                    },
                                )

                        for visible_text in tool_aware.finish():
                            if not visible_text:
                                continue
                            visible_text_parts.append(visible_text)
                            yield _responses_event(
                                "response.output_text.delta",
                                {
                                    "output_index": text_output_index,
                                    "item_id": message_id,
                                    "content_index": 0,
                                    "delta": visible_text,
                                },
                            )

                        raw_output = "".join(raw_tokens)
                        parsed = _normalize_tool_turn_output(
                            bridge.parse_model_output(raw_output),
                            tools_declared=True,
                        )

                        final_visible_text = "".join(visible_text_parts)
                        if tool_aware.tool_detected and not parsed.tool_calls and parsed.text:
                            if parsed.text.startswith(final_visible_text):
                                final_visible_text += parsed.text[len(final_visible_text):]
                            else:
                                final_visible_text = parsed.text

                        yield _responses_event(
                            "response.output_text.done",
                            {
                                "output_index": text_output_index,
                                "item_id": message_id,
                                "content_index": 0,
                                "text": final_visible_text,
                            },
                        )

                        for idx, call in enumerate(parsed.tool_calls):
                            output_index = text_output_index + 1 + idx
                            yield _responses_event(
                                "response.output_item.added",
                                {
                                    "output_index": output_index,
                                    "item": {
                                        "id": f"fc_{call.call_id}",
                                        "type": "function_call",
                                        "call_id": call.call_id,
                                        "name": call.name,
                                        "arguments": "",
                                    },
                                },
                            )
                            yield _responses_event(
                                "response.function_call_arguments.delta",
                                {
                                    "output_index": output_index,
                                    "item_id": f"fc_{call.call_id}",
                                    "delta": call.arguments_json,
                                },
                            )
                            yield _responses_event(
                                "response.function_call_arguments.done",
                                {
                                    "output_index": output_index,
                                    "item_id": f"fc_{call.call_id}",
                                    "arguments": call.arguments_json,
                                },
                            )
                    else:
                        yield _responses_event(
                            "response.output_item.added",
                            {
                                "output_index": 0,
                                "item": {
                                    "id": message_id,
                                    "type": "message",
                                    "role": "assistant",
                                    "content": [],
                                },
                            },
                        )
                        yield _responses_event(
                            "response.content_part.added",
                            {
                                "output_index": 0,
                                "item_id": message_id,
                                "content_index": 0,
                                "part": {"type": "output_text", "text": ""},
                            },
                        )

                        async for raw_chunk in stream_gen:
                            token, usage_candidate, run_candidate = runtime.parse_stream_chunk(raw_chunk)
                            if run_candidate and not run_id:
                                run_id = run_candidate
                            best_usage = runtime.merge_stream_usage(best_usage, usage_candidate)
                            if not token:
                                continue
                            raw_tokens.append(token)
                            yield _responses_event(
                                "response.output_text.delta",
                                {
                                    "output_index": 0,
                                    "item_id": message_id,
                                    "content_index": 0,
                                    "delta": token,
                                },
                            )

                        final_text = "".join(raw_tokens)
                        yield _responses_event(
                            "response.output_text.done",
                            {
                                "output_index": 0,
                                "item_id": message_id,
                                "content_index": 0,
                                "text": final_text,
                            },
                        )

                    raw_output = "".join(raw_tokens)
                    if parsed is None:
                        parsed = _normalize_tool_turn_output(
                            bridge.parse_model_output(raw_output),
                            tools_declared=bool(canonical.tools),
                        )
                    usage = runtime.finalize_usage(
                        preferred_usage=best_usage,
                        prompt_text=prompt_text,
                        output_text=raw_output,
                        fallback_source="estimate.responses_stream",
                    )

                    completed = bridge.to_openai_responses_response(
                        model=canonical.model,
                        request_id=request_id,
                        parsed=parsed,
                        usage=usage,
                    )
                    yield _responses_event("response.completed", {"response": completed})

                    await _persist_stream_success(
                        runtime,
                        session,
                        resolved=resolved,
                        api_type="openai",
                        canonical=canonical,
                        output_preview=parsed.text or raw_output,
                        usage=usage,
                        start_time=start_time,
                        client_ip=client_ip,
                    )
                except Exception as exc:
                    logger.exception("OpenAI responses stream failed")
                    api_error = runtime.map_backend_exception(exc)
                    yield _responses_event(
                        "error",
                        {
                            "error": _openai_error_payload(error_handler, api_error)["error"],
                        },
                    )

                    await runtime.persist_error(
                        session,
                        resolved=resolved,
                        api_type="openai",
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
        parsed = _normalize_tool_turn_output(
            bridge.parse_model_output(raw_output),
            tools_declared=bool(canonical.tools),
        )
        usage = runtime.usage_from_sync(
            backend_response=backend_response,
            prompt_text=prompt_text,
            output_text=raw_output,
        )

        response_data = bridge.to_openai_responses_response(
            model=canonical.model,
            request_id=request_id,
            parsed=parsed,
            usage=usage,
        )

        await runtime.persist_success(
            session,
            resolved=resolved,
            api_type="openai",
            input_preview=canonical.input_preview(),
            output_preview=parsed.text or raw_output,
            usage=usage,
            response_time_ms=runtime.elapsed_ms(start_time),
            is_stream=False,
            client_ip=client_ip,
        )

        return JSONResponse(content=response_data, headers={"X-Request-ID": request_id})

    except GatewayAuthError as exc:
        return _openai_error_response(error_handler, exc.error)
    except Exception as exc:
        logger.exception("OpenAI responses request failed")
        api_error = runtime.map_backend_exception(exc)

        await runtime.persist_error(
            session,
            resolved=resolved,
            api_type="openai",
            model=canonical.model,
            input_preview=canonical.input_preview(),
            response_time_ms=runtime.elapsed_ms(start_time),
            client_ip=client_ip,
            error_message=api_error.message,
        )
        return _openai_error_response(error_handler, api_error)


@router.get("/models", response_model=ModelsResponse)
async def list_models(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    session: AsyncSession = Depends(get_session),
):
    bridge = get_protocol_bridge()
    error_handler = get_error_handler()
    api_key_service = get_api_key_service()

    raw_key = bridge.extract_api_key(authorization=authorization, x_api_key=x_api_key)
    if not raw_key:
        error = error_handler.create_authentication_error("Missing API key")
        return _openai_error_response(error_handler, error)

    api_key_obj = await api_key_service.get_key_by_raw(session, raw_key)
    if api_key_obj is None or api_key_obj.status == "revoked":
        error = error_handler.create_authentication_error("Invalid API key")
        return _openai_error_response(error_handler, error)

    now = int(time.time())
    models = [
        ModelInfo(id=model_name, created=now)
        for model_name in api_key_service.get_model_groups(api_key_obj)
    ]

    return ModelsResponse(data=models)


@router.get("/models/{model_id}")
async def get_model(
    model_id: str,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    session: AsyncSession = Depends(get_session),
):
    bridge = get_protocol_bridge()
    error_handler = get_error_handler()
    api_key_service = get_api_key_service()

    raw_key = bridge.extract_api_key(authorization=authorization, x_api_key=x_api_key)
    if not raw_key:
        error = error_handler.create_authentication_error("Missing API key")
        return _openai_error_response(error_handler, error)

    api_key_obj = await api_key_service.get_key_by_raw(session, raw_key)
    if api_key_obj is None or api_key_obj.status == "revoked":
        error = error_handler.create_authentication_error("Invalid API key")
        return _openai_error_response(error_handler, error)

    allowed_models = set(api_key_service.get_model_groups(api_key_obj))
    if model_id not in allowed_models:
        error = error_handler.create_not_found_error(f"Model '{model_id}' not found")
        return _openai_error_response(error_handler, error)

    return {
        "id": model_id,
        "object": "model",
        "created": int(time.time()),
        "owned_by": "organization",
    }


@router.get("/key/info")
async def get_current_key_info(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    api_key_query: Optional[str] = Query(None, alias="api_key"),
    key_query: Optional[str] = Query(None, alias="key"),
    session: AsyncSession = Depends(get_session),
):
    bridge = get_protocol_bridge()
    error_handler = get_error_handler()
    api_key_service = get_api_key_service()
    call_logger = get_call_logger_service()
    runtime = get_gateway_runtime()

    raw_key = bridge.extract_api_key(
        authorization=authorization,
        x_api_key=x_api_key,
        api_key_query=api_key_query,
        key_query=key_query,
    )
    if not raw_key:
        error = error_handler.create_authentication_error("Missing API key")
        return _openai_error_response(error_handler, error)

    api_key_obj = await api_key_service.get_key_by_raw(session, raw_key)
    if api_key_obj is None or api_key_obj.status == "revoked":
        error = error_handler.create_authentication_error("Invalid API key")
        return _openai_error_response(error_handler, error)

    allowed_models = api_key_service.get_model_groups(api_key_obj)
    usage_by_model = await call_logger.get_api_key_usage_by_model(
        session,
        api_key_obj.id,
        allowed_models=allowed_models,
        since=api_key_obj.created_at,
    )

    models: List[Dict[str, Any]] = []
    for model_name in allowed_models:
        models.append(
            {
                "id": model_name,
                "accounts": await runtime.account_pool.get_accounts_by_model_group(session, model_name),
                "usage": usage_by_model.get(model_name),
            }
        )

    return build_public_key_info_payload(api_key_obj, models)
