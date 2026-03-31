"""
Cost recalculation helpers.

These helpers preserve the existing "per call, then sum" rounding semantics by
replaying pricing on each stored call-log row instead of aggregating tokens
before pricing.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Iterable, Optional, Tuple

from app.services.pricing import PricingService, get_pricing_service


@dataclass
class RecalculatedCostTotals:
    total_cost: str
    total_requests: int
    total_tokens: int


def recalculate_cost_totals(
    rows: Iterable[Tuple[Optional[str], Optional[str], Optional[int], Optional[int]]],
    *,
    pricing_service: Optional[PricingService] = None,
) -> RecalculatedCostTotals:
    """
    Recalculate totals from call-log rows while preserving per-row rounding.

    Each row is expected to be: (model, model_group, input_tokens, output_tokens)
    """
    pricing = pricing_service or get_pricing_service()
    total_cost = Decimal("0")
    total_requests = 0
    total_tokens = 0

    for model, model_group, input_tokens, output_tokens in rows:
        safe_input = max(0, int(input_tokens or 0))
        safe_output = max(0, int(output_tokens or 0))
        effective_model = str(model or model_group or "")

        _, _, cost = pricing.calculate(effective_model, safe_input, safe_output)
        total_cost += Decimal(cost)
        total_requests += 1
        total_tokens += safe_input + safe_output

    return RecalculatedCostTotals(
        total_cost=str(total_cost),
        total_requests=total_requests,
        total_tokens=total_tokens,
    )


def recalculate_cost_totals_by_key(
    rows: Iterable[Tuple[Optional[str], Optional[str], Optional[str], Optional[int], Optional[int]]],
    *,
    pricing_service: Optional[PricingService] = None,
) -> Dict[str, RecalculatedCostTotals]:
    """
    Recalculate totals grouped by API key id.

    Each row is expected to be:
    (api_key_id, model, model_group, input_tokens, output_tokens)
    """
    grouped_rows: Dict[str, list[Tuple[Optional[str], Optional[str], Optional[int], Optional[int]]]] = {}
    for api_key_id, model, model_group, input_tokens, output_tokens in rows:
        if not api_key_id:
            continue
        grouped_rows.setdefault(str(api_key_id), []).append(
            (model, model_group, input_tokens, output_tokens)
        )

    pricing = pricing_service or get_pricing_service()
    return {
        key_id: recalculate_cost_totals(group_rows, pricing_service=pricing)
        for key_id, group_rows in grouped_rows.items()
    }
