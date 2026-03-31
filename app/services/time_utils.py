"""
UTC time helpers.

The current schema stores naive UTC timestamps in multiple tables. Runtime logic
should still reason in timezone-aware UTC and normalize to naive values only at
database boundaries for compatibility.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional


UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_now_naive() -> datetime:
    return utc_now().replace(tzinfo=None)


def utc_today() -> date:
    return utc_now().date()


def ensure_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_utc_naive(value: datetime) -> datetime:
    normalized = ensure_utc(value)
    assert normalized is not None
    return normalized.replace(tzinfo=None)
