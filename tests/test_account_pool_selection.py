import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import List

from app.services.account_pool import AccountPoolService


@dataclass
class _FakeAccount:
    id: str
    status: str
    daily_used: int
    daily_quota: int
    last_used_at: datetime | None = None
    updated_at: datetime | None = None


class _FakeScalars:
    def __init__(self, rows: List[_FakeAccount]):
        self._rows = rows

    def unique(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeResult:
    def __init__(self, rows: List[_FakeAccount]):
        self._rows = rows

    def scalars(self):
        return _FakeScalars(self._rows)


class _FakeSession:
    def __init__(self, execute_rows: List[List[_FakeAccount]]):
        self._execute_rows = list(execute_rows)
        self.flush_calls = 0
        self.commit_calls = 0

    async def execute(self, _query):
        if not self._execute_rows:
            raise AssertionError("unexpected execute() call")
        return _FakeResult(self._execute_rows.pop(0))

    async def flush(self):
        self.flush_calls += 1

    async def commit(self):
        self.commit_calls += 1


def test_get_available_account_recovers_stale_exhausted_status():
    service = AccountPoolService()
    stale = _FakeAccount(
        id="acc-1",
        status="exhausted",
        daily_used=120,
        daily_quota=1000,
    )
    session = _FakeSession(execute_rows=[[stale], []])

    selected = asyncio.run(service.get_available_account(session, "claude-opus-4-6"))

    assert selected is stale
    assert stale.status == "active"
    assert stale.last_used_at is not None
    assert session.flush_calls >= 1
    assert session.commit_calls == 1


def test_get_available_account_returns_none_when_no_active_or_recoverable():
    service = AccountPoolService()
    exhausted = _FakeAccount(
        id="acc-1",
        status="exhausted",
        daily_used=1000,
        daily_quota=1000,
    )
    disabled = _FakeAccount(
        id="acc-2",
        status="disabled",
        daily_used=0,
        daily_quota=1000,
    )
    session = _FakeSession(execute_rows=[[exhausted, disabled], []])

    selected = asyncio.run(service.get_available_account(session, "claude-opus-4-6"))

    assert selected is None
    assert session.commit_calls == 0


def test_get_available_account_marks_quota_blocked_active_as_exhausted():
    service = AccountPoolService()
    blocked = _FakeAccount(
        id="acc-1",
        status="active",
        daily_used=1000,
        daily_quota=1000,
    )
    session = _FakeSession(execute_rows=[[blocked], []])

    selected = asyncio.run(service.get_available_account(session, "claude-opus-4-6"))

    assert selected is None
    assert blocked.status == "exhausted"
    assert blocked.updated_at is not None
    assert session.flush_calls >= 1
