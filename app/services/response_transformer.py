"""
Response Transformer Service
将后端响应转换为标准 API 格式（OpenAI、Anthropic、Gemini）

支持 Tool Use 功能：
- to_anthropic_response_with_tools(): 将后端响应转换为带 tool_use 支持的 Anthropic 格式
- create_tool_use_block(): 创建 Anthropic tool_use 内容块
"""

import json
import time
import uuid
from typing import Optional, Dict, Any, AsyncGenerator, Literal, List, TYPE_CHECKING
from dataclasses import dataclass

from app.services.upstream_sanitizer import is_stream_completion_marker

if TYPE_CHECKING:
    from .tool_parser import ToolParser, ParsedToolCall


# ============================================================================
# Response Data Classes
# ============================================================================

@dataclass
class TokenUsage:
    """Token 使用统计"""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class BackendResponse:
    """后端响应格式"""
    output: str
    metadata: Optional[Dict[str, Any]] = None


# 兼容旧名称
STResponse = BackendResponse


# ============================================================================
# Response Transformer Service
# ============================================================================

class ResponseTransformer:
    """
    响应转换器
    
    负责将后端响应转换为标准 API 格式（OpenAI、Anthropic、Gemini）。
    支持同步响应和 SSE 流式响应的转换。
    """
    
    # ========================================================================
    # OpenAI Format Conversion (Requirement 1.1)
    # ========================================================================
    
    def to_openai_response(
        self,
        backend_response: Dict[str, Any],
        model: str,
        request_id: Optional[str] = None,
        input_text: str = None
    ) -> Dict[str, Any]:
        """
        将 后端响应转换为 OpenAI 格式
        
        Args:
            backend_response: 后端返回的响应数据
            model: 请求的模型名称
            request_id: 请求 ID（可选）
            input_text: 输入文本（用于准确计算 prompt_tokens）
            
        Returns:
            OpenAI 格式的响应字典
        """
        # 提取输出内容
        content = self._extract_content(backend_response)
        
        # 估算 token 使用量（传入输入文本用于计算 prompt_tokens）
        usage = self._estimate_token_usage(content, input_text=input_text)
        
        return {
            "id": f"chatcmpl-{request_id or uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content
                    },
                    "finish_reason": "stop"
                }
            ],
            "usage": {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens
            }
        }

    
    # ========================================================================
    # OpenAI SSE Stream Conversion (Requirements 1.6, 1.7)
    # ========================================================================
    
    def to_openai_stream_chunk(
        self,
        token: str,
        model: str,
        request_id: Optional[str] = None,
        is_final: bool = False
    ) -> str:
        """
        将单个 token 转换为 OpenAI SSE 数据块
        
        Args:
            token: 要发送的 token 内容
            model: 模型名称
            request_id: 请求 ID
            is_final: 是否为最后一个数据块
            
        Returns:
            SSE 格式的数据字符串
        """
        chunk_id = f"chatcmpl-{request_id or uuid.uuid4().hex[:24]}"
        
        if is_final:
            # 最终数据块，包含 finish_reason
            data = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop"
                    }
                ]
            }
        else:
            # 普通数据块，包含 token 内容
            data = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "content": token
                        },
                        "finish_reason": None
                    }
                ]
            }
        
        return f"data: {json.dumps(data)}\n\n"
    
    def to_openai_stream_done(self) -> str:
        """
        生成 OpenAI SSE 流结束标记
        
        Returns:
            SSE 流结束标记字符串
        """
        return "data: [DONE]\n\n"
    
    async def transform_backend_sse_to_openai(
        self,
        backend_stream: AsyncGenerator[str, None],
        model: str,
        request_id: Optional[str] = None
    ) -> AsyncGenerator[str, None]:
        """
        将 后端 SSE 流转换为 OpenAI SSE 格式
        
        Args:
            backend_stream: 后端返回的 SSE 流
            model: 模型名称
            request_id: 请求 ID
            
        Yields:
            OpenAI 格式的 SSE 数据块
        """
        chunk_id = request_id or uuid.uuid4().hex[:24]
        
        async for raw_data in backend_stream:
            # 解析 后端 SSE 数据
            token = self._parse_backend_sse(raw_data)
            if token:
                yield self.to_openai_stream_chunk(token, model, chunk_id, is_final=False)
        
        # 发送最终数据块和结束标记
        yield self.to_openai_stream_chunk("", model, chunk_id, is_final=True)
        yield self.to_openai_stream_done()

    
    # ========================================================================
    # Anthropic Format Conversion (Requirement 1.2)
    # ========================================================================
    
    def to_anthropic_response(
        self,
        backend_response: Dict[str, Any],
        model: str,
        request_id: Optional[str] = None,
        thinking_enabled: bool = False
    ) -> Dict[str, Any]:
        """
        将 后端响应转换为 Anthropic 格式
        
        Args:
            backend_response: 后端返回的响应数据
            model: 请求的模型名称
            request_id: 请求 ID（可选）
            thinking_enabled: 是否启用思维链
            
        Returns:
            Anthropic 格式的响应字典
        """
        raw_content = self._extract_content(backend_response)
        
        # 如果启用思维链，尝试分离思维链和正文
        if thinking_enabled:
            thinking_content, text_content = self._split_thinking_content(raw_content)
        else:
            thinking_content, text_content = "", raw_content
        
        # 构建 content 数组
        content_blocks = []
        
        if thinking_content:
            content_blocks.append({
                "type": "thinking",
                "thinking": thinking_content
            })
        
        content_blocks.append({
            "type": "text",
            "text": text_content
        })
        
        # 计算 token
        usage = self._estimate_token_usage(raw_content)
        
        return {
            "id": f"msg_{request_id or uuid.uuid4().hex[:24]}",
            "type": "message",
            "role": "assistant",
            "content": content_blocks,
            "model": model,
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": usage.prompt_tokens,
                "output_tokens": usage.completion_tokens
            }
        }
    
    def to_anthropic_stream_event(
        self,
        event_type: str,
        data: Dict[str, Any]
    ) -> str:
        """
        生成 Anthropic SSE 事件
        
        Args:
            event_type: 事件类型
            data: 事件数据
            
        Returns:
            SSE 格式的事件字符串
        """
        return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    
    async def transform_backend_sse_to_anthropic(
        self,
        backend_stream: AsyncGenerator[str, None],
        model: str,
        request_id: Optional[str] = None
    ) -> AsyncGenerator[str, None]:
        """
        将 后端 SSE 流转换为 Anthropic SSE 格式
        
        Args:
            backend_stream: 后端返回的 SSE 流
            model: 模型名称
            request_id: 请求 ID
            
        Yields:
            Anthropic 格式的 SSE 事件
        """
        msg_id = f"msg_{request_id or uuid.uuid4().hex[:24]}"
        
        # 发送 message_start 事件
        yield self.to_anthropic_stream_event("message_start", {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0}
            }
        })
        
        # 发送 content_block_start 事件
        yield self.to_anthropic_stream_event("content_block_start", {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""}
        })
        
        output_tokens = 0
        async for raw_data in backend_stream:
            token = self._parse_backend_sse(raw_data)
            if token:
                output_tokens += 1
                # 发送 content_block_delta 事件
                yield self.to_anthropic_stream_event("content_block_delta", {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": token}
                })
        
        # 发送 content_block_stop 事件
        yield self.to_anthropic_stream_event("content_block_stop", {
            "type": "content_block_stop",
            "index": 0
        })
        
        # 发送 message_delta 事件（包含 stop_reason）
        yield self.to_anthropic_stream_event("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": output_tokens}
        })
        
        # 发送 message_stop 事件
        yield self.to_anthropic_stream_event("message_stop", {
            "type": "message_stop"
        })

    async def transform_backend_sse_to_anthropic_with_thinking(
        self,
        backend_stream: AsyncGenerator[str, None],
        model: str,
        request_id: Optional[str] = None,
        thinking_budget: int = 10000
    ) -> AsyncGenerator[str, None]:
        """
        将后端 SSE 流转换为 Anthropic SSE 格式（带思维链）- 真正的实时流式输出
        
        实时检测 <thinking> 标签，边接收边输出：
        1. 检测到 <thinking> 开始标签时，开始输出 thinking 内容块
        2. 检测到 </thinking> 结束标签时，切换到 text 内容块
        3. 支持多种思维链标签格式
        
        Args:
            backend_stream: 后端返回的 SSE 流
            model: 模型名称
            request_id: 请求 ID
            thinking_budget: 思维链 token 预算
            
        Yields:
            Anthropic 格式的 SSE 事件（包含 thinking）
        """
        import re
        
        msg_id = f"msg_{request_id or uuid.uuid4().hex[:24]}"
        
        # 状态机
        STATE_INIT = 0           # 初始状态，等待内容
        STATE_IN_THINKING = 1    # 在思维链内容中
        STATE_IN_TEXT = 2        # 在正文内容中
        
        state = STATE_INIT
        content_index = 0
        buffer = ""  # 用于检测标签的缓冲区
        thinking_tokens = 0
        text_tokens = 0
        message_started = False
        thinking_block_started = False
        text_block_started = False
        
        # 支持的开始标签和结束标签
        thinking_start_tags = ['<thinking>', '<think>', '【思考】', '[思考]', '<thought>']
        thinking_end_tags = ['</thinking>', '</think>', '【/思考】', '[/思考]', '</thought>']
        
        def find_tag(text: str, tags: list) -> tuple:
            """查找标签，返回 (标签, 位置) 或 (None, -1)"""
            for tag in tags:
                pos = text.find(tag)
                if pos != -1:
                    return tag, pos
            return None, -1
        
        async def ensure_message_started():
            """确保 message_start 事件已发送"""
            nonlocal message_started
            if not message_started:
                message_started = True
                return self.to_anthropic_stream_event("message_start", {
                    "type": "message_start",
                    "message": {
                        "id": msg_id,
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": model,
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0}
                    }
                })
            return None
        
        async def start_thinking_block():
            """开始 thinking 内容块"""
            nonlocal thinking_block_started, content_index
            if not thinking_block_started:
                thinking_block_started = True
                return self.to_anthropic_stream_event("content_block_start", {
                    "type": "content_block_start",
                    "index": content_index,
                    "content_block": {"type": "thinking", "thinking": ""}
                })
            return None
        
        async def end_thinking_block():
            """结束 thinking 内容块"""
            nonlocal thinking_block_started, content_index
            if thinking_block_started:
                thinking_block_started = False
                event = self.to_anthropic_stream_event("content_block_stop", {
                    "type": "content_block_stop",
                    "index": content_index
                })
                content_index += 1
                return event
            return None
        
        async def start_text_block():
            """开始 text 内容块"""
            nonlocal text_block_started, content_index
            if not text_block_started:
                text_block_started = True
                return self.to_anthropic_stream_event("content_block_start", {
                    "type": "content_block_start",
                    "index": content_index,
                    "content_block": {"type": "text", "text": ""}
                })
            return None
        
        async def end_text_block():
            """结束 text 内容块"""
            nonlocal text_block_started, content_index
            if text_block_started:
                text_block_started = False
                return self.to_anthropic_stream_event("content_block_stop", {
                    "type": "content_block_stop",
                    "index": content_index
                })
            return None
        
        def emit_thinking_delta(text: str):
            """发送 thinking delta"""
            nonlocal thinking_tokens
            thinking_tokens += len(text)
            return self.to_anthropic_stream_event("content_block_delta", {
                "type": "content_block_delta",
                "index": content_index,
                "delta": {"type": "thinking_delta", "thinking": text}
            })
        
        def emit_text_delta(text: str):
            """发送 text delta"""
            nonlocal text_tokens
            text_tokens += len(text)
            return self.to_anthropic_stream_event("content_block_delta", {
                "type": "content_block_delta",
                "index": content_index,
                "delta": {"type": "text_delta", "text": text}
            })
        
        # 处理流
        async for raw_data in backend_stream:
            token = self._parse_backend_sse(raw_data)
            if not token:
                continue
            
            buffer += token
            
            while buffer:
                if state == STATE_INIT:
                    # 初始状态：检测是否有思维链开始标签
                    start_tag, start_pos = find_tag(buffer, thinking_start_tags)
                    
                    if start_tag and start_pos != -1:
                        # 找到思维链开始标签
                        # 先输出标签前的内容作为普通文本（如果有）
                        if start_pos > 0:
                            pre_text = buffer[:start_pos].strip()
                            if pre_text:
                                event = await ensure_message_started()
                                if event:
                                    yield event
                                event = await start_text_block()
                                if event:
                                    yield event
                                yield emit_text_delta(pre_text)
                                event = await end_text_block()
                                if event:
                                    yield event
                        
                        # 移除标签，进入思维链状态
                        buffer = buffer[start_pos + len(start_tag):]
                        state = STATE_IN_THINKING
                        
                        # 发送 message_start 和 thinking block start
                        event = await ensure_message_started()
                        if event:
                            yield event
                        event = await start_thinking_block()
                        if event:
                            yield event
                    else:
                        # 没有找到开始标签
                        # 检查是否可能是不完整的标签（缓冲区末尾）
                        max_tag_len = max(len(t) for t in thinking_start_tags)
                        if len(buffer) < max_tag_len:
                            # 缓冲区太短，等待更多数据
                            break
                        
                        # 没有思维链标签，直接作为普通文本输出
                        event = await ensure_message_started()
                        if event:
                            yield event
                        event = await start_text_block()
                        if event:
                            yield event
                        
                        # 保留可能的不完整标签
                        safe_len = len(buffer) - max_tag_len + 1
                        if safe_len > 0:
                            yield emit_text_delta(buffer[:safe_len])
                            buffer = buffer[safe_len:]
                        
                        state = STATE_IN_TEXT
                        break
                
                elif state == STATE_IN_THINKING:
                    # 在思维链中：检测结束标签
                    end_tag, end_pos = find_tag(buffer, thinking_end_tags)
                    
                    if end_tag and end_pos != -1:
                        # 找到结束标签
                        # 输出标签前的思维链内容
                        if end_pos > 0:
                            yield emit_thinking_delta(buffer[:end_pos])
                        
                        # 结束思维链块
                        event = await end_thinking_block()
                        if event:
                            yield event
                        
                        # 移除标签，进入正文状态
                        buffer = buffer[end_pos + len(end_tag):]
                        state = STATE_IN_TEXT
                        
                        # 开始文本块
                        event = await start_text_block()
                        if event:
                            yield event
                    else:
                        # 没有找到结束标签
                        max_tag_len = max(len(t) for t in thinking_end_tags)
                        if len(buffer) < max_tag_len:
                            break
                        
                        # 输出安全的部分
                        safe_len = len(buffer) - max_tag_len + 1
                        if safe_len > 0:
                            yield emit_thinking_delta(buffer[:safe_len])
                            buffer = buffer[safe_len:]
                        break
                
                elif state == STATE_IN_TEXT:
                    # 在正文中：直接输出
                    # 检查是否有新的思维链开始（理论上不应该，但以防万一）
                    start_tag, start_pos = find_tag(buffer, thinking_start_tags)
                    
                    if start_tag and start_pos != -1:
                        # 输出标签前的内容
                        if start_pos > 0:
                            yield emit_text_delta(buffer[:start_pos])
                        
                        # 结束当前文本块，开始新的思维链
                        event = await end_text_block()
                        if event:
                            yield event
                        
                        buffer = buffer[start_pos + len(start_tag):]
                        state = STATE_IN_THINKING
                        
                        event = await start_thinking_block()
                        if event:
                            yield event
                    else:
                        # 正常输出文本
                        max_tag_len = max(len(t) for t in thinking_start_tags)
                        safe_len = len(buffer) - max_tag_len + 1
                        if safe_len > 0:
                            yield emit_text_delta(buffer[:safe_len])
                            buffer = buffer[safe_len:]
                        break
        
        # 处理剩余缓冲区
        if buffer:
            if state == STATE_INIT:
                # 从未开始，输出为普通文本
                event = await ensure_message_started()
                if event:
                    yield event
                event = await start_text_block()
                if event:
                    yield event
                yield emit_text_delta(buffer)
            elif state == STATE_IN_THINKING:
                # 思维链未正常结束，输出剩余内容
                yield emit_thinking_delta(buffer)
            elif state == STATE_IN_TEXT:
                yield emit_text_delta(buffer)
        
        # 确保所有块都已关闭
        if thinking_block_started:
            event = await end_thinking_block()
            if event:
                yield event
        
        if text_block_started:
            event = await end_text_block()
            if event:
                yield event
        elif not text_block_started and message_started:
            # 如果只有思维链没有正文，添加一个空的文本块
            event = await start_text_block()
            if event:
                yield event
            event = await end_text_block()
            if event:
                yield event
        
        # 如果从未开始消息（空响应），发送基本结构
        if not message_started:
            yield self.to_anthropic_stream_event("message_start", {
                "type": "message_start",
                "message": {
                    "id": msg_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0}
                }
            })
            yield self.to_anthropic_stream_event("content_block_start", {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""}
            })
            yield self.to_anthropic_stream_event("content_block_stop", {
                "type": "content_block_stop",
                "index": 0
            })
        
        # 发送 message_delta 和 message_stop
        total_output_tokens = (thinking_tokens + text_tokens) // 4  # 粗略估算
        yield self.to_anthropic_stream_event("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": total_output_tokens}
        })
        
        yield self.to_anthropic_stream_event("message_stop", {
            "type": "message_stop"
        })
    
    def _split_thinking_content(self, content: str) -> tuple:
        """
        分离思维链内容和最终回答
        
        支持的格式：
        1. <thinking>...</thinking> 标签
        2. <think>...</think> 标签
        3. 【思考】...【/思考】 标签
        4. [思考]...[/思考] 标签
        
        Args:
            content: 完整内容
            
        Returns:
            (thinking_content, text_content) 元组
        """
        import re
        
        # 尝试匹配各种思维链标签
        patterns = [
            (r'<thinking>(.*?)</thinking>', re.DOTALL),
            (r'<think>(.*?)</think>', re.DOTALL),
            (r'【思考】(.*?)【/思考】', re.DOTALL),
            (r'\[思考\](.*?)\[/思考\]', re.DOTALL),
            (r'<thought>(.*?)</thought>', re.DOTALL),
        ]
        
        for pattern, flags in patterns:
            match = re.search(pattern, content, flags)
            if match:
                thinking = match.group(1).strip()
                # 移除思维链标签，获取剩余内容
                text = re.sub(pattern, '', content, flags=flags).strip()
                return thinking, text
        
        # 没有找到思维链标签，返回空思维链和完整内容
        return "", content

    
    # ========================================================================
    # Anthropic Tool Use Support (Requirements 4.1, 4.2, 4.3, 4.4)
    # ========================================================================
    
    def create_tool_use_block(
        self,
        tool_call: "ParsedToolCall",
        tool_use_id: str
    ) -> Dict[str, Any]:
        """
        创建 Anthropic tool_use 内容块
        
        Args:
            tool_call: 解析的工具调用
            tool_use_id: 工具使用 ID
            
        Returns:
            Anthropic tool_use 内容块字典
            
        Requirements: 4.1, 4.2
        """
        return {
            "type": "tool_use",
            "id": tool_use_id,
            "name": tool_call.tool_name,
            "input": tool_call.arguments
        }
    
    def to_anthropic_response_with_tools(
        self,
        backend_response: Dict[str, Any],
        model: str,
        request_id: Optional[str] = None,
        tool_parser: Optional["ToolParser"] = None
    ) -> Dict[str, Any]:
        """
        将后端响应转换为带有 tool_use 支持的 Anthropic 格式
        
        处理逻辑：
        1. 如果没有 tool_parser 或响应中没有工具调用，返回标准文本响应
        2. 如果有工具调用，解析并转换为 tool_use 内容块
        3. 如果工具调用之前有文本，将文本作为单独的内容块包含在 tool_use 块之前
        
        Args:
            backend_response: 后端返回的响应数据
            model: 请求的模型名称
            request_id: 请求 ID（可选）
            tool_parser: 工具解析器实例（可选）
            
        Returns:
            Anthropic 格式的响应字典，可能包含 tool_use 内容块
            
        Requirements: 4.1, 4.2, 4.3, 4.4
        """
        raw_content = self._extract_content(backend_response)
        
        # 如果没有 tool_parser，返回标准响应
        if tool_parser is None:
            return self.to_anthropic_response(backend_response, model, request_id)
        
        # 解析工具调用
        parse_result = tool_parser.parse(raw_content)
        
        # 如果没有工具调用，返回标准文本响应
        if not parse_result.has_tool_calls:
            return self.to_anthropic_response(backend_response, model, request_id)
        
        # 构建 content 数组
        content_blocks: List[Dict[str, Any]] = []
        
        # 如果工具调用之前有文本，添加文本块 (Requirement 4.4)
        if parse_result.text_before and parse_result.text_before.strip():
            content_blocks.append({
                "type": "text",
                "text": parse_result.text_before.strip()
            })
        
        # 为每个工具调用创建 tool_use 块 (Requirements 4.1, 4.2)
        for tool_call in parse_result.tool_calls:
            tool_use_id = tool_parser.generate_tool_use_id()
            tool_use_block = self.create_tool_use_block(tool_call, tool_use_id)
            content_blocks.append(tool_use_block)
        
        # 如果工具调用之后有文本，添加文本块
        if parse_result.text_after and parse_result.text_after.strip():
            content_blocks.append({
                "type": "text",
                "text": parse_result.text_after.strip()
            })
        
        # 如果没有任何内容块（理论上不应该发生），添加空文本块 (Requirement 4.3)
        if not content_blocks:
            content_blocks.append({
                "type": "text",
                "text": ""
            })
        
        # 计算 token
        usage = self._estimate_token_usage(raw_content)
        
        # 确定 stop_reason：如果有工具调用，使用 "tool_use"
        stop_reason = "tool_use" if parse_result.has_tool_calls else "end_turn"
        
        return {
            "id": f"msg_{request_id or uuid.uuid4().hex[:24]}",
            "type": "message",
            "role": "assistant",
            "content": content_blocks,
            "model": model,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": usage.prompt_tokens,
                "output_tokens": usage.completion_tokens
            }
        }

    # ========================================================================
    # Streaming Tool Use Support (Requirements 4.5, 4.6, 7.1, 7.2, 7.3, 7.4, 7.5)
    # ========================================================================

    async def transform_backend_sse_to_anthropic_with_tools(
        self,
        backend_stream: AsyncGenerator[str, None],
        model: str,
        request_id: Optional[str] = None,
        tool_parser: Optional["ToolParser"] = None,
        max_buffer_size: int = 65536
    ) -> AsyncGenerator[str, None]:
        """
        将后端 SSE 流转换为带有 tool_use 检测的 Anthropic SSE 格式
        
        支持两种工具调用格式：
        1. JSON 代码块: ```json {"tool": "...", "arguments": {...}} ```
        2. XML 标签: <tool_use id="..." name="...">JSON参数</tool_use>
        
        Args:
            backend_stream: 后端返回的 SSE 流
            model: 模型名称
            request_id: 请求 ID
            tool_parser: 工具解析器实例（可选）
            max_buffer_size: 最大缓冲区大小，防止内存溢出
            
        Yields:
            Anthropic 格式的 SSE 事件
            
        Requirements: 4.5, 4.6, 7.1, 7.2, 7.3, 7.4, 7.5
        """
        msg_id = f"msg_{request_id or uuid.uuid4().hex[:24]}"
        
        # 状态常量
        STATE_TEXT = 0           # 普通文本状态
        STATE_BUFFERING_JSON = 1 # 缓冲 JSON 代码块状态
        STATE_BUFFERING_XML = 2  # 缓冲 XML tool_use 标签状态
        
        state = STATE_TEXT
        content_index = 0
        buffer = ""
        output_tokens = 0
        message_started = False
        current_block_started = False
        current_block_type = "text"  # "text" or "tool_use"
        has_tool_calls = False
        
        # 标记
        JSON_BLOCK_START = "```json"
        JSON_BLOCK_END = "```"
        XML_TOOL_START = "<tool_use"
        XML_TOOL_END = "</tool_use>"
        
        # XML tool_use 正则
        import re
        XML_TOOL_USE_PATTERN = re.compile(
            r'<tool_use\s+id="([^"]+)"\s+name="([^"]+)">(.*?)</tool_use>',
            re.DOTALL
        )
        
        def emit_message_start() -> str:
            """发送 message_start 事件"""
            return self.to_anthropic_stream_event("message_start", {
                "type": "message_start",
                "message": {
                    "id": msg_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0}
                }
            })
        
        def emit_text_block_start(index: int) -> str:
            """发送 text content_block_start 事件"""
            return self.to_anthropic_stream_event("content_block_start", {
                "type": "content_block_start",
                "index": index,
                "content_block": {"type": "text", "text": ""}
            })
        
        def emit_text_delta(index: int, text: str) -> str:
            """发送 text_delta 事件"""
            return self.to_anthropic_stream_event("content_block_delta", {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "text_delta", "text": text}
            })
        
        def emit_tool_use_block_start(index: int, tool_use_id: str, tool_name: str) -> str:
            """发送 tool_use content_block_start 事件 (Requirement 7.1)"""
            return self.to_anthropic_stream_event("content_block_start", {
                "type": "content_block_start",
                "index": index,
                "content_block": {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": tool_name,
                    "input": {}
                }
            })
        
        def emit_tool_use_delta(index: int, partial_json: str) -> str:
            """发送 input_json_delta 事件 (Requirement 7.2)"""
            return self.to_anthropic_stream_event("content_block_delta", {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "input_json_delta", "partial_json": partial_json}
            })
        
        def emit_block_stop(index: int) -> str:
            """发送 content_block_stop 事件 (Requirement 7.3)"""
            return self.to_anthropic_stream_event("content_block_stop", {
                "type": "content_block_stop",
                "index": index
            })
        
        async def ensure_message_started():
            """确保 message_start 事件已发送"""
            nonlocal message_started
            if not message_started:
                message_started = True
                return emit_message_start()
            return None
        
        async def start_text_block():
            """开始文本块"""
            nonlocal current_block_started, current_block_type, content_index
            if not current_block_started:
                current_block_started = True
                current_block_type = "text"
                return emit_text_block_start(content_index)
            return None
        
        async def end_current_block():
            """结束当前块 (Requirement 7.4)"""
            nonlocal current_block_started, content_index
            if current_block_started:
                current_block_started = False
                event = emit_block_stop(content_index)
                content_index += 1
                return event
            return None
        
        def parse_tool_call_from_json(json_str: str):
            """从 JSON 字符串解析工具调用"""
            if tool_parser is None:
                return None
            
            try:
                data = json.loads(json_str)
                if not isinstance(data, dict):
                    return None
                
                tool_name = data.get("tool")
                if not tool_name or not isinstance(tool_name, str):
                    return None
                
                arguments = data.get("arguments", {})
                if not isinstance(arguments, dict):
                    arguments = {}
                
                # 验证工具调用
                from .tool_parser import ParsedToolCall
                parsed_call = ParsedToolCall(
                    tool_name=tool_name,
                    arguments=arguments,
                    raw_json=json_str
                )
                
                if tool_parser.validate_tool_call(parsed_call):
                    return parsed_call
                return None
                
            except json.JSONDecodeError:
                return None
            except Exception:
                return None
        
        def parse_xml_tool_use(xml_content: str):
            """从 XML tool_use 标签解析工具调用"""
            if tool_parser is None:
                return None, None
            
            match = XML_TOOL_USE_PATTERN.match(xml_content)
            if not match:
                return None, None
            
            tool_id = match.group(1)
            tool_name = match.group(2)
            json_content = match.group(3).strip()
            
            try:
                arguments = json.loads(json_content) if json_content else {}
                if not isinstance(arguments, dict):
                    arguments = {}
            except json.JSONDecodeError:
                return None, None
            
            from .tool_parser import ParsedToolCall
            parsed_call = ParsedToolCall(
                tool_name=tool_name,
                arguments=arguments,
                raw_json=json_content
            )
            
            if tool_parser.validate_tool_call(parsed_call):
                return parsed_call, tool_id
            return None, None
        
        # 处理流
        async for raw_data in backend_stream:
            token = self._parse_backend_sse(raw_data)
            if not token:
                continue
            
            output_tokens += 1
            buffer += token
            
            while buffer:
                if state == STATE_TEXT:
                    # 检查是否有 JSON 代码块或 XML tool_use 开始标记
                    json_start_pos = buffer.find(JSON_BLOCK_START)
                    xml_start_pos = buffer.find(XML_TOOL_START)
                    
                    # 确定哪个标记先出现
                    start_pos = -1
                    start_type = None
                    
                    if json_start_pos != -1 and xml_start_pos != -1:
                        if json_start_pos < xml_start_pos:
                            start_pos = json_start_pos
                            start_type = "json"
                        else:
                            start_pos = xml_start_pos
                            start_type = "xml"
                    elif json_start_pos != -1:
                        start_pos = json_start_pos
                        start_type = "json"
                    elif xml_start_pos != -1:
                        start_pos = xml_start_pos
                        start_type = "xml"
                    
                    if start_pos != -1:
                        # 找到开始标记
                        # 先输出开始标记之前的文本
                        if start_pos > 0:
                            text_before = buffer[:start_pos]
                            event = await ensure_message_started()
                            if event:
                                yield event
                            event = await start_text_block()
                            if event:
                                yield event
                            yield emit_text_delta(content_index, text_before)
                        
                        # 移除已处理的文本
                        buffer = buffer[start_pos:]
                        state = STATE_BUFFERING_JSON if start_type == "json" else STATE_BUFFERING_XML
                    else:
                        # 没有找到开始标记
                        # 检查缓冲区末尾是否可能是不完整的开始标记
                        potential_start_len = max(len(JSON_BLOCK_START), len(XML_TOOL_START)) - 1
                        
                        if len(buffer) <= potential_start_len:
                            # 缓冲区太短，等待更多数据
                            break
                        
                        # 输出安全的部分（保留可能的不完整标记）
                        safe_len = len(buffer) - potential_start_len
                        if safe_len > 0:
                            text_to_output = buffer[:safe_len]
                            event = await ensure_message_started()
                            if event:
                                yield event
                            event = await start_text_block()
                            if event:
                                yield event
                            yield emit_text_delta(content_index, text_to_output)
                            buffer = buffer[safe_len:]
                        break
                
                elif state == STATE_BUFFERING_JSON:
                    # 在缓冲 JSON 代码块
                    search_start = len(JSON_BLOCK_START)
                    end_pos = buffer.find(JSON_BLOCK_END, search_start)
                    
                    if end_pos != -1:
                        # 找到结束标记
                        json_content = buffer[len(JSON_BLOCK_START):end_pos].strip()
                        
                        # 尝试解析工具调用
                        tool_call = parse_tool_call_from_json(json_content)
                        
                        if tool_call is not None:
                            # 有效的工具调用
                            has_tool_calls = True
                            
                            event = await end_current_block()
                            if event:
                                yield event
                            
                            event = await ensure_message_started()
                            if event:
                                yield event
                            
                            tool_use_id = tool_parser.generate_tool_use_id()
                            yield emit_tool_use_block_start(content_index, tool_use_id, tool_call.tool_name)
                            
                            input_json = json.dumps(tool_call.arguments)
                            yield emit_tool_use_delta(content_index, input_json)
                            
                            yield emit_block_stop(content_index)
                            content_index += 1
                            current_block_started = False
                        else:
                            # 不是有效的工具调用，作为普通文本输出
                            full_block = buffer[:end_pos + len(JSON_BLOCK_END)]
                            event = await ensure_message_started()
                            if event:
                                yield event
                            event = await start_text_block()
                            if event:
                                yield event
                            yield emit_text_delta(content_index, full_block)
                        
                        buffer = buffer[end_pos + len(JSON_BLOCK_END):]
                        state = STATE_TEXT
                    else:
                        if len(buffer) > max_buffer_size:
                            event = await ensure_message_started()
                            if event:
                                yield event
                            event = await start_text_block()
                            if event:
                                yield event
                            yield emit_text_delta(content_index, buffer)
                            buffer = ""
                            state = STATE_TEXT
                        break
                
                elif state == STATE_BUFFERING_XML:
                    # 在缓冲 XML tool_use 标签
                    end_pos = buffer.find(XML_TOOL_END)
                    
                    if end_pos != -1:
                        # 找到结束标记
                        xml_content = buffer[:end_pos + len(XML_TOOL_END)]
                        
                        # 尝试解析工具调用
                        tool_call, original_id = parse_xml_tool_use(xml_content)
                        
                        if tool_call is not None:
                            # 有效的工具调用
                            has_tool_calls = True
                            
                            event = await end_current_block()
                            if event:
                                yield event
                            
                            event = await ensure_message_started()
                            if event:
                                yield event
                            
                            # 使用原始 ID 或生成新 ID
                            tool_use_id = original_id if original_id else tool_parser.generate_tool_use_id()
                            yield emit_tool_use_block_start(content_index, tool_use_id, tool_call.tool_name)
                            
                            input_json = json.dumps(tool_call.arguments)
                            yield emit_tool_use_delta(content_index, input_json)
                            
                            yield emit_block_stop(content_index)
                            content_index += 1
                            current_block_started = False
                        else:
                            # 不是有效的工具调用，作为普通文本输出
                            event = await ensure_message_started()
                            if event:
                                yield event
                            event = await start_text_block()
                            if event:
                                yield event
                            yield emit_text_delta(content_index, xml_content)
                        
                        buffer = buffer[end_pos + len(XML_TOOL_END):]
                        state = STATE_TEXT
                    else:
                        if len(buffer) > max_buffer_size:
                            event = await ensure_message_started()
                            if event:
                                yield event
                            event = await start_text_block()
                            if event:
                                yield event
                            yield emit_text_delta(content_index, buffer)
                            buffer = ""
                            state = STATE_TEXT
                        break
        
        # 处理剩余缓冲区
        if buffer:
            if state in (STATE_BUFFERING_JSON, STATE_BUFFERING_XML):
                # 代码块未完成，作为普通文本输出
                event = await ensure_message_started()
                if event:
                    yield event
                event = await start_text_block()
                if event:
                    yield event
                yield emit_text_delta(content_index, buffer)
            elif state == STATE_TEXT:
                # 普通文本
                event = await ensure_message_started()
                if event:
                    yield event
                event = await start_text_block()
                if event:
                    yield event
                yield emit_text_delta(content_index, buffer)
        
        # 确保当前块已关闭
        event = await end_current_block()
        if event:
            yield event
        
        # 如果从未开始消息（空响应），发送基本结构
        if not message_started:
            yield emit_message_start()
            yield emit_text_block_start(0)
            yield emit_block_stop(0)
            content_index = 1
        
        # 确定 stop_reason：如果有工具调用，使用 "tool_use"
        stop_reason = "tool_use" if has_tool_calls else "end_turn"
        
        # 发送 message_delta 和 message_stop
        yield self.to_anthropic_stream_event("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": output_tokens}
        })
        
        yield self.to_anthropic_stream_event("message_stop", {
            "type": "message_stop"
        })

    
    # ========================================================================
    # Gemini Format Conversion (Requirement 1.3)
    # ========================================================================
    
    def to_gemini_response(
        self,
        backend_response: Dict[str, Any],
        model: str
    ) -> Dict[str, Any]:
        """
        将 后端响应转换为 Gemini 格式
        
        Args:
            backend_response: 后端返回的响应数据
            model: 请求的模型名称
            
        Returns:
            Gemini 格式的响应字典
        """
        content = self._extract_content(backend_response)
        usage = self._estimate_token_usage(content)
        
        return {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": content}
                        ],
                        "role": "model"
                    },
                    "finishReason": "STOP",
                    "index": 0,
                    "safetyRatings": []
                }
            ],
            "usageMetadata": {
                "promptTokenCount": usage.prompt_tokens,
                "candidatesTokenCount": usage.completion_tokens,
                "totalTokenCount": usage.total_tokens
            },
            "modelVersion": model
        }
    
    async def transform_backend_sse_to_gemini(
        self,
        backend_stream: AsyncGenerator[str, None],
        model: str
    ) -> AsyncGenerator[str, None]:
        """
        将 后端 SSE 流转换为 Gemini SSE 格式
        
        Args:
            backend_stream: 后端返回的 SSE 流
            model: 模型名称
            
        Yields:
            Gemini 格式的 SSE 数据块
        """
        async for raw_data in backend_stream:
            token = self._parse_backend_sse(raw_data)
            if token:
                chunk = {
                    "candidates": [
                        {
                            "content": {
                                "parts": [{"text": token}],
                                "role": "model"
                            },
                            "finishReason": None,
                            "index": 0
                        }
                    ]
                }
                yield f"data: {json.dumps(chunk)}\n\n"
        
        # 发送最终数据块
        final_chunk = {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": ""}],
                        "role": "model"
                    },
                    "finishReason": "STOP",
                    "index": 0
                }
            ]
        }
        yield f"data: {json.dumps(final_chunk)}\n\n"

    
    # ========================================================================
    # Utility Methods
    # ========================================================================
    
    def _extract_content(self, backend_response: Dict[str, Any]) -> str:
        """
        从 后端响应中提取输出内容
        
        后端响应格式可能有多种形式：
        - {"outputs": {"out-0": "content"}}
        - {"output": "content"}
        - {"out-0": "content"}
        - {"result": "content"}
        - 直接是字符串
        
        Args:
            backend_response: 后端返回的响应数据
            
        Returns:
            提取的输出内容字符串
        """
        if isinstance(backend_response, str):
            return backend_response
        
        # 首先检查 outputs 字段（后端标准格式）
        if "outputs" in backend_response:
            outputs = backend_response["outputs"]
            if isinstance(outputs, dict):
                # 遍历 outputs 中的所有字段，找到第一个非空字符串值
                for key, value in outputs.items():
                    if isinstance(value, str) and value.strip():
                        return value
                # 如果没有字符串值，尝试递归提取
                for key, value in outputs.items():
                    if isinstance(value, dict):
                        result = self._extract_content(value)
                        if result and result != "{}":
                            return result
            elif isinstance(outputs, str):
                return outputs
        
        # 尝试常见的输出字段名
        for key in ["output", "out-0", "result", "response", "text", "content", "message", "answer"]:
            if key in backend_response:
                value = backend_response[key]
                if isinstance(value, str):
                    return value
                elif isinstance(value, dict):
                    # 递归提取
                    return self._extract_content(value)
        
        # 如果没有找到已知字段，尝试遍历所有字段找字符串
        for key, value in backend_response.items():
            if isinstance(value, str) and value.strip() and key not in ["user_id", "id", "status"]:
                return value
        
        # 如果没有找到，返回整个响应的 JSON 字符串
        return json.dumps(backend_response)
    
    def _estimate_token_usage(
        self,
        content: str,
        prompt_tokens: int = 0,
        input_text: str = None
    ) -> TokenUsage:
        """
        估算 token 使用量
        
        注意：只计算输出 token，输入 token 设为 0。
        因为对话历史会导致输入 token 不断累积，与后端实际消耗不符。
        
        Args:
            content: 输出内容
            prompt_tokens: 输入 token 数（已弃用，不再使用）
            input_text: 输入文本（已弃用，不再使用）
            
        Returns:
            TokenUsage 对象
        """
        from app.services.token_counter import get_token_counter
        
        token_counter = get_token_counter()
        completion_tokens = token_counter.count(content) if content else 0
        
        # 只计算输出 token，输入 token 设为 0
        # 因为我们无法准确知道后端实际消耗的输入 token
        return TokenUsage(
            prompt_tokens=0,
            completion_tokens=completion_tokens,
            total_tokens=completion_tokens
        )
    
    def _parse_backend_sse(self, raw_data: str) -> Optional[str]:
        """
        解析 后端 SSE 数据
        
        后端 SSE 格式:
        - {"outputs":{"out-0":"内容片段"},"citations":null,"run_id":"...","metadata":{},"progress_data":{...}}
        - "data: {\"token\": \"content\"}"
        - "data: content"
        - 直接是 token 内容
        
        Args:
            raw_data: 原始 SSE 数据
            
        Returns:
            解析出的 token 内容，如果无法解析则返回 None
        """
        if not raw_data:
            return None
        
        # 去除前缀
        data = raw_data.strip()
        if data.startswith("data:"):
            data = data[5:].strip()
        
        # 跳过空数据和结束标记
        if not data or data == "[DONE]" or is_stream_completion_marker(data):
            return None
        
        # 尝试解析 JSON
        try:
            parsed = json.loads(data)
            if isinstance(parsed, dict):
                if is_stream_completion_marker(parsed):
                    return None

                # 首先检查 outputs 字段（st 标准流式格式）
                if "outputs" in parsed:
                    outputs = parsed["outputs"]
                    if isinstance(outputs, dict):
                        # 遍历 outputs 找到内容
                        for key, value in outputs.items():
                            if str(key).lower() == "stream_complete":
                                continue
                            if isinstance(value, str) and not is_stream_completion_marker(value):
                                return value
                        if is_stream_completion_marker(outputs):
                            return None
                    elif isinstance(outputs, str):
                        if is_stream_completion_marker(outputs):
                            return None
                        return outputs
                
                # 尝试其他常见字段
                for key in ["token", "text", "content", "delta", "output", "out-0"]:
                    if key in parsed:
                        value = parsed[key]
                        if isinstance(value, str):
                            if is_stream_completion_marker(value):
                                return None
                            return value
                        elif isinstance(value, dict) and "content" in value:
                            if is_stream_completion_marker(value["content"]):
                                return None
                            return value["content"]
            elif isinstance(parsed, str):
                if is_stream_completion_marker(parsed):
                    return None
                return parsed
        except json.JSONDecodeError:
            # 不是 JSON，直接返回原始数据
            if is_stream_completion_marker(data):
                return None
            return data
        
        return None
    
    def transform_response(
        self,
        backend_response: Dict[str, Any],
        model: str,
        target_format: Literal["openai", "anthropic", "gemini"] = "openai",
        request_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        根据目标格式转换响应
        
        Args:
            backend_response: 后端返回的响应数据
            model: 模型名称
            target_format: 目标 API 格式
            request_id: 请求 ID
            
        Returns:
            转换后的响应字典
        """
        if target_format == "openai":
            return self.to_openai_response(backend_response, model, request_id)
        elif target_format == "anthropic":
            return self.to_anthropic_response(backend_response, model, request_id)
        elif target_format == "gemini":
            return self.to_gemini_response(backend_response, model)
        else:
            raise ValueError(f"Unsupported target format: {target_format}")
    
    async def transform_stream(
        self,
        backend_stream: AsyncGenerator[str, None],
        model: str,
        target_format: Literal["openai", "anthropic", "gemini"] = "openai",
        request_id: Optional[str] = None
    ) -> AsyncGenerator[str, None]:
        """
        根据目标格式转换 SSE 流
        
        Args:
            backend_stream: 后端返回的 SSE 流
            model: 模型名称
            target_format: 目标 API 格式
            request_id: 请求 ID
            
        Yields:
            转换后的 SSE 数据块
        """
        if target_format == "openai":
            async for chunk in self.transform_backend_sse_to_openai(
                backend_stream, model, request_id
            ):
                yield chunk
        elif target_format == "anthropic":
            async for chunk in self.transform_backend_sse_to_anthropic(
                backend_stream, model, request_id
            ):
                yield chunk
        elif target_format == "gemini":
            async for chunk in self.transform_backend_sse_to_gemini(
                backend_stream, model
            ):
                yield chunk
        else:
            raise ValueError(f"Unsupported target format: {target_format}")


# ============================================================================
# Global Instance
# ============================================================================

_response_transformer: Optional[ResponseTransformer] = None


def get_response_transformer() -> ResponseTransformer:
    """获取全局响应转换器实例"""
    global _response_transformer
    if _response_transformer is None:
        _response_transformer = ResponseTransformer()
    return _response_transformer
