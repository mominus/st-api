"""
ST Usage Helper
统一提取和归一化 ST 返回的 token 使用数据。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

INPUT_TOKEN_KEYS = (
    "input_tokens",
    "prompt_tokens",
    "prompttokencount",
    "inputtokencount",
    "prompttokens",
    "inputtokens",
)

OUTPUT_TOKEN_KEYS = (
    "output_tokens",
    "completion_tokens",
    "candidatestokencount",
    "completiontokencount",
    "outputtokencount",
    "completiontokens",
    "outputtokens",
)

TOTAL_TOKEN_KEYS = (
    "total_tokens",
    "totaltokencount",
    "totaltokens",
    "token_count",
    "tokencount",
)

RUN_ID_KEYS = ("run_id", "runid")

MAX_RECURSION_DEPTH = 8
MAX_STRING_PARSE_LEN = 300000

TOKEN_REGEX = {
    "input": re.compile(r"(?:input_tokens|prompt_tokens|promptTokenCount|inputTokenCount)\s*[:=]\s*(\d+)", re.IGNORECASE),
    "output": re.compile(r"(?:output_tokens|completion_tokens|candidatesTokenCount|completionTokenCount)\s*[:=]\s*(\d+)", re.IGNORECASE),
    "total": re.compile(r"(?:total_tokens|totalTokenCount|token_count)\s*[:=]\s*(\d+)", re.IGNORECASE),
}


@dataclass
class STUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    source: str = "unknown"
    exact: bool = False

    def normalized(self) -> "STUsage":
        input_tokens = max(0, int(self.input_tokens or 0))
        output_tokens = max(0, int(self.output_tokens or 0))
        total_tokens = max(0, int(self.total_tokens or 0))

        if total_tokens == 0 and (input_tokens > 0 or output_tokens > 0):
            total_tokens = input_tokens + output_tokens

        if total_tokens < input_tokens + output_tokens:
            total_tokens = input_tokens + output_tokens

        exact = bool(input_tokens > 0 or output_tokens > 0)

        return STUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            source=self.source,
            exact=exact,
        )


def choose_better_usage(current: Optional[STUsage], candidate: Optional[STUsage]) -> Optional[STUsage]:
    """选择更高质量的 usage。"""
    if candidate is None:
        return current
    if current is None:
        return candidate.normalized()

    current_n = current.normalized()
    candidate_n = candidate.normalized()

    if _usage_score(candidate_n) > _usage_score(current_n):
        return candidate_n
    return current_n


def extract_run_id(payload: Any) -> Optional[str]:
    """递归提取 run_id。"""
    return _extract_run_id(payload, depth=0)


def extract_usage(payload: Any, source: str = "payload") -> Optional[STUsage]:
    """递归提取最优 usage。"""
    best: Optional[STUsage] = None
    for usage in _iter_usages(payload, depth=0):
        best = choose_better_usage(best, usage)

    if best is None:
        return None

    best = best.normalized()
    best.source = source
    return best


def extract_usage_from_llms(llms_payload: Any, source: str = "analytics.llms") -> Optional[STUsage]:
    """
    从 analytics 的 llms 字段提取 usage。
    llms 常见为字符串化 JSON，可能包含多个模型节点，按节点聚合。
    """
    parsed = _parse_json_like(llms_payload)
    if parsed is None:
        parsed = llms_payload

    node_items: List[Any] = []
    if isinstance(parsed, list):
        node_items = parsed
    elif isinstance(parsed, dict):
        # 兼容 map 结构：{"nodeA": {...}, "nodeB": {...}}
        node_items = list(parsed.values())
    else:
        return extract_usage(parsed, source=source)

    total_input = 0
    total_output = 0
    total_total = 0
    matched = 0

    for item in node_items:
        usage = extract_usage(item, source=source)
        if usage is None:
            continue
        usage = usage.normalized()

        # 只聚合有 token 的节点
        if usage.total_tokens <= 0 and usage.input_tokens <= 0 and usage.output_tokens <= 0:
            continue

        matched += 1
        total_input += usage.input_tokens
        total_output += usage.output_tokens
        total_total += usage.total_tokens

    if matched == 0:
        return None

    # 如果节点只给了 total，则保留 total
    if total_total == 0:
        total_total = total_input + total_output

    return STUsage(
        input_tokens=total_input,
        output_tokens=total_output,
        total_tokens=total_total,
        source=source,
        exact=(total_input > 0 or total_output > 0),
    ).normalized()


def extract_usage_from_analytics_run(run: Dict[str, Any]) -> Optional[STUsage]:
    """从单条 analytics run 提取 usage（优先 llms 的精确分项）。"""
    if not isinstance(run, dict):
        return None

    best = extract_usage(run, source="analytics.run")

    llms_usage = extract_usage_from_llms(run.get("llms"), source="analytics.llms")
    best = choose_better_usage(best, llms_usage)
    if llms_usage is not None and llms_usage.exact:
        # 同分情况下优先 llms（更接近模型节点统计）
        if best is None or not best.exact or llms_usage.total_tokens == best.total_tokens:
            best = llms_usage.normalized()

    # 兜底 total_tokens 字段
    if best is None:
        total_tokens = _safe_int(run.get("total_tokens"))
        if total_tokens > 0:
            best = STUsage(
                input_tokens=0,
                output_tokens=total_tokens,
                total_tokens=total_tokens,
                source="analytics.total_tokens",
                exact=False,
            )

    return best.normalized() if best else None


def split_total_with_fallback(
    usage: STUsage,
    fallback_input_tokens: int = 0,
    fallback_output_tokens: int = 0,
) -> STUsage:
    """
    当只有 total 没有 input/output 时，用 fallback 比例切分。
    不使用固定 1:2 规则。
    """
    usage_n = usage.normalized()
    if usage_n.exact:
        return usage_n

    total = usage_n.total_tokens
    if total <= 0:
        return usage_n

    fallback_input = max(0, int(fallback_input_tokens or 0))
    fallback_output = max(0, int(fallback_output_tokens or 0))
    fallback_sum = fallback_input + fallback_output

    if fallback_sum > 0:
        input_tokens = round(total * (fallback_input / fallback_sum))
        output_tokens = total - input_tokens
    else:
        input_tokens = 0
        output_tokens = total

    return STUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total,
        source=usage_n.source,
        exact=False,
    ).normalized()


def _usage_score(usage: STUsage) -> Tuple[int, int, int]:
    """
    排序评分：
    1) exact 优先
    2) 字段覆盖度优先（input/output/total）
    3) total 更大优先（通常是最终累计值）
    """
    coverage = int(usage.input_tokens > 0) + int(usage.output_tokens > 0) + int(usage.total_tokens > 0)
    exact_rank = 1 if usage.exact else 0
    return (exact_rank, coverage, usage.total_tokens)


def _safe_int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        if value <= 0:
            return 0
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return 0
        if s.isdigit():
            return int(s)
        try:
            f = float(s)
            return int(f) if f > 0 else 0
        except Exception:
            return 0
    return 0


def _get_from_keys(data: Dict[str, Any], keys: Iterable[str]) -> int:
    lower = {str(k).lower(): v for k, v in data.items()}
    for key in keys:
        if key in lower:
            value = _safe_int(lower[key])
            if value >= 0:
                return value
    return 0


def _usage_from_mapping(data: Dict[str, Any]) -> Optional[STUsage]:
    if not data:
        return None

    input_tokens = _get_from_keys(data, INPUT_TOKEN_KEYS)
    output_tokens = _get_from_keys(data, OUTPUT_TOKEN_KEYS)
    total_tokens = _get_from_keys(data, TOTAL_TOKEN_KEYS)

    if total_tokens == 0 and (input_tokens > 0 or output_tokens > 0):
        total_tokens = input_tokens + output_tokens

    if total_tokens <= 0 and input_tokens <= 0 and output_tokens <= 0:
        return None

    return STUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        source="mapping",
        exact=(input_tokens > 0 or output_tokens > 0),
    ).normalized()


def _extract_regex_usage(text: str) -> Optional[STUsage]:
    if not text:
        return None

    input_match = TOKEN_REGEX["input"].search(text)
    output_match = TOKEN_REGEX["output"].search(text)
    total_match = TOKEN_REGEX["total"].search(text)

    input_tokens = int(input_match.group(1)) if input_match else 0
    output_tokens = int(output_match.group(1)) if output_match else 0
    total_tokens = int(total_match.group(1)) if total_match else 0

    if total_tokens == 0 and (input_tokens > 0 or output_tokens > 0):
        total_tokens = input_tokens + output_tokens

    if total_tokens <= 0 and input_tokens <= 0 and output_tokens <= 0:
        return None

    return STUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        source="regex",
        exact=(input_tokens > 0 or output_tokens > 0),
    ).normalized()


def _parse_json_like(value: Any) -> Optional[Any]:
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text or len(text) > MAX_STRING_PARSE_LEN:
        return None

    # 兼容 SSE 行：data: {...}
    if text.startswith("data:"):
        text = text[5:].strip()

    parsed: Any = text
    for _ in range(2):
        if not isinstance(parsed, str):
            break

        s = parsed.strip()
        if not s:
            return None

        if not ((s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")) or (s.startswith('"') and s.endswith('"'))):
            return None

        try:
            parsed = json.loads(s)
        except Exception:
            return None

    return parsed if parsed is not value else None


def _iter_usages(payload: Any, depth: int = 0):
    if depth > MAX_RECURSION_DEPTH or payload is None:
        return

    if isinstance(payload, dict):
        usage = _usage_from_mapping(payload)
        if usage is not None:
            yield usage

        # 常见 usage 字段优先递归
        preferred_keys = (
            "usage",
            "usage_metadata",
            "usagemetadata",
            "token_usage",
            "llms",
            "metadata",
            "progress_data",
        )
        lower_to_value = {str(k).lower(): v for k, v in payload.items()}
        for key in preferred_keys:
            if key in lower_to_value:
                yield from _iter_usages(lower_to_value[key], depth + 1)

        for value in payload.values():
            yield from _iter_usages(value, depth + 1)
        return

    if isinstance(payload, list):
        for item in payload:
            yield from _iter_usages(item, depth + 1)
        return

    if isinstance(payload, str):
        parsed = _parse_json_like(payload)
        if parsed is not None:
            yield from _iter_usages(parsed, depth + 1)

        regex_usage = _extract_regex_usage(payload)
        if regex_usage is not None:
            yield regex_usage


def _extract_run_id(payload: Any, depth: int) -> Optional[str]:
    if depth > MAX_RECURSION_DEPTH or payload is None:
        return None

    if isinstance(payload, dict):
        lower = {str(k).lower(): v for k, v in payload.items()}
        for key in RUN_ID_KEYS:
            if key in lower:
                value = lower[key]
                if value is not None:
                    run_id = str(value).strip()
                    if run_id:
                        return run_id

        for value in payload.values():
            found = _extract_run_id(value, depth + 1)
            if found:
                return found
        return None

    if isinstance(payload, list):
        for item in payload:
            found = _extract_run_id(item, depth + 1)
            if found:
                return found
        return None

    if isinstance(payload, str):
        parsed = _parse_json_like(payload)
        if parsed is not None and parsed is not payload:
            return _extract_run_id(parsed, depth + 1)

    return None
