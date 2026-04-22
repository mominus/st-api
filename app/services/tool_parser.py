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

    # 匹配 Claude Code 常见的 WebSearch 函数式调用
    # 示例: WebSearch("latest ai news")
    LEGACY_WEBSEARCH_CALL_PATTERN = re.compile(
        r"\bWeb\s*Search\s*\(\s*(?P<query>\"[^\"\n]*\"|'[^'\n]*'|[^)\n]+?)\s*\)",
        re.IGNORECASE,
    )

    # 匹配 Claude Code WebSearch 回退语句
    # 示例: Perform a web search for the query: today news
    LEGACY_WEBSEARCH_PHRASE_PATTERN = re.compile(
        r"^\s*(?:[-*]\s*)?Perform a web search for the query:\s*(?P<query>.+?)\s*$",
        re.IGNORECASE | re.MULTILINE,
    )

    # 匹配裸 JSON tool call 开始位置
    # 示例:
    # {"tool":"Write","arguments":{"file_path":"a","content":"b"}}
    PLAIN_TOOL_JSON_START_PATTERN = re.compile(
        r'(^|\n)\s*(\{\s*"tool")',
        re.IGNORECASE,
    )

    # 匹配顶层数组形式的工具调用
    # 示例:
    # [
    #   {"tool":"TaskCreate","arguments":{...}},
    #   {"tool":"TaskCreate","arguments":{...}}
    # ]
    PLAIN_TOOL_JSON_ARRAY_START_PATTERN = re.compile(
        r'(^|\n)\s*(\[\s*\{\s*"tool")',
        re.IGNORECASE,
    )

    JSON_STRING_ESCAPE_CHARS = frozenset('"\\/bfnrtu')
    
    # 工具调用 ID 前缀
    TOOL_USE_ID_PREFIX = "toolu_"
    
    def __init__(
        self,
        registry: Optional[ToolRegistry] = None,
        *,
        allow_unknown_tools: bool = True,
    ):
        """
        初始化工具解析器
        
        Args:
            registry: 工具注册表实例，用于验证工具调用
            allow_unknown_tools: 当 registry 存在但工具名未注册时，是否仍允许通过
        """
        self._registry = registry
        self._allow_unknown_tools = bool(allow_unknown_tools)
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

        # 然后尝试 Claude Code WebSearch 回退格式
        legacy_websearch_result = self._parse_legacy_websearch_format(content)
        if legacy_websearch_result.has_tool_calls:
            logger.debug("Parsed WebSearch tool call from legacy fallback format")
            return legacy_websearch_result
        
        # 然后尝试 JSON 代码块格式
        json_result = self._parse_json_format(content)
        if json_result.has_tool_calls:
            logger.debug(f"Parsed {len(json_result.tool_calls)} tool calls from JSON format")
            return json_result

        # 最后尝试裸 JSON tool call
        plain_json_result = self._parse_plain_json_format(content)
        if plain_json_result.has_tool_calls:
            logger.debug(f"Parsed {len(plain_json_result.tool_calls)} tool calls from plain JSON format")
            return plain_json_result
        
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
            arguments = self.load_jsonish(json_content) if json_content else {}
            if not isinstance(arguments, dict):
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
                decoded = self.load_jsonish_prefix(stripped_segment)
                if decoded is not None:
                    parsed_obj, consumed_len = decoded
                    if isinstance(parsed_obj, dict):
                        arguments = parsed_obj
                    trailing = stripped_segment[consumed_len:]
                else:
                    # 兼容 WebSearch 非 JSON 参数写法:
                    # [tool_call ... name=WebSearch]
                    # Perform a web search for the query: ...
                    legacy_query = self._extract_legacy_websearch_query(stripped_segment)
                    if tool_name.lower() == "websearch" and legacy_query:
                        arguments = {"query": legacy_query}
                        trailing = ""
                    else:
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

    def _parse_legacy_websearch_format(self, content: str) -> ParseResult:
        """
        解析 Claude Code WebSearch 常见回退格式。

        支持:
        1) WebSearch("query") / Web Search("query")
        2) Perform a web search for the query: query text
        """
        result = ParseResult()

        call_match = self.LEGACY_WEBSEARCH_CALL_PATTERN.search(content)
        phrase_match = self.LEGACY_WEBSEARCH_PHRASE_PATTERN.search(content)

        picked = None
        if call_match and phrase_match:
            picked = call_match if call_match.start() <= phrase_match.start() else phrase_match
        else:
            picked = call_match or phrase_match

        if not picked:
            return result

        query = self._normalize_legacy_query(picked.group("query"))
        if not query:
            return result

        parsed_call = ParsedToolCall(
            tool_name="WebSearch",
            arguments={"query": query},
            raw_json=json.dumps({"query": query}, ensure_ascii=False, separators=(",", ":")),
        )

        if not self._is_valid_tool_call(parsed_call):
            return result

        result.text_before = content[:picked.start()].rstrip()
        result.tool_calls = [parsed_call]
        result.has_tool_calls = True
        result.text_after = content[picked.end():].lstrip()
        return result

    @staticmethod
    def _normalize_legacy_query(raw_query: str) -> str:
        query = (raw_query or "").strip()
        if len(query) >= 2 and query[0] == query[-1] and query[0] in {"'", '"'}:
            query = query[1:-1].strip()
        return query

    def _extract_legacy_websearch_query(self, content: str) -> Optional[str]:
        if not content:
            return None

        call_match = self.LEGACY_WEBSEARCH_CALL_PATTERN.search(content)
        if call_match:
            query = self._normalize_legacy_query(call_match.group("query"))
            if query:
                return query

        phrase_match = self.LEGACY_WEBSEARCH_PHRASE_PATTERN.search(content)
        if phrase_match:
            query = self._normalize_legacy_query(phrase_match.group("query"))
            if query:
                return query

        return None

    @classmethod
    def load_jsonish(cls, content: str) -> Optional[Any]:
        """
        宽容解析 JSON-like 文本。

        Claude Code 在 Write/Edit 等工具参数中偶尔会输出未转义的引号或反斜杠，
        这里先尝试严格 JSON，再做最小修复后重试，避免工具调用外泄。
        """
        stripped = (content or "").strip()
        if not stripped:
            return None

        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            repaired = cls._repair_jsonish(stripped)
            if repaired == stripped:
                return None
            try:
                return json.loads(repaired)
            except json.JSONDecodeError as exc:
                logger.debug("Failed to repair malformed JSON-like tool payload: %s", exc)
                return None

    @classmethod
    def load_jsonish_prefix(cls, content: str) -> Optional[Tuple[Any, int]]:
        """
        解析开头处的完整 JSON-like 对象，并返回消费长度。
        """
        if not content:
            return None

        end_idx = cls._find_complete_jsonish_prefix_end(content)
        if end_idx is None:
            return None

        parsed = cls.load_jsonish(content[:end_idx])
        if parsed is None:
            return None

        consumed_len = end_idx
        while consumed_len < len(content) and content[consumed_len] in " \t\r\n":
            consumed_len += 1
        return parsed, consumed_len

    @classmethod
    def _find_complete_jsonish_prefix_end(cls, content: str) -> Optional[int]:
        if not content or content[0] not in "{[":
            return None

        depth = 0
        in_string = False
        escape_next = False

        for idx, ch in enumerate(content):
            if in_string:
                if escape_next:
                    escape_next = False
                    continue

                if ch == "\\":
                    if cls._is_valid_json_escape(content, idx):
                        escape_next = True
                    continue

                if ch == '"':
                    if cls._quote_terminates_json_string(content, idx):
                        in_string = False
                    continue

                continue

            if ch == '"':
                in_string = True
                continue

            if ch in "{[":
                depth += 1
                continue

            if ch in "}]":
                depth -= 1
                if depth == 0:
                    return idx + 1
                if depth < 0:
                    return None

        return None

    @classmethod
    def _repair_jsonish(cls, content: str) -> str:
        if not content:
            return content

        repaired: List[str] = []
        in_string = False
        escape_next = False

        for idx, ch in enumerate(content):
            if in_string:
                if escape_next:
                    repaired.append(ch)
                    escape_next = False
                    continue

                if ch == "\\":
                    if cls._is_valid_json_escape(content, idx):
                        repaired.append(ch)
                        escape_next = True
                    else:
                        repaired.append("\\\\")
                    continue

                if ch == '"':
                    if cls._quote_terminates_json_string(content, idx):
                        repaired.append(ch)
                        in_string = False
                    else:
                        repaired.append('\\"')
                    continue

                if ch == "\n":
                    repaired.append("\\n")
                    continue

                if ch == "\r":
                    repaired.append("\\r")
                    continue

                if ch == "\t":
                    repaired.append("\\t")
                    continue

                repaired.append(ch)
                continue

            repaired.append(ch)
            if ch == '"':
                in_string = True

        if in_string:
            repaired.append('"')

        return "".join(repaired)

    @classmethod
    def _quote_terminates_json_string(cls, content: str, quote_idx: int) -> bool:
        next_char = cls._next_non_whitespace_char(content, quote_idx + 1)
        return next_char is None or next_char in ",:}]"

    @classmethod
    def _is_valid_json_escape(cls, content: str, slash_idx: int) -> bool:
        next_idx = slash_idx + 1
        return next_idx < len(content) and content[next_idx] in cls.JSON_STRING_ESCAPE_CHARS

    @staticmethod
    def _next_non_whitespace_char(content: str, start_idx: int) -> Optional[str]:
        idx = start_idx
        while idx < len(content) and content[idx] in " \t\r\n":
            idx += 1
        if idx >= len(content):
            return None
        return content[idx]
    
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
            parsed_calls = self._parse_json_block(json_str)
            
            if parsed_calls:
                tool_calls.extend(parsed_calls)
            else:
                if self._looks_like_tool_json_block(json_str):
                    logger.warning(f"Failed to parse JSON block: {json_str}")
                else:
                    logger.debug("Ignoring non-tool JSON block in model output")
            
            last_end = match.end()
        
        # 处理最后一个匹配之后的文本
        result.text_after = content[last_end:].lstrip()
        
        result.tool_calls = tool_calls
        result.has_tool_calls = len(tool_calls) > 0
        
        return result

    @staticmethod
    def _looks_like_tool_json_block(json_str: str) -> bool:
        """
        判断 JSON 代码块是否“看起来像”工具调用。

        非工具 JSON（如标题/元信息块）不应打印 warning，避免日志噪音。
        """
        compact = (json_str or "").strip()
        if not compact:
            return False
        lowered = compact.lower()
        return (
            '"tool"' in lowered
            or "'tool'" in lowered
            or '"arguments"' in lowered
            or "'arguments'" in lowered
            or "[tool_call" in lowered
            or "<tool_use" in lowered
        )

    def _parse_plain_json_format(self, content: str) -> ParseResult:
        """
        解析裸 JSON tool call。

        支持模型直接输出单个 JSON 对象或 JSON 数组而不包裹 ```json 代码块。
        """
        result = ParseResult()
        if not content:
            return result

        start_candidates = []
        array_match = self.PLAIN_TOOL_JSON_ARRAY_START_PATTERN.search(content)
        if array_match is not None:
            start_candidates.append(array_match.start(2))
        object_match = self.PLAIN_TOOL_JSON_START_PATTERN.search(content)
        if object_match is not None:
            start_candidates.append(object_match.start(2))

        if not start_candidates:
            return result

        start_pos = min(start_candidates)
        trailing = content[start_pos:]

        decoded = self.load_jsonish_prefix(trailing)
        if decoded is None:
            return result
        parsed_obj, end_idx = decoded

        parsed_calls = self.extract_tool_calls_from_jsonish(
            parsed_obj,
            raw_json=json.dumps(parsed_obj, ensure_ascii=False, separators=(",", ":")),
        )
        if not parsed_calls:
            return result

        consumed_end = start_pos + end_idx
        while consumed_end < len(content) and content[consumed_end] in " \t\r\n":
            consumed_end += 1

        result.text_before = content[:start_pos].rstrip()
        result.tool_calls = parsed_calls
        result.has_tool_calls = True
        result.text_after = content[consumed_end:].lstrip()
        return result
    
    def _parse_json_block(self, json_str: str) -> List[ParsedToolCall]:
        """
        解析单个 JSON 代码块，可返回一个或多个工具调用。
        
        Args:
            json_str: JSON 字符串
            
        Returns:
            解析出的工具调用列表
        """
        try:
            data = self.load_jsonish(json_str)
            return self.extract_tool_calls_from_jsonish(data, raw_json=json_str)

        except Exception as e:
            logger.debug(f"Error parsing JSON block: {e}")
            return []

    def extract_tool_calls_from_jsonish(
        self,
        data: Any,
        *,
        raw_json: str = "",
    ) -> List[ParsedToolCall]:
        """
        从已解析的 JSON-like 结构中提取一个或多个工具调用。

        支持:
        1. 单个对象: {"tool":"Write","arguments":{...}}
        2. 对象数组: [{"tool":"TaskCreate","arguments":{...}}, ...]
        """
        if isinstance(data, dict):
            parsed_call = self._build_tool_call_from_mapping(data, raw_json=raw_json)
            if parsed_call is None or not self._is_valid_tool_call(parsed_call):
                return []
            return [parsed_call]

        if isinstance(data, list):
            if not data:
                return []

            tool_calls: List[ParsedToolCall] = []
            for item in data:
                if not isinstance(item, dict):
                    return []
                item_json = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                parsed_call = self._build_tool_call_from_mapping(item, raw_json=item_json)
                if parsed_call is None or not self._is_valid_tool_call(parsed_call):
                    return []
                tool_calls.append(parsed_call)
            return tool_calls

        return []

    @staticmethod
    def _build_tool_call_from_mapping(
        data: Dict[str, Any],
        *,
        raw_json: str,
    ) -> Optional[ParsedToolCall]:
        tool_name = data.get("tool")
        if not tool_name or not isinstance(tool_name, str):
            return None

        arguments = data.get("arguments", {})
        if not isinstance(arguments, dict):
            arguments = {}

        return ParsedToolCall(
            tool_name=tool_name,
            arguments=arguments,
            raw_json=raw_json,
        )
    
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
        2. 如果工具不在注册表中，则按 allow_unknown_tools 决定是否允许
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
            if self._allow_unknown_tools:
                # 宽松模式下允许未声明工具，兼容自定义工具场景。
                logger.debug(f"Tool '{tool_call.tool_name}' not in registry, allowing anyway")
                return True
            logger.debug(f"Tool '{tool_call.tool_name}' not in registry, rejecting in strict mode")
            return False
        
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
