"""
Tool Context Builder Service
工具上下文构建器服务，重建包含 tool_use/tool_result 的对话上下文

Requirements: 6.1, 6.2, 6.3, 6.5
"""

from typing import Dict, Any, List, Optional
import logging
import copy

from .tool_result_formatter import ToolResultFormatter

logger = logging.getLogger(__name__)


class ToolContextBuilder:
    """
    工具上下文构建器
    
    重建包含 tool_use/tool_result 的对话上下文，保持正确的消息顺序，
    支持多轮工具使用对话。
    
    对话顺序：用户消息 → 助手 tool_use → 用户 tool_result → 助手响应
    """
    
    def __init__(self, formatter: Optional[ToolResultFormatter] = None):
        """
        初始化工具上下文构建器
        
        Args:
            formatter: 工具结果格式化器实例，如果为 None 则创建新实例
        """
        self._formatter = formatter or ToolResultFormatter()
    
    def build_context(
        self,
        messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        重建包含 tool_use/tool_result 的对话上下文
        
        处理 Anthropic 格式的消息列表，将 tool_use 和 tool_result 内容块
        转换为模型能够理解的文本格式，同时保持正确的消息顺序。
        
        Args:
            messages: Anthropic 格式的消息列表，每个消息包含：
                - role: "user" 或 "assistant"
                - content: 字符串或内容块列表
                
        Returns:
            转换后的消息列表，tool_use/tool_result 被转换为文本格式
        """
        if not messages:
            return []
        
        result = []
        
        for message in messages:
            converted_message = self._convert_message(message)
            if converted_message:
                result.append(converted_message)
        
        return result
    
    def _convert_message(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        转换单个消息
        
        Args:
            message: Anthropic 格式的消息
            
        Returns:
            转换后的消息，如果消息无效则返回 None
        """
        role = message.get("role")
        content = message.get("content")
        
        if not role:
            logger.warning("Message missing 'role' field")
            return None
        
        # 处理字符串内容（简单文本消息）
        if isinstance(content, str):
            return {"role": role, "content": content}
        
        # 处理内容块列表
        if isinstance(content, list):
            converted_content = self._convert_content_blocks(content, role)
            if converted_content:
                return {"role": role, "content": converted_content}
            return None
        
        # 内容为空或无效
        if content is None:
            return {"role": role, "content": ""}
        
        logger.warning(f"Unexpected content type: {type(content)}")
        return {"role": role, "content": str(content)}
    
    def _convert_content_blocks(
        self,
        content_blocks: List[Dict[str, Any]],
        role: str
    ) -> str:
        """
        转换内容块列表为文本
        
        Args:
            content_blocks: 内容块列表
            role: 消息角色 ("user" 或 "assistant")
            
        Returns:
            转换后的文本内容
        """
        parts = []
        has_tool_use = role == "assistant" and any(
            isinstance(block, dict) and block.get("type") == "tool_use"
            for block in content_blocks
        )
        
        for block in content_blocks:
            if not isinstance(block, dict):
                # 如果块是字符串，直接添加
                if isinstance(block, str):
                    parts.append(block)
                continue
            
            block_type = block.get("type")
            
            if block_type == "text":
                # 文本块
                text = block.get("text", "")
                # assistant mixed text + tool_use 常带有临时草稿；保留 tool_use，抑制这类前置文本。
                if text and not has_tool_use:
                    parts.append(text)
            
            elif block_type == "tool_use":
                # 助手的工具调用
                formatted = self._formatter.format_tool_use_response(block)
                parts.append(formatted)
            
            elif block_type == "tool_result":
                # 用户的工具执行结果
                formatted = self._formatter.format_tool_result(block)
                parts.append(formatted)
            
            else:
                # 其他类型的块，尝试提取文本
                if "text" in block:
                    parts.append(block["text"])
                else:
                    logger.debug(f"Unknown content block type: {block_type}")
        
        return "\n\n".join(parts)
    
    def extract_tool_use_ids(
        self,
        messages: List[Dict[str, Any]]
    ) -> List[str]:
        """
        从消息列表中提取所有 tool_use ID
        
        用于跟踪对话中的工具调用
        
        Args:
            messages: Anthropic 格式的消息列表
            
        Returns:
            tool_use ID 列表
        """
        tool_use_ids = []
        
        for message in messages:
            content = message.get("content", [])
            
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "tool_use":
                            tool_id = block.get("id")
                            if tool_id:
                                tool_use_ids.append(tool_id)
        
        return tool_use_ids
    
    def extract_tool_result_ids(
        self,
        messages: List[Dict[str, Any]]
    ) -> List[str]:
        """
        从消息列表中提取所有 tool_result 对应的 tool_use_id
        
        Args:
            messages: Anthropic 格式的消息列表
            
        Returns:
            tool_use_id 列表
        """
        tool_result_ids = []
        
        for message in messages:
            content = message.get("content", [])
            
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "tool_result":
                            tool_id = block.get("tool_use_id")
                            if tool_id:
                                tool_result_ids.append(tool_id)
        
        return tool_result_ids
    
    def validate_tool_conversation(
        self,
        messages: List[Dict[str, Any]]
    ) -> bool:
        """
        验证工具使用对话的完整性
        
        检查每个 tool_use 是否有对应的 tool_result
        
        Args:
            messages: Anthropic 格式的消息列表
            
        Returns:
            对话是否完整（所有 tool_use 都有对应的 tool_result）
        """
        tool_use_ids = set(self.extract_tool_use_ids(messages))
        tool_result_ids = set(self.extract_tool_result_ids(messages))
        
        # 检查是否所有 tool_use 都有对应的 tool_result
        # 注意：最后一个 tool_use 可能还没有 tool_result（正在等待执行）
        # 所以我们只检查 tool_result 是否都有对应的 tool_use
        return tool_result_ids.issubset(tool_use_ids)
    
    def get_pending_tool_uses(
        self,
        messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        获取尚未有 tool_result 的 tool_use
        
        Args:
            messages: Anthropic 格式的消息列表
            
        Returns:
            待处理的 tool_use 块列表
        """
        tool_result_ids = set(self.extract_tool_result_ids(messages))
        pending = []
        
        for message in messages:
            content = message.get("content", [])
            
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "tool_use":
                            tool_id = block.get("id")
                            if tool_id and tool_id not in tool_result_ids:
                                pending.append(block)
        
        return pending
    
    def build_context_with_tool_history(
        self,
        messages: List[Dict[str, Any]],
        include_pending: bool = True
    ) -> List[Dict[str, Any]]:
        """
        构建包含完整工具历史的上下文
        
        这是 build_context 的增强版本，提供更详细的工具使用历史信息
        
        Args:
            messages: Anthropic 格式的消息列表
            include_pending: 是否包含待处理的 tool_use
            
        Returns:
            转换后的消息列表
        """
        result = self.build_context(messages)
        
        if include_pending:
            pending = self.get_pending_tool_uses(messages)
            if pending:
                # 添加待处理工具调用的提示
                pending_info = self._format_pending_tool_uses(pending)
                if result and result[-1]["role"] == "assistant":
                    # 如果最后一条是助手消息，追加到其中
                    result[-1]["content"] += f"\n\n{pending_info}"
                else:
                    # 否则添加新的助手消息
                    result.append({
                        "role": "assistant",
                        "content": pending_info
                    })
        
        return result
    
    def _format_pending_tool_uses(
        self,
        pending_tool_uses: List[Dict[str, Any]]
    ) -> str:
        """
        格式化待处理的工具调用
        
        Args:
            pending_tool_uses: 待处理的 tool_use 块列表
            
        Returns:
            格式化的字符串
        """
        formatted = self._formatter.format_multiple_tool_uses(pending_tool_uses)
        return f"[Pending tool calls awaiting results]\n{formatted}"
    
    def merge_tool_results_into_context(
        self,
        context: List[Dict[str, Any]],
        tool_results: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        将工具执行结果合并到上下文中
        
        Args:
            context: 现有的对话上下文
            tool_results: 要合并的 tool_result 块列表
            
        Returns:
            合并后的上下文
        """
        if not tool_results:
            return context
        
        result = copy.deepcopy(context)
        
        # 格式化工具结果
        formatted_results = self._formatter.format_multiple_tool_results(tool_results)
        
        # 添加为用户消息
        result.append({
            "role": "user",
            "content": formatted_results
        })
        
        return result
