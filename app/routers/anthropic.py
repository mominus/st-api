"""
Anthropic Compatible Router
实现 Anthropic API 兼容的路由端点

Requirements: 1.2
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
    AnthropicRequest, AnthropicMessage, RequestTransformer, get_transformer
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
from sqlalchemy import select

logger = logging.getLogger(__name__)

# 创建路由器
router = APIRouter(prefix="/v1", tags=["Anthropic Compatible"])


# ============================================================================
# Request/Response Models
# ============================================================================

class MessagesRequest(BaseModel):
    """Anthropic Messages 请求模型"""
    model: str = Field(..., description="模型名称")
    messages: List[AnthropicMessage] = Field(..., description="消息列表")
    max_tokens: Optional[int] = Field(default=4096, description="最大 token 数")
    stream: bool = Field(default=False, description="是否流式响应")
    system: Optional[Any] = Field(default=None, description="系统提示词，可以是字符串或内容块数组")
    temperature: Optional[float] = Field(default=None, description="温度参数")
    top_p: Optional[float] = Field(default=None, description="Top-p 采样")
    top_k: Optional[int] = Field(default=None, description="Top-k 采样")
    stop_sequences: Optional[List[str]] = Field(default=None, description="停止序列")
    metadata: Optional[Dict[str, Any]] = Field(default=None, description="元数据")
    
    class Config:
        extra = "allow"  # 允许额外字段
    
    def get_system_text(self) -> Optional[str]:
        """提取系统提示词文本"""
        if self.system is None:
            return None
        if isinstance(self.system, str):
            return self.system
        if isinstance(self.system, list):
            # 处理内容块数组格式 [{"type": "text", "text": "..."}]
            texts = []
            for block in self.system:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        texts.append(block.get("text", ""))
                    elif "text" in block:
                        texts.append(block.get("text", ""))
                elif isinstance(block, str):
                    texts.append(block)
            return "\n".join(texts) if texts else None
        return str(self.system)


# ============================================================================
# Helper Functions
# ============================================================================

def extract_api_key(x_api_key: Optional[str], authorization: Optional[str]) -> Optional[str]:
    """
    从请求头提取 API Key
    
    Anthropic 支持两种方式:
    - x-api-key: sk-xxx
    - Authorization: Bearer sk-xxx
    """
    # 优先使用 x-api-key
    if x_api_key:
        return x_api_key.strip()
    
    # 其次使用 Authorization header
    if authorization:
        auth = authorization.strip()
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return auth
    
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
            content=error_handler.to_anthropic_error(error)
        )
    
    return api_key_obj, None


# ============================================================================
# Debug Endpoint - 用于调试请求
# ============================================================================

@router.post("/messages/debug")
async def debug_message(
    http_request: Request
):
    """调试端点：返回收到的原始请求"""
    body = await http_request.body()
    try:
        json_body = json.loads(body)
    except:
        json_body = {"raw": body.decode("utf-8", errors="replace")}
    
    headers = dict(http_request.headers)
    return {
        "headers": headers,
        "body": json_body
    }


# ============================================================================
# Messages Endpoint (Requirement 1.2)
# ============================================================================

@router.post("/messages")
async def create_message(
    http_request: Request,
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    authorization: Optional[str] = Header(None),
    anthropic_version: Optional[str] = Header(None, alias="anthropic-version"),
    session: AsyncSession = Depends(get_session)
):
    """
    Anthropic 兼容的 Messages 端点
    
    - 支持同步和流式响应
    - 验证 API Key 和模型权限
    - 将请求转换为后端格式并执行
    - 将响应转换为 Anthropic 格式返回
    """
    request_id = uuid.uuid4().hex[:24]
    start_time = time.time()
    
    # 手动解析请求体以获得更好的错误信息
    try:
        body = await http_request.body()
        body_json = json.loads(body)
        logger.info(f"Request {request_id}: Anthropic raw body: {json.dumps(body_json, ensure_ascii=False)[:500]}")
        request = MessagesRequest(**body_json)
    except json.JSONDecodeError as e:
        logger.error(f"Request {request_id}: JSON decode error: {e}")
        return JSONResponse(
            status_code=400,
            content={"error": {"type": "invalid_request_error", "message": f"Invalid JSON: {e}"}}
        )
    except Exception as e:
        logger.error(f"Request {request_id}: Request validation error: {e}")
        return JSONResponse(
            status_code=422,
            content={"error": {"type": "invalid_request_error", "message": f"Validation error: {e}"}}
        )
    
    # 获取服务实例
    api_key_service = get_api_key_service()
    account_pool = get_account_pool_service()
    backend_client = get_backend_client()
    transformer = get_transformer()
    response_transformer = get_response_transformer()
    error_handler = get_error_handler()
    
    # 1. 提取并验证 API Key
    raw_key = extract_api_key(x_api_key, authorization)
    if not raw_key:
        error = error_handler.create_authentication_error(
            "Missing API key. Please include 'x-api-key' header or 'Authorization: Bearer YOUR_API_KEY' header."
        )
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_anthropic_error(error)
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
            content=error_handler.to_anthropic_error(error)
        )
    
    # 4. 转换请求格式
    # 使用 get_system_text() 方法提取系统提示词文本
    system_text = request.get_system_text()
    
    anthropic_request = AnthropicRequest(
        model=request.model,
        messages=request.messages,
        max_tokens=request.max_tokens,
        stream=request.stream,
        system=system_text
    )
    unified_request = transformer.parse_anthropic_request(anthropic_request)
    
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
    # 从 metadata 中提取 user_id，如果没有则使用 anonymous
    user_id = "anonymous"
    if request.metadata and "user_id" in request.metadata:
        user_id = str(request.metadata["user_id"])
    
    backend_payload = transformer.to_stackai_dict(
        unified_request,
        input_mapping=input_mapping,
        user_id=user_id
    )
    
    logger.info(f"Request {request_id}: model={request.model}, stream={request.stream}")
    
    try:
        # 7. 执行请求（同步或流式）
        if request.stream:
            # 流式响应 - 累积内容并在结束后更新统计
            logger_service = get_logger_service()
            client_ip = http_request.client.host if http_request.client else None
            
            # 获取账号的 Private API Key（用于从 Backend Analytics 获取真实 token）
            private_api_key = account_pool.decrypt_private_api_key(account)
            
            # 保存上下文信息用于流结束后更新统计
            stream_context = {
                "request_id": request_id,
                "api_key_id": api_key_obj.id if api_key_obj else None,
                "api_key_prefix": api_key_obj.key_prefix if api_key_obj else None,
                "client_ip": client_ip,
                "model": request.model,
                "account_id": account.id,
                "account_org_id": account.org_id,
                "account_flow_id": account.flow_id,
                "private_api_key": private_api_key,
                "start_time": start_time,
                "accumulated_content": [],
                "pre_request_tokens": account.daily_used
            }
            
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
                    async for chunk in response_transformer.transform_backend_sse_to_anthropic(
                        stream_gen, request.model, request_id
                    ):
                        # 尝试从 chunk 中提取内容用于 token 估算
                        if "content_block_delta" in chunk:
                            try:
                                # Anthropic 格式: event: content_block_delta\ndata: {...}
                                for line in chunk.split("\n"):
                                    if line.startswith("data: "):
                                        chunk_data = json.loads(line[6:])
                                        text = chunk_data.get("delta", {}).get("text", "")
                                        if text:
                                            stream_context["accumulated_content"].append(text)
                            except (json.JSONDecodeError, KeyError):
                                pass
                        yield chunk
                    
                    # 流结束后，异步更新统计
                    import asyncio
                    asyncio.create_task(update_anthropic_stream_stats(stream_context))
                        
                except BackendClientError as e:
                    logger.error(f"Request {request_id}: Backend error - {e.message}")
                    # 在流式响应中发送错误事件
                    error_event = response_transformer.to_anthropic_stream_event("error", {
                        "type": "error",
                        "error": {
                            "type": "api_error",
                            "message": e.message
                        }
                    })
                    yield error_event
                except Exception as e:
                    logger.exception(f"Request {request_id}: Unexpected error")
                    error_event = response_transformer.to_anthropic_stream_event("error", {
                        "type": "error",
                        "error": {
                            "type": "api_error",
                            "message": str(e)
                        }
                    })
                    yield error_event
            
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
            anthropic_response = response_transformer.to_anthropic_response(
                backend_response, request.model, request_id
            )
            
            # 更新 token 使用量
            usage = anthropic_response.get("usage", {})
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            total_tokens = input_tokens + output_tokens
            
            if total_tokens > 0:
                await account_pool.update_token_usage(
                    session, account.id,
                    input_tokens,
                    output_tokens
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
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                response_time_ms=elapsed_ms
            )
            
            # 更新 API Key 累计统计
            if api_key_obj:
                await api_key_service.update_key_stats(
                    session, api_key_obj.id, input_tokens, output_tokens
                )
            
            # 更新系统累计统计
            stats_service = get_stats_service()
            await stats_service.update_system_stats(session, input_tokens, output_tokens)
            
            await session.commit()
            logger.info(f"Request {request_id}: completed in {elapsed_ms}ms")
            
            return JSONResponse(
                content=anthropic_response,
                headers={"X-Request-ID": request_id}
            )
            
    except BackendAPIError as e:
        logger.error(f"Request {request_id}: Backend API error - {e.message}")
        error = error_handler.parse_backend_error(
            e.response_data or {}, e.status_code or 502
        )
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_anthropic_error(error)
        )
    except BackendConnectionError as e:
        logger.error(f"Request {request_id}: Connection error - {e.message}")
        error = error_handler.create_backend_error(
            "Failed to connect to Backend backend"
        )
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_anthropic_error(error)
        )
    except BackendTimeoutError as e:
        logger.error(f"Request {request_id}: Timeout - {e.message}")
        error = error_handler.create_backend_error(
            "Request to Backend backend timed out"
        )
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_anthropic_error(error)
        )
    except Exception as e:
        logger.exception(f"Request {request_id}: Unexpected error")
        error = error_handler.create_server_error(str(e))
        return JSONResponse(
            status_code=error.status_code,
            content=error_handler.to_anthropic_error(error)
        )


# ============================================================================
# Stream Stats Update Helper
# ============================================================================

async def update_anthropic_stream_stats(context: dict):
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
    
    try:
        elapsed_ms = int((time.time() - context["start_time"]) * 1000)
        input_tokens = 0
        output_tokens = 0
        total_tokens = 0
        
        # 尝试从 Backend Analytics 获取真实的 token 数据
        private_api_key = context.get("private_api_key")
        if private_api_key:
            try:
                analytics_service = get_analytics_service()
                import asyncio
                await asyncio.sleep(1)
                
                stats = await analytics_service.get_recent_stats(
                    org_id=context["account_org_id"],
                    flow_id=context["account_flow_id"],
                    private_api_key=private_api_key,
                    days=1
                )
                
                if stats:
                    pre_tokens = context.get("pre_request_tokens", 0)
                    current_tokens = stats.today_tokens
                    total_tokens = max(0, current_tokens - pre_tokens)
                    input_tokens = total_tokens // 3
                    output_tokens = total_tokens - input_tokens
                    logger.debug(f"Got real token data from Backend: delta={total_tokens}")
            except Exception as e:
                logger.warning(f"Failed to get token data from Backend Analytics: {e}")
        
        # 如果无法从 StackAI 获取，使用 tiktoken 估算
        if total_tokens == 0:
            accumulated_content = "".join(context.get("accumulated_content", []))
            if accumulated_content:
                token_counter = get_token_counter()
                output_tokens = token_counter.count(accumulated_content)
                total_tokens = output_tokens
        
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
            
            # 更新 API Key 累计统计
            if context["api_key_id"]:
                api_key_service = get_api_key_service()
                await api_key_service.update_key_stats(
                    session, context["api_key_id"], input_tokens, output_tokens
                )
            
            # 更新系统累计统计
            stats_service = get_stats_service()
            await stats_service.update_system_stats(session, input_tokens, output_tokens)
            
            await session.commit()
            
        logger.debug(
            f"Anthropic stream stats updated for request {context['request_id']}: "
            f"tokens={total_tokens}, elapsed={elapsed_ms}ms"
        )
    except Exception as e:
        logger.error(f"Failed to update Anthropic stream stats: {e}")
