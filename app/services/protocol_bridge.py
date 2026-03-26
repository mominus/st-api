"""
Unified protocol bridge.

This module normalizes OpenAI / Anthropic / Gemini request shapes into a single
canonical request, and maps model output back into each protocol format.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.services.tool_parser import ToolParser


TEXT_PART_TYPES = {
    "text",
    "input_text",
    "output_text",
}


@dataclass
class CanonicalTool:
    name: str
    description: str = ""
    input_schema: Dict[str, Any] = field(default_factory=dict)
    source_type: str = "function"


@dataclass
class CanonicalToolCall:
    call_id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)

    @property
    def arguments_json(self) -> str:
        return json.dumps(self.arguments or {}, ensure_ascii=False, separators=(",", ":"))


@dataclass
class CanonicalMessage:
    role: str
    content: str


@dataclass
class CanonicalRequest:
    source: str
    model: str
    stream: bool = False
    system_prompt: str = ""
    messages: List[CanonicalMessage] = field(default_factory=list)
    tools: List[CanonicalTool] = field(default_factory=list)
    tool_choice: Any = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    capabilities: List[str] = field(default_factory=list)
    response_language: str = "auto"
    raw: Dict[str, Any] = field(default_factory=dict)

    def input_preview(self) -> str:
        for msg in reversed(self.messages):
            if msg.role == "user" and msg.content.strip():
                return msg.content.strip()[:500]
        if self.messages:
            return self.messages[-1].content.strip()[:500]
        return ""


@dataclass
class ParsedOutput:
    text: str
    tool_calls: List[CanonicalToolCall] = field(default_factory=list)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    @property
    def stop_reason(self) -> str:
        return "tool_use" if self.tool_calls else "end_turn"


@dataclass
class UsageNumbers:
    input_tokens: int
    output_tokens: int
    total_tokens: int


class ProtocolBridge:
    """Normalize provider requests and format provider responses."""

    def __init__(self) -> None:
        self._tool_parser = ToolParser(registry=None)

    # ---------------------------------------------------------------------
    # Key helpers
    # ---------------------------------------------------------------------

    @staticmethod
    def sanitize_api_key(key: str) -> str:
        return key.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")

    def extract_api_key(
        self,
        *,
        authorization: Optional[str] = None,
        x_api_key: Optional[str] = None,
        x_goog_api_key: Optional[str] = None,
        api_key_query: Optional[str] = None,
        key_query: Optional[str] = None,
    ) -> Optional[str]:
        if authorization:
            auth = authorization.strip()
            if auth.lower().startswith("bearer "):
                return self.sanitize_api_key(auth[7:].strip())
            return self.sanitize_api_key(auth)

        if x_api_key:
            return self.sanitize_api_key(x_api_key.strip())

        if x_goog_api_key:
            return self.sanitize_api_key(x_goog_api_key.strip())

        if api_key_query:
            return self.sanitize_api_key(api_key_query.strip())

        if key_query:
            return self.sanitize_api_key(key_query.strip())

        return None

    # ---------------------------------------------------------------------
    # Request parsing
    # ---------------------------------------------------------------------

    def parse_openai_chat(self, payload: Dict[str, Any]) -> CanonicalRequest:
        model = str(payload.get("model") or "").strip()
        if not model:
            raise ValueError("model is required")

        stream = bool(payload.get("stream", False))
        messages_raw = payload.get("messages") or []
        if not isinstance(messages_raw, list):
            raise ValueError("messages must be a list")

        system_lines: List[str] = []
        messages: List[CanonicalMessage] = []

        for item in messages_raw:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "user")
            content = self._flatten_openai_content(item.get("content"))

            # Preserve assistant tool-call history for model continuation.
            tool_calls = item.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                content = self._append_openai_tool_calls_history(content, tool_calls)

            if role == "system":
                if content:
                    system_lines.append(content)
                continue

            if role == "tool":
                tool_id = str(item.get("tool_call_id") or "")
                tool_name = str(item.get("name") or "tool")
                tool_result = f"[tool_result id={tool_id} name={tool_name}]\n{content}".strip()
                messages.append(CanonicalMessage(role="user", content=tool_result))
                continue

            if role not in {"user", "assistant"}:
                role = "user"
            messages.append(CanonicalMessage(role=role, content=content))

        tools, capabilities = self._parse_openai_tools(payload.get("tools"))

        return CanonicalRequest(
            source="openai_chat",
            model=model,
            stream=stream,
            system_prompt="\n\n".join(line for line in system_lines if line),
            messages=messages,
            tools=tools,
            tool_choice=payload.get("tool_choice"),
            temperature=self._to_optional_float(payload.get("temperature")),
            top_p=self._to_optional_float(payload.get("top_p")),
            max_tokens=self._to_optional_int(payload.get("max_tokens")),
            capabilities=capabilities,
            response_language=self._detect_preferred_language(messages),
            raw=payload,
        )

    def parse_openai_responses(self, payload: Dict[str, Any]) -> CanonicalRequest:
        model = str(payload.get("model") or "").strip()
        if not model:
            raise ValueError("model is required")

        stream = bool(payload.get("stream", False))

        instructions = self._flatten_openai_content(payload.get("instructions"))
        system_lines = [instructions] if instructions else []

        messages: List[CanonicalMessage] = []
        input_obj = payload.get("input")
        if isinstance(input_obj, str):
            messages.append(CanonicalMessage(role="user", content=input_obj))
        elif isinstance(input_obj, list):
            for item in input_obj:
                if isinstance(item, str):
                    messages.append(CanonicalMessage(role="user", content=item))
                    continue
                if not isinstance(item, dict):
                    continue

                role = str(item.get("role") or "user")
                content = self._flatten_openai_content(item.get("content"))

                # Responses API can contain structured output input from previous turns.
                if role == "system":
                    if content:
                        system_lines.append(content)
                    continue

                if role not in {"user", "assistant", "tool"}:
                    role = "user"

                if role == "tool":
                    tool_name = str(item.get("name") or "tool")
                    call_id = str(item.get("call_id") or item.get("tool_call_id") or "")
                    tool_result = f"[tool_result id={call_id} name={tool_name}]\n{content}".strip()
                    messages.append(CanonicalMessage(role="user", content=tool_result))
                else:
                    messages.append(CanonicalMessage(role=role, content=content))

        tools, capabilities = self._parse_openai_tools(payload.get("tools"))

        return CanonicalRequest(
            source="openai_responses",
            model=model,
            stream=stream,
            system_prompt="\n\n".join(line for line in system_lines if line),
            messages=messages,
            tools=tools,
            tool_choice=payload.get("tool_choice"),
            temperature=self._to_optional_float(payload.get("temperature")),
            top_p=self._to_optional_float(payload.get("top_p")),
            max_tokens=self._to_optional_int(
                payload.get("max_output_tokens") or payload.get("max_tokens")
            ),
            capabilities=capabilities,
            response_language=self._detect_preferred_language(messages),
            raw=payload,
        )

    def parse_anthropic_messages(self, payload: Dict[str, Any]) -> CanonicalRequest:
        model = str(payload.get("model") or "").strip()
        if not model:
            raise ValueError("model is required")

        stream = bool(payload.get("stream", False))

        system_prompt = self._flatten_anthropic_system(payload.get("system"))

        messages: List[CanonicalMessage] = []
        for item in payload.get("messages") or []:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "user")
            if role not in {"user", "assistant"}:
                role = "user"
            content = self._flatten_anthropic_content(item.get("content"))
            messages.append(CanonicalMessage(role=role, content=content))

        tools = self._parse_anthropic_tools(payload.get("tools"))

        return CanonicalRequest(
            source="anthropic_messages",
            model=model,
            stream=stream,
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            tool_choice=payload.get("tool_choice"),
            temperature=self._to_optional_float(payload.get("temperature")),
            top_p=self._to_optional_float(payload.get("top_p")),
            max_tokens=self._to_optional_int(payload.get("max_tokens")),
            response_language=self._detect_preferred_language(messages),
            raw=payload,
        )

    def parse_gemini_content(
        self,
        model: str,
        payload: Dict[str, Any],
        *,
        stream: bool,
    ) -> CanonicalRequest:
        model_name = model[7:] if model.startswith("models/") else model
        model_name = str(model_name or "").strip()
        if not model_name:
            raise ValueError("model is required")

        system_prompt = self._flatten_gemini_system(payload.get("system_instruction"))

        messages: List[CanonicalMessage] = []
        for item in payload.get("contents") or []:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "user")
            if role == "model":
                mapped_role = "assistant"
            elif role == "user":
                mapped_role = "user"
            else:
                mapped_role = "user"
            content = self._flatten_gemini_parts(item.get("parts"))
            messages.append(CanonicalMessage(role=mapped_role, content=content))

        tools = self._parse_gemini_tools(payload.get("tools"))

        generation_config = payload.get("generationConfig") or payload.get("generation_config") or {}
        if not isinstance(generation_config, dict):
            generation_config = {}

        return CanonicalRequest(
            source="gemini_generate_content",
            model=model_name,
            stream=stream,
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            tool_choice=(payload.get("toolConfig") or payload.get("tool_config")),
            temperature=self._to_optional_float(generation_config.get("temperature")),
            top_p=self._to_optional_float(generation_config.get("topP") or generation_config.get("top_p")),
            max_tokens=self._to_optional_int(
                generation_config.get("maxOutputTokens") or generation_config.get("max_output_tokens")
            ),
            response_language=self._detect_preferred_language(messages),
            raw=payload,
        )

    # ---------------------------------------------------------------------
    # Prompt rendering
    # ---------------------------------------------------------------------

    def render_prompt(self, request: CanonicalRequest) -> str:
        parts: List[str] = []

        if request.system_prompt:
            parts.append(f"[System]\n{request.system_prompt}")

        if request.capabilities:
            cap_text = "\n".join(f"- {cap}" for cap in request.capabilities)
            parts.append(f"[Capabilities]\n{cap_text}")

        if request.tools:
            parts.append(
                self._render_tool_instruction(
                    request.tools,
                    request.tool_choice,
                    response_language=request.response_language,
                )
            )

        has_tool_results = self._has_tool_results(request.messages)
        parts.append(
            self._render_response_policy(
                request.response_language,
                has_tools=bool(request.tools),
                has_tool_results=has_tool_results,
            )
        )

        for msg in request.messages:
            label = self._role_label(msg.role)
            parts.append(f"[{label}]\n{msg.content}")

        if request.max_tokens:
            parts.append(f"[Generation Constraints]\nmax_tokens={request.max_tokens}")

        return "\n\n".join(part for part in parts if part)

    # ---------------------------------------------------------------------
    # Output parsing
    # ---------------------------------------------------------------------

    def parse_model_output(self, output: str) -> ParsedOutput:
        text = output or ""
        parse_result = self._tool_parser.parse(text)

        if parse_result.has_tool_calls:
            merged_text = self._merge_clean_text(parse_result.text_before, parse_result.text_after)
            return ParsedOutput(
                text=merged_text,
                tool_calls=[
                    CanonicalToolCall(
                        call_id=self._new_call_id(),
                        name=call.tool_name,
                        arguments=call.arguments or {},
                    )
                    for call in parse_result.tool_calls
                ],
            )

        fallback_call = self._parse_single_tool_object(text)
        if fallback_call is not None:
            return ParsedOutput(text="", tool_calls=[fallback_call])

        return ParsedOutput(text=text)

    # ---------------------------------------------------------------------
    # Provider response formatting
    # ---------------------------------------------------------------------

    def to_openai_chat_response(
        self,
        *,
        model: str,
        request_id: str,
        parsed: ParsedOutput,
        usage: UsageNumbers,
    ) -> Dict[str, Any]:
        message: Dict[str, Any] = {
            "role": "assistant",
            "content": parsed.text,
        }

        if parsed.has_tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.arguments_json,
                    },
                }
                for call in parsed.tool_calls
            ]

        finish_reason = "tool_calls" if parsed.has_tool_calls else "stop"

        return {
            "id": f"chatcmpl-{request_id}",
            "object": "chat.completion",
            "created": self._unix_ts(),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": usage.input_tokens,
                "completion_tokens": usage.output_tokens,
                "total_tokens": usage.total_tokens,
            },
        }

    def to_openai_responses_response(
        self,
        *,
        model: str,
        request_id: str,
        parsed: ParsedOutput,
        usage: UsageNumbers,
    ) -> Dict[str, Any]:
        output_items: List[Dict[str, Any]] = []

        message_item_id = f"msg_{request_id}"
        if parsed.text:
            output_items.append(
                {
                    "id": message_item_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": parsed.text}],
                }
            )

        for call in parsed.tool_calls:
            output_items.append(
                {
                    "id": f"fc_{call.call_id}",
                    "type": "function_call",
                    "call_id": call.call_id,
                    "name": call.name,
                    "arguments": call.arguments_json,
                }
            )

        if not output_items:
            output_items.append(
                {
                    "id": message_item_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": ""}],
                }
            )

        return {
            "id": f"resp_{request_id}",
            "object": "response",
            "created_at": self._unix_ts(),
            "status": "completed",
            "model": model,
            "output": output_items,
            "usage": {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "total_tokens": usage.total_tokens,
            },
        }

    def to_anthropic_response(
        self,
        *,
        model: str,
        request_id: str,
        parsed: ParsedOutput,
        usage: UsageNumbers,
    ) -> Dict[str, Any]:
        content_blocks: List[Dict[str, Any]] = []

        if parsed.text or not parsed.tool_calls:
            content_blocks.append({"type": "text", "text": parsed.text})

        for call in parsed.tool_calls:
            content_blocks.append(
                {
                    "type": "tool_use",
                    "id": call.call_id.replace("call_", "toolu_"),
                    "name": call.name,
                    "input": call.arguments,
                }
            )

        return {
            "id": f"msg_{request_id}",
            "type": "message",
            "role": "assistant",
            "content": content_blocks,
            "model": model,
            "stop_reason": "tool_use" if parsed.tool_calls else "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
            },
        }

    def to_gemini_response(
        self,
        *,
        model: str,
        parsed: ParsedOutput,
        usage: UsageNumbers,
    ) -> Dict[str, Any]:
        parts: List[Dict[str, Any]] = []
        if parsed.text:
            parts.append({"text": parsed.text})

        for call in parsed.tool_calls:
            parts.append(
                {
                    "functionCall": {
                        "name": call.name,
                        "args": call.arguments,
                    }
                }
            )

        if not parts:
            parts = [{"text": ""}]

        return {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": parts,
                    },
                    "finishReason": "STOP",
                    "index": 0,
                }
            ],
            "usageMetadata": {
                "promptTokenCount": usage.input_tokens,
                "candidatesTokenCount": usage.output_tokens,
                "totalTokenCount": usage.total_tokens,
            },
            "modelVersion": model,
        }

    # ---------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------

    def _flatten_openai_content(self, content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, (int, float, bool)):
            return str(content)

        if isinstance(content, dict):
            if "text" in content:
                return self._flatten_openai_content(content.get("text"))
            if "content" in content:
                return self._flatten_openai_content(content.get("content"))
            return json.dumps(content, ensure_ascii=False)

        if isinstance(content, list):
            chunks: List[str] = []
            for part in content:
                if isinstance(part, str):
                    chunks.append(part)
                    continue
                if isinstance(part, dict):
                    part_type = str(part.get("type") or "")
                    if part_type in TEXT_PART_TYPES:
                        chunks.append(str(part.get("text") or ""))
                    elif part_type == "image_url":
                        image_url = part.get("image_url") or {}
                        if isinstance(image_url, dict):
                            url = str(image_url.get("url") or "")
                        else:
                            url = str(image_url)
                        chunks.append(f"[image:{url}]" if url else "[image]")
                    elif part_type:
                        chunks.append(self._flatten_openai_content(part.get("text") or part.get("content") or part))
                    else:
                        chunks.append(self._flatten_openai_content(part))
                    continue

                chunks.append(str(part))
            return "\n".join(x for x in chunks if x)

        return str(content)

    def _flatten_anthropic_system(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts: List[str] = []
            for block in value:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(str(block.get("text") or ""))
                    elif "text" in block:
                        parts.append(str(block.get("text") or ""))
                else:
                    parts.append(str(block))
            return "\n".join(p for p in parts if p)
        return str(value)

    def _flatten_anthropic_content(self, content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            has_tool_use = any(
                isinstance(block, dict) and str(block.get("type") or "") == "tool_use"
                for block in content
            )
            pieces: List[str] = []
            for block in content:
                if isinstance(block, str):
                    if not has_tool_use:
                        pieces.append(block)
                    continue
                if not isinstance(block, dict):
                    if not has_tool_use:
                        pieces.append(str(block))
                    continue

                block_type = str(block.get("type") or "")
                if block_type == "text":
                    # Assistant messages that also contain tool_use often include
                    # provisional drafts ("initial analysis"), which can cause
                    # duplicated final answers in the next turn. Keep tool context
                    # and drop verbose pre-tool text in those mixed messages.
                    if not has_tool_use:
                        pieces.append(str(block.get("text") or ""))
                elif block_type == "tool_use":
                    tool_id = str(block.get("id") or "")
                    tool_name = str(block.get("name") or "tool")
                    tool_input = block.get("input") or {}
                    pieces.append(
                        "\n".join(
                            [
                                f"[tool_call id={tool_id} name={tool_name}]",
                                json.dumps(tool_input, ensure_ascii=False, separators=(",", ":")),
                            ]
                        )
                    )
                elif block_type == "tool_result":
                    tool_id = str(block.get("tool_use_id") or "")
                    result_text = self._flatten_anthropic_content(block.get("content"))
                    pieces.append(f"[tool_result id={tool_id}]\n{result_text}")
                else:
                    pieces.append(self._flatten_openai_content(block))
            return "\n".join(x for x in pieces if x)

        if isinstance(content, dict):
            return self._flatten_openai_content(content)

        return str(content)

    def _flatten_gemini_system(self, system_instruction: Any) -> str:
        if not isinstance(system_instruction, dict):
            return ""
        return self._flatten_gemini_parts(system_instruction.get("parts"))

    def _flatten_gemini_parts(self, parts: Any) -> str:
        if not isinstance(parts, list):
            return ""

        chunks: List[str] = []
        for part in parts:
            if isinstance(part, str):
                chunks.append(part)
                continue
            if not isinstance(part, dict):
                chunks.append(str(part))
                continue

            if "text" in part:
                chunks.append(str(part.get("text") or ""))
                continue

            function_call = part.get("functionCall") or part.get("function_call")
            if isinstance(function_call, dict):
                name = str(function_call.get("name") or "tool")
                args = function_call.get("args") or {}
                chunks.append(f"[tool_call name={name}]\n{json.dumps(args, ensure_ascii=False, separators=(",", ":"))}")
                continue

            function_response = part.get("functionResponse") or part.get("function_response")
            if isinstance(function_response, dict):
                name = str(function_response.get("name") or "tool")
                response = function_response.get("response") or {}
                chunks.append(f"[tool_result name={name}]\n{json.dumps(response, ensure_ascii=False, separators=(",", ":"))}")
                continue

            chunks.append(self._flatten_openai_content(part))

        return "\n".join(x for x in chunks if x)

    def _append_openai_tool_calls_history(self, content: str, tool_calls: List[Any]) -> str:
        lines: List[str] = [content] if content else []
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            function_obj = call.get("function") or {}
            if not isinstance(function_obj, dict):
                function_obj = {}
            call_id = str(call.get("id") or "")
            name = str(function_obj.get("name") or "tool")
            arguments = function_obj.get("arguments") or "{}"
            lines.append(f"[tool_call id={call_id} name={name}]\n{arguments}")
        return "\n".join(line for line in lines if line)

    def _parse_openai_tools(self, tools_raw: Any) -> Tuple[List[CanonicalTool], List[str]]:
        tools: List[CanonicalTool] = []
        capabilities: List[str] = []

        if not isinstance(tools_raw, list):
            return tools, capabilities

        for idx, item in enumerate(tools_raw):
            if not isinstance(item, dict):
                continue
            tool_type = str(item.get("type") or "")
            if tool_type == "function":
                fn = item.get("function") or {}
                if not isinstance(fn, dict):
                    continue
                name = str(fn.get("name") or "").strip()
                if not name:
                    continue
                description = str(fn.get("description") or "")
                schema = fn.get("parameters")
                if not isinstance(schema, dict):
                    schema = {"type": "object", "properties": {}}
                tools.append(
                    CanonicalTool(
                        name=name,
                        description=description,
                        input_schema=schema,
                        source_type="function",
                    )
                )
                continue

            # Non-function tools (mcp / code interpreter / search) become capability hints.
            if tool_type:
                capabilities.append(f"openai_tool_type={tool_type}")
                if tool_type == "mcp":
                    server_label = str(item.get("server_label") or item.get("server") or "mcp")
                    capabilities.append(f"mcp_server={server_label}")
                # Optional generic synthetic tool for easier routing through textual prompt.
                synthetic_name = f"{tool_type}_{idx + 1}"
                tools.append(
                    CanonicalTool(
                        name=synthetic_name,
                        description=f"Synthetic capability wrapper for {tool_type}",
                        input_schema={"type": "object", "properties": {}},
                        source_type=tool_type,
                    )
                )

        return tools, capabilities

    def _parse_anthropic_tools(self, tools_raw: Any) -> List[CanonicalTool]:
        tools: List[CanonicalTool] = []
        if not isinstance(tools_raw, list):
            return tools

        for item in tools_raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            description = str(item.get("description") or "")
            schema = item.get("input_schema")
            if not isinstance(schema, dict):
                schema = {"type": "object", "properties": {}}
            tools.append(
                CanonicalTool(
                    name=name,
                    description=description,
                    input_schema=schema,
                    source_type="anthropic_tool",
                )
            )
        return tools

    def _parse_gemini_tools(self, tools_raw: Any) -> List[CanonicalTool]:
        tools: List[CanonicalTool] = []
        if not isinstance(tools_raw, list):
            return tools

        for item in tools_raw:
            if not isinstance(item, dict):
                continue
            declarations = item.get("functionDeclarations") or item.get("function_declarations") or []
            if not isinstance(declarations, list):
                continue
            for decl in declarations:
                if not isinstance(decl, dict):
                    continue
                name = str(decl.get("name") or "").strip()
                if not name:
                    continue
                description = str(decl.get("description") or "")
                schema = decl.get("parameters")
                if not isinstance(schema, dict):
                    schema = {"type": "object", "properties": {}}
                tools.append(
                    CanonicalTool(
                        name=name,
                        description=description,
                        input_schema=schema,
                        source_type="gemini_function",
                    )
                )
        return tools

    def _render_tool_instruction(
        self,
        tools: Iterable[CanonicalTool],
        tool_choice: Any,
        response_language: str = "auto",
    ) -> str:
        is_zh = response_language == "zh"
        if is_zh:
            lines: List[str] = [
                "[Tooling]",
                "你可以通过输出 JSON 代码块调用工具。",
                "每次调用必须严格为：",
                "```json",
                '{"tool":"<name>","arguments":{}}',
                "```",
                "可用工具：",
            ]
        else:
            lines = [
                "[Tooling]",
                "You can call tools by emitting JSON code blocks.",
                "Each call must be exactly:",
                "```json",
                '{"tool":"<name>","arguments":{}}',
                "```",
                "Available tools:",
            ]

        tool_names: List[str] = []
        for tool in tools:
            schema_text = json.dumps(tool.input_schema or {}, ensure_ascii=False, separators=(",", ":"))
            lines.append(f"- {tool.name}: {tool.description}".rstrip())
            lines.append(f"  schema={schema_text}")
            tool_names.append(tool.name)

        normalized_tool_names = {name.lower() for name in tool_names}
        if "explore" in normalized_tool_names and "read" in normalized_tool_names:
            if is_zh:
                lines.append("做仓库级分析时，优先先发起一次 Explore。")
                lines.append("避免在同一轮输出大量独立 Read 调用。")
            else:
                lines.append("For repository-wide analysis, prefer one Explore call first.")
                lines.append("Avoid emitting long batches of standalone Read calls in one turn.")

        if tool_choice is not None:
            lines.append(f"tool_choice={json.dumps(tool_choice, ensure_ascii=False)}")

        if is_zh:
            lines.append("如需使用工具，返回工具调用，不要先输出完整结论。")
        else:
            lines.append("If a tool is required, return tool calls instead of plain prose.")
        return "\n".join(lines)

    def _render_response_policy(
        self,
        response_language: str,
        has_tools: bool,
        has_tool_results: bool,
    ) -> str:
        lines = ["[Response Policy]"]
        if response_language == "zh":
            lines.append("默认使用简体中文回答（除非系统指令明确要求其他语言）。")
            lines.append("工具调用前后的进度提示也必须使用简体中文，不要以英文开头。")
            lines.append("除代码标识符、文件名、命令名外，不要使用英文句子或英文标题。")
        else:
            lines.append("Respond in the same language as the latest user message.")

        if has_tools:
            if response_language == "zh":
                lines.append("如果需要使用工具，先收集证据，再给最终结论。")
                lines.append("首次工具调用前，前置说明最多一句且必须简短。")
                lines.append("不要在同一轮输出“初稿+修正稿”。")
                lines.append(
                    "若维护任务清单，必须在每次 tool_result 后立即更新完成状态与计数，避免遗留未完成条目。"
                )
                lines.append(
                    "避免长时间只做计划不执行；应尽快发起第一个工具调用。"
                )
            else:
                lines.append(
                    "If tools are needed, gather evidence first and avoid giving a full final answer before tool results."
                )
                lines.append(
                    "Before first tool call, keep text short (one concise sentence max)."
                )
                lines.append(
                    "Do not output draft + correction in the same turn."
                )
                lines.append(
                    "If you keep a task checklist, update task status/counts immediately after each tool_result."
                )
                lines.append(
                    "Avoid long planning-only turns; start the first tool call quickly."
                )
        if has_tool_results:
            if response_language == "zh":
                lines.append("工具结果已提供：仅输出一份整合后的最终答案。")
                lines.append("不要出现“更新分析”“修正”等重复草稿措辞。")
            else:
                lines.append(
                    "Tool results are already available: output exactly one consolidated final answer."
                )
                lines.append(
                    "Do not include phrases like 'update analysis', 'correction', or repeat prior drafts."
                )
        return "\n".join(lines)

    @staticmethod
    def _detect_preferred_language(messages: List[CanonicalMessage]) -> str:
        # Ignore synthetic tool-result user turns when detecting language.
        # Claude Code/agent clients often append English tool outputs as the
        # latest "user" message, which can incorrectly override user language.
        latest_user = ""
        fallback_user = ""

        for message in reversed(messages):
            if message.role != "user":
                continue
            content = (message.content or "").strip()
            if not content:
                continue
            if not fallback_user:
                fallback_user = content
            if ProtocolBridge._is_synthetic_tool_user_message(content):
                continue
            latest_user = content
            break

        target = latest_user or fallback_user
        if not target:
            return "auto"

        cjk_count = sum(1 for ch in target if "\u4e00" <= ch <= "\u9fff")
        if cjk_count >= 2:
            return "zh"
        return "auto"

    @staticmethod
    def _is_synthetic_tool_user_message(content: str) -> bool:
        lowered = (content or "").strip().lower()
        if not lowered:
            return False

        synthetic_prefixes = (
            "[tool_result",
            "[tool_call",
            "<tool_use",
        )
        return lowered.startswith(synthetic_prefixes)

    @staticmethod
    def _has_tool_results(messages: List[CanonicalMessage]) -> bool:
        for message in messages:
            if message.role != "user":
                continue
            if "[tool_result" in (message.content or ""):
                return True
        return False

    @staticmethod
    def _role_label(role: str) -> str:
        if role == "assistant":
            return "Assistant"
        if role == "user":
            return "Human"
        if role == "system":
            return "System"
        return "Tool"

    @staticmethod
    def _merge_clean_text(text_before: str, text_after: str) -> str:
        left = (text_before or "").strip()
        right = (text_after or "").strip()
        if left and right:
            return f"{left}\n{right}"
        return left or right

    def _parse_single_tool_object(self, text: str) -> Optional[CanonicalToolCall]:
        stripped = (text or "").strip()
        if not stripped:
            return None

        try:
            parsed = json.loads(stripped)
        except Exception:
            return None

        if not isinstance(parsed, dict):
            return None

        if "tool" not in parsed:
            return None

        name = str(parsed.get("tool") or "").strip()
        if not name:
            return None

        args = parsed.get("arguments")
        if not isinstance(args, dict):
            args = {}

        return CanonicalToolCall(call_id=self._new_call_id(), name=name, arguments=args)

    @staticmethod
    def _new_call_id() -> str:
        return f"call_{uuid.uuid4().hex[:24]}"

    @staticmethod
    def _to_optional_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _to_optional_int(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(value)
        except Exception:
            return None

    @staticmethod
    def _unix_ts() -> int:
        import time

        return int(time.time())


_protocol_bridge: Optional[ProtocolBridge] = None


def get_protocol_bridge() -> ProtocolBridge:
    global _protocol_bridge
    if _protocol_bridge is None:
        _protocol_bridge = ProtocolBridge()
    return _protocol_bridge
