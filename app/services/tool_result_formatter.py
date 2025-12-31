"""
Tool Result Formatter Service
工具结果格式化器服务，格式化 tool_result 消息以注入到对话中

Requirements: 5.1, 5.2, 5.3, 5.4, 5.5
"""

from typing import Dict, Any, Optional, List
import logging
import json

logger = logging.getLogger(__name__)


class ToolResultFormatter:
    """
    工具结果格式化器
    
    将 Anthropic 格式的 tool_result 块格式化为模型能够理解的上下文字符串，
    并将助手的 tool_use 格式化为上下文字符串。
    """
    
    def format_tool_result(self, tool_result: Dict[str, Any]) -> str:
        """
        将 tool_result 块格式化为上下文字符串
        
        处理成功和错误情况：
        - 成功情况：包含输出内容
        - 错误情况（is_error: true）：包含错误消息
        
        Args:
            tool_result: Anthropic tool_result 内容块，包含：
                - tool_use_id: 对应的 tool_use ID
                - content: 工具执行结果内容（字符串或内容块列表）
                - is_error: 可选，是否为错误结果
                
        Returns:
            用于模型上下文的格式化字符串
        """
        tool_use_id = tool_result.get("tool_use_id", "unknown")
        content = tool_result.get("content", "")
        is_error = tool_result.get("is_error", False)
        
        # 处理 content 可能是列表的情况（Anthropic 格式支持内容块列表）
        content_str = self._extract_content_string(content)
        
        # 根据是否为错误构建不同的格式
        if is_error:
            return self._format_error_result(tool_use_id, content_str)
        else:
            return self._format_success_result(tool_use_id, content_str)
    
    def _extract_content_string(self, content: Any) -> str:
        """
        从 content 字段提取字符串内容
        
        content 可能是：
        - 字符串
        - 内容块列表（每个块有 type 和 text 字段）
        
        Args:
            content: tool_result 的 content 字段
            
        Returns:
            提取的字符串内容
        """
        if isinstance(content, str):
            return content
        
        if isinstance(content, list):
            # 处理内容块列表
            text_parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif "text" in block:
                        text_parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    text_parts.append(block)
            return "\n".join(text_parts)
        
        # 其他情况，尝试转换为字符串
        return str(content) if content else ""
    
    def _format_success_result(self, tool_use_id: str, content: str) -> str:
        """
        格式化成功的工具执行结果
        
        使用简洁的格式，保持工具调用 ID 的可追踪性
        
        Args:
            tool_use_id: 工具使用 ID
            content: 执行结果内容
            
        Returns:
            格式化的成功结果字符串
        """
        return f"[Tool Result: {tool_use_id}]\n{content}"
    
    def _format_error_result(self, tool_use_id: str, error_message: str) -> str:
        """
        格式化错误的工具执行结果
        
        Args:
            tool_use_id: 工具使用 ID
            error_message: 错误消息
            
        Returns:
            格式化的错误结果字符串
        """
        return f"[Tool Result: {tool_use_id}] Error: {error_message}"
    
    def format_tool_use_response(self, tool_use: Dict[str, Any]) -> str:
        """
        将助手的 tool_use 格式化为上下文字符串
        
        用于在对话上下文中表示助手之前的工具调用
        
        Args:
            tool_use: Anthropic tool_use 内容块，包含：
                - id: tool_use ID
                - name: 工具名称
                - input: 工具输入参数
                
        Returns:
            用于模型上下文的格式化字符串
        """
        tool_use_id = tool_use.get("id", "unknown")
        tool_name = tool_use.get("name", "unknown")
        tool_input = tool_use.get("input", {})
        
        # 将输入参数格式化为 JSON
        input_json = json.dumps(tool_input, ensure_ascii=False)
        
        return f"[Tool Call: {tool_name} ({tool_use_id})]\n{input_json}"
    
    def format_multiple_tool_results(
        self, 
        tool_results: List[Dict[str, Any]]
    ) -> str:
        """
        格式化多个工具执行结果
        
        Args:
            tool_results: tool_result 内容块列表
            
        Returns:
            格式化的多个结果字符串，用换行符分隔
        """
        if not tool_results:
            return ""
        
        formatted_results = [
            self.format_tool_result(result) 
            for result in tool_results
        ]
        return "\n\n".join(formatted_results)
    
    def format_multiple_tool_uses(
        self, 
        tool_uses: List[Dict[str, Any]]
    ) -> str:
        """
        格式化多个工具调用
        
        Args:
            tool_uses: tool_use 内容块列表
            
        Returns:
            格式化的多个工具调用字符串，用换行符分隔
        """
        if not tool_uses:
            return ""
        
        formatted_uses = [
            self.format_tool_use_response(use) 
            for use in tool_uses
        ]
        return "\n\n".join(formatted_uses)
    
    def extract_tool_results_from_message(
        self, 
        message: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        从消息中提取 tool_result 内容块
        
        Args:
            message: Anthropic 格式的消息，包含 content 字段
            
        Returns:
            tool_result 内容块列表
        """
        content = message.get("content", [])
        
        if isinstance(content, str):
            return []
        
        if isinstance(content, list):
            return [
                block for block in content
                if isinstance(block, dict) and block.get("type") == "tool_result"
            ]
        
        return []
    
    def extract_tool_uses_from_message(
        self, 
        message: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        从消息中提取 tool_use 内容块
        
        Args:
            message: Anthropic 格式的消息，包含 content 字段
            
        Returns:
            tool_use 内容块列表
        """
        content = message.get("content", [])
        
        if isinstance(content, str):
            return []
        
        if isinstance(content, list):
            return [
                block for block in content
                if isinstance(block, dict) and block.get("type") == "tool_use"
            ]
        
        return []
