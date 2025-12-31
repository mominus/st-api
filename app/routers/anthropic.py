"""
Anthropic Compatible Router
实现 Anthropic API 兼容的路由端点

Requirements: 1.2, 2.1, 5.1, 6.1, 7.1, 7.2, 7.3, 7.4
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

# Tool Use 相关导入
from app.services.tool_config import get_tool_config_service, is_tool_use_enabled
from app.services.tool_registry import get_tool_registry
from app.services.tool_injector import ToolInjector
from app.services.tool_parser import ToolParser
from app.services.tool_context_builder import ToolContextBuilder
from app.services.tool_result_formatter import ToolResultFormatter

logger = logging.getLogger(__name__)

# 创建路由器
router = APIRouter(prefix="/v1", tags=["Anthropic Compatible"])


# ============================================================================
# Request/Response Models
# ============================================================================

class ThinkingConfig(BaseModel):
    """思维链配置"""
    model_config = {"extra": "allow"}
    
    type: str = Field(default="enabled", description="思维链类型: enabled/disabled")
    budget_tokens: Optional[int] = Field(default=10000, description="思维链 token 预算")


class MessagesRequest(BaseModel):
    """Anthropic Messages 请求模型"""
    model_config = {"extra": "allow"}  # 允许额外字段
    
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
    thinking: Optional[ThinkingConfig] = Field(default=None, description="思维链配置")
    tools: Optional[List[Dict[str, Any]]] = Field(default=None, description="工具定义列表")
    tool_choice: Optional[Dict[str, Any]] = Field(default=None, description="工具选择配置")
    
    def is_thinking_enabled(self) -> bool:
        """
        检查是否启用思维链
        
        触发条件（满足任一即可）：
        1. 请求中明确设置 thinking.type = "enabled"
        2. 模型名包含 thinking/think/reason 等关键词
        3. 默认启用（可通过环境变量 DEFAULT_THINKING_ENABLED 控制）
        """
        import os
        
        # 条件1：请求中明确启用
        if self.thinking is not None and self.thinking.type == "enabled":
            return True
        
        # 条件2：模型名包含思维链关键词（自动启用）
        model_lower = self.model.lower()
        thinking_keywords = ['thinking', 'think', 'reason', 'reasoning', 'cot', 'deep']
        for keyword in thinking_keywords:
            if keyword in model_lower:
                return True
        
        # 条件3：检查环境变量是否默认启用
        default_enabled = os.getenv("DEFAULT_THINKING_ENABLED", "false").lower() == "true"
        if default_enabled:
            return True
        
        return False
    
    def get_thinking_budget(self) -> int:
        """获取思维链 token 预算"""
        if self.thinking is None:
            return 0
        return self.thinking.budget_tokens or 10000
    
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
    
    def has_tools(self) -> bool:
        """检查请求是否包含工具定义"""
        return self.tools is not None and len(self.tools) > 0
    
    def has_tool_results(self) -> bool:
        """检查消息中是否包含 tool_result 内容块"""
        for msg in self.messages:
            content = msg.content
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        return True
        return False
    
    def has_tool_use_in_messages(self) -> bool:
        """检查消息中是否包含 tool_use 内容块（助手之前的工具调用）"""
        for msg in self.messages:
            content = msg.content
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        return True
        return False


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


def should_inject_tools(request: "MessagesRequest", system_text: Optional[str]) -> bool:
    """
    判断是否需要注入工具定义
    
    返回 False 的情况：
    - 请求的 system prompt 已包含工具定义（避免重复注入）
    
    返回 True 的情况：
    - system prompt 不包含工具定义
    
    注意：即使请求包含 tools 字段，由于 StackAI 后端不支持原生 tools 参数，
    我们仍然需要将工具定义注入到上下文中，让模型知道有哪些工具可用。
    
    Args:
        request: 请求对象
        system_text: 系统提示词文本
        
    Returns:
        是否需要注入工具定义
    """
    # 检查 system_text 是否已经包含工具定义
    if system_text:
        tool_indicators = [
            "# Available Tools",
            "## Available Tools", 
            "To use a tool, output a JSON code block",
            '{"tool":',
            "```json\n{\"tool\":",
            "# Tools",
            "## Tools",
        ]
        if any(indicator in system_text for indicator in tool_indicators):
            return False
    
    # system prompt 不包含工具定义，需要注入
    return True


def extract_api_key(x_api_key: Optional[str], authorization: Optional[str]) -> Optional[str]:
    """
    从请求头提取 API Key
    
    Anthropic 支持两种方式:
    - x-api-key: sk-xxx
    - Authorization: Bearer sk-xxx
    """
    # 优先使用 x-api-key
    if x_api_key:
        return sanitize_api_key(x_api_key.strip())
    
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
        
        # 详细日志：打印完整请求
        logger.info(f"Request {request_id}: Anthropic raw body: {json.dumps(body_json, ensure_ascii=False)[:500]}")
        logger.info(f"Request {request_id}: [THINKING DEBUG] model={body_json.get('model')}, thinking={body_json.get('thinking')}, stream={body_json.get('stream')}")
        
        # 打印 system 字段的长度和前 200 字符
        system_field = body_json.get('system')
        if system_field:
            if isinstance(system_field, str):
                logger.info(f"Request {request_id}: [SYSTEM DEBUG] system length={len(system_field)}, first 200 chars: {system_field[:200]}")
            else:
                logger.info(f"Request {request_id}: [SYSTEM DEBUG] system is list with {len(system_field)} blocks")
        else:
            logger.info(f"Request {request_id}: [SYSTEM DEBUG] system is None or empty")
        
        # 打印 tools 字段
        tools_field = body_json.get('tools')
        if tools_field:
            logger.info(f"Request {request_id}: [TOOLS DEBUG] {len(tools_field)} tools: {[t.get('name') for t in tools_field]}")
        else:
            logger.info(f"Request {request_id}: [TOOLS DEBUG] no tools in request")
        
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
    
    # 检查是否启用思维链
    thinking_enabled = request.is_thinking_enabled()
    thinking_budget = request.get_thinking_budget() if thinking_enabled else 0
    
    # 日志：打印思维链检测结果
    logger.info(f"Request {request_id}: [THINKING DEBUG] thinking_enabled={thinking_enabled}, thinking_budget={thinking_budget}, request.thinking={request.thinking}")
    
    # ========================================================================
    # Tool Use 处理 (Requirements 2.1, 5.1, 6.1)
    # ========================================================================
    tool_use_enabled = False
    tool_parser = None
    tool_injector = None
    filtered_tools = []
    
    # 检查是否启用工具使用功能并且请求包含工具
    # 记录工具使用决策的详细日志
    logger.info(f"Request {request_id}: [TOOL USE] is_tool_use_enabled={is_tool_use_enabled()}, has_tools={request.has_tools()}")
    
    if is_tool_use_enabled() and request.has_tools():
        tool_use_enabled = True
        tool_config_service = get_tool_config_service()
        tool_registry = get_tool_registry()
        
        # 记录请求中的工具列表
        tool_names_in_request = [t.get("name", "") for t in request.tools]
        logger.info(f"Request {request_id}: [TOOL USE] Tools in request: {tool_names_in_request}")
        
        # 根据配置过滤工具
        for tool in request.tools:
            tool_name = tool.get("name", "")
            if tool_config_service.is_tool_enabled(tool_name):
                filtered_tools.append(tool)
        
        if filtered_tools:
            # 创建工具注入器和解析器
            tool_injector = ToolInjector(tool_registry)
            tool_parser = ToolParser(tool_registry)
            
            filtered_names = [t.get("name", "") for t in filtered_tools]
            logger.info(f"Request {request_id}: [TOOL USE] Enabled with {len(filtered_tools)} tools: {filtered_names}")
        else:
            tool_use_enabled = False
            logger.info(f"Request {request_id}: [TOOL USE] No enabled tools after filtering")
    elif request.has_tools():
        logger.info(f"Request {request_id}: [TOOL USE] Request has tools but ENABLE_TOOL_USE is false")
    
    # 将 Anthropic 消息格式化为上下文字符串，包括 system prompt
    # Anthropic 的 system 是独立的字段，需要先添加到上下文
    context_parts = []
    
    # 处理系统提示词
    final_system_text = system_text
    
    # 如果启用工具使用，需要将工具定义注入到上下文中
    # 因为 StackAI 后端不支持原生的 tools 参数，模型只能通过文本方式了解可用工具
    if tool_use_enabled and tool_injector and filtered_tools:
        # 检查 system prompt 是否已经包含工具定义
        if should_inject_tools(request, system_text):
            # system prompt 不包含工具定义，需要注入
            final_system_text = tool_injector.inject_tools(system_text, filtered_tools)
            logger.info(f"Request {request_id}: [TOOL USE] Injected tools into system prompt")
        else:
            # system prompt 已经包含工具定义，不需要重复注入
            logger.info(f"Request {request_id}: [TOOL USE] System prompt already contains tool definitions, skipping injection")
    
    # 如果启用思维链，添加思维链指令到系统提示词
    if thinking_enabled:
        thinking_instruction = """在回答问题之前，请先进行深入思考。将你的思考过程放在 <thinking> 标签中，然后再给出最终回答。

格式示例：
<thinking>
这里是你的思考过程...
分析问题的各个方面...
考虑可能的解决方案...
</thinking>

这里是你的最终回答..."""
        if final_system_text:
            context_parts.append(f"[System]\n{thinking_instruction}\n\n{final_system_text}")
        else:
            context_parts.append(f"[System]\n{thinking_instruction}")
    elif final_system_text:
        # 当请求包含 tools 字段时，使用简洁格式保持 system prompt 原样
        # 避免使用可能干扰 Claude 身份认知的 XML 标签
        if request.has_tools():
            context_parts.append(f"[System]\n{final_system_text}")
        else:
            context_parts.append(f"[System]\n{final_system_text}")
    
    # 处理消息历史
    # 如果启用工具使用且消息中包含 tool_use/tool_result，使用 ToolContextBuilder 处理 (Requirements 5.1, 6.1)
    if tool_use_enabled and (request.has_tool_results() or request.has_tool_use_in_messages()):
        tool_context_builder = ToolContextBuilder()
        # 将消息转换为字典格式
        messages_as_dicts = [
            {"role": msg.role, "content": msg.content if isinstance(msg.content, (str, list)) else str(msg.content)}
            for msg in request.messages
        ]
        # 使用 ToolContextBuilder 重建对话上下文
        converted_messages = tool_context_builder.build_context(messages_as_dicts)
        
        for msg in converted_messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user":
                context_parts.append(f"[Human]\n{content}")
            elif role == "assistant":
                context_parts.append(f"[Assistant]\n{content}")
        
        logger.debug(f"Request {request_id}: [TOOL USE] Rebuilt context with tool_use/tool_result")
    else:
        # 添加对话历史（原有逻辑）
        for msg in request.messages:
            role = msg.role
            # Anthropic messages 可以有 content 为列表的情况
            if isinstance(msg.content, list):
                content = ""
                for block in msg.content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        content += block.get("text", "")
                    elif isinstance(block, str):
                        content += block
            else:
                content = msg.content
            
            if role == "user":
                context_parts.append(f"[Human]\n{content}")
            elif role == "assistant":
                context_parts.append(f"[Assistant]\n{content}")
    
    messages_context = "\n\n".join(context_parts)
    
    # 调试日志：打印完整的上下文
    logger.info(f"Request {request_id}: [CONTEXT DEBUG] tool_use_enabled={tool_use_enabled}, has_tools={request.has_tools()}")
    logger.info(f"Request {request_id}: [CONTEXT DEBUG] context length={len(messages_context)}, first 500 chars: {messages_context[:500]}")
    
    # 构建 payload
    backend_payload = {
        "user_id": user_id,
        "in-0": messages_context,  # 完整对话上下文
        "conversation_id": str(uuid.uuid4())  # 每次请求使用新的会话ID
    }
    
    # 如果有自定义输入映射，应用它
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
            
            # 获取账号的 Private API Key（用于从 Backend Analytics 获取真实 token）
            private_api_key = account_pool.decrypt_private_api_key(account)
            
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
                        logger.debug(f"Request {request_id}: Pre-request today_tokens from Analytics: {pre_request_tokens}")
                except Exception as e:
                    logger.debug(f"Request {request_id}: Failed to get pre-request tokens: {e}")
                    pre_request_tokens = 0
            
            # 提取输入预览
            from app.services.call_logger import extract_input_preview
            input_preview = extract_input_preview(
                [m.model_dump() if hasattr(m, 'model_dump') else m for m in request.messages],
                api_type="anthropic"
            )
            
            # 保存上下文信息用于流结束后更新统计
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
                "pre_request_tokens": pre_request_tokens  # 使用从 Stack AI Analytics 获取的真实值
            }
            
            # 流式响应
            async def generate_stream():
                try:
                    stream_gen = await backend_client.execute_with_account(
                        account=account,
                        payload=backend_payload,
                        stream=True,
                        account_pool=account_pool
                    )
                    
                    # 根据功能选择不同的转换方法 (Requirements 7.1, 7.2, 7.3, 7.4)
                    if tool_use_enabled and tool_parser:
                        # 使用带工具使用检测的流式转换
                        transform_gen = response_transformer.transform_backend_sse_to_anthropic_with_tools(
                            stream_gen, request.model, request_id, tool_parser
                        )
                        logger.debug(f"Request {request_id}: [TOOL USE] Using streaming with tool detection")
                    elif thinking_enabled:
                        # 使用带思维链的实时流式转换
                        transform_gen = response_transformer.transform_backend_sse_to_anthropic_with_thinking(
                            stream_gen, request.model, request_id, thinking_budget
                        )
                    else:
                        # 使用普通的流式转换
                        transform_gen = response_transformer.transform_backend_sse_to_anthropic(
                            stream_gen, request.model, request_id
                        )
                    
                    # 直接转换并输出，同时累积内容
                    async for chunk in transform_gen:
                        # 尝试从 chunk 中提取内容用于 token 估算
                        if "content_block_delta" in chunk:
                            try:
                                for line in chunk.split("\n"):
                                    if line.startswith("data: "):
                                        chunk_data = json.loads(line[6:])
                                        delta = chunk_data.get("delta", {})
                                        # 提取 text 或 thinking 或 input_json_delta 内容
                                        text = delta.get("text", "") or delta.get("thinking", "") or delta.get("partial_json", "")
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
            
            # 调试日志：打印后端原始响应
            logger.info(f"Request {request_id}: [BACKEND RESPONSE] {json.dumps(backend_response, ensure_ascii=False)[:1000]}")
            
            # 转换响应格式
            # 如果启用工具使用，使用带工具解析的转换方法
            if tool_use_enabled and tool_parser:
                anthropic_response = response_transformer.to_anthropic_response_with_tools(
                    backend_response, request.model, request_id, tool_parser
                )
                logger.info(f"Request {request_id}: [TOOL USE] Parsed response for tool calls")
                logger.info(f"Request {request_id}: [ANTHROPIC RESPONSE] {json.dumps(anthropic_response, ensure_ascii=False)[:1000]}")
            else:
                # 使用普通转换（传入 thinking_enabled 参数）
                anthropic_response = response_transformer.to_anthropic_response(
                    backend_response, request.model, request_id, thinking_enabled=thinking_enabled
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
            
            # 计算费用
            from app.services.pricing import get_pricing_service
            pricing_service = get_pricing_service()
            _, _, total_cost = pricing_service.calculate(request.model, input_tokens, output_tokens)
            
            # 更新 API Key 累计统计（包含费用）
            if api_key_obj:
                await api_key_service.update_key_stats(
                    session, api_key_obj.id, input_tokens, output_tokens, cost=total_cost
                )
            
            # 更新系统累计统计
            stats_service = get_stats_service()
            await stats_service.update_system_stats(session, input_tokens, output_tokens)
            
            # 记录详细调用日志
            from app.services.call_logger import get_call_logger_service, extract_input_preview
            call_logger = get_call_logger_service()
            
            # 提取输入预览
            input_preview = extract_input_preview(
                [m.model_dump() if hasattr(m, 'model_dump') else m for m in request.messages],
                api_type="anthropic"
            )
            
            # 提取输出预览
            output_preview = ""
            content = anthropic_response.get("content", [])
            if content:
                for block in content:
                    if block.get("type") == "text":
                        output_preview = block.get("text", "")[:500]
                        break
            
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
                api_type="anthropic",
                is_stream=False,
                input_preview=input_preview,
                output_preview=output_preview,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                response_time_ms=elapsed_ms,
                status="success",
            )
            
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
    from app.services.call_logger import get_call_logger_service
    
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
        
        # 累积的输出内容
        accumulated_content = "".join(context.get("accumulated_content", []))
        
        # 如果无法从 StackAI 获取，使用 tiktoken 估算
        if total_tokens == 0:
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
                api_type="anthropic",
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
            f"Anthropic stream stats updated for request {context['request_id']}: "
            f"tokens={total_tokens}, elapsed={elapsed_ms}ms"
        )
    except Exception as e:
        logger.error(f"Failed to update Anthropic stream stats: {e}")
