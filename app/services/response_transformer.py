"""
Response Transformer Service
将后端响应转换为标准 API 格式（OpenAI、Anthropic、Gemini）
"""

import json
import time
import uuid
from typing import Optional, Dict, Any, AsyncGenerator, Literal
from dataclasses import dataclass


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
StackAIResponse = BackendResponse


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
        request_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        将 后端响应转换为 OpenAI 格式
        
        Args:
            backend_response: 后端返回的响应数据
            model: 请求的模型名称
            request_id: 请求 ID（可选）
            
        Returns:
            OpenAI 格式的响应字典
        """
        # 提取输出内容
        content = self._extract_content(backend_response)
        
        # 估算 token 使用量
        usage = self._estimate_token_usage(content)
        
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
        request_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        将 后端响应转换为 Anthropic 格式
        
        Args:
            backend_response: 后端返回的响应数据
            model: 请求的模型名称
            request_id: 请求 ID（可选）
            
        Returns:
            Anthropic 格式的响应字典
        """
        content = self._extract_content(backend_response)
        usage = self._estimate_token_usage(content)
        
        return {
            "id": f"msg_{request_id or uuid.uuid4().hex[:24]}",
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": content
                }
            ],
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
        prompt_tokens: int = 0
    ) -> TokenUsage:
        """
        估算 token 使用量
        
        使用 tiktoken 库进行准确计算（如果可用），否则使用字符估算
        
        Args:
            content: 输出内容
            prompt_tokens: 输入 token 数（如果已知）
            
        Returns:
            TokenUsage 对象
        """
        from app.services.token_counter import get_token_counter
        
        token_counter = get_token_counter()
        completion_tokens = token_counter.count(content) if content else 0
        
        return TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens
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
        if not data or data == "[DONE]":
            return None
        
        # 尝试解析 JSON
        try:
            parsed = json.loads(data)
            if isinstance(parsed, dict):
                # 首先检查 outputs 字段（StackAI 标准流式格式）
                if "outputs" in parsed:
                    outputs = parsed["outputs"]
                    if isinstance(outputs, dict):
                        # 遍历 outputs 找到内容
                        for key, value in outputs.items():
                            if isinstance(value, str):
                                return value
                    elif isinstance(outputs, str):
                        return outputs
                
                # 尝试其他常见字段
                for key in ["token", "text", "content", "delta", "output", "out-0"]:
                    if key in parsed:
                        value = parsed[key]
                        if isinstance(value, str):
                            return value
                        elif isinstance(value, dict) and "content" in value:
                            return value["content"]
            elif isinstance(parsed, str):
                return parsed
        except json.JSONDecodeError:
            # 不是 JSON，直接返回原始数据
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
