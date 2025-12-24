"""
Tool Serializer Service
工具序列化器服务，将工具调用序列化为 JSON 代码块格式

用于往返测试，验证解析器的正确性

Requirements: 9.1, 9.2
"""

import json
from typing import List

from .tool_parser import ParsedToolCall


class ToolSerializer:
    """
    工具调用序列化器
    
    将 ParsedToolCall 对象序列化为 JSON 代码块格式，
    产生与解析器预期输入格式匹配的输出。
    """
    
    def serialize(self, tool_call: ParsedToolCall) -> str:
        """
        将工具调用序列化为 JSON 代码块格式
        
        Args:
            tool_call: 要序列化的工具调用
            
        Returns:
            格式为 ```json\n{"tool": "...", "arguments": {...}}\n``` 的字符串
        """
        # 构建工具调用 JSON 对象
        tool_json = {
            "tool": tool_call.tool_name,
            "arguments": tool_call.arguments
        }
        
        # 序列化为格式化的 JSON 字符串
        json_str = json.dumps(tool_json, ensure_ascii=False, indent=2)
        
        # 包装为 JSON 代码块格式
        return f"```json\n{json_str}\n```"
    
    def serialize_multiple(self, tool_calls: List[ParsedToolCall]) -> str:
        """
        序列化多个工具调用
        
        每个工具调用序列化为单独的 JSON 代码块，
        代码块之间用换行符分隔。
        
        Args:
            tool_calls: 要序列化的工具调用列表
            
        Returns:
            包含所有工具调用的字符串，每个调用为独立的 JSON 代码块
        """
        if not tool_calls:
            return ""
        
        serialized_blocks = [self.serialize(tc) for tc in tool_calls]
        return "\n\n".join(serialized_blocks)
