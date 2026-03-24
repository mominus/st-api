"""
Gemini Compatible Router
实现 Gemini API 兼容的路由端?

Requirements: 1.3
"""

import json
import uuid
import time
import logging
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Request, Depends, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_session, ModelGroup
from app.services.transformer import (
    GeminiRequest, GeminiContent, GeminiPart, GeminiGenerationConfig,
    RequestTransformer, get_transformer
)
from app.services.api_key import APIKeyService, get_api_key_service
from app.services.account_pool import AccountPoolService, get_account_pool_service
from app.services.backend_client import (
    BackendClient, get_backend_client,
    BackendClientError, BackendAPIError, BackendConnectionError, BackendTimeoutError
)
from app.services.response_transformer import ResponseTransformer, get_response_transformer
from app.services.error_handler import ErrorHandler, get_error_handler, APIError, ErrorType
from app.services.logger import LoggerService, get_logger_service
from app.services.stats import StatsService, get_stats_service
from app.services.st_usage import (
    STUsage,
    choose_better_usage,
    extract_run_id,
    extract_usage,
    split_total_with_fallback,
)
from sqlalchemy import select

logger = logging.getLogger(__name__)

# 创建路由?
router = APIRouter(prefix="/v1beta", tags=["Gemini Compatible"])


# ============================================================================
# Request/Response Models
# ============================================================================

class GenerateContentRequest(BaseModel):
    """Gemini generateContent 请求模型"""
    contents: List[GeminiContent] = Field(..., description="内容列表")
    generationConfig: Optional[GeminiGenerationConfig] = Field(default=None, description="生成配置")
    safetySettings: Optional[List[Dict[str, Any]]] = Field(default=None, description="安全设置")


# ============================================================================
# Helper Functions
# ============================================================================

def sanitize_api_key(key: str) -> str:
    """
    清理 API Key 中可能的 Unicode 连字符变体
    
    用户复制粘贴时可能引入这些字符（从 Word、PDF、网页等）
    EN DASH (U+2013), EM DASH (U+2014), MINUS SIGN (U+2212) -> ASCII hyphen (U+002D)
    """
    return key.replace('\u2013', '-').replace('\u2014', '-').replace('\u2212', '-')


def extract_api_key(
    x_goog_api_key: Optional[str],
    authorization: Optional[str]
) -> Optional[str]:
    """
    从请求头提取 API Key
    
    Gemini 支持两种方式:
    - x-goog-api-key: sk-xxx
    - Authorization: Bearer sk-xxx
    """
    # 优先使用 x-goog-api-key
    if x_goog_api_key:
        return sanitize_api_key(x_goog_api_key.strip())
    
    # 其次使用 Authorization header
    if authorization:
        auth = authorization.strip()
        if auth.lower().startswith("bearer "):
            return sanitize_api_key(auth[7:].strip())
        return sanitize_api_key(auth)
    
    return None


async def validate_api_key_and_model(
    session: AsyncSession,
    raw_key: str,
    model: str,
    api_key_service: APIKeyService,
    error_handler: ErrorHandler
) -> tuple:
    """
    验证 API Key 和模型权?
    
    Returns:
        (api_key_obj, error_response) - 如果验证失败，error_response 不为 None
    """
    is_valid, error_msg, api_key_obj = await api_key_service.validate_key(
        session, raw_key, model
    )
    
    if not is_valid:
        if "not authorized" in (error_msg or "").lower():
            error = error_handler.create_permission_error(error_msg)
        elif "quota" in (error_msg or "").lower():
            error = error_handler.create_quota_exceeded_error(error_msg)
        elif "expired" in (error_msg or "").lower():
            error = error_handler.create_authentication_error(error_msg)
        else:
            error = error_handler.create_authentication_error(error_msg or "Invalid API key")
        
        return None, JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_gemini_error(error)
        )
    
    return api_key_obj, None


def build_backend_payload(
    user_id: str,
    messages_context: str,
    model_name: str,
    input_mapping: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """根据输入映射构建后端 payload（支持 model_id -> in-1）。"""
    payload = {
        "user_id": user_id,
        "conversation_id": str(uuid.uuid4())
    }

    user_field = "in-0"
    if input_mapping and "user_input" in input_mapping:
        user_field = input_mapping["user_input"]
    payload[user_field] = messages_context

    model_field = None
    if input_mapping:
        model_field = input_mapping.get("model_id") or input_mapping.get("model")
    if model_field:
        payload[model_field] = model_name

    return payload


def _finalize_usage(
    preferred_usage: Optional[STUsage],
    fallback_input_tokens: int,
    fallback_output_tokens: int,
    fallback_source: str,
) -> STUsage:
    if preferred_usage is None:
        return STUsage(
            input_tokens=max(0, int(fallback_input_tokens or 0)),
            output_tokens=max(0, int(fallback_output_tokens or 0)),
            total_tokens=max(0, int(fallback_input_tokens or 0)) + max(0, int(fallback_output_tokens or 0)),
            source=fallback_source,
            exact=False,
        ).normalized()

    preferred = preferred_usage.normalized()
    if preferred.exact:
        return preferred

    return split_total_with_fallback(
        preferred,
        fallback_input_tokens=fallback_input_tokens,
        fallback_output_tokens=fallback_output_tokens,
    )


async def _fetch_analytics_usage(
    org_id: str,
    flow_id: str,
    private_api_key: Optional[str],
    run_id: Optional[str] = None,
    fallback_to_latest: bool = False,
    retries: int = 2,
    delay_seconds: float = 0.5,
) -> Optional[STUsage]:
    if not private_api_key:
        return None

    from app.services.analytics import get_analytics_service
    import asyncio

    analytics_service = get_analytics_service()
    for attempt in range(retries):
        usage = await analytics_service.get_run_usage(
            org_id=org_id,
            flow_id=flow_id,
            private_api_key=private_api_key,
            run_id=run_id,
            fallback_to_latest=fallback_to_latest
        )
        if usage is not None:
            return usage.normalized()

        if attempt < retries - 1 and delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

    return None


def extract_model_name(model_path: str) -> str:
    """
    从路径中提取模型名称
    
    例如: "models/gemini-pro" -> "gemini-pro"
    """
    if model_path.startswith("models/"):
        return model_path[7:]
    return model_path


# ============================================================================
# generateContent Endpoint (Requirement 1.3)
# ============================================================================

@router.post("/models/{model}:generateContent")
async def generate_content(
    model: str,
    request: GenerateContentRequest,
    http_request: Request,
    x_goog_api_key: Optional[str] = Header(None, alias="x-goog-api-key"),
    authorization: Optional[str] = Header(None),
    session: AsyncSession = Depends(get_session)
):
    """
    Gemini 兼容?generateContent 端点
    
    - 支持同步响应
    - 验证 API Key 和模型权?
    - 将请求转换为后端格式并执?
    - 将响应转换为 Gemini 格式返回
    """
    request_id = uuid.uuid4().hex[:24]
    start_time = time.time()
    
    # 提取模型名称
    model_name = extract_model_name(model)
    
    # 获取服务实例
    api_key_service = get_api_key_service()
    account_pool = get_account_pool_service()
    backend_client = get_backend_client()
    transformer = get_transformer()
    response_transformer = get_response_transformer()
    error_handler = get_error_handler()
    
    # 1. 提取并验?API Key
    raw_key = extract_api_key(x_goog_api_key, authorization)
    if not raw_key:
        error = error_handler.create_authentication_error(
            "Missing API key. Please include 'x-goog-api-key' header or 'Authorization: Bearer YOUR_API_KEY' header."
        )
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_gemini_error(error)
        )
    
    # 2. 验证 API Key 和模型权?
    api_key_obj, error_response = await validate_api_key_and_model(
        session, raw_key, model_name, api_key_service, error_handler
    )
    if error_response:
        return error_response
    
    # 3. 获取可用账号
    account = await account_pool.get_available_account(session, model_name)
    if not account:
        error = error_handler.create_service_unavailable_error(
            f"No available accounts for model '{model_name}'"
        )
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_gemini_error(error)
        )
    
    # 4. 转换请求格式
    gemini_request = GeminiRequest(
        contents=request.contents,
        generationConfig=request.generationConfig
    )
    unified_request = transformer.parse_gemini_request(gemini_request, model_name, stream=False)
    
    # 5. 获取模型组的输入映射配置
    input_mapping = None
    result = await session.execute(
        select(ModelGroup).where(ModelGroup.name == model_name)
    )
    model_group = result.scalar_one_or_none()
    if model_group and model_group.input_mapping:
        try:
            input_mapping = json.loads(model_group.input_mapping)
        except json.JSONDecodeError:
            pass
    
    # 6. 转换为 ST 格式
    # 将 Gemini 消息格式化为上下文字符串
    # 使用 XML 风格标签避免 Claude 误解为对话模板
    context_parts = []
    for content in request.contents:
        role = content.role or "user"
        # 处理 parts
        text_parts = []
        for part in content.parts:
            if hasattr(part, 'text') and part.text:
                text_parts.append(part.text)
            elif isinstance(part, dict) and 'text' in part:
                text_parts.append(part['text'])
        text_content = "\n".join(text_parts)
        
        if role == "user":
            context_parts.append(f"<human_message>\n{text_content}\n</human_message>")
        elif role == "model":
            context_parts.append(f"<assistant_message>\n{text_content}\n</assistant_message>")
    
    messages_context = "\n\n".join(context_parts)
    
    backend_payload = build_backend_payload(
        user_id="anonymous",
        messages_context=messages_context,
        model_name=model_name,
        input_mapping=input_mapping
    )
    
    logger.info(f"Request {request_id}: model={model_name}, stream=False")
    
    try:
        # 7. 执行同步请求
        backend_response = await backend_client.run_with_account(
            account=account,
            payload=backend_payload,
            account_pool=account_pool
        )
        
        # 转换响应格式
        gemini_response = response_transformer.to_gemini_response(
            backend_response, model_name
        )

        # 从后端响应提取 usage；必要时回退到 analytics(run_id)
        estimated_usage = gemini_response.get("usageMetadata", {})
        estimated_prompt_tokens = estimated_usage.get("promptTokenCount", 0)
        estimated_completion_tokens = estimated_usage.get("candidatesTokenCount", 0)

        backend_usage = extract_usage(backend_response, source="backend.sync")
        run_id = extract_run_id(backend_response)

        analytics_usage = None
        if backend_usage is None or not backend_usage.exact:
            private_api_key = account_pool.decrypt_private_api_key(account)
            analytics_usage = await _fetch_analytics_usage(
                org_id=account.org_id,
                flow_id=account.flow_id,
                private_api_key=private_api_key,
                run_id=run_id,
                fallback_to_latest=False,
                retries=2,
                delay_seconds=0.4,
            )

        selected_usage = choose_better_usage(backend_usage, analytics_usage)
        final_usage = _finalize_usage(
            preferred_usage=selected_usage,
            fallback_input_tokens=estimated_prompt_tokens,
            fallback_output_tokens=estimated_completion_tokens,
            fallback_source="estimate.gemini",
        )

        prompt_tokens = final_usage.input_tokens
        completion_tokens = final_usage.output_tokens
        total_tokens = final_usage.total_tokens

        gemini_response["usageMetadata"] = {
            "promptTokenCount": prompt_tokens,
            "candidatesTokenCount": completion_tokens,
            "totalTokenCount": total_tokens
        }

        logger.debug(
            f"Request {request_id}: usage source={final_usage.source}, run_id={run_id}, "
            f"tokens in/out/total={prompt_tokens}/{completion_tokens}/{total_tokens}"
        )
        
        if total_tokens > 0:
            await account_pool.update_token_usage(
                session, account.id,
                prompt_tokens,
                completion_tokens
            )
        
        elapsed_ms = int((time.time() - start_time) * 1000)
        
        # 记录请求日志
        logger_service = get_logger_service()
        client_ip = http_request.client.host if http_request.client else None
        await logger_service.log_success(
            session=session,
            request_id=request_id,
            api_key_prefix=api_key_obj.key_prefix if api_key_obj else None,
            client_ip=client_ip,
            model=model_name,
            account_id=account.id,
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
            response_time_ms=elapsed_ms
        )
        
        # 计算费用
        from app.services.pricing import get_pricing_service
        pricing_service = get_pricing_service()
        _, _, total_cost = pricing_service.calculate(model_name, prompt_tokens, completion_tokens)
        
        # 更新 API Key 累计统计（包含费用）
        if api_key_obj:
            await api_key_service.update_key_stats(
                session, api_key_obj.id, prompt_tokens, completion_tokens, cost=total_cost
            )
        
        # 更新系统累计统计
        stats_service = get_stats_service()
        await stats_service.update_system_stats(session, prompt_tokens, completion_tokens)
        
        # 记录详细调用日志
        from app.services.call_logger import get_call_logger_service, extract_input_preview
        call_logger = get_call_logger_service()
        
        # 提取输入预览
        input_preview = extract_input_preview(
            [c.model_dump() if hasattr(c, 'model_dump') else c for c in request.contents],
            api_type="gemini"
        )
        
        # 提取输出预览
        output_preview = ""
        candidates = gemini_response.get("candidates", [])
        if candidates:
            content = candidates[0].get("content", {})
            parts = content.get("parts", [])
            if parts:
                output_preview = parts[0].get("text", "")[:500]
        
        await call_logger.log_call(
            session,
            api_key_id=api_key_obj.id if api_key_obj else None,
            api_key_name=api_key_obj.name if api_key_obj else None,
            api_key_prefix=api_key_obj.key_prefix if api_key_obj else None,
            client_ip=client_ip,
            account_id=account.id,
            account_name=account.name,
            model_group=model_name,
            model=model_name,
            api_type="gemini",
            is_stream=False,
            input_preview=input_preview,
            output_preview=output_preview,
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
            response_time_ms=elapsed_ms,
            status="success",
        )
        
        await session.commit()
        logger.info(f"Request {request_id}: completed in {elapsed_ms}ms")
        
        return JSONResponse(
            content=gemini_response,
            headers={"X-Request-ID": request_id}
        )
        
    except BackendAPIError as e:
        logger.error(f"Request {request_id}: Backend API error - {e.message}")
        error = error_handler.parse_backend_error(
            e.response_data or {}, e.status_code or 502
        )
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_gemini_error(error)
        )
    except BackendConnectionError as e:
        logger.error(f"Request {request_id}: Connection error - {e.message}")
        error = error_handler.create_backend_error(
            "Failed to connect to Backend backend"
        )
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_gemini_error(error)
        )
    except BackendTimeoutError as e:
        logger.error(f"Request {request_id}: Timeout - {e.message}")
        error = error_handler.create_backend_error(
            "Request to Backend backend timed out"
        )
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_gemini_error(error)
        )
    except Exception as e:
        logger.exception(f"Request {request_id}: Unexpected error")
        error = error_handler.create_server_error()
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_gemini_error(error)
        )


# ============================================================================
# streamGenerateContent Endpoint (Requirement 1.3)
# ============================================================================

@router.post("/models/{model}:streamGenerateContent")
async def stream_generate_content(
    model: str,
    request: GenerateContentRequest,
    http_request: Request,
    x_goog_api_key: Optional[str] = Header(None, alias="x-goog-api-key"),
    authorization: Optional[str] = Header(None),
    session: AsyncSession = Depends(get_session)
):
    """
    Gemini 兼容?streamGenerateContent 端点
    
    - 支持流式响应
    - 验证 API Key 和模型权?
    - 将请求转换为后端格式并执?
    - 将响应转换为 Gemini SSE 格式返回
    """
    request_id = uuid.uuid4().hex[:24]
    start_time = time.time()
    
    # 提取模型名称
    model_name = extract_model_name(model)
    
    # 获取服务实例
    api_key_service = get_api_key_service()
    account_pool = get_account_pool_service()
    backend_client = get_backend_client()
    transformer = get_transformer()
    response_transformer = get_response_transformer()
    error_handler = get_error_handler()
    
    # 1. 提取并验?API Key
    raw_key = extract_api_key(x_goog_api_key, authorization)
    if not raw_key:
        error = error_handler.create_authentication_error(
            "Missing API key. Please include 'x-goog-api-key' header or 'Authorization: Bearer YOUR_API_KEY' header."
        )
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_gemini_error(error)
        )
    
    # 2. 验证 API Key 和模型权?
    api_key_obj, error_response = await validate_api_key_and_model(
        session, raw_key, model_name, api_key_service, error_handler
    )
    if error_response:
        return error_response
    
    # 3. 获取可用账号
    account = await account_pool.get_available_account(session, model_name)
    if not account:
        error = error_handler.create_service_unavailable_error(
            f"No available accounts for model '{model_name}'"
        )
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_gemini_error(error)
        )
    
    # 4. 转换请求格式
    gemini_request = GeminiRequest(
        contents=request.contents,
        generationConfig=request.generationConfig
    )
    unified_request = transformer.parse_gemini_request(gemini_request, model_name, stream=True)
    
    # 5. 获取模型组的输入映射配置
    input_mapping = None
    result = await session.execute(
        select(ModelGroup).where(ModelGroup.name == model_name)
    )
    model_group = result.scalar_one_or_none()
    if model_group and model_group.input_mapping:
        try:
            input_mapping = json.loads(model_group.input_mapping)
        except json.JSONDecodeError:
            pass
    
    # 6. 转换为 ST 格式
    # 将 Gemini 消息格式化为上下文字符串
    # 使用 XML 风格标签避免 Claude 误解为对话模板
    context_parts = []
    for content in request.contents:
        role = content.role or "user"
        # 处理 parts
        text_parts = []
        for part in content.parts:
            if hasattr(part, 'text') and part.text:
                text_parts.append(part.text)
            elif isinstance(part, dict) and 'text' in part:
                text_parts.append(part['text'])
        text_content = "\n".join(text_parts)
        
        if role == "user":
            context_parts.append(f"<human_message>\n{text_content}\n</human_message>")
        elif role == "model":
            context_parts.append(f"<assistant_message>\n{text_content}\n</assistant_message>")
    
    messages_context = "\n\n".join(context_parts)
    
    backend_payload = build_backend_payload(
        user_id="anonymous",
        messages_context=messages_context,
        model_name=model_name,
        input_mapping=input_mapping
    )
    
    logger.info(f"Request {request_id}: model={model_name}, stream=True")
    
    # 流式响应 - 累积内容并在结束后更新统?
    logger_service = get_logger_service()
    client_ip = http_request.client.host if http_request.client else None
    
    # 获取账号的 Private API Key（用于从 Backend Analytics 获取真实 token）
    private_api_key = account_pool.decrypt_private_api_key(account)
    
    # 提取输入预览
    from app.services.call_logger import extract_input_preview
    input_preview = extract_input_preview(
        [c.model_dump() if hasattr(c, 'model_dump') else c for c in request.contents],
        api_type="gemini"
    )
    
    # 保存上下文信息用于流结束后更新统计
    stream_context = {
        "request_id": request_id,
        "api_key_id": api_key_obj.id if api_key_obj else None,
        "api_key_name": api_key_obj.name if api_key_obj else None,
        "api_key_prefix": api_key_obj.key_prefix if api_key_obj else None,
        "client_ip": client_ip,
        "model": model_name,
        "account_id": account.id,
        "account_name": account.name,
        "model_group": model_name,
        "account_org_id": account.org_id,
        "account_flow_id": account.flow_id,
        "private_api_key": private_api_key,
        "start_time": start_time,
        "accumulated_content": [],
        "input_preview": input_preview,
        "st_run_id": None,
        "st_usage": None,
    }
    
    # 7. 流式响应
    async def generate_stream():
        try:
            stream_gen = await backend_client.execute_with_account(
                account=account,
                payload=backend_payload,
                stream=True,
                account_pool=account_pool
            )

            async def tracked_backend_stream():
                async for raw_chunk in stream_gen:
                    run_id = extract_run_id(raw_chunk)
                    if run_id and not stream_context.get("st_run_id"):
                        stream_context["st_run_id"] = run_id

                    usage_candidate = extract_usage(raw_chunk, source="backend.stream")
                    if usage_candidate is not None:
                        stream_context["st_usage"] = choose_better_usage(
                            stream_context.get("st_usage"),
                            usage_candidate
                        )

                    yield raw_chunk

            async for chunk in response_transformer.transform_backend_sse_to_gemini(
                tracked_backend_stream(), model_name
            ):
                # 尝试?chunk 中提取内容用?token 估算
                if chunk.startswith("data: "):
                    try:
                        chunk_data = json.loads(chunk[6:])
                        candidates = chunk_data.get("candidates", [])
                        if candidates:
                            content = candidates[0].get("content", {})
                            parts = content.get("parts", [])
                            for part in parts:
                                text = part.get("text", "")
                                if text:
                                    stream_context["accumulated_content"].append(text)
                    except (json.JSONDecodeError, IndexError, KeyError):
                        pass
                yield chunk
            
            # 流结束后，异步更新统?
            import asyncio
            asyncio.create_task(update_gemini_stream_stats(stream_context))
                
        except BackendClientError as e:
            logger.error(f"Request {request_id}: Backend error - {e.message}")
            error_data = error_handler.to_gemini_error(
                error_handler.from_backend_exception(e)
            )
            yield f"data: {json.dumps(error_data)}\n\n"
        except Exception as e:
            logger.exception(f"Request {request_id}: Unexpected error")
            error_data = error_handler.to_gemini_error(
                error_handler.create_server_error()
            )
            yield f"data: {json.dumps(error_data)}\n\n"
    
    return StreamingResponse(
        generate_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Request-ID": request_id
        }
    )


# ============================================================================
# Models Endpoint (Requirement 1.3)
# ============================================================================

@router.get("/models")
async def list_models(
    x_goog_api_key: Optional[str] = Header(None, alias="x-goog-api-key"),
    authorization: Optional[str] = Header(None),
    session: AsyncSession = Depends(get_session)
):
    """
    列出可用的模?
    
    必须提供有效?API Key 才能访问
    """
    api_key_service = get_api_key_service()
    error_handler = get_error_handler()
    
    # 必须提供有效?API Key
    raw_key = extract_api_key(x_goog_api_key, authorization)
    if not raw_key:
        error = error_handler.create_authentication_error()
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_gemini_error(error)
        )
    
    is_valid, error_msg, api_key_obj = await api_key_service.validate_key(
        session, raw_key
    )
    if not is_valid:
        error = error_handler.create_authentication_error()
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_gemini_error(error)
        )
    
    # 获取?Key 授权的模型列?
    allowed_models = api_key_service.get_model_groups(api_key_obj)
    
    # 获取所有模型组
    result = await session.execute(select(ModelGroup))
    model_groups = list(result.scalars().all())
    
    # 构建模型列表（Gemini 格式?
    models = []
    
    for group in model_groups:
        # 只返回授权的模型
        if allowed_models is not None and group.name not in allowed_models:
            continue
        
        models.append({
            "name": f"models/{group.name}",
            "version": "001",
            "displayName": group.name,
            "description": group.description or f"Model: {group.name}",
            "inputTokenLimit": 32768,
            "outputTokenLimit": 8192,
            "supportedGenerationMethods": [
                "generateContent",
                "streamGenerateContent"
            ],
            "temperature": 1.0,
            "topP": 0.95,
            "topK": 64
        })
    
    return {"models": models}


@router.get("/models/{model}")
async def get_model(
    model: str,
    x_goog_api_key: Optional[str] = Header(None, alias="x-goog-api-key"),
    authorization: Optional[str] = Header(None),
    session: AsyncSession = Depends(get_session)
):
    """
    获取单个模型的详细信?
    
    必须提供有效?API Key 才能访问
    """
    api_key_service = get_api_key_service()
    error_handler = get_error_handler()
    
    # 必须提供有效?API Key
    raw_key = extract_api_key(x_goog_api_key, authorization)
    if not raw_key:
        error = error_handler.create_authentication_error()
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_gemini_error(error)
        )
    
    is_valid, error_msg, api_key_obj = await api_key_service.validate_key(
        session, raw_key
    )
    if not is_valid:
        error = error_handler.create_authentication_error()
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_gemini_error(error)
        )
    
    # 提取模型名称
    model_name = extract_model_name(model)
    
    # 查找模型?
    result = await session.execute(
        select(ModelGroup).where(ModelGroup.name == model_name)
    )
    model_group = result.scalar_one_or_none()
    
    if not model_group:
        # 不暴露模型是否存在，统一返回 401
        error = error_handler.create_authentication_error(
        )
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_gemini_error(error)
        )
    
    return {
        "name": f"models/{model_group.name}",
        "version": "001",
        "displayName": model_group.name,
        "description": model_group.description or f"Model: {model_group.name}",
        "inputTokenLimit": 32768,
        "outputTokenLimit": 8192,
        "supportedGenerationMethods": [
            "generateContent",
            "streamGenerateContent"
        ],
        "temperature": 1.0,
        "topP": 0.95,
        "topK": 64
    }


# ============================================================================
# Stream Stats Update Helper
# ============================================================================

async def update_gemini_stream_stats(context: dict):
    """
    流式响应结束后更新统计
    
    优先从 Backend Analytics 获取真实的 token 数据，
    如果无法获取则使用 tiktoken 估算。
    
    Args:
        context: 包含请求上下文信息的字典
    """
    from app.models.database import get_session_factory
    from app.services.token_counter import get_token_counter
    from app.services.call_logger import get_call_logger_service
    
    try:
        elapsed_ms = int((time.time() - context["start_time"]) * 1000)
        # 累积的输出内容
        accumulated_content = "".join(context.get("accumulated_content", []))

        token_counter = get_token_counter()
        fallback_input_tokens = 0
        fallback_output_tokens = token_counter.count(accumulated_content) if accumulated_content else 0

        # 优先使用流中捕获 usage
        selected_usage = context.get("st_usage")

        # 若缺失则按 run_id 查询 analytics
        run_id = context.get("st_run_id")
        if selected_usage is None or not selected_usage.exact:
            analytics_usage = await _fetch_analytics_usage(
                org_id=context["account_org_id"],
                flow_id=context["account_flow_id"],
                private_api_key=context.get("private_api_key"),
                run_id=run_id,
                fallback_to_latest=False,
                retries=3,
                delay_seconds=0.5,
            )
            selected_usage = choose_better_usage(selected_usage, analytics_usage)

        final_usage = _finalize_usage(
            preferred_usage=selected_usage,
            fallback_input_tokens=fallback_input_tokens,
            fallback_output_tokens=fallback_output_tokens,
            fallback_source="estimate.stream",
        )

        input_tokens = final_usage.input_tokens
        output_tokens = final_usage.output_tokens
        total_tokens = final_usage.total_tokens
        
        # 创建新的数据库会话
        session_factory = get_session_factory()
        async with session_factory() as session:
            # 记录请求日志
            logger_service = get_logger_service()
            await logger_service.log_success(
                session=session,
                request_id=context["request_id"],
                api_key_prefix=context["api_key_prefix"],
                client_ip=context["client_ip"],
                model=context["model"],
                account_id=context["account_id"],
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                response_time_ms=elapsed_ms
            )
            
            # 计算费用
            from app.services.pricing import get_pricing_service
            pricing_service = get_pricing_service()
            _, _, total_cost = pricing_service.calculate(context["model"], input_tokens, output_tokens)
            
            # 更新 API Key 累计统计（包含费用）
            if context["api_key_id"]:
                api_key_service = get_api_key_service()
                await api_key_service.update_key_stats(
                    session, context["api_key_id"], input_tokens, output_tokens, cost=total_cost
                )
            
            # 更新系统累计统计
            stats_service = get_stats_service()
            await stats_service.update_system_stats(session, input_tokens, output_tokens)
            
            # 记录详细调用日志
            call_logger = get_call_logger_service()
            await call_logger.log_call(
                session,
                api_key_id=context.get("api_key_id"),
                api_key_name=context.get("api_key_name"),
                api_key_prefix=context.get("api_key_prefix"),
                client_ip=context.get("client_ip"),
                account_id=context.get("account_id"),
                account_name=context.get("account_name"),
                model_group=context.get("model_group"),
                model=context.get("model"),
                api_type="gemini",
                is_stream=True,
                input_preview=context.get("input_preview"),
                output_preview=accumulated_content[:500] if accumulated_content else None,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                response_time_ms=elapsed_ms,
                status="success",
            )
            
            await session.commit()
            
        logger.debug(
            f"Gemini stream stats updated for request {context['request_id']}: "
            f"tokens={total_tokens}, source={final_usage.source}, "
            f"run_id={context.get('st_run_id')}, elapsed={elapsed_ms}ms"
        )
    except Exception as e:
        logger.error(f"Failed to update Gemini stream stats: {e}")

