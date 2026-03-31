"""
Public API Key Info Service
构建 API Key 自查询接口的响应载荷。
"""

from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from app.services.time_utils import ensure_utc, utc_now


def _to_decimal(value: Optional[str]) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _decimal_to_str(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None

    normalized = format(value.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def _format_token_display(value: Optional[int]) -> str:
    token_count = int(value or 0)

    if token_count >= 1_000_000:
        compact = f"{token_count / 1_000_000:.1f}".rstrip("0").rstrip(".")
        return f"{compact}M tokens"
    if token_count >= 1_000:
        compact = f"{token_count / 1_000:.1f}".rstrip("0").rstrip(".")
        return f"{compact}K tokens"
    return f"{token_count} tokens"


def build_integer_limit_info(total: Optional[int], used: int) -> Dict[str, Any]:
    exhausted = total is not None and used >= total
    return {
        "total": total,
        "used": used,
        "remaining": max(total - used, 0) if total is not None else None,
        "unlimited": total is None,
        "exhausted": exhausted,
    }


def build_decimal_limit_info(total: Optional[str], used: Optional[str]) -> Dict[str, Any]:
    total_decimal = _to_decimal(total)
    used_decimal = _to_decimal(used) or Decimal("0")
    exhausted = total_decimal is not None and used_decimal >= total_decimal

    remaining = None
    if total_decimal is not None:
        remaining = _decimal_to_str(max(total_decimal - used_decimal, Decimal("0")))

    return {
        "total": _decimal_to_str(total_decimal),
        "used": _decimal_to_str(used_decimal),
        "remaining": remaining,
        "unlimited": total_decimal is None,
        "exhausted": exhausted,
    }


def _build_key_status(api_key: Any) -> Dict[str, Any]:
    reasons: List[str] = []
    now = utc_now()

    legacy_tokens = build_integer_limit_info(api_key.quota, api_key.used or 0)
    request_quota = build_integer_limit_info(api_key.request_quota, api_key.total_requests or 0)
    token_quota = build_integer_limit_info(api_key.token_quota, api_key.total_tokens or 0)
    cost_quota = build_decimal_limit_info(api_key.cost_limit, api_key.total_cost or "0")

    if api_key.status == "revoked":
        reasons.append("revoked")
    expires_at = ensure_utc(api_key.expires_at)
    if expires_at and expires_at < now:
        reasons.append("expired")
    if api_key.status == "exhausted":
        reasons.append("status_exhausted")
    if legacy_tokens["exhausted"]:
        reasons.append("legacy_token_quota_exceeded")
    if request_quota["exhausted"]:
        reasons.append("request_quota_exceeded")
    if token_quota["exhausted"]:
        reasons.append("token_quota_exceeded")
    if cost_quota["exhausted"]:
        reasons.append("cost_limit_exceeded")

    if "revoked" in reasons:
        status = "revoked"
    elif "expired" in reasons:
        status = "expired"
    elif any(reason.endswith("_exceeded") or reason == "status_exhausted" for reason in reasons):
        status = "exhausted"
    else:
        status = "active"

    return {
        "status": status,
        "reasons": reasons,
        "legacy_tokens": legacy_tokens,
        "request_quota": request_quota,
        "token_quota": token_quota,
        "cost_quota": cost_quota,
    }


def _account_has_remaining_quota(account: Any) -> bool:
    daily_quota = getattr(account, "daily_quota", None)
    daily_used = getattr(account, "daily_used", 0) or 0

    if daily_quota is None:
        return True

    return daily_used < daily_quota


def _build_model_status(accounts: List[Any]) -> Dict[str, Any]:
    if not accounts:
        return {
            "status": "unavailable",
            "unavailable_reasons": ["no_accounts"],
        }

    active_accounts = [
        account for account in accounts
        if getattr(account, "status", None) == "active"
    ]
    if any(_account_has_remaining_quota(account) for account in active_accounts):
        return {
            "status": "active",
            "unavailable_reasons": [],
        }

    account_statuses = {getattr(account, "status", None) for account in accounts}
    reasons: List[str] = []

    if active_accounts and all(not _account_has_remaining_quota(account) for account in active_accounts):
        reasons.append("accounts_exhausted")
    elif "exhausted" in account_statuses:
        reasons.append("accounts_exhausted")

    if "disabled" in account_statuses and not active_accounts:
        reasons.append("accounts_disabled")

    if not reasons:
        reasons.append("no_available_accounts")

    if reasons == ["accounts_disabled"]:
        status = "disabled"
    elif reasons == ["accounts_exhausted"]:
        status = "exhausted"
    else:
        status = "unavailable"

    return {
        "status": status,
        "unavailable_reasons": reasons,
    }


def _build_model_available_tokens(accounts: List[Any]) -> int:
    available_tokens = 0

    for account in accounts:
        if getattr(account, "status", None) != "active":
            continue

        daily_quota = getattr(account, "daily_quota", 0) or 0
        daily_used = getattr(account, "daily_used", 0) or 0
        available_tokens += max(daily_quota - daily_used, 0)

    return available_tokens


def _build_default_model_usage(api_key: Any, model_count: int) -> Dict[str, Any]:
    if model_count == 1:
        return {
            "requests": api_key.total_requests or 0,
            "tokens": api_key.total_tokens or 0,
            "cost": api_key.total_cost or "0",
            "legacy_tokens": api_key.used or 0,
        }

    return {
        "requests": 0,
        "tokens": 0,
        "cost": "0",
        "legacy_tokens": 0,
    }


def build_public_key_info_payload(
    api_key: Any,
    models: List[Dict[str, Any]],
) -> Dict[str, Any]:
    key_status_info = _build_key_status(api_key)

    model_items: List[Dict[str, Any]] = []
    for model in models:
        model_available_tokens = _build_model_available_tokens(model.get("accounts", []))
        usage = model.get("usage") or _build_default_model_usage(api_key, len(models))

        if key_status_info["status"] != "active":
            model_status = key_status_info["status"]
            model_reasons = list(key_status_info["reasons"])
        else:
            model_status_info = _build_model_status(model.get("accounts", []))
            model_status = model_status_info["status"]
            model_reasons = model_status_info["unavailable_reasons"]

        model_items.append({
            "model": model["id"],
            "status": model_status,
            "unavailable_reasons": model_reasons,
            "current_usage": _format_token_display(usage.get("tokens", 0)),
            "available_tokens": _format_token_display(
                model_available_tokens if key_status_info["status"] == "active" else 0
            ),
        })

    return {
        "models": model_items,
    }
