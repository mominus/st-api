"""Gemini-compatible router (rewritten with unified protocol/runtime core)."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_session
from app.services.api_key import get_api_key_service
from app.services.error_handler import APIError, get_error_handler
from app.services.gateway_runtime import GatewayAuthError, get_gateway_runtime
from app.services.history_budget import get_history_budget_service
from app.services.protocol_bridge import CanonicalRequest, UsageNumbers, get_protocol_bridge

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1beta", tags=["Gemini Compatible"])


class GenerateContentRequest(BaseModel):
    model_config = {"extra": "allow"}

    contents: List[Dict[str, Any]] = Field(...)



def _gemini_error_payload(error_handler, error: APIError) -> Dict[str, Any]:
    return error_handler.to_gemini_error(error)



def _gemini_error_response(
    error_handler,
    error: APIError,
    *,
    request_id: Optional[str] = None,
) -> JSONResponse:
    headers = {"X-Request-ID": request_id} if request_id else None
    return JSONResponse(
        status_code=error.status_code,
        content=_gemini_error_payload(error_handler, error),
        headers=headers,
    )



def _sse_data(data: Dict[str, Any]) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


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
        api_type="gemini",
        input_preview=canonical.input_preview(),
        output_preview=output_preview,
        usage=usage,
        response_time_ms=runtime.elapsed_ms(start_time),
        is_stream=True,
        client_ip=client_ip,
    )


@router.post("/models/{model}:generateContent")
async def generate_content(
    model: str,
    http_request: Request,
    x_goog_api_key: Optional[str] = Header(None, alias="x-goog-api-key"),
    authorization: Optional[str] = Header(None),
    key_query: Optional[str] = Query(None, alias="key"),
    session: AsyncSession = Depends(get_session),
):
    bridge = get_protocol_bridge()
    runtime = get_gateway_runtime()
    error_handler = get_error_handler()
    history_budget = get_history_budget_service()
    request_id = runtime.resolve_request_id(headers=http_request.headers)
    start_time = time.time()

    try:
        body_json = await http_request.json()
        GenerateContentRequest(**body_json)
        canonical = bridge.parse_gemini_content(model, body_json, stream=False)
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
        error = error_handler.create_invalid_request_error(str(exc))
        return _gemini_error_response(error_handler, error, request_id=request_id)

    raw_key = bridge.extract_api_key(
        authorization=authorization,
        x_goog_api_key=x_goog_api_key,
        key_query=key_query,
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

        response_data = bridge.to_gemini_response(
            model=canonical.model,
            parsed=parsed,
            usage=usage,
        )

        await runtime.persist_success(
            session,
            resolved=resolved,
            api_type="gemini",
            input_preview=canonical.input_preview(),
            output_preview=parsed.text or raw_output,
            usage=usage,
            response_time_ms=runtime.elapsed_ms(start_time),
            is_stream=False,
            client_ip=client_ip,
        )

        return JSONResponse(content=response_data, headers={"X-Request-ID": request_id})

    except GatewayAuthError as exc:
        return _gemini_error_response(error_handler, exc.error, request_id=request_id)
    except Exception as exc:
        logger.exception("Gemini streamGenerateContent failed")
        api_error = runtime.map_backend_exception(exc)

        await runtime.persist_error(
            session,
            resolved=resolved,
            api_type="gemini",
            model=canonical.model,
            input_preview=canonical.input_preview(),
            response_time_ms=runtime.elapsed_ms(start_time),
            client_ip=client_ip,
            error_message=api_error.message,
            api_error=api_error,
        )
        return _gemini_error_response(error_handler, api_error, request_id=request_id)
    except Exception as exc:
        logger.exception("Gemini generateContent failed")
        api_error = runtime.map_backend_exception(exc)

        await runtime.persist_error(
            session,
            resolved=resolved,
            api_type="gemini",
            model=canonical.model,
            input_preview=canonical.input_preview(),
            response_time_ms=runtime.elapsed_ms(start_time),
            client_ip=client_ip,
            error_message=api_error.message,
            api_error=api_error,
        )
        return _gemini_error_response(error_handler, api_error, request_id=request_id)


@router.post("/models/{model}:streamGenerateContent")
async def stream_generate_content(
    model: str,
    http_request: Request,
    x_goog_api_key: Optional[str] = Header(None, alias="x-goog-api-key"),
    authorization: Optional[str] = Header(None),
    key_query: Optional[str] = Query(None, alias="key"),
    session: AsyncSession = Depends(get_session),
):
    bridge = get_protocol_bridge()
    runtime = get_gateway_runtime()
    error_handler = get_error_handler()
    history_budget = get_history_budget_service()
    request_id = runtime.resolve_request_id(headers=http_request.headers)
    start_time = time.time()

    try:
        body_json = await http_request.json()
        GenerateContentRequest(**body_json)
        canonical = bridge.parse_gemini_content(model, body_json, stream=True)
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
        error = error_handler.create_invalid_request_error(str(exc))
        return _gemini_error_response(error_handler, error, request_id=request_id)

    raw_key = bridge.extract_api_key(
        authorization=authorization,
        x_goog_api_key=x_goog_api_key,
        key_query=key_query,
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
            logger.exception("Gemini stream bootstrap failed")
            api_error = runtime.map_backend_exception(exc)

            await runtime.persist_error(
                session,
                resolved=resolved,
                api_type="gemini",
                model=canonical.model,
                input_preview=canonical.input_preview(),
                response_time_ms=runtime.elapsed_ms(start_time),
                client_ip=client_ip,
                error_message=api_error.message,
                api_error=api_error,
            )
            return _gemini_error_response(error_handler, api_error, request_id=request_id)

        async def generate_stream():
            raw_tokens: List[str] = []
            best_usage = None
            run_id = None

            async def iter_raw_stream():
                if first_raw_chunk is not None:
                    yield first_raw_chunk
                async for raw_chunk in stream_gen:
                    yield raw_chunk

            try:
                if canonical.tools:
                    # Buffer for safe functionCall emission.
                    async for raw_chunk in iter_raw_stream():
                        token, usage_candidate, run_candidate = runtime.parse_stream_chunk(raw_chunk)
                        if run_candidate and not run_id:
                            run_id = run_candidate
                        best_usage = runtime.merge_stream_usage(best_usage, usage_candidate)
                        if token:
                            raw_tokens.append(token)

                    raw_output = "".join(raw_tokens)
                    parsed = bridge.parse_model_output(raw_output)

                    first = bridge.to_gemini_response(
                        model=canonical.model,
                        parsed=parsed,
                        usage=UsageNumbers(input_tokens=0, output_tokens=0, total_tokens=0),
                    )
                    # First buffered payload includes functionCall parts when present.
                    yield _sse_data(
                        {
                            "candidates": first.get("candidates", []),
                        }
                    )
                else:
                    async for raw_chunk in iter_raw_stream():
                        token, usage_candidate, run_candidate = runtime.parse_stream_chunk(raw_chunk)
                        if run_candidate and not run_id:
                            run_id = run_candidate
                        best_usage = runtime.merge_stream_usage(best_usage, usage_candidate)
                        if not token:
                            continue
                        raw_tokens.append(token)
                        yield _sse_data(
                            {
                                "candidates": [
                                    {
                                        "content": {
                                            "role": "model",
                                            "parts": [{"text": token}],
                                        },
                                        "index": 0,
                                        "finishReason": None,
                                    }
                                ]
                            }
                        )

                raw_output = "".join(raw_tokens)
                parsed = bridge.parse_model_output(raw_output)
                usage = runtime.finalize_usage(
                    preferred_usage=best_usage,
                    prompt_text=prompt_text,
                    output_text=raw_output,
                    fallback_source="estimate.gemini_stream",
                )

                final_chunk = {
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [{"text": ""}],
                            },
                            "index": 0,
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": usage.input_tokens,
                        "candidatesTokenCount": usage.output_tokens,
                        "totalTokenCount": usage.total_tokens,
                    },
                }
                yield _sse_data(final_chunk)

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
                logger.exception("Gemini stream failed")
                api_error = runtime.map_backend_exception(exc)
                yield _sse_data(
                    {
                        "error": _gemini_error_payload(error_handler, api_error).get("error", {}),
                    }
                )

                await runtime.persist_error(
                    session,
                    resolved=resolved,
                    api_type="gemini",
                    model=canonical.model,
                    input_preview=canonical.input_preview(),
                    response_time_ms=runtime.elapsed_ms(start_time),
                    client_ip=client_ip,
                    error_message=api_error.message,
                    api_error=api_error,
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

    except GatewayAuthError as exc:
        return _gemini_error_response(error_handler, exc.error, request_id=request_id)


@router.get("/models")
async def list_models(
    authorization: Optional[str] = Header(None),
    x_goog_api_key: Optional[str] = Header(None, alias="x-goog-api-key"),
    key_query: Optional[str] = Query(None, alias="key"),
    session: AsyncSession = Depends(get_session),
):
    bridge = get_protocol_bridge()
    runtime = get_gateway_runtime()
    error_handler = get_error_handler()
    api_key_service = get_api_key_service()

    raw_key = bridge.extract_api_key(
        authorization=authorization,
        x_goog_api_key=x_goog_api_key,
        key_query=key_query,
    )
    if not raw_key:
        error = error_handler.create_authentication_error("Missing API key")
        return _gemini_error_response(error_handler, error)

    try:
        api_key_obj = await runtime.run_db_guarded(
            session,
            lambda: api_key_service.get_key_by_raw(session, raw_key),
        )
    except GatewayAuthError as exc:
        return _gemini_error_response(error_handler, exc.error)
    if api_key_obj is None or api_key_obj.status == "revoked":
        error = error_handler.create_authentication_error("Invalid API key")
        return _gemini_error_response(error_handler, error)

    model_items = []
    for model_name in api_key_service.get_model_groups(api_key_obj):
        model_items.append(
            {
                "name": f"models/{model_name}",
                "baseModelId": model_name,
                "version": "1",
                "displayName": model_name,
                "description": f"Proxy model for {model_name}",
                "inputTokenLimit": 1048576,
                "outputTokenLimit": 65536,
                "supportedGenerationMethods": [
                    "generateContent",
                    "streamGenerateContent",
                ],
            }
        )

    return {"models": model_items}


@router.get("/models/{model}")
async def get_model(
    model: str,
    authorization: Optional[str] = Header(None),
    x_goog_api_key: Optional[str] = Header(None, alias="x-goog-api-key"),
    key_query: Optional[str] = Query(None, alias="key"),
    session: AsyncSession = Depends(get_session),
):
    bridge = get_protocol_bridge()
    runtime = get_gateway_runtime()
    error_handler = get_error_handler()
    api_key_service = get_api_key_service()

    model_name = model[7:] if model.startswith("models/") else model

    raw_key = bridge.extract_api_key(
        authorization=authorization,
        x_goog_api_key=x_goog_api_key,
        key_query=key_query,
    )
    if not raw_key:
        error = error_handler.create_authentication_error("Missing API key")
        return _gemini_error_response(error_handler, error)

    try:
        api_key_obj = await runtime.run_db_guarded(
            session,
            lambda: api_key_service.get_key_by_raw(session, raw_key),
        )
    except GatewayAuthError as exc:
        return _gemini_error_response(error_handler, exc.error)
    if api_key_obj is None or api_key_obj.status == "revoked":
        error = error_handler.create_authentication_error("Invalid API key")
        return _gemini_error_response(error_handler, error)

    allowed = set(api_key_service.get_model_groups(api_key_obj))
    if model_name not in allowed:
        error = error_handler.create_not_found_error(f"Model '{model_name}' not found")
        return _gemini_error_response(error_handler, error)

    return {
        "name": f"models/{model_name}",
        "baseModelId": model_name,
        "version": "1",
        "displayName": model_name,
        "description": f"Proxy model for {model_name}",
        "inputTokenLimit": 1048576,
        "outputTokenLimit": 65536,
        "supportedGenerationMethods": [
            "generateContent",
            "streamGenerateContent",
        ],
    }
