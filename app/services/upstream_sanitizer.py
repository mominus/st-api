"""
Upstream Sanitizer Service
隐藏上游服务信息，避免透传给下游用户。
"""

import re
from typing import Any


_SENSITIVE_REPLACEMENTS = (
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


def sanitize_exposed_text(text: str | None) -> str | None:
    """清理面向下游用户暴露的文本。"""
    if text is None:
        return None

    sanitized = text
    for pattern, replacement in _SENSITIVE_REPLACEMENTS:
        sanitized = pattern.sub(replacement, sanitized)

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
