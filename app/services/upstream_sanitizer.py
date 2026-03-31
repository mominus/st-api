"""
Upstream Sanitizer Service
隐藏上游服务信息，避免透传给下游用户。
"""

import json
import os
import re
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse


_STATIC_SENSITIVE_REPLACEMENTS = (
    (re.compile(r"support@stack-ai\.com", re.IGNORECASE), "upstream support"),
    (
        re.compile(r"\b(?:https?://)?(?:[\w-]+\.)*stack-ai\.com\b", re.IGNORECASE),
        "upstream service",
    ),
    (re.compile(r"\bstack[\s_-]?ai\b", re.IGNORECASE), "upstream service"),
)

_STREAM_COMPLETION_MARKERS = {
    "stream_complete",
    "event: stream_complete",
    "stream completed successfully",
    "event: stream completed successfully",
}

_STREAM_COMPLETION_PATTERNS = (
    re.compile(r"^stream\s+completed\s+successfully\.?$", re.IGNORECASE),
    re.compile(r"^event:\s*stream\s+completed\s+successfully\.?$", re.IGNORECASE),
)

_TOOL_USE_XML_PATTERN = re.compile(
    r"(<tool_use\b[^>]*>)(.*?)(</tool_use>)",
    re.IGNORECASE | re.DOTALL,
)
_ARGUMENTS_KEY_PATTERN = re.compile(r'"arguments"\s*:\s*', re.IGNORECASE)


def _derive_domain_candidates(raw_url: str | None) -> set[str]:
    if not raw_url:
        return set()

    parsed = urlparse(raw_url)
    hostname = (parsed.hostname or "").strip().lower().strip(".")
    if not hostname:
        return set()

    candidates = {hostname}
    labels = [label for label in hostname.split(".") if label]
    if len(labels) >= 2:
        candidates.add(".".join(labels[-2:]))
    return {item for item in candidates if item}


@lru_cache(maxsize=8)
def _build_sensitive_replacements(raw_backend_url: str | None):
    replacements = list(_STATIC_SENSITIVE_REPLACEMENTS)
    for domain in sorted(_derive_domain_candidates(raw_backend_url)):
        escaped = re.escape(domain)
        replacements.append(
            (
                re.compile(rf"\b[A-Z0-9._%+-]+@(?:[\w-]+\.)*{escaped}\b", re.IGNORECASE),
                "upstream support",
            )
        )
        replacements.append(
            (
                re.compile(rf"\b(?:https?://)?(?:[\w-]+\.)*{escaped}\b", re.IGNORECASE),
                "upstream service",
            )
        )
    return tuple(replacements)


def _find_json_like_value_end(text: str, start: int) -> int:
    """Find the end index of a JSON-like value starting at `start`."""
    if start >= len(text):
        return start

    first = text[start]
    pairs = {"{": "}", "[": "]"}

    if first in pairs:
        stack = [first]
        in_string = False
        escaped = False
        idx = start
        while idx < len(text):
            ch = text[idx]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch in pairs:
                    stack.append(ch)
                elif ch in "}]":
                    if not stack:
                        return idx + 1
                    opener = stack.pop()
                    if pairs.get(opener) != ch:
                        return idx + 1
                    if not stack:
                        return idx + 1
            idx += 1
        return len(text)

    if first == '"':
        idx = start + 1
        escaped = False
        while idx < len(text):
            ch = text[idx]
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                return idx + 1
            idx += 1
        return len(text)

    idx = start
    while idx < len(text) and text[idx] not in ",}\r\n\t ":
        idx += 1
    return idx


def _redact_arguments_values(text: str) -> str:
    """Redact any JSON-like value of `arguments` keys."""
    if not text:
        return text

    result_parts: list[str] = []
    cursor = 0
    for match in _ARGUMENTS_KEY_PATTERN.finditer(text):
        result_parts.append(text[cursor:match.end()])
        value_start = match.end()
        while value_start < len(text) and text[value_start].isspace():
            value_start += 1

        if value_start >= len(text):
            cursor = value_start
            break

        value_end = _find_json_like_value_end(text, value_start)
        result_parts.append('{"_redacted":true}')
        cursor = value_end

    result_parts.append(text[cursor:])
    return "".join(result_parts)


def _redact_tool_payloads(text: str) -> str:
    """Redact tool payload internals that may leak file/content arguments."""
    redacted = text
    redacted = _TOOL_USE_XML_PATTERN.sub(
        lambda m: f'{m.group(1)}{{"_redacted":true}}{m.group(3)}',
        redacted,
    )
    redacted = _redact_arguments_values(redacted)

    # If the full text itself is a JSON tool object, keep JSON shape stable.
    stripped = redacted.strip()
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict) and "tool" in parsed and "arguments" in parsed:
            parsed["arguments"] = {"_redacted": True}
            redacted = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        pass

    return redacted


def sanitize_exposed_text(text: str | None) -> str | None:
    """清理面向下游用户暴露的文本。"""
    if text is None:
        return None

    sanitized = text
    replacements = _build_sensitive_replacements(os.getenv("BACKEND_API_URL"))
    for pattern, replacement in replacements:
        sanitized = pattern.sub(replacement, sanitized)
    sanitized = _redact_tool_payloads(sanitized)

    return re.sub(r"[ \t]{2,}", " ", sanitized).strip()


def sanitize_exposed_payload(payload: Any) -> Any:
    """递归清理响应载荷中的敏感上游信息。"""
    if isinstance(payload, dict):
        return {
            key: sanitize_exposed_payload(value)
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [sanitize_exposed_payload(item) for item in payload]
    if isinstance(payload, tuple):
        return tuple(sanitize_exposed_payload(item) for item in payload)
    if isinstance(payload, str):
        return sanitize_exposed_text(payload)
    return payload


def is_stream_completion_marker(value: Any) -> bool:
    """识别上游流式结束标记，避免透传到最终输出。"""
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _STREAM_COMPLETION_MARKERS:
            return True
        return any(pattern.match(value.strip()) for pattern in _STREAM_COMPLETION_PATTERNS)

    if isinstance(value, dict):
        if value.get("stream_complete") is True:
            return True

        for key in ("event", "type", "status"):
            marker = value.get(key)
            if isinstance(marker, str) and marker.strip().lower() == "stream_complete":
                return True

    return False
