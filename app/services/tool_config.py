"""
Tool Use Configuration Service
工具使用功能的配置管理

Requirements: 8.1, 8.2, 8.3, 8.4, 8.5
"""

import os
from dataclasses import dataclass, field
from typing import List, Set, Optional
import logging

logger = logging.getLogger(__name__)


# 默认启用的工具列表
DEFAULT_ENABLED_TOOLS = [
    "Read",
    "Write", 
    "Edit",
    "Bash",
    "Grep",
    "Glob",
    "Diff",
    "LS",
]


@dataclass
class ToolUseConfig:
    """
    工具使用配置
    
    Attributes:
        enable_tool_use: 是否启用工具使用功能
        enabled_tools: 启用的工具名称列表
    """
    enable_tool_use: bool = True
    enabled_tools: List[str] = field(default_factory=lambda: DEFAULT_ENABLED_TOOLS.copy())
    
    def is_tool_enabled(self, tool_name: str) -> bool:
        """
        检查指定工具是否启用
        
        Args:
            tool_name: 工具名称
            
        Returns:
            工具是否启用
        """
        if not self.enable_tool_use:
            return False
        return tool_name in self.enabled_tools
    
    def get_enabled_tools_set(self) -> Set[str]:
        """
        获取启用的工具名称集合
        
        Returns:
            启用的工具名称集合
        """
        if not self.enable_tool_use:
            return set()
        return set(self.enabled_tools)


class ToolConfigService:
    """
    工具配置服务
    
    从环境变量加载工具使用相关配置，提供工具过滤逻辑
    """
    
    def __init__(self):
        """初始化工具配置服务"""
        self._config: ToolUseConfig = ToolUseConfig()
        self.load_from_env()
    
    @property
    def config(self) -> ToolUseConfig:
        """获取当前配置"""
        return self._config
    
    @property
    def enable_tool_use(self) -> bool:
        """是否启用工具使用功能"""
        return self._config.enable_tool_use
    
    @property
    def enabled_tools(self) -> List[str]:
        """获取启用的工具列表"""
        return self._config.enabled_tools
    
    def load_from_env(self) -> None:
        """
        从环境变量加载配置
        
        环境变量:
            ENABLE_TOOL_USE: 是否启用工具使用功能 (true/false)，默认 true
            ENABLED_TOOLS: 启用的工具列表 (逗号分隔)，默认所有工具
        """
        # 读取 ENABLE_TOOL_USE 环境变量
        enable_tool_use_str = os.getenv("ENABLE_TOOL_USE", "true").lower()
        self._config.enable_tool_use = enable_tool_use_str in ("true", "1", "yes", "on")
        
        # 读取 ENABLED_TOOLS 环境变量
        enabled_tools_str = os.getenv("ENABLED_TOOLS", "")
        if enabled_tools_str.strip():
            # 解析逗号分隔的工具列表
            self._config.enabled_tools = [
                tool.strip() 
                for tool in enabled_tools_str.split(",")
                if tool.strip()
            ]
        else:
            # 使用默认工具列表
            self._config.enabled_tools = DEFAULT_ENABLED_TOOLS.copy()
        
        logger.info(
            f"Tool config loaded: enable_tool_use={self._config.enable_tool_use}, "
            f"enabled_tools={self._config.enabled_tools}"
        )
    
    def is_tool_enabled(self, tool_name: str) -> bool:
        """
        检查指定工具是否启用
        
        Args:
            tool_name: 工具名称
            
        Returns:
            工具是否启用
        """
        return self._config.is_tool_enabled(tool_name)
    
    def filter_tools(self, tool_names: List[str]) -> List[str]:
        """
        过滤工具列表，只返回启用的工具
        
        Args:
            tool_names: 工具名称列表
            
        Returns:
            过滤后的工具名称列表
        """
        if not self._config.enable_tool_use:
            return []
        
        enabled_set = self._config.get_enabled_tools_set()
        return [name for name in tool_names if name in enabled_set]
    
    def should_process_tools(self) -> bool:
        """
        检查是否应该处理工具
        
        当 ENABLE_TOOL_USE 为 false 时返回 False，
        表示应该直接传递请求而不进行工具注入或解析
        
        Returns:
            是否应该处理工具
        """
        return self._config.enable_tool_use
    
    def reload(self) -> None:
        """重新从环境变量加载配置"""
        self.load_from_env()


# 全局工具配置服务实例
_tool_config_service: Optional[ToolConfigService] = None


def get_tool_config_service() -> ToolConfigService:
    """
    获取全局工具配置服务实例
    
    Returns:
        工具配置服务实例
    """
    global _tool_config_service
    if _tool_config_service is None:
        _tool_config_service = ToolConfigService()
    return _tool_config_service


def init_tool_config_service() -> ToolConfigService:
    """
    初始化全局工具配置服务
    
    Returns:
        工具配置服务实例
    """
    global _tool_config_service
    _tool_config_service = ToolConfigService()
    return _tool_config_service


def get_tool_config() -> ToolUseConfig:
    """
    获取当前工具使用配置的快捷方法
    
    Returns:
        工具使用配置
    """
    return get_tool_config_service().config


def is_tool_use_enabled() -> bool:
    """
    检查工具使用功能是否启用的快捷方法
    
    Returns:
        工具使用功能是否启用
    """
    return get_tool_config_service().enable_tool_use


def is_tool_enabled(tool_name: str) -> bool:
    """
    检查指定工具是否启用的快捷方法
    
    Args:
        tool_name: 工具名称
        
    Returns:
        工具是否启用
    """
    return get_tool_config_service().is_tool_enabled(tool_name)
