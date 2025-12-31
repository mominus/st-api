"""
Tool Injector Service
工具注入器服务，将工具定义注入到 Prompt 中

Requirements: 2.1, 2.2, 2.3, 2.4, 2.5
"""

from typing import List, Dict, Any, Optional
import logging
import json

from .tool_registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)


class ToolInjector:
    """
    工具注入器
    
    将工具定义注入到系统提示词中，使模型知道哪些工具可用以及如何调用它们
    格式与 Claude Code 的 system prompt 保持一致
    """
    
    # 工具定义指示符列表
    TOOL_INDICATORS = [
        "# Available Tools",
        "## Available Tools",
        "# Tools",
        "## Tools",
        "To use a tool, output a JSON code block",
        '{"tool":',
        "```json\n{\"tool\":",
    ]
    
    def __init__(self, registry: ToolRegistry):
        """
        初始化工具注入器
        
        Args:
            registry: 工具注册表实例
        """
        self._registry = registry
    
    def should_inject(self, system_prompt: Optional[str]) -> bool:
        """
        检测是否需要注入工具定义
        
        如果 system_prompt 已经包含工具定义，则不需要注入
        
        Args:
            system_prompt: 系统提示词
            
        Returns:
            是否需要注入工具定义
        """
        if not system_prompt:
            return True
        
        # 检查是否包含任何工具定义指示符
        for indicator in self.TOOL_INDICATORS:
            if indicator in system_prompt:
                return False
        
        return True
    
    def inject_tools(
        self,
        system_prompt: Optional[str],
        tools: Optional[List[Dict[str, Any]]] = None
    ) -> str:
        """
        将工具定义注入到系统提示词中
        
        Args:
            system_prompt: 原始系统提示词，可以为 None
            tools: Anthropic 格式的工具定义列表，如果为 None 则使用注册表中的工具
            
        Returns:
            带有工具指令的修改后的系统提示词
        """
        # 获取要注入的工具
        if tools is not None:
            # 从 Anthropic 格式转换为 ToolSchema
            tool_schemas = self._convert_anthropic_tools(tools)
        else:
            # 使用注册表中已启用的工具
            tool_schemas = self._registry.get_enabled_tools()
        
        # 如果没有工具，直接返回原始提示词
        if not tool_schemas:
            return system_prompt or ""
        
        # 构建工具注入块
        tool_injection = self._build_tool_injection_block(tool_schemas)
        
        # 合并原始提示词和工具注入块
        if system_prompt:
            return f"{system_prompt}\n\n{tool_injection}"
        else:
            return tool_injection
    
    def _convert_anthropic_tools(self, tools: List[Dict[str, Any]]) -> List[ToolSchema]:
        """
        将 Anthropic 格式的工具定义转换为 ToolSchema
        
        Args:
            tools: Anthropic 格式的工具定义列表
            
        Returns:
            ToolSchema 列表
        """
        result = []
        for tool in tools:
            name = tool.get("name", "")
            description = tool.get("description", "")
            input_schema = tool.get("input_schema", {})
            
            if name:
                result.append(ToolSchema(
                    name=name,
                    description=description,
                    input_schema=input_schema
                ))
        return result
    
    def _build_tool_injection_block(self, tools: List[ToolSchema]) -> str:
        """
        构建工具注入块 - 使用简洁格式
        
        注意：不使用 <TOOL_DEFINITIONS> 等自定义 XML 标签，
        因为这些标签可能被 Claude 误解为注入尝试。
        
        Args:
            tools: 工具 Schema 列表
            
        Returns:
            格式化的工具注入块字符串
        """
        parts = []
        
        tool_names = [t.name for t in tools]
        
        # 使用简洁的 Markdown 格式
        parts.append("# Available Tools")
        parts.append("")
        parts.append(f"You have access to the following tools: {', '.join(tool_names)}")
        parts.append("")
        parts.append("To use a tool, output a JSON code block:")
        parts.append('```json')
        parts.append('{"tool": "ToolName", "arguments": {"param": "value"}}')
        parts.append('```')
        parts.append("")
        parts.append("## Tool Definitions")
        parts.append("")
        
        # 添加工具定义
        for tool in tools:
            parts.append(self._format_single_tool(tool))
            parts.append("")
        
        return "\n".join(parts)
    
    def format_tool_definitions(self, tools: List[ToolSchema]) -> str:
        """
        将工具定义格式化为指令块
        
        Args:
            tools: 工具 Schema 列表
            
        Returns:
            格式化的工具定义字符串
        """
        if not tools:
            return ""
        
        definitions = []
        for tool in tools:
            tool_def = self._format_single_tool(tool)
            definitions.append(tool_def)
        
        return "\n\n".join(definitions)
    
    def _format_single_tool(self, tool: ToolSchema) -> str:
        """
        格式化单个工具定义 - 使用与 Claude Code 相同的格式
        
        Args:
            tool: 工具 Schema
            
        Returns:
            格式化的工具定义字符串
        """
        lines = []
        # 使用 ## 作为工具名称标题（与 Claude Code 一致）
        lines.append(f"## {tool.name}")
        # 描述直接跟在标题后面
        lines.append(tool.description)
        
        # 格式化参数
        if tool.input_schema:
            properties = tool.input_schema.get("properties", {})
            required = tool.input_schema.get("required", [])
            
            if properties:
                lines.append("")
                lines.append("Parameters:")
                
                for param_name, param_info in properties.items():
                    param_type = param_info.get("type", "any")
                    param_desc = param_info.get("description", "")
                    is_required = param_name in required
                    required_marker = ", required" if is_required else ", optional"
                    
                    lines.append(f"- {param_name} ({param_type}{required_marker}): {param_desc}")
        
        return "\n".join(lines)
    
    def get_tool_call_instructions(self) -> str:
        """
        获取模型应如何输出工具调用的指令
        
        Returns:
            工具调用指令字符串
        """
        return '''To use a tool, output a JSON code block with the tool name and arguments:

```json
{"tool": "ToolName", "arguments": {"param1": "value1"}}
```'''

