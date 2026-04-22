"""Gateway-side prompt history budgeting and lightweight auto-compaction."""

from __future__ import annotations

import copy
import logging
import os
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from app.services.protocol_bridge import CanonicalMessage, CanonicalRequest, ProtocolBridge

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


GATEWAY_HISTORY_BUDGET_ENABLED = _env_bool("GATEWAY_HISTORY_BUDGET_ENABLED", True)
GATEWAY_HISTORY_BUDGET_TOKENS = max(
    0,
    int(os.getenv("GATEWAY_HISTORY_BUDGET_TOKENS", "120000")),
)
GATEWAY_HISTORY_COMPACT_MAX_CHARS = max(
    160,
    int(os.getenv("GATEWAY_HISTORY_COMPACT_MAX_CHARS", "1200")),
)
GATEWAY_HISTORY_COMPACT_RECENT_MESSAGES = max(
    1,
    int(os.getenv("GATEWAY_HISTORY_COMPACT_RECENT_MESSAGES", "4")),
)


@dataclass
class HistoryBudgetResult:
    request: CanonicalRequest
    applied: bool
    original_tokens: int
    compacted_tokens: int
    dropped_messages: int = 0


class HistoryBudgetService:
    """Trim older conversation history before the upstream prompt gets too large."""

    def __init__(
        self,
        *,
        enabled: bool = GATEWAY_HISTORY_BUDGET_ENABLED,
        max_input_tokens: int = GATEWAY_HISTORY_BUDGET_TOKENS,
        compact_max_chars: int = GATEWAY_HISTORY_COMPACT_MAX_CHARS,
        compact_recent_messages: int = GATEWAY_HISTORY_COMPACT_RECENT_MESSAGES,
    ) -> None:
        self.enabled = bool(enabled)
        self.max_input_tokens = max(0, int(max_input_tokens))
        self.compact_max_chars = max(160, int(compact_max_chars))
        self.compact_recent_messages = max(1, int(compact_recent_messages))

    def compact_request(
        self,
        bridge: ProtocolBridge,
        request: CanonicalRequest,
        token_counter: Any,
    ) -> HistoryBudgetResult:
        original_prompt = bridge.render_prompt(request)
        original_tokens = self._count_tokens(token_counter, original_prompt)

        if (
            not self.enabled
            or self.max_input_tokens <= 0
            or original_tokens <= self.max_input_tokens
            or len(request.messages) <= 1
        ):
            return HistoryBudgetResult(
                request=request,
                applied=False,
                original_tokens=original_tokens,
                compacted_tokens=original_tokens,
            )

        segments = self._segment_ranges(request)
        if len(segments) <= 1:
            return HistoryBudgetResult(
                request=request,
                applied=False,
                original_tokens=original_tokens,
                compacted_tokens=original_tokens,
            )

        best_request = request
        best_tokens = original_tokens
        best_dropped_messages = 0

        for keep_segment_index in range(1, len(segments)):
            keep_start = segments[keep_segment_index][0]
            candidate, candidate_tokens = self._select_candidate(
                bridge,
                request,
                keep_start,
                token_counter,
            )
            dropped_messages = keep_start

            if candidate_tokens < best_tokens:
                best_request = candidate
                best_tokens = candidate_tokens
                best_dropped_messages = dropped_messages

            if candidate_tokens <= self.max_input_tokens:
                logger.debug(
                    "Compacted request history source=%s model=%s original_tokens=%s compacted_tokens=%s dropped_messages=%s",
                    request.source,
                    request.model,
                    original_tokens,
                    candidate_tokens,
                    dropped_messages,
                )
                return HistoryBudgetResult(
                    request=candidate,
                    applied=True,
                    original_tokens=original_tokens,
                    compacted_tokens=candidate_tokens,
                    dropped_messages=dropped_messages,
                )

        if best_request is not request and best_tokens < original_tokens:
            logger.debug(
                "Compacted request history to best-effort floor source=%s model=%s original_tokens=%s compacted_tokens=%s dropped_messages=%s budget=%s",
                request.source,
                request.model,
                original_tokens,
                best_tokens,
                best_dropped_messages,
                self.max_input_tokens,
            )
            return HistoryBudgetResult(
                request=best_request,
                applied=True,
                original_tokens=original_tokens,
                compacted_tokens=best_tokens,
                dropped_messages=best_dropped_messages,
            )

        return HistoryBudgetResult(
            request=request,
            applied=False,
            original_tokens=original_tokens,
            compacted_tokens=original_tokens,
        )

    def _count_tokens(self, token_counter: Any, text: str) -> int:
        if not text:
            return 0
        counter = getattr(token_counter, "count", None)
        if callable(counter):
            try:
                return max(0, int(counter(text)))
            except Exception:
                logger.exception("History budget token counting failed; falling back to char length")
        return len(text)

    def _segment_ranges(self, request: CanonicalRequest) -> list[tuple[int, int]]:
        source_items = self._segment_source_items(request)
        if not source_items:
            return []

        segments: list[tuple[int, int]] = []
        idx = 0
        while idx < len(source_items):
            end = idx + 1
            if (
                self._contains_tool_use(source_items[idx])
                and idx + 1 < len(source_items)
                and self._contains_tool_result(source_items[idx + 1])
            ):
                end = idx + 2
            segments.append((idx, end))
            idx = end
        return segments

    def _segment_source_items(self, request: CanonicalRequest) -> list[Any]:
        if request.source == "anthropic_messages" and request.raw_messages:
            if len(request.raw_messages) == len(request.messages):
                return list(request.raw_messages)
            return list(request.messages)
        return list(request.messages)

    def _build_compacted_request(
        self,
        request: CanonicalRequest,
        keep_start: int,
        *,
        include_notice: bool,
    ) -> CanonicalRequest:
        compacted = copy.deepcopy(request)
        dropped_messages = request.messages[:keep_start]
        compacted.messages = copy.deepcopy(request.messages[keep_start:])

        if request.source == "anthropic_messages":
            if request.raw_messages and len(request.raw_messages) == len(request.messages):
                compacted.raw_messages = copy.deepcopy(request.raw_messages[keep_start:])
            else:
                compacted.raw_messages = []

        notice = self._build_compaction_notice(dropped_messages) if include_notice else ""
        if notice:
            compacted.system_prompt = self._merge_system_prompt(request.system_prompt, notice)

        return compacted

    def _select_candidate(
        self,
        bridge: ProtocolBridge,
        request: CanonicalRequest,
        keep_start: int,
        token_counter: Any,
    ) -> tuple[CanonicalRequest, int]:
        without_notice = self._build_compacted_request(
            request,
            keep_start,
            include_notice=False,
        )
        without_notice_tokens = self._count_tokens(
            token_counter,
            bridge.render_prompt(without_notice),
        )

        with_notice = self._build_compacted_request(
            request,
            keep_start,
            include_notice=True,
        )
        with_notice_tokens = self._count_tokens(
            token_counter,
            bridge.render_prompt(with_notice),
        )

        if with_notice_tokens <= self.max_input_tokens:
            return with_notice, with_notice_tokens
        if without_notice_tokens <= self.max_input_tokens:
            return without_notice, without_notice_tokens
        if without_notice_tokens <= with_notice_tokens:
            return without_notice, without_notice_tokens
        return with_notice, with_notice_tokens

    def _build_compaction_notice(self, dropped_messages: Iterable[CanonicalMessage]) -> str:
        dropped = [message for message in dropped_messages if isinstance(message, CanonicalMessage)]
        if not dropped:
            return ""

        lines = [
            "[Gateway compacted history to fit the upstream context budget.]",
            f"Omitted {len(dropped)} earlier message(s).",
        ]

        latest_omitted = dropped[-1]
        preview = self._compact_whitespace(latest_omitted.content)
        if preview:
            role_label = "Human" if latest_omitted.role == "user" else "Assistant"
            snippet = preview[: min(self.compact_max_chars, 56)]
            if len(snippet) < len(preview) and len(snippet) >= 3:
                snippet = f"{snippet[:-3]}..."
            lines.append(f"Latest omitted: {role_label}: {snippet}")

        lines.append("Use the retained recent turns; re-check tools/files if older details matter.")
        return "\n".join(lines)

    @staticmethod
    def _merge_system_prompt(system_prompt: str, compact_notice: str) -> str:
        if system_prompt and compact_notice:
            return f"{system_prompt}\n\n{compact_notice}"
        return system_prompt or compact_notice

    @staticmethod
    def _compact_whitespace(text: str) -> str:
        return " ".join(str(text or "").split())

    @staticmethod
    def _contains_tool_use(item: Any) -> bool:
        if isinstance(item, CanonicalMessage):
            return item.role == "assistant" and "[tool_call" in (item.content or "")
        if not isinstance(item, dict):
            return False
        content = item.get("content")
        blocks = content if isinstance(content, list) else [content]
        return any(
            isinstance(block, dict) and str(block.get("type") or "") == "tool_use"
            for block in blocks
        )

    @staticmethod
    def _contains_tool_result(item: Any) -> bool:
        if isinstance(item, CanonicalMessage):
            return item.role == "user" and "[tool_result" in (item.content or "")
        if not isinstance(item, dict):
            return False
        content = item.get("content")
        blocks = content if isinstance(content, list) else [content]
        return any(
            isinstance(block, dict) and str(block.get("type") or "") == "tool_result"
            for block in blocks
        )


_history_budget_service: Optional[HistoryBudgetService] = None


def get_history_budget_service() -> HistoryBudgetService:
    global _history_budget_service
    if _history_budget_service is None:
        _history_budget_service = HistoryBudgetService()
    return _history_budget_service
