"""
Tool Registry Service
工具注册表服务，管理工具 Schema 定义和配置

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 1.10
"""

from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Set
import logging
import copy

logger = logging.getLogger(__name__)


@dataclass
class ToolSchema:
    """
    工具 Schema 定义
    
    遵循 Anthropic 工具定义格式，包含 name、description 和 input_schema 字段
    
    Attributes:
        name: 工具名称
        description: 工具描述
        input_schema: JSON Schema 格式的输入参数定义
    """
    name: str
    description: str
    input_schema: Dict[str, Any] = field(default_factory=dict)
    
    def to_anthropic_format(self) -> Dict[str, Any]:
        """
        将工具 Schema 转换为 Anthropic API 格式
        
        Returns:
            Anthropic 工具定义格式的字典
        """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": copy.deepcopy(self.input_schema)
        }


class ToolRegistry:
    """
    工具注册表
    
    管理可用工具的 Schema 定义，支持注册、获取、启用/禁用工具
    """
    
    def __init__(self):
        """初始化工具注册表"""
        self._tools: Dict[str, ToolSchema] = {}
        self._enabled_tools: Set[str] = set()
    
    def register_tool(self, tool: ToolSchema) -> None:
        """
        注册工具 Schema
        
        Args:
            tool: 工具 Schema 定义
        """
        self._tools[tool.name] = tool
        # 默认启用新注册的工具
        self._enabled_tools.add(tool.name)
        logger.debug(f"Registered tool: {tool.name}")
    
    def unregister_tool(self, name: str) -> bool:
        """
        取消注册工具
        
        Args:
            name: 工具名称
            
        Returns:
            是否成功取消注册
        """
        if name in self._tools:
            del self._tools[name]
            self._enabled_tools.discard(name)
            logger.debug(f"Unregistered tool: {name}")
            return True
        return False
    
    def get_tool(self, name: str) -> Optional[ToolSchema]:
        """
        按名称获取工具
        
        Args:
            name: 工具名称
            
        Returns:
            工具 Schema，如果不存在则返回 None
        """
        return self._tools.get(name)
    
    def get_all_tools(self) -> List[ToolSchema]:
        """
        获取所有已注册的工具
        
        Returns:
            所有工具 Schema 列表
        """
        return list(self._tools.values())
    
    def get_enabled_tools(self) -> List[ToolSchema]:
        """
        获取所有已启用的工具
        
        Returns:
            已启用的工具 Schema 列表
        """
        return [
            tool for name, tool in self._tools.items()
            if name in self._enabled_tools
        ]
    
    def enable_tool(self, name: str) -> bool:
        """
        启用工具
        
        Args:
            name: 工具名称
            
        Returns:
            是否成功启用（工具必须已注册）
        """
        if name in self._tools:
            self._enabled_tools.add(name)
            logger.debug(f"Enabled tool: {name}")
            return True
        return False
    
    def disable_tool(self, name: str) -> bool:
        """
        禁用工具
        
        Args:
            name: 工具名称
            
        Returns:
            是否成功禁用
        """
        if name in self._enabled_tools:
            self._enabled_tools.discard(name)
            logger.debug(f"Disabled tool: {name}")
            return True
        return False
    
    def is_tool_enabled(self, name: str) -> bool:
        """
        检查工具是否已启用
        
        Args:
            name: 工具名称
            
        Returns:
            工具是否已启用
        """
        return name in self._enabled_tools
    
    def is_tool_registered(self, name: str) -> bool:
        """
        检查工具是否已注册
        
        Args:
            name: 工具名称
            
        Returns:
            工具是否已注册
        """
        return name in self._tools
    
    def set_enabled_tools(self, tool_names: List[str]) -> None:
        """
        设置启用的工具列表
        
        只有已注册的工具才会被启用
        
        Args:
            tool_names: 要启用的工具名称列表
        """
        self._enabled_tools = {
            name for name in tool_names
            if name in self._tools
        }
        logger.debug(f"Set enabled tools: {self._enabled_tools}")
    
    def to_anthropic_format(self) -> List[Dict[str, Any]]:
        """
        将所有已启用的工具转换为 Anthropic API 格式
        
        Returns:
            Anthropic 工具定义格式的列表
        """
        return [
            tool.to_anthropic_format()
            for tool in self.get_enabled_tools()
        ]
    
    def clear(self) -> None:
        """清空所有工具"""
        self._tools.clear()
        self._enabled_tools.clear()
        logger.debug("Cleared all tools")


# ============================================================================
# 预定义的文件操作工具 Schema
# ============================================================================

# Read 工具：读取文件内容
READ_TOOL = ToolSchema(
    name="Read",
    description="读取指定路径文件的内容",
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "要读取的文件路径"
            },
            "offset": {
                "type": "integer",
                "description": "开始读取的行偏移量（可选）"
            },
            "limit": {
                "type": "integer",
                "description": "最大读取行数（可选）"
            }
        },
        "required": ["file_path"]
    }
)

# Write 工具：写入文件内容
WRITE_TOOL = ToolSchema(
    name="Write",
    description="将内容写入指定路径的文件，如果文件不存在则创建",
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "要写入的文件路径"
            },
            "content": {
                "type": "string",
                "description": "要写入的文件内容"
            }
        },
        "required": ["file_path", "content"]
    }
)

# Edit 工具：编辑文件内容
EDIT_TOOL = ToolSchema(
    name="Edit",
    description="编辑文件内容，将指定的旧文本替换为新文本",
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "要编辑的文件路径"
            },
            "old_string": {
                "type": "string",
                "description": "要替换的旧文本（必须精确匹配）"
            },
            "new_string": {
                "type": "string",
                "description": "替换后的新文本"
            }
        },
        "required": ["file_path", "old_string", "new_string"]
    }
)

# Bash 工具：执行 Shell 命令
BASH_TOOL = ToolSchema(
    name="Bash",
    description="执行 Shell 命令并返回输出",
    input_schema={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 Shell 命令"
            },
            "timeout": {
                "type": "integer",
                "description": "命令执行超时时间（秒），可选"
            }
        },
        "required": ["command"]
    }
)

# Grep 工具：搜索文件内容
GREP_TOOL = ToolSchema(
    name="Grep",
    description="在文件或目录中搜索匹配指定模式的内容",
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "要搜索的正则表达式模式"
            },
            "path": {
                "type": "string",
                "description": "要搜索的文件或目录路径"
            },
            "include": {
                "type": "string",
                "description": "文件名匹配模式，如 '*.py'（可选）"
            }
        },
        "required": ["pattern", "path"]
    }
)

# Glob 工具：文件模式匹配
GLOB_TOOL = ToolSchema(
    name="Glob",
    description="使用 glob 模式匹配文件路径",
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "glob 模式，如 '*.py' 或 '**/*.txt'"
            },
            "path": {
                "type": "string",
                "description": "搜索的基础目录路径（可选，默认为当前目录）"
            }
        },
        "required": ["pattern"]
    }
)

# Diff 工具：比较文件差异
DIFF_TOOL = ToolSchema(
    name="Diff",
    description="比较原始内容和修改后内容的差异",
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "文件路径"
            },
            "original": {
                "type": "string",
                "description": "原始内容"
            },
            "modified": {
                "type": "string",
                "description": "修改后的内容"
            }
        },
        "required": ["file_path", "original", "modified"]
    }
)

# LS 工具：列出目录内容
LS_TOOL = ToolSchema(
    name="LS",
    description="列出指定目录的内容",
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要列出内容的目录路径（可选，默认为当前目录）"
            },
            "all": {
                "type": "boolean",
                "description": "是否显示隐藏文件（可选）"
            }
        },
        "required": []
    }
)

# 所有预定义工具列表
DEFAULT_TOOLS = [
    READ_TOOL,
    WRITE_TOOL,
    EDIT_TOOL,
    BASH_TOOL,
    GREP_TOOL,
    GLOB_TOOL,
    DIFF_TOOL,
    LS_TOOL,
]


def create_default_registry() -> ToolRegistry:
    """
    创建包含所有默认工具的注册表
    
    Returns:
        预配置的工具注册表
    """
    registry = ToolRegistry()
    for tool in DEFAULT_TOOLS:
        registry.register_tool(tool)
    return registry


# 全局工具注册表实例
_tool_registry: Optional[ToolRegistry] = None


def get_tool_registry() -> ToolRegistry:
    """
    获取全局工具注册表实例
    
    Returns:
        工具注册表实例
    """
    global _tool_registry
    if _tool_registry is None:
        _tool_registry = create_default_registry()
    return _tool_registry


def init_tool_registry() -> ToolRegistry:
    """
    初始化全局工具注册表
    
    Returns:
        工具注册表实例
    """
    global _tool_registry
    _tool_registry = create_default_registry()
    return _tool_registry
