"""
Capability matrix service for honest Claude Code compatibility exposure.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional


STATUS_NATIVE = "native"
STATUS_SIMULATED = "simulated"
STATUS_UNSUPPORTED = "unsupported"
VALID_CAPABILITY_STATUSES = {
    STATUS_NATIVE,
    STATUS_SIMULATED,
    STATUS_UNSUPPORTED,
}
CLAUDE_CODE_CAPABILITY_NAMES = (
    "system_prompt",
    "chat_history",
    "max_tokens",
    "temperature",
    "metadata",
    "tool_use",
    "tool_choice",
    "thinking",
    "anthropic_beta",
    "prompt_caching",
    "image_input",
    "pdf_input",
    "context_management",
    "structured_outputs",
    "interleaved_thinking",
    "token_efficient_tools",
    "advanced_tool_use",
)


def _normalized_input_mapping(input_mapping: Optional[Dict[str, Any]]) -> Dict[str, str]:
    if not isinstance(input_mapping, dict):
        return {}

    normalized: Dict[str, str] = {}
    for key, value in input_mapping.items():
        field_name = str(value or "").strip()
        if field_name:
            normalized[str(key or "").strip()] = field_name
    return normalized


def parse_input_mapping(raw_value: Any) -> Dict[str, str]:
    if raw_value in (None, ""):
        return {}

    parsed = raw_value
    if isinstance(raw_value, str):
        try:
            parsed = json.loads(raw_value)
        except Exception:
            return {}

    return _normalized_input_mapping(parsed if isinstance(parsed, dict) else {})


def _normalized_capability_overrides(
    capability_overrides: Any,
    *,
    strict: bool,
) -> Dict[str, Dict[str, Any]]:
    if capability_overrides is None:
        return {}

    if not isinstance(capability_overrides, dict):
        if strict:
            raise ValueError("capability_overrides must be a JSON object")
        return {}

    normalized: Dict[str, Dict[str, Any]] = {}

    for raw_name, raw_value in capability_overrides.items():
        capability_name = str(raw_name or "").strip()
        if not capability_name:
            continue
        if capability_name not in CLAUDE_CODE_CAPABILITY_NAMES:
            if strict:
                raise ValueError(f"Unknown capability override: {capability_name}")
            continue

        detail: Optional[str] = None
        mapped_field: Optional[str] = None
        if isinstance(raw_value, str):
            status = raw_value.strip().lower()
        elif isinstance(raw_value, dict):
            status = str(raw_value.get("status") or "").strip().lower()
            raw_detail = raw_value.get("detail")
            if raw_detail is not None:
                detail_text = str(raw_detail).strip()
                if detail_text:
                    detail = detail_text
            raw_mapped_field = raw_value.get("mapped_field")
            if raw_mapped_field is not None:
                mapped_field_text = str(raw_mapped_field).strip()
                if mapped_field_text:
                    mapped_field = mapped_field_text
        else:
            if strict:
                raise ValueError(
                    f"Capability override '{capability_name}' must be a string or object"
                )
            continue

        if status not in VALID_CAPABILITY_STATUSES:
            if strict:
                raise ValueError(
                    f"Invalid capability status for '{capability_name}': {status or '<empty>'}"
                )
            continue

        item: Dict[str, Any] = {"status": status}
        if detail:
            item["detail"] = detail
        if mapped_field:
            item["mapped_field"] = mapped_field
        normalized[capability_name] = item

    return normalized


def validate_capability_overrides(
    capability_overrides: Optional[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    return _normalized_capability_overrides(capability_overrides, strict=True)


def coerce_capability_overrides(
    capability_overrides: Any,
) -> Dict[str, Dict[str, Any]]:
    return _normalized_capability_overrides(capability_overrides, strict=False)


def parse_capability_overrides(raw_value: Any) -> Dict[str, Dict[str, Any]]:
    if raw_value in (None, ""):
        return {}

    parsed = raw_value
    if isinstance(raw_value, str):
        try:
            parsed = json.loads(raw_value)
        except Exception:
            return {}

    return coerce_capability_overrides(parsed)


def _native_or_fallback(
    *,
    input_mapping: Dict[str, str],
    logical_name: str,
    fallback_status: str,
    native_detail: str,
    fallback_detail: str,
) -> Dict[str, Any]:
    mapped_field = input_mapping.get(logical_name)
    if mapped_field:
        return {
            "status": STATUS_NATIVE,
            "detail": native_detail,
            "mapped_field": mapped_field,
        }
    return {
        "status": fallback_status,
        "detail": fallback_detail,
    }


def build_claude_code_capability_matrix(
    *,
    model: str,
    input_mapping: Optional[Dict[str, Any]] = None,
    capability_overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    mapping = _normalized_input_mapping(input_mapping)
    overrides = coerce_capability_overrides(capability_overrides)

    capabilities: Dict[str, Dict[str, Any]] = {
        "system_prompt": _native_or_fallback(
            input_mapping=mapping,
            logical_name="system_prompt",
            fallback_status=STATUS_SIMULATED,
            native_detail="Direct passthrough to the upstream workflow via input_mapping.system_prompt.",
            fallback_detail="If unmapped, the gateway flattens system prompt content into prompt text before sending it upstream.",
        ),
        "chat_history": _native_or_fallback(
            input_mapping=mapping,
            logical_name="chat_history",
            fallback_status=STATUS_SIMULATED,
            native_detail="Direct passthrough to the upstream workflow via input_mapping.chat_history.",
            fallback_detail="If unmapped, prior turns are flattened into prompt text with best-effort role/tool reconstruction.",
        ),
        "max_tokens": _native_or_fallback(
            input_mapping=mapping,
            logical_name="max_tokens",
            fallback_status=STATUS_SIMULATED,
            native_detail="Direct passthrough to the upstream workflow via input_mapping.max_tokens.",
            fallback_detail="If unmapped, max_tokens survives only as prompt-level generation guidance.",
        ),
        "temperature": _native_or_fallback(
            input_mapping=mapping,
            logical_name="temperature",
            fallback_status=STATUS_UNSUPPORTED,
            native_detail="Direct passthrough to the upstream workflow via input_mapping.temperature.",
            fallback_detail="Temperature is not sent upstream unless input_mapping.temperature is configured.",
        ),
        "metadata": _native_or_fallback(
            input_mapping=mapping,
            logical_name="metadata",
            fallback_status=STATUS_UNSUPPORTED,
            native_detail="Direct passthrough to the upstream workflow via input_mapping.metadata.",
            fallback_detail="Metadata is parsed and retained by the gateway but not forwarded upstream unless input_mapping.metadata is configured.",
        ),
        "tool_use": {
            "status": STATUS_SIMULATED,
            "detail": "Anthropic tool_use blocks are reconstructed from upstream text output; the StackAI workflow path does not expose native tool calling primitives.",
        },
        "tool_choice": {
            "status": STATUS_SIMULATED,
            "detail": "tool_choice is best-effort prompt guidance, not upstream-native hard enforcement.",
        },
        "thinking": {
            "status": STATUS_SIMULATED,
            "detail": (
                "Thinking is accepted and preserved by the gateway, then best-effort reconstructed from tags such as "
                "<thinking>...</thinking>; this is not Anthropic-native reasoning."
            ),
            **({"mapped_field": mapping["thinking"]} if mapping.get("thinking") else {}),
        },
        "anthropic_beta": {
            "status": STATUS_SIMULATED,
            "detail": (
                "anthropic-beta headers are normalized by the gateway and only selected betas currently affect behavior; "
                "they are not exposed as upstream-native Anthropic beta support."
            ),
        },
        "prompt_caching": {
            "status": STATUS_UNSUPPORTED,
            "detail": "The current upstream path does not expose Anthropic prompt caching primitives, and the gateway does not emulate cache token accounting.",
        },
        "image_input": {
            "status": STATUS_UNSUPPORTED,
            "detail": "The current StackAI workflow path is treated as text-only for Claude Code compatibility; Anthropic image content blocks are not exposed as supported upstream inputs.",
        },
        "pdf_input": {
            "status": STATUS_UNSUPPORTED,
            "detail": "PDF/document multimodal ingestion is not exposed as a supported Anthropic-compatible upstream capability.",
        },
        "context_management": {
            "status": STATUS_UNSUPPORTED,
            "detail": "Anthropic context-management beta behavior is not implemented.",
        },
        "structured_outputs": {
            "status": STATUS_UNSUPPORTED,
            "detail": "Anthropic structured outputs beta behavior is not implemented.",
        },
        "interleaved_thinking": {
            "status": STATUS_UNSUPPORTED,
            "detail": "Interleaved thinking beta behavior is not implemented.",
        },
        "token_efficient_tools": {
            "status": STATUS_UNSUPPORTED,
            "detail": "Anthropic token-efficient-tools beta behavior is not implemented.",
        },
        "advanced_tool_use": {
            "status": STATUS_UNSUPPORTED,
            "detail": "Anthropic advanced-tool-use beta behavior is not implemented.",
        },
    }

    for capability_name, override in overrides.items():
        merged = dict(capabilities.get(capability_name, {}))
        merged.update(override)
        capabilities[capability_name] = merged

    summary = {
        STATUS_NATIVE: 0,
        STATUS_SIMULATED: 0,
        STATUS_UNSUPPORTED: 0,
    }
    for item in capabilities.values():
        summary[str(item.get("status") or STATUS_UNSUPPORTED)] += 1

    return {
        "profile": "claude_code",
        "model": model,
        "source": "derived_from_model_group_input_mapping_and_gateway_defaults",
        "summary": summary,
        "capabilities": capabilities,
    }
