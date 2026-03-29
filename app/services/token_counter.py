"""
Token Counter Service
使用 tiktoken 库准确计算 token 数量

参考:
- OpenAI: https://github.com/openai/tiktoken
- Anthropic: 使用 cl100k_base 编码（与 Claude 兼容）
- Gemini: 使用 cl100k_base 编码（近似）
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 延迟导入 tiktoken，避免启动时加载
_encoding = None
_encoding_initialized = False


def _get_encoding():
    """获取 tiktoken 编码器（延迟加载）"""
    global _encoding, _encoding_initialized
    if not _encoding_initialized:
        try:
            import tiktoken
            # cl100k_base 是 GPT-4, GPT-3.5-turbo, text-embedding-ada-002 使用的编码
            # 也适用于 Claude 和 Gemini 的近似计算
            _encoding = tiktoken.get_encoding("cl100k_base")
            logger.info("Tiktoken encoding loaded: cl100k_base")
        except ImportError:
            logger.warning("tiktoken not installed, using fallback estimation")
            _encoding = None
        except Exception as e:
            logger.error(f"Failed to load tiktoken: {e}")
            _encoding = None
        finally:
            _encoding_initialized = True
    return _encoding


def count_tokens(text: str) -> int:
    """
    计算文本的 token 数量
    
    Args:
        text: 要计算的文本
        
    Returns:
        token 数量
    """
    if not text:
        return 0
    
    encoding = _get_encoding()
    if encoding:
        try:
            return len(encoding.encode(text))
        except Exception as e:
            logger.warning(f"Token counting failed, using fallback: {e}")
    
    # 降级方案：简单估算
    # 英文约 4 字符 = 1 token
    # 中文约 1.5-2 字符 = 1 token
    # 这里使用保守估算
    return _estimate_tokens(text)


def _estimate_tokens(text: str) -> int:
    """
    估算 token 数量（降级方案）
    
    使用简单的字符计数规则：
    - 英文/数字/标点：约 4 字符 = 1 token
    - 中文/日文/韩文：约 1.5 字符 = 1 token
    """
    if not text:
        return 0
    
    # 统计中文字符数量
    cjk_count = sum(1 for char in text if '\u4e00' <= char <= '\u9fff' or 
                   '\u3040' <= char <= '\u30ff' or  # 日文
                   '\uac00' <= char <= '\ud7af')    # 韩文
    
    # 非 CJK 字符数量
    other_count = len(text) - cjk_count
    
    # 估算 token 数量
    cjk_tokens = int(cjk_count / 1.5)
    other_tokens = int(other_count / 4)
    
    return max(1, cjk_tokens + other_tokens)


def count_message_tokens(messages: list, model: str = "gpt-4") -> int:
    """
    计算消息列表的 token 数量（OpenAI 格式）
    
    Args:
        messages: OpenAI 格式的消息列表
        model: 模型名称（用于选择编码方式）
        
    Returns:
        token 数量
    """
    total = 0
    for message in messages:
        # 每条消息有固定的开销
        total += 4  # <|im_start|>{role}\n ... <|im_end|>\n
        
        if isinstance(message, dict):
            content = message.get("content", "")
            if isinstance(content, str):
                total += count_tokens(content)
            elif isinstance(content, list):
                # 多模态消息
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        total += count_tokens(part.get("text", ""))
    
    total += 2  # 回复的开头
    return total


class TokenCounter:
    """Token 计数器类"""
    
    @staticmethod
    def count(text: str) -> int:
        """计算文本的 token 数量"""
        return count_tokens(text)
    
    @staticmethod
    def count_messages(messages: list, model: str = "gpt-4") -> int:
        """计算消息列表的 token 数量"""
        return count_message_tokens(messages, model)
    
    @staticmethod
    def estimate(text: str) -> int:
        """估算 token 数量（不使用 tiktoken）"""
        return _estimate_tokens(text)


# 全局实例
_token_counter: Optional[TokenCounter] = None


def get_token_counter() -> TokenCounter:
    """获取全局 Token 计数器实例"""
    global _token_counter
    if _token_counter is None:
        _token_counter = TokenCounter()
    return _token_counter
