"""
Tool Parser Service
工具解析器服务，从模型输出中解析工具调用

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6
"""

from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple
import logging
import json
import re
import uuid

from .tool_registry import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass
class ParsedToolCall:
    """
    从模型输出解析的工具调用
    
    Attributes:
        tool_name: 工具名称
        arguments: 工具参数字典
        raw_json: 原始 JSON 字符串
    """
    tool_name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    raw_json: str = ""


@dataclass
class ParseResult:
    """
    解析模型输出的结果
    
    Attributes:
        text_before: 工具调用之前的文本
        tool_calls: 解析出的工具调用列表
        text_after: 工具调用之后的文本
        has_tool_calls: 是否包含工具调用
    """
    text_before: str = ""
    tool_calls: List[ParsedToolCall] = field(default_factory=list)
    text_after: str = ""
    has_tool_calls: bool = False


class ToolParser:
    """
    工具解析器
    
    从模型输出中解析工具调用，支持两种格式：
    1. JSON 代码块格式: ```json {"tool": "...", "arguments": {...}} ```
    2. XML 标签格式: <tool_use id="..." name="...">JSON参数</tool_use>
    """
    
    # 匹配 JSON 代码块的正则表达式
    # 匹配 ```json ... ``` 格式的代码块
    JSON_BLOCK_PATTERN = re.compile(
        r'```json\s*\n(.*?)\n\s*```',
        re.DOTALL
    )
    
    # 匹配 XML tool_use 标签的正则表达式
    # 匹配 <tool_use id="..." name="...">JSON</tool_use> 格式
    XML_TOOL_USE_PATTERN = re.compile(
        r'<tool_use\s+id="([^"]+)"\s+name="([^"]+)">(.*?)</tool_use>',
        re.DOTALL
    )

    # 匹配 bracket 工具调用头
    # 匹配 [tool_call id=... name=ToolName] 格式
    BRACKET_TOOL_CALL_HEADER_PATTERN = re.compile(
        r"\[tool_call\s+id=([^\s\]]+)\s+name=([^\]]+)\]\s*",
        re.IGNORECASE,
    )
    
    # 工具调用 ID 前缀
    TOOL_USE_ID_PREFIX = "toolu_"
    
    def __init__(self, registry: Optional[ToolRegistry] = None):
        """
        初始化工具解析器
        
        Args:
            registry: 工具注册表实例，用于验证工具调用
        """
        self._registry = registry
        self._generated_ids: set = set()
    
    def parse(self, content: str) -> ParseResult:
        """
        解析模型输出中的工具调用
        
        支持两种格式：
        1. JSON 代码块: ```json {"tool": "ToolName", "arguments": {...}} ```
        2. XML 标签: <tool_use id="..." name="ToolName">JSON参数</tool_use>
        
        Args:
            content: 模型输出内容
            
        Returns:
            ParseResult 包含解析结果
        """
        if not content:
            return ParseResult()
        
        # 优先尝试 XML 格式（st 返回的格式）
        xml_result = self._parse_xml_format(content)
        if xml_result.has_tool_calls:
            logger.debug(f"Parsed {len(xml_result.tool_calls)} tool calls from XML format")
            return xml_result

        # 然后尝试 bracket 格式
        bracket_result = self._parse_bracket_format(content)
        if bracket_result.has_tool_calls:
            logger.debug(f"Parsed {len(bracket_result.tool_calls)} tool calls from bracket format")
            return bracket_result
        
        # 然后尝试 JSON 代码块格式
        json_result = self._parse_json_format(content)
        if json_result.has_tool_calls:
            logger.debug(f"Parsed {len(json_result.tool_calls)} tool calls from JSON format")
            return json_result
        
        # 没有找到工具调用
        return ParseResult(text_before=content)
    
    def _parse_xml_format(self, content: str) -> ParseResult:
        """
        解析 XML 格式的工具调用
        
        格式: <tool_use id="..." name="ToolName">JSON参数</tool_use>
        
        Args:
            content: 模型输出内容
            
        Returns:
            ParseResult 包含解析结果
        """
        result = ParseResult()
        tool_calls = []
        
        matches = list(self.XML_TOOL_USE_PATTERN.finditer(content))
        
        if not matches:
            return result
        
        # 处理第一个匹配之前的文本
        first_match = matches[0]
        result.text_before = content[:first_match.start()].rstrip()
        
        # 处理每个 XML tool_use 标签
        last_end = first_match.start()
        for match in matches:
            tool_id = match.group(1)
            tool_name = match.group(2)
            json_content = match.group(3).strip()
            
            # 解析 JSON 参数
            try:
                arguments = json.loads(json_content) if json_content else {}
                if not isinstance(arguments, dict):
                    arguments = {}
            except json.JSONDecodeError:
                logger.warning(f"Failed to parse JSON in XML tool_use: {json_content}")
                arguments = {}
            
            parsed_call = ParsedToolCall(
                tool_name=tool_name,
                arguments=arguments,
                raw_json=json_content
            )
            
            # 验证工具调用
            if self._is_valid_tool_call(parsed_call):
                tool_calls.append(parsed_call)
                # 保存原始 ID 以便后续使用
                parsed_call.raw_json = f"xml_id:{tool_id}"
            else:
                logger.warning(f"Invalid XML tool call: {tool_name}")
            
            last_end = match.end()
        
        # 处理最后一个匹配之后的文本
        result.text_after = content[last_end:].lstrip()
        
        result.tool_calls = tool_calls
        result.has_tool_calls = len(tool_calls) > 0
        
        return result

    def _parse_bracket_format(self, content: str) -> ParseResult:
        """
        解析 bracket 格式的工具调用

        格式:
        [tool_call id=toolu_xxx name=ToolName]
        {"arg":"value"}
        """
        result = ParseResult()
        matches = list(self.BRACKET_TOOL_CALL_HEADER_PATTERN.finditer(content))
        if not matches:
            return result

        decoder = json.JSONDecoder()
        tool_calls: List[ParsedToolCall] = []
        result.text_before = content[:matches[0].start()].rstrip()
        trailing_after_last = ""

        for idx, match in enumerate(matches):
            tool_id = match.group(1)
            tool_name = match.group(2).strip()
            next_start = matches[idx + 1].start() if idx + 1 < len(matches) else len(content)

            segment = content[match.end():next_start]
            stripped_segment = segment.lstrip()
            trailing = ""
            arguments: Dict[str, Any] = {}

            if stripped_segment:
                try:
                    parsed_obj, end_idx = decoder.raw_decode(stripped_segment)
                    if isinstance(parsed_obj, dict):
                        arguments = parsed_obj
                    trailing = stripped_segment[end_idx:]
                except json.JSONDecodeError:
                    # 允许参数解析失败，仍保留工具调用本体，避免 tool_call 文本外泄
                    trailing = stripped_segment

            parsed_call = ParsedToolCall(
                tool_name=tool_name,
                arguments=arguments,
                raw_json=json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
            )

            if self._is_valid_tool_call(parsed_call):
                parsed_call.raw_json = f"bracket_id:{tool_id}"
                tool_calls.append(parsed_call)
            else:
                logger.warning(f"Invalid bracket tool call: {tool_name}")

            if idx == len(matches) - 1:
                trailing_after_last = trailing

        result.tool_calls = tool_calls
        result.has_tool_calls = len(tool_calls) > 0
        if trailing_after_last and trailing_after_last.strip():
            result.text_after = trailing_after_last.lstrip()

        return result
    
    def _parse_json_format(self, content: str) -> ParseResult:
        """
        解析 JSON 代码块格式的工具调用
        
        格式: ```json {"tool": "ToolName", "arguments": {...}} ```
        
        Args:
            content: 模型输出内容
            
        Returns:
            ParseResult 包含解析结果
        """
        result = ParseResult()
        tool_calls = []
        
        # 查找所有 JSON 代码块
        matches = list(self.JSON_BLOCK_PATTERN.finditer(content))
        
        if not matches:
            result.text_before = content
            return result
        
        # 处理第一个匹配之前的文本
        first_match = matches[0]
        result.text_before = content[:first_match.start()].rstrip()
        
        # 处理每个 JSON 代码块
        last_end = first_match.start()
        for match in matches:
            json_str = match.group(1).strip()
            
            # 尝试解析 JSON
            parsed_call = self._parse_json_block(json_str)
            
            if parsed_call is not None:
                # 验证工具调用
                if self._is_valid_tool_call(parsed_call):
                    tool_calls.append(parsed_call)
                else:
                    logger.warning(f"Invalid tool call: {json_str}")
            else:
                logger.warning(f"Failed to parse JSON block: {json_str}")
            
            last_end = match.end()
        
        # 处理最后一个匹配之后的文本
        result.text_after = content[last_end:].lstrip()
        
        result.tool_calls = tool_calls
        result.has_tool_calls = len(tool_calls) > 0
        
        return result
    
    def _parse_json_block(self, json_str: str) -> Optional[ParsedToolCall]:
        """
        解析单个 JSON 代码块
        
        Args:
            json_str: JSON 字符串
            
        Returns:
            ParsedToolCall 或 None（如果解析失败）
        """
        try:
            data = json.loads(json_str)
            
            # 检查是否是工具调用格式
            if not isinstance(data, dict):
                return None
            
            # 提取工具名称
            tool_name = data.get("tool")
            if not tool_name or not isinstance(tool_name, str):
                return None
            
            # 提取参数
            arguments = data.get("arguments", {})
            if not isinstance(arguments, dict):
                arguments = {}
            
            return ParsedToolCall(
                tool_name=tool_name,
                arguments=arguments,
                raw_json=json_str
            )
            
        except json.JSONDecodeError as e:
            logger.debug(f"JSON decode error: {e}")
            return None
        except Exception as e:
            logger.debug(f"Error parsing JSON block: {e}")
            return None
    
    def _is_valid_tool_call(self, tool_call: ParsedToolCall) -> bool:
        """
        检查工具调用是否有效
        
        如果没有注册表，只检查基本格式
        如果有注册表，还会验证工具名称和必需参数
        
        Args:
            tool_call: 解析的工具调用
            
        Returns:
            工具调用是否有效
        """
        # 基本检查：工具名称必须非空
        if not tool_call.tool_name or not tool_call.tool_name.strip():
            return False
        
        # 如果没有注册表，只做基本检查
        if self._registry is None:
            return True
        
        # 使用注册表验证
        return self.validate_tool_call(tool_call)
    
    def validate_tool_call(self, tool_call: ParsedToolCall) -> bool:
        """
        根据 Schema 验证工具调用
        
        验证逻辑：
        1. 如果没有注册表，只检查基本格式
        2. 如果工具不在注册表中，仍然允许通过（宽松模式）
        3. 如果工具在注册表中，检查必需参数
        
        Args:
            tool_call: 解析的工具调用
            
        Returns:
            工具调用是否有效
        """
        if self._registry is None:
            return True
        
        # 检查工具是否已注册
        tool_schema = self._registry.get_tool(tool_call.tool_name)
        if tool_schema is None:
            # 工具不在注册表中，但仍然允许通过（宽松模式）
            # 这样可以支持 Claude Code 发送的自定义工具
            logger.debug(f"Tool '{tool_call.tool_name}' not in registry, allowing anyway")
            return True
        
        # 检查必需参数
        input_schema = tool_schema.input_schema
        required_params = input_schema.get("required", [])
        
        for param in required_params:
            if param not in tool_call.arguments:
                logger.warning(
                    f"Missing required parameter '{param}' for tool '{tool_call.tool_name}'"
                )
                return False
        
        return True
    
    def generate_tool_use_id(self) -> str:
        """
        生成唯一的 tool_use_id
        
        格式：toolu_<uuid>
        
        Returns:
            唯一的 tool_use_id
        """
        while True:
            # 生成新的 ID
            unique_id = f"{self.TOOL_USE_ID_PREFIX}{uuid.uuid4().hex[:24]}"
            
            # 确保 ID 唯一
            if unique_id not in self._generated_ids:
                self._generated_ids.add(unique_id)
                return unique_id
    
    def reset_generated_ids(self) -> None:
        """
        重置已生成的 ID 集合
        
        用于测试或新会话开始时
        """
        self._generated_ids.clear()
    
    def parse_and_assign_ids(self, content: str) -> Tuple[ParseResult, List[str]]:
        """
        解析内容并为每个工具调用分配唯一 ID
        
        Args:
            content: 模型输出内容
            
        Returns:
            (ParseResult, tool_use_ids) 元组
        """
        result = self.parse(content)
        tool_use_ids = []
        
        for _ in result.tool_calls:
            tool_use_ids.append(self.generate_tool_use_id())
        
        return result, tool_use_ids
