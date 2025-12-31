"""
Request Transformer Service
将不同 API 格式的请求转换为统一的内部格式，再映射到后端输入
"""

import json
import uuid
from typing import Optional, List, Dict, Any, Literal, Union
from dataclasses import dataclass, field
from pydantic import BaseModel


# ============================================================================
# Pydantic Models for API Request Parsing
# ============================================================================

class OpenAIMessage(BaseModel):
    """OpenAI 消息格式"""
    role: Literal["system", "user", "assistant"]
    content: str


class OpenAIChatRequest(BaseModel):
    """OpenAI /v1/chat/completions 请求格式"""
    model: str
    messages: List[OpenAIMessage]
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


class AnthropicContentBlock(BaseModel):
    """Anthropic 内容块"""
    type: str = "text"
    text: Optional[str] = None
    
    class Config:
        extra = "allow"  # 允许额外字段


class AnthropicMessage(BaseModel):
    """Anthropic 消息格式"""
    role: Literal["user", "assistant"]
    content: Union[str, List[Any]]  # 可以是字符串或内容块数组
    
    class Config:
        extra = "allow"  # 允许额外字段
    
    def get_text_content(self) -> str:
        """提取文本内容"""
        if isinstance(self.content, str):
            return self.content
        elif isinstance(self.content, list):
            # 处理内容块数组格式
            texts = []
            for block in self.content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        texts.append(block.get("text", ""))
                    elif "text" in block:
                        texts.append(block.get("text", ""))
                elif isinstance(block, str):
                    texts.append(block)
                elif hasattr(block, "text"):
                    texts.append(block.text or "")
            return "\n".join(texts) if texts else ""
        return str(self.content) if self.content else ""


class AnthropicRequest(BaseModel):
    """Anthropic /v1/messages 请求格式"""
    model: str
    messages: List[AnthropicMessage]
    max_tokens: Optional[int] = 4096
    stream: bool = False
    system: Optional[str] = None


class GeminiPart(BaseModel):
    """Gemini 内容部分"""
    text: str


class GeminiContent(BaseModel):
    """Gemini 内容格式"""
    role: Optional[str] = None
    parts: List[GeminiPart]


class GeminiGenerationConfig(BaseModel):
    """Gemini 生成配置"""
    temperature: Optional[float] = None
    maxOutputTokens: Optional[int] = None
    topP: Optional[float] = None
    topK: Optional[int] = None


class GeminiRequest(BaseModel):
    """Gemini /v1beta/models/{model}:generateContent 请求格式"""
    contents: List[GeminiContent]
    generationConfig: Optional[GeminiGenerationConfig] = None
    # model is extracted from URL path, not body


# ============================================================================
# Unified Internal Request Format
# ============================================================================

@dataclass
class ChatMessage:
    """统一的聊天消息格式"""
    role: str  # "system", "user", "assistant"
    content: str


@dataclass
class UnifiedRequest:
    """
    统一的内部请求格式
    
    所有 API 格式（OpenAI、Anthropic、Gemini）都会被转换为此格式，
    然后再转换为后端的输入格式。
    """
    model: str
    system_prompt: Optional[str] = None
    user_input: str = ""
    chat_history: List[ChatMessage] = field(default_factory=list)
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    
    # 原始请求格式，用于响应转换
    source_format: str = "openai"  # "openai", "anthropic", "gemini"


@dataclass
class BackendPayload:
    """
    后端请求格式
    
    后端工作流的输入格式，字段名称根据模型组配置动态映射。
    """
    user_id: str = "anonymous"
    input_fields: Dict[str, Any] = field(default_factory=dict)


# 兼容旧名称
StackAIPayload = BackendPayload


# ============================================================================
# Request Transformer Service
# ============================================================================

class RequestTransformer:
    """
    请求转换器
    
    负责将不同 API 格式的请求转换为统一的内部格式，
    再根据模型组配置映射到后端输入格式。
    """
    
    # ========================================================================
    # OpenAI Format Parsing (Requirement 1.1)
    # ========================================================================
    
    def parse_openai_request(self, request: OpenAIChatRequest) -> UnifiedRequest:
        """
        解析 OpenAI 格式请求
        
        Args:
            request: OpenAI 格式的请求对象
            
        Returns:
            统一格式的请求对象
        """
        system_prompt: Optional[str] = None
        user_input: str = ""
        chat_history: List[ChatMessage] = []
        
        # 遍历消息，提取 system/user/assistant 消息
        for msg in request.messages:
            if msg.role == "system":
                # 系统提示词（可能有多个，合并）
                if system_prompt is None:
                    system_prompt = msg.content
                else:
                    system_prompt = f"{system_prompt}\n{msg.content}"
            elif msg.role == "user":
                # 用户消息 - 最后一条作为当前输入，之前的加入历史
                if user_input:
                    chat_history.append(ChatMessage(role="user", content=user_input))
                user_input = msg.content
            elif msg.role == "assistant":
                # 助手消息 - 加入历史
                chat_history.append(ChatMessage(role="assistant", content=msg.content))
        
        return UnifiedRequest(
            model=request.model,
            system_prompt=system_prompt,
            user_input=user_input,
            chat_history=chat_history,
            stream=request.stream,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            source_format="openai"
        )
    
    def parse_openai_dict(self, data: Dict[str, Any]) -> UnifiedRequest:
        """
        从字典解析 OpenAI 格式请求
        
        Args:
            data: 请求数据字典
            
        Returns:
            统一格式的请求对象
        """
        request = OpenAIChatRequest(**data)
        return self.parse_openai_request(request)

    
    # ========================================================================
    # Anthropic Format Parsing (Requirement 1.2)
    # ========================================================================
    
    def parse_anthropic_request(self, request: AnthropicRequest) -> UnifiedRequest:
        """
        解析 Anthropic 格式请求
        
        Args:
            request: Anthropic 格式的请求对象
            
        Returns:
            统一格式的请求对象
        """
        user_input: str = ""
        chat_history: List[ChatMessage] = []
        
        # Anthropic 的 system 是顶级字段
        system_prompt = request.system
        
        # 遍历消息，提取 user/assistant 消息
        for msg in request.messages:
            # 使用 get_text_content 方法提取文本内容
            content = msg.get_text_content()
            if msg.role == "user":
                # 用户消息 - 最后一条作为当前输入，之前的加入历史
                if user_input:
                    chat_history.append(ChatMessage(role="user", content=user_input))
                user_input = content
            elif msg.role == "assistant":
                # 助手消息 - 加入历史
                chat_history.append(ChatMessage(role="assistant", content=content))
        
        return UnifiedRequest(
            model=request.model,
            system_prompt=system_prompt,
            user_input=user_input,
            chat_history=chat_history,
            stream=request.stream,
            temperature=None,  # Anthropic 在其他地方处理
            max_tokens=request.max_tokens,
            source_format="anthropic"
        )
    
    def parse_anthropic_dict(self, data: Dict[str, Any]) -> UnifiedRequest:
        """
        从字典解析 Anthropic 格式请求
        
        Args:
            data: 请求数据字典
            
        Returns:
            统一格式的请求对象
        """
        request = AnthropicRequest(**data)
        return self.parse_anthropic_request(request)
    
    # ========================================================================
    # Gemini Format Parsing (Requirement 1.3)
    # ========================================================================
    
    def parse_gemini_request(
        self, 
        request: GeminiRequest, 
        model: str,
        stream: bool = False
    ) -> UnifiedRequest:
        """
        解析 Gemini 格式请求
        
        Args:
            request: Gemini 格式的请求对象
            model: 模型名称（从 URL 路径提取）
            stream: 是否流式响应
            
        Returns:
            统一格式的请求对象
        """
        system_prompt: Optional[str] = None
        user_input: str = ""
        chat_history: List[ChatMessage] = []
        
        # 遍历内容，提取消息
        for content in request.contents:
            # 合并所有 parts 的文本
            text = " ".join(part.text for part in content.parts)
            
            # Gemini 的 role 可能是 "user", "model", 或 None
            role = content.role or "user"
            
            if role == "user":
                # 用户消息 - 最后一条作为当前输入，之前的加入历史
                if user_input:
                    chat_history.append(ChatMessage(role="user", content=user_input))
                user_input = text
            elif role == "model":
                # 模型消息（相当于 assistant）- 加入历史
                chat_history.append(ChatMessage(role="assistant", content=text))
        
        # 提取生成配置
        temperature = None
        max_tokens = None
        if request.generationConfig:
            temperature = request.generationConfig.temperature
            max_tokens = request.generationConfig.maxOutputTokens
        
        return UnifiedRequest(
            model=model,
            system_prompt=system_prompt,
            user_input=user_input,
            chat_history=chat_history,
            stream=stream,
            temperature=temperature,
            max_tokens=max_tokens,
            source_format="gemini"
        )
    
    def parse_gemini_dict(
        self, 
        data: Dict[str, Any], 
        model: str,
        stream: bool = False
    ) -> UnifiedRequest:
        """
        从字典解析 Gemini 格式请求
        
        Args:
            data: 请求数据字典
            model: 模型名称（从 URL 路径提取）
            stream: 是否流式响应
            
        Returns:
            统一格式的请求对象
        """
        request = GeminiRequest(**data)
        return self.parse_gemini_request(request, model, stream)
    
    # ========================================================================
    # Unified to Backend Format Conversion
    # ========================================================================
    
    def to_stackai_payload(
        self, 
        unified: UnifiedRequest,
        input_mapping: Optional[Dict[str, str]] = None,
        user_id: str = "anonymous",
        merge_context: bool = True
    ) -> BackendPayload:
        """
        将统一格式转换为后端输入格式
        
        Args:
            unified: 统一格式的请求对象
            input_mapping: 输入字段映射配置，如 {"user_input": "in-0", "system_prompt": "in-1"}
            user_id: 用户 ID
            merge_context: 是否将完整上下文合并到单一输入字段
                          True: 将系统提示、历史对话、当前输入合并为一个字符串
                          False: 分开发送到不同字段
            
        Returns:
            后端格式的请求对象
        """
        # 默认映射配置
        if input_mapping is None:
            input_mapping = {
                "user_input": "in-0",
                "system_prompt": "in-1",
                "chat_history": "in-2"
            }
        
        input_fields: Dict[str, Any] = {}
        
        if merge_context:
            # 合并模式：将完整上下文发送到 user_input 字段
            # 这样 Stack AI 工作流只需要一个输入节点即可获得完整对话历史
            full_context = self._format_full_context(unified)
            if "user_input" in input_mapping:
                input_fields[input_mapping["user_input"]] = full_context
        else:
            # 分离模式：分别发送到不同字段
            # 映射用户输入
            if "user_input" in input_mapping:
                input_fields[input_mapping["user_input"]] = unified.user_input
            
            # 映射系统提示词
            if "system_prompt" in input_mapping and unified.system_prompt:
                input_fields[input_mapping["system_prompt"]] = unified.system_prompt
            
            # 映射聊天历史
            if "chat_history" in input_mapping and unified.chat_history:
                # 将聊天历史转换为字符串格式
                history_str = self._format_chat_history(unified.chat_history)
                input_fields[input_mapping["chat_history"]] = history_str
        
        return BackendPayload(
            user_id=user_id,
            input_fields=input_fields
        )
    
    def _format_chat_history(self, history: List[ChatMessage]) -> str:
        """
        格式化聊天历史为字符串
        
        使用 XML 风格标签避免 Claude 误解为对话模板
        
        Args:
            history: 聊天历史消息列表
            
        Returns:
            格式化的字符串
        """
        formatted_messages = []
        for msg in history:
            if msg.role == "user":
                formatted_messages.append(f"<human_message>\n{msg.content}\n</human_message>")
            else:
                formatted_messages.append(f"<assistant_message>\n{msg.content}\n</assistant_message>")
        return "\n\n".join(formatted_messages)
    
    def _format_full_context(self, unified: UnifiedRequest) -> str:
        """
        将完整对话上下文格式化为单一字符串
        
        使用 XML 风格标签避免 Claude 误解为对话模板/角色扮演场景。
        这样可以防止 Claude 在多轮对话后自说自话生成 "User:" 内容。
        
        Args:
            unified: 统一格式的请求对象
            
        Returns:
            格式化的完整上下文字符串
        """
        # 使用 get_all_messages 获取正确顺序的所有消息
        all_messages = self.get_all_messages(unified)
        
        parts = []
        for msg in all_messages:
            if msg.role == "system":
                parts.append(f"<system_instruction>\n{msg.content}\n</system_instruction>")
            elif msg.role == "user":
                parts.append(f"<human_message>\n{msg.content}\n</human_message>")
            elif msg.role == "assistant":
                parts.append(f"<assistant_message>\n{msg.content}\n</assistant_message>")
        
        return "\n\n".join(parts)
    
    def format_messages_to_context(self, messages: List) -> str:
        """
        将 OpenAI 格式的 messages 数组直接格式化为上下文字符串
        
        使用 XML 风格标签避免 Claude 误解为对话模板。
        保持原始消息顺序，不进行任何重排。
        
        Args:
            messages: OpenAI 格式的消息列表
            
        Returns:
            格式化的上下文字符串
        """
        parts = []
        for msg in messages:
            # 支持 OpenAIMessage 对象和字典
            if hasattr(msg, 'role'):
                role = msg.role
                content = msg.content
            else:
                role = msg.get('role', 'user')
                content = msg.get('content', '')
            
            if role == "system":
                parts.append(f"<system_instruction>\n{content}\n</system_instruction>")
            elif role == "user":
                parts.append(f"<human_message>\n{content}\n</human_message>")
            elif role == "assistant":
                parts.append(f"<assistant_message>\n{content}\n</assistant_message>")
        
        return "\n\n".join(parts)
    
    def to_stackai_dict(
        self, 
        unified: UnifiedRequest,
        input_mapping: Optional[Dict[str, str]] = None,
        user_id: str = "anonymous",
        stateless: bool = True
    ) -> Dict[str, Any]:
        """
        将统一格式转换为后端请求字典
        
        Args:
            unified: 统一格式的请求对象
            input_mapping: 输入字段映射配置
            user_id: 用户 ID
            stateless: 是否无状态模式（每次生成新的 conversation_id）
            
        Returns:
            后端请求字典，可直接用于 HTTP 请求
        """
        payload = self.to_stackai_payload(unified, input_mapping, user_id)
        
        result = {
            "user_id": payload.user_id,
            **payload.input_fields
        }
        
        # 无状态模式：每次请求使用新的 conversation_id
        # 这样 Stack AI 就不会累积历史，每次都是全新对话
        if stateless:
            result["conversation_id"] = str(uuid.uuid4())
        
        return result
    
    # ========================================================================
    # Utility Methods
    # ========================================================================
    
    def extract_user_content(self, unified: UnifiedRequest) -> str:
        """
        提取用户输入内容
        
        Args:
            unified: 统一格式的请求对象
            
        Returns:
            用户输入内容
        """
        return unified.user_input
    
    def get_all_messages(self, unified: UnifiedRequest) -> List[ChatMessage]:
        """
        获取所有消息（包括系统提示、历史和当前输入）
        
        Args:
            unified: 统一格式的请求对象
            
        Returns:
            所有消息列表
        """
        messages: List[ChatMessage] = []
        
        if unified.system_prompt:
            messages.append(ChatMessage(role="system", content=unified.system_prompt))
        
        messages.extend(unified.chat_history)
        
        if unified.user_input:
            messages.append(ChatMessage(role="user", content=unified.user_input))
        
        return messages
    
    def should_stream(self, unified: UnifiedRequest) -> bool:
        """
        判断是否应该使用流式响应
        
        Args:
            unified: 统一格式的请求对象
            
        Returns:
            是否使用流式响应
        """
        return unified.stream


# 全局转换器实例
_transformer: Optional[RequestTransformer] = None


def get_transformer() -> RequestTransformer:
    """获取全局转换器实例"""
    global _transformer
    if _transformer is None:
        _transformer = RequestTransformer()
    return _transformer
