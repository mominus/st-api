"""
OpenAI Compatible Router
实现 OpenAI API 兼容的路由端点

Requirements: 1.1, 1.4, 1.5
"""

import json
import uuid
import time
import logging
from typing import Optional, List, Dict, Any, Union

from fastapi import APIRouter, Request, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_session, ModelGroup
from app.services.transformer import (
    OpenAIChatRequest, OpenAIMessage, RequestTransformer, get_transformer
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
from app.services.key_info import build_public_key_info_payload
from app.services.call_logger import get_call_logger_service
from sqlalchemy import select

logger = logging.getLogger(__name__)

# 创建路由器
router = APIRouter(prefix="/v1", tags=["OpenAI Compatible"])


# ============================================================================
# Request/Response Models
# ============================================================================

class ChatCompletionRequest(BaseModel):
    """OpenAI Chat Completion 请求模型"""
    model: str = Field(..., description="模型名称")
    messages: List[OpenAIMessage] = Field(..., description="消息列表")
    stream: bool = Field(default=False, description="是否流式响应")
    temperature: Optional[float] = Field(default=None, ge=0, le=2, description="温度参数")
    max_tokens: Optional[int] = Field(default=None, gt=0, description="最大 token 数")
    top_p: Optional[float] = Field(default=None, ge=0, le=1, description="Top-p 采样")
    frequency_penalty: Optional[float] = Field(default=None, ge=-2, le=2, description="频率惩罚")
    presence_penalty: Optional[float] = Field(default=None, ge=-2, le=2, description="存在惩罚")
    stop: Optional[List[str]] = Field(default=None, description="停止序列")
    user: Optional[str] = Field(default=None, description="用户标识")


class ResponsesRequest(BaseModel):
    """OpenAI Responses API 请求模型 (用于 Codex 等)"""
    model_config = {"extra": "allow"}  # 允许额外字段
    
    model: str = Field(..., description="模型名称")
    input: Any = Field(..., description="输入内容，可以是字符串或消息数组")
    stream: bool = Field(default=False, description="是否流式响应")
    temperature: Optional[float] = Field(default=None, description="温度参数")
    max_output_tokens: Optional[int] = Field(default=None, description="最大输出 token 数")
    instructions: Optional[str] = Field(default=None, description="系统指令")


class ModelInfo(BaseModel):
    """模型信息"""
    id: str
    object: str = "model"
    created: int
    owned_by: str = "organization"


class ModelsResponse(BaseModel):
    """模型列表响应"""
    object: str = "list"
    data: List[ModelInfo]


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


def extract_api_key(authorization: Optional[str]) -> Optional[str]:
    """
    从 Authorization header 提取 API Key
    
    支持格式:
    - Bearer sk-xxx
    - sk-xxx
    """
    if not authorization:
        return None
    
    auth = authorization.strip()
    if auth.lower().startswith("bearer "):
        key = auth[7:].strip()
    else:
        key = auth
    
    return sanitize_api_key(key)


def extract_api_key_from_headers(
    authorization: Optional[str],
    x_api_key: Optional[str] = None
) -> Optional[str]:
    """
    从多个请求头中提取 API Key。

    优先级:
    1. Authorization: Bearer sk-xxx
    2. x-api-key: sk-xxx
    """
    key = extract_api_key(authorization)
    if key:
        return key

    if x_api_key:
        return sanitize_api_key(x_api_key.strip())

    return None


def extract_api_key_from_request(
    authorization: Optional[str],
    x_api_key: Optional[str] = None,
    api_key_query: Optional[str] = None,
    key_query: Optional[str] = None
) -> Optional[str]:
    """
    从请求头或查询参数中提取 API Key。

    优先级:
    1. Authorization: Bearer sk-xxx
    2. x-api-key: sk-xxx
    3. ?api_key=sk-xxx
    4. ?key=sk-xxx
    """
    key = extract_api_key_from_headers(authorization, x_api_key)
    if key:
        return key

    if api_key_query:
        return sanitize_api_key(api_key_query.strip())

    if key_query:
        return sanitize_api_key(key_query.strip())

    return None


async def validate_api_key_and_model(
    session: AsyncSession,
    raw_key: str,
    model: str,
    api_key_service: APIKeyService,
    error_handler: ErrorHandler
) -> tuple:
    """
    验证 API Key 和模型权限
    
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
            content=error_handler.to_openai_error(error)
        )
    
    return api_key_obj, None


# ============================================================================
# Chat Completions Endpoint (Requirement 1.1, 1.4, 1.5)
# ============================================================================

@router.post("/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    http_request: Request,
    authorization: Optional[str] = Header(None),
    session: AsyncSession = Depends(get_session)
):
    """
    OpenAI 兼容的 Chat Completions 端点
    
    - 支持同步和流式响应
    - 验证 API Key 和模型权限
    - 将请求转换为后端格式并执行
    - 将响应转换为 OpenAI 格式返回
    """
    request_id = uuid.uuid4().hex[:24]
    start_time = time.time()
    
    # 调试日志
    logger.info(f"Request {request_id}: [OPENAI DEBUG] model={request.model}, stream={request.stream}")
    
    # 获取服务实例
    api_key_service = get_api_key_service()
    account_pool = get_account_pool_service()
    backend_client = get_backend_client()
    transformer = get_transformer()
    response_transformer = get_response_transformer()
    error_handler = get_error_handler()
    
    # 1. 提取并验证 API Key
    raw_key = extract_api_key(authorization)
    if not raw_key:
        error = error_handler.create_authentication_error(
            "Missing API key. Please include 'Authorization: Bearer YOUR_API_KEY' header."
        )
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_openai_error(error)
        )
    
    # 2. 验证 API Key 和模型权限
    api_key_obj, error_response = await validate_api_key_and_model(
        session, raw_key, request.model, api_key_service, error_handler
    )
    if error_response:
        return error_response
    
    # 3. 获取可用账号
    account = await account_pool.get_available_account(session, request.model)
    if not account:
        error = error_handler.create_service_unavailable_error(
            f"No available accounts for model '{request.model}'"
        )
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_openai_error(error)
        )
    
    # 4. 转换请求格式
    openai_request = OpenAIChatRequest(
        model=request.model,
        messages=request.messages,
        stream=request.stream,
        temperature=request.temperature,
        max_tokens=request.max_tokens
    )
    unified_request = transformer.parse_openai_request(openai_request)
    
    # 5. 获取模型组的输入映射配置
    input_mapping = None
    result = await session.execute(
        select(ModelGroup).where(ModelGroup.name == request.model)
    )
    model_group = result.scalar_one_or_none()
    if model_group and model_group.input_mapping:
        try:
            input_mapping = json.loads(model_group.input_mapping)
        except json.JSONDecodeError:
            pass
    
    # 6. 转换为后端格式
    # 使用 format_messages_to_context 直接格式化原始消息，保持正确顺序
    messages_context = transformer.format_messages_to_context(request.messages)
    
    # 构建 payload
    backend_payload = {
        "user_id": request.user or "anonymous",
        "in-0": messages_context,  # 完整对话上下文
        "conversation_id": str(uuid.uuid4())  # 每次请求使用新的会话ID
    }
    
    # 如果有自定义输入映射，应用它（但 merge_context 模式下只使用 user_input 映射）
    if input_mapping and "user_input" in input_mapping:
        field_name = input_mapping["user_input"]
        if field_name != "in-0":
            backend_payload[field_name] = messages_context
            del backend_payload["in-0"]
    
    logger.info(f"Request {request_id}: model={request.model}, stream={request.stream}")
    
    try:
        # 7. 执行请求（同步或流式）
        if request.stream:
            # 流式响应 - 累积内容并在结束后更新统计
            logger_service = get_logger_service()
            client_ip = http_request.client.host if http_request.client else None
            
            # 保存上下文信息用于流结束后更新统计
            # 获取账号的 Private API Key（用于从 Backend Analytics 获取真实 token）
            private_api_key = account_pool.decrypt_private_api_key(account)
            
            # 提取输入预览
            from app.services.call_logger import extract_input_preview
            input_preview = extract_input_preview(
                [m.model_dump() if hasattr(m, 'model_dump') else m for m in request.messages],
                api_type="openai"
            )
            
            # 提取最后一条用户消息（用于 token 计算）
            last_user_msg = ""
            for msg in reversed(request.messages):
                if msg.role == "user":
                    last_user_msg = msg.content if isinstance(msg.content, str) else str(msg.content)
                    break
            
            # 从 Stack AI Analytics 获取请求前的 today_tokens（用于计算差值）
            pre_request_tokens = 0
            if private_api_key:
                try:
                    from app.services.analytics import get_analytics_service
                    analytics_service = get_analytics_service()
                    stats = await analytics_service.get_recent_stats(
                        org_id=account.org_id,
                        flow_id=account.flow_id,
                        private_api_key=private_api_key,
                        days=1
                    )
                    if stats:
                        pre_request_tokens = stats.today_tokens
                        print(f"[DEBUG] Pre-request today_tokens from Analytics: {pre_request_tokens}")
                except Exception as e:
                    print(f"[DEBUG] Failed to get pre-request tokens: {e}")
                    pre_request_tokens = 0
            
            stream_context = {
                "request_id": request_id,
                "api_key_id": api_key_obj.id if api_key_obj else None,
                "api_key_name": api_key_obj.name if api_key_obj else None,
                "api_key_prefix": api_key_obj.key_prefix if api_key_obj else None,
                "client_ip": client_ip,
                "model": request.model,
                "account_id": account.id,
                "account_name": account.name,
                "model_group": account.model_group,
                "account_org_id": account.org_id,
                "account_flow_id": account.flow_id,
                "private_api_key": private_api_key,
                "start_time": start_time,
                "accumulated_content": [],
                "input_preview": input_preview,
                "last_user_msg": last_user_msg,  # 只用当前消息计算输入 token
                "pre_request_tokens": pre_request_tokens  # 从 Stack AI Analytics 获取的请求前 token
            }
            
            # [DEBUG] 流式请求调试输出
            print(f"[DEBUG STREAM] last_user_msg='{last_user_msg[:80] if len(last_user_msg) > 80 else last_user_msg}', len={len(last_user_msg)}")
            
            # 流式响应 - 真正的实时流式输出
            async def generate_stream():
                try:
                    stream_gen = await backend_client.execute_with_account(
                        account=account,
                        payload=backend_payload,
                        stream=True,
                        account_pool=account_pool
                    )
                    
                    # 直接转换并输出，同时累积内容
                    async for chunk in response_transformer.transform_backend_sse_to_openai(
                        stream_gen, request.model, request_id
                    ):
                        # 尝试从 chunk 中提取内容用于 token 估算
                        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
                            try:
                                chunk_data = json.loads(chunk[6:])
                                content = chunk_data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                                if content:
                                    stream_context["accumulated_content"].append(content)
                            except (json.JSONDecodeError, IndexError, KeyError):
                                pass
                        yield chunk
                    
                    # 流结束后，异步更新统计
                    import asyncio
                    asyncio.create_task(update_stream_stats(stream_context))
                        
                except BackendClientError as e:
                    logger.error(f"Request {request_id}: Backend error - {e.message}")
                    error = error_handler.from_backend_exception(e)
                    error_data = error_handler.to_openai_error(error)
                    yield f"data: {json.dumps(error_data)}\n\n"
                    yield "data: [DONE]\n\n"
                except Exception as e:
                    logger.exception(f"Request {request_id}: Unexpected error")
                    error_data = error_handler.to_openai_error(
                        error_handler.create_server_error()
                    )
                    yield f"data: {json.dumps(error_data)}\n\n"
                    yield "data: [DONE]\n\n"
            
            return StreamingResponse(
                generate_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Request-ID": request_id
                }
            )
        else:
            # 同步响应
            backend_response = await backend_client.run_with_account(
                account=account,
                payload=backend_payload,
                account_pool=account_pool
            )
            
            # 转换响应格式
            # 只用最后一条用户消息计算输入 token（匹配 Stack AI 的计费方式）
            last_user_msg = ""
            for msg in reversed(request.messages):
                if msg.role == "user":
                    last_user_msg = msg.content if isinstance(msg.content, str) else str(msg.content)
                    break
            
            openai_response = response_transformer.to_openai_response(
                backend_response, request.model, request_id,
                input_text=last_user_msg  # 只用当前用户消息计算 prompt_tokens
            )
            
            # 更新 token 使用量
            usage = openai_response.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            total_tokens = usage.get("total_tokens", 0)
            
            # 调试日志：显示 token 计算来源
            print(f"[DEBUG] Token calculation - last_user_msg='{last_user_msg[:50]}...', input_text_len={len(last_user_msg)}, prompt_tokens={prompt_tokens}, completion_tokens={completion_tokens}")
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
                model=request.model,
                account_id=account.id,
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens,
                response_time_ms=elapsed_ms
            )
            
            # 计算费用
            from app.services.pricing import get_pricing_service
            pricing_service = get_pricing_service()
            _, _, total_cost = pricing_service.calculate(request.model, prompt_tokens, completion_tokens)
            
            # 更新 API Key 累计统计（包含费用）
            if api_key_obj:
                await api_key_service.update_key_stats(
                    session, api_key_obj.id, prompt_tokens, completion_tokens, cost=total_cost
                )
            
            # 更新系统累计统计
            stats_service = get_stats_service()
            await stats_service.update_system_stats(
                session, prompt_tokens, completion_tokens
            )
            
            # 记录详细调用日志
            from app.services.call_logger import get_call_logger_service, extract_input_preview
            call_logger = get_call_logger_service()
            
            # 提取输入预览
            input_preview = extract_input_preview(
                [m.model_dump() if hasattr(m, 'model_dump') else m for m in request.messages],
                api_type="openai"
            )
            
            # 提取输出预览
            output_preview = ""
            choices = openai_response.get("choices", [])
            if choices:
                message = choices[0].get("message", {})
                output_preview = message.get("content", "")[:500]
            
            await call_logger.log_call(
                session,
                api_key_id=api_key_obj.id if api_key_obj else None,
                api_key_name=api_key_obj.name if api_key_obj else None,
                api_key_prefix=api_key_obj.key_prefix if api_key_obj else None,
                client_ip=client_ip,
                account_id=account.id,
                account_name=account.name,
                model_group=account.model_group,
                model=request.model,
                api_type="openai",
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
                content=openai_response,
                headers={"X-Request-ID": request_id}
            )
            
    except BackendAPIError as e:
        logger.error(f"Request {request_id}: Backend API error - {e.message}")
        error = error_handler.parse_backend_error(
            e.response_data or {}, e.status_code or 502
        )
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_openai_error(error)
        )
    except BackendConnectionError as e:
        logger.error(f"Request {request_id}: Connection error - {e.message}")
        error = error_handler.create_backend_error(
            "Failed to connect to Backend backend"
        )
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_openai_error(error)
        )
    except BackendTimeoutError as e:
        logger.error(f"Request {request_id}: Timeout - {e.message}")
        error = error_handler.create_backend_error(
            "Request to Backend backend timed out"
        )
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_openai_error(error)
        )
    except Exception as e:
        logger.exception(f"Request {request_id}: Unexpected error")
        error = error_handler.create_server_error()
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_openai_error(error)
        )


# ============================================================================
# Models Endpoint (Requirement 1.1)
# ============================================================================

@router.get("/models", response_model=ModelsResponse)
async def list_models(
    authorization: Optional[str] = Header(None),
    session: AsyncSession = Depends(get_session)
):
    """
    列出可用的模型
    
    必须提供有效的 API Key 才能访问
    """
    api_key_service = get_api_key_service()
    error_handler = get_error_handler()
    
    # 必须提供有效的 API Key
    raw_key = extract_api_key(authorization)
    if not raw_key:
        error = error_handler.create_authentication_error()
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_openai_error(error)
        )
    
    is_valid, error_msg, api_key_obj = await api_key_service.validate_key(
        session, raw_key
    )
    if not is_valid:
        error = error_handler.create_authentication_error()
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_openai_error(error)
        )
    
    # 获取该 Key 授权的模型列表
    allowed_models = api_key_service.get_model_groups(api_key_obj)
    
    # 获取所有模型组
    result = await session.execute(select(ModelGroup))
    model_groups = list(result.scalars().all())
    
    # 构建模型列表
    models = []
    current_time = int(time.time())
    
    for group in model_groups:
        # 只返回授权的模型
        if allowed_models is not None and group.name not in allowed_models:
            continue
        
        models.append(ModelInfo(
            id=group.name,
            object="model",
            created=int(group.created_at.timestamp()) if group.created_at else current_time,
            owned_by="organization"
        ))
    
    return ModelsResponse(
        object="list",
        data=models
    )


@router.get("/models/{model_id}")
async def get_model(
    model_id: str,
    authorization: Optional[str] = Header(None),
    session: AsyncSession = Depends(get_session)
):
    """
    获取单个模型的详细信息
    
    必须提供有效的 API Key 才能访问
    """
    api_key_service = get_api_key_service()
    error_handler = get_error_handler()
    
    # 必须提供有效的 API Key
    raw_key = extract_api_key(authorization)
    if not raw_key:
        error = error_handler.create_authentication_error()
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_openai_error(error)
        )
    
    is_valid, error_msg, api_key_obj = await api_key_service.validate_key(
        session, raw_key
    )
    if not is_valid:
        error = error_handler.create_authentication_error()
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_openai_error(error)
        )
    
    # 查找模型组
    result = await session.execute(
        select(ModelGroup).where(ModelGroup.name == model_id)
    )
    model_group = result.scalar_one_or_none()
    
    if not model_group:
        # 不暴露模型是否存在，统一返回 401
        error = error_handler.create_authentication_error()
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_openai_error(error)
        )
    
    current_time = int(time.time())
    return {
        "id": model_group.name,
        "object": "model",
        "created": int(model_group.created_at.timestamp()) if model_group.created_at else current_time,
        "owned_by": "organization",
        "permission": [],
        "root": model_group.name,
        "parent": None
    }


@router.get("/key/info")
async def get_current_key_info(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    api_key: Optional[str] = Query(None, description="API Key，支持浏览器地址栏查询"),
    key: Optional[str] = Query(None, description="API Key，api_key 的简写"),
    session: AsyncSession = Depends(get_session)
):
    """
    获取当前 API Key 的模型权限、额度和使用情况。

    支持:
    - Authorization: Bearer sk-xxx
    - x-api-key: sk-xxx
    - /v1/key/info?api_key=sk-xxx
    - /v1/key/info?key=sk-xxx
    """
    api_key_service = get_api_key_service()
    account_pool = get_account_pool_service()
    call_logger = get_call_logger_service()
    error_handler = get_error_handler()

    raw_key = extract_api_key_from_request(authorization, x_api_key, api_key, key)
    if not raw_key:
        error = error_handler.create_authentication_error(
            "Missing API key. Please include Authorization, x-api-key, api_key, or key."
        )
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_openai_error(error)
        )

    api_key_obj = await api_key_service.get_key_by_raw(session, raw_key)
    if api_key_obj is None or api_key_obj.status == "revoked":
        error = error_handler.create_authentication_error("Invalid API key")
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_openai_error(error)
        )

    allowed_models = api_key_service.get_model_groups(api_key_obj)
    usage_by_model = await call_logger.get_api_key_usage_by_model(
        session,
        api_key_obj.id,
        allowed_models=allowed_models,
        since=api_key_obj.created_at,
    )
    models: List[Dict[str, Any]] = []

    if allowed_models:
        for model_name in allowed_models:
            models.append({
                "id": model_name,
                "accounts": await account_pool.get_accounts_by_model_group(session, model_name),
                "usage": usage_by_model.get(model_name),
            })

    return build_public_key_info_payload(api_key_obj, models)


# ============================================================================
# Responses API Endpoint (for Codex compatibility)
# ============================================================================

@router.post("/responses")
async def create_response(
    http_request: Request,
    authorization: Optional[str] = Header(None),
    session: AsyncSession = Depends(get_session)
):
    """
    OpenAI Responses API 端点 (用于 Codex 等工具)
    
    将 Responses API 请求转换为内部格式处理
    """
    request_id = uuid.uuid4().hex[:24]
    start_time = time.time()
    
    # 获取服务实例
    api_key_service = get_api_key_service()
    account_pool = get_account_pool_service()
    backend_client = get_backend_client()
    transformer = get_transformer()
    response_transformer = get_response_transformer()
    error_handler = get_error_handler()
    
    # 解析请求体
    try:
        body = await http_request.body()
        body_json = json.loads(body)
        request = ResponsesRequest(**body_json)
    except json.JSONDecodeError as e:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": f"Invalid JSON: {e}", "type": "invalid_request_error"}}
        )
    except Exception as e:
        return JSONResponse(
            status_code=422,
            content={"error": {"message": f"Validation error: {e}", "type": "invalid_request_error"}}
        )
    
    # 1. 提取并验证 API Key
    raw_key = extract_api_key(authorization)
    if not raw_key:
        error = error_handler.create_authentication_error(
            "Missing API key. Please include 'Authorization: Bearer YOUR_API_KEY' header."
        )
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_openai_error(error)
        )
    
    # 2. 验证 API Key 和模型权限
    api_key_obj, error_response = await validate_api_key_and_model(
        session, raw_key, request.model, api_key_service, error_handler
    )
    if error_response:
        return error_response
    
    # 3. 获取可用账号
    account = await account_pool.get_available_account(session, request.model)
    if not account:
        error = error_handler.create_service_unavailable_error(
            f"No available accounts for model '{request.model}'"
        )
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_openai_error(error)
        )
    
    # 4. 将 Responses API 格式转换为 messages 格式
    messages = []
    
    # 添加系统指令
    if request.instructions:
        messages.append(OpenAIMessage(role="system", content=request.instructions))
    
    # 处理 input 字段
    if isinstance(request.input, str):
        messages.append(OpenAIMessage(role="user", content=request.input))
    elif isinstance(request.input, list):
        for item in request.input:
            if isinstance(item, dict):
                role = item.get("role", "user")
                content = item.get("content", "")
                if isinstance(content, list):
                    # 处理多部分内容
                    text_parts = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "input_text":
                            text_parts.append(part.get("text", ""))
                        elif isinstance(part, dict) and part.get("type") == "text":
                            text_parts.append(part.get("text", ""))
                        elif isinstance(part, str):
                            text_parts.append(part)
                    content = "\n".join(text_parts)
                messages.append(OpenAIMessage(role=role, content=content))
            elif isinstance(item, str):
                messages.append(OpenAIMessage(role="user", content=item))
    
    # 5. 转换请求格式
    openai_request = OpenAIChatRequest(
        model=request.model,
        messages=messages,
        stream=request.stream,
        temperature=request.temperature,
        max_tokens=request.max_output_tokens
    )
    unified_request = transformer.parse_openai_request(openai_request)
    
    # 6. 获取模型组的输入映射配置
    input_mapping = None
    result = await session.execute(
        select(ModelGroup).where(ModelGroup.name == request.model)
    )
    model_group = result.scalar_one_or_none()
    if model_group and model_group.input_mapping:
        try:
            input_mapping = json.loads(model_group.input_mapping)
        except json.JSONDecodeError:
            pass
    
    # 7. 转换为后端格式
    backend_payload = transformer.to_stackai_dict(
        unified_request,
        input_mapping=input_mapping,
        user_id="anonymous"
    )
    
    logger.info(f"Responses API Request {request_id}: model={request.model}, stream={request.stream}")
    
    try:
        if request.stream:
            # 流式响应
            async def generate_stream():
                try:
                    stream_gen = await backend_client.execute_with_account(
                        account=account,
                        payload=backend_payload,
                        stream=True,
                        account_pool=account_pool
                    )
                    
                    # 发送 response.created 事件
                    created_event = {
                        "type": "response.created",
                        "response": {
                            "id": f"resp_{request_id}",
                            "object": "response",
                            "created_at": int(time.time()),
                            "status": "in_progress",
                            "model": request.model,
                            "output": []
                        }
                    }
                    yield f"event: response.created\ndata: {json.dumps(created_event)}\n\n"
                    
                    # 发送 response.output_item.added 事件
                    item_added = {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {
                            "type": "message",
                            "id": f"msg_{request_id}",
                            "role": "assistant",
                            "content": []
                        }
                    }
                    yield f"event: response.output_item.added\ndata: {json.dumps(item_added)}\n\n"
                    
                    # 发送 response.content_part.added 事件
                    content_added = {
                        "type": "response.content_part.added",
                        "item_id": f"msg_{request_id}",
                        "output_index": 0,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": ""}
                    }
                    yield f"event: response.content_part.added\ndata: {json.dumps(content_added)}\n\n"
                    
                    # 流式输出内容
                    full_text = ""
                    async for raw_chunk in stream_gen:
                        token = response_transformer._parse_backend_sse(raw_chunk)
                        if token:
                            full_text += token
                            delta_event = {
                                "type": "response.output_text.delta",
                                "item_id": f"msg_{request_id}",
                                "output_index": 0,
                                "content_index": 0,
                                "delta": token
                            }
                            yield f"event: response.output_text.delta\ndata: {json.dumps(delta_event)}\n\n"
                    
                    # 发送 response.output_text.done 事件
                    text_done = {
                        "type": "response.output_text.done",
                        "item_id": f"msg_{request_id}",
                        "output_index": 0,
                        "content_index": 0,
                        "text": full_text
                    }
                    yield f"event: response.output_text.done\ndata: {json.dumps(text_done)}\n\n"
                    
                    # 发送 response.content_part.done 事件
                    content_done = {
                        "type": "response.content_part.done",
                        "item_id": f"msg_{request_id}",
                        "output_index": 0,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": full_text}
                    }
                    yield f"event: response.content_part.done\ndata: {json.dumps(content_done)}\n\n"
                    
                    # 发送 response.output_item.done 事件
                    item_done = {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": {
                            "type": "message",
                            "id": f"msg_{request_id}",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": full_text}]
                        }
                    }
                    yield f"event: response.output_item.done\ndata: {json.dumps(item_done)}\n\n"
                    
                    # 发送 response.completed 事件
                    completed_event = {
                        "type": "response.completed",
                        "response": {
                            "id": f"resp_{request_id}",
                            "object": "response",
                            "created_at": int(time.time()),
                            "status": "completed",
                            "model": request.model,
                            "output": [{
                                "type": "message",
                                "id": f"msg_{request_id}",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": full_text}]
                            }]
                        }
                    }
                    yield f"event: response.completed\ndata: {json.dumps(completed_event)}\n\n"
                    
                except BackendClientError as e:
                    logger.error(f"Responses API Request {request_id}: Backend error - {e.message}")
                    error_event = {
                        "type": "error",
                        "error": error_handler.to_openai_error(
                            error_handler.from_backend_exception(e)
                        )["error"]
                    }
                    yield f"event: error\ndata: {json.dumps(error_event)}\n\n"
                except Exception as e:
                    logger.exception(f"Responses API Request {request_id}: Error")
                    error_event = {
                        "type": "error",
                        "error": error_handler.to_openai_error(
                            error_handler.create_server_error()
                        )["error"]
                    }
                    yield f"event: error\ndata: {json.dumps(error_event)}\n\n"
            
            return StreamingResponse(
                generate_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Request-ID": request_id
                }
            )
        else:
            # 同步响应
            backend_response = await backend_client.run_with_account(
                account=account,
                payload=backend_payload,
                account_pool=account_pool
            )
            
            # 提取内容
            content = response_transformer._extract_content(backend_response)
            
            # 构建 Responses API 格式的响应
            response_data = {
                "id": f"resp_{request_id}",
                "object": "response",
                "created_at": int(time.time()),
                "status": "completed",
                "model": request.model,
                "output": [{
                    "type": "message",
                    "id": f"msg_{request_id}",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": content}]
                }],
                "usage": {
                    "input_tokens": len(str(request.input)) // 4,
                    "output_tokens": len(content) // 4,
                    "total_tokens": (len(str(request.input)) + len(content)) // 4
                }
            }
            
            return JSONResponse(
                content=response_data,
                headers={"X-Request-ID": request_id}
            )
            
    except BackendClientError as e:
        logger.error(f"Responses API Request {request_id}: Backend error - {e.message}")
        error = error_handler.from_backend_exception(e)
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_openai_error(error)
        )
    except Exception as e:
        logger.exception(f"Responses API Request {request_id}: Unexpected error")
        error = error_handler.create_server_error()
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_openai_error(error)
        )


# ============================================================================
# Stream Stats Update Helper
# ============================================================================

async def update_stream_stats(context: dict):
    """
    流式响应结束后更新统计
    
    优先从 Backend Analytics 获取真实的 token 数据，
    如果无法获取则使用 tiktoken 估算。
    
    Args:
        context: 包含请求上下文信息的字典
    """
    from app.models.database import get_session_factory
    from app.services.token_counter import get_token_counter
    from app.services.analytics import get_analytics_service
    from app.services.call_logger import get_call_logger_service
    
    try:
        elapsed_ms = int((time.time() - context["start_time"]) * 1000)
        input_tokens = 0
        output_tokens = 0
        total_tokens = 0
        
        # 方法 1：从 Stack AI Analytics 获取真实 token（使用调用前后差值）
        # pre_request_tokens 是调用前从 Stack AI 获取的 today_tokens
        # 现在请求完成后再次获取 today_tokens，差值就是本次请求的 token
        private_api_key = context.get("private_api_key")
        analytics_success = False
        
        if private_api_key:
            try:
                from app.services.analytics import get_analytics_service
                analytics_service = get_analytics_service()
                
                # 等待一小段时间让 Stack AI 更新统计
                import asyncio
                await asyncio.sleep(0.5)
                
                # 获取请求后的 today_tokens
                stats = await analytics_service.get_recent_stats(
                    org_id=context["account_org_id"],
                    flow_id=context["account_flow_id"],
                    private_api_key=private_api_key,
                    days=1
                )
                
                if stats and stats.today_tokens > 0:
                    # pre_request_tokens 是真正的调用前 today_tokens
                    pre_tokens = context.get("pre_request_tokens", 0)
                    current_tokens = stats.today_tokens
                    
                    # 只有当 current > pre 时才使用差值
                    if current_tokens > pre_tokens:
                        total_tokens = current_tokens - pre_tokens
                        # 假设输入:输出比例约 1:2
                        input_tokens = total_tokens // 3
                        output_tokens = total_tokens - input_tokens
                        analytics_success = True
                        print(f"[DEBUG] Analytics: pre={pre_tokens}, current={current_tokens}, delta={total_tokens}")
            except Exception as e:
                print(f"[DEBUG] Analytics failed: {e}")
        
        # 获取累积的输出内容（用于 output_preview 和 tiktoken 估算）
        accumulated_content = "".join(context.get("accumulated_content", []))
        
        # 方法 2：如果 Analytics 失败，使用 tiktoken 估算
        if not analytics_success:
            
            token_counter = get_token_counter()
            
            # 计算输入 token（只用最后一条用户消息）
            last_user_msg = context.get("last_user_msg", "")
            if last_user_msg:
                input_tokens = token_counter.count(last_user_msg)
            
            # 计算输出 token
            if accumulated_content:
                output_tokens = token_counter.count(accumulated_content)
            
            total_tokens = input_tokens + output_tokens
            print(f"[DEBUG] Tiktoken fallback: input={input_tokens}, output={output_tokens}, total={total_tokens}")
        
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
                api_type="openai",
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
            f"Stream stats updated for request {context['request_id']}: "
            f"tokens={total_tokens} (in={input_tokens}, out={output_tokens}), elapsed={elapsed_ms}ms"
        )
    except Exception as e:
        logger.error(f"Failed to update stream stats: {e}")
