import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import List

from app.services.account_pool import AccountPoolService
from app.services.time_utils import utc_now_naive


@dataclass
class _FakeAccount:
    id: str
    status: str
    daily_used: int
    daily_quota: int
    model_group: str | None = None
    inflight_requests: int = 0
    inflight_updated_at: datetime | None = None
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


class _BatchResult:
    def __init__(self, *, fetch_rows=None, scalar_rows=None):
        self._fetch_rows = list(fetch_rows or [])
        self._scalar_rows = list(scalar_rows or [])

    def fetchall(self):
        return list(self._fetch_rows)

    def scalars(self):
        return _FakeScalars(self._scalar_rows)


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


class _BatchSession:
    def __init__(self, results):
        self._results = list(results)

    async def execute(self, _query):
        if not self._results:
            raise AssertionError("unexpected execute() call")
        return self._results.pop(0)


class _FakePGResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _FakePGBind:
    class _Dialect:
        name = "postgresql"

    dialect = _Dialect()


class _FakePGSession:
    def __init__(self, rows):
        if isinstance(rows, list):
            self._rows = list(rows)
        else:
            self._rows = [rows]
        self.flush_calls = 0
        self.execute_calls = 0

    def get_bind(self):
        return _FakePGBind()

    async def execute(self, _query):
        self.execute_calls += 1
        if not self._rows:
            raise AssertionError("unexpected execute() call")
        return _FakePGResult(self._rows.pop(0))

    async def flush(self):
        self.flush_calls += 1


def test_get_available_account_skips_exhausted_even_when_local_quota_appears_remaining():
    service = AccountPoolService()
    stale = _FakeAccount(
        id="acc-1",
        status="exhausted",
        daily_used=120,
        daily_quota=1000,
    )
    session = _FakeSession(execute_rows=[[stale], []])

    selected = asyncio.run(service.get_available_account(session, "claude-opus-4-6"))

    assert selected is None
    assert stale.status == "exhausted"
    assert stale.last_used_at is None
    assert session.flush_calls == 0
    assert session.commit_calls == 0


def test_get_available_account_prefers_fresh_active_accounts_over_partially_used_active_accounts():
    service = AccountPoolService()
    partially_used = _FakeAccount(
        id="acc-used",
        status="active",
        daily_used=100,
        daily_quota=1000,
    )
    fresh_active = _FakeAccount(
        id="acc-fresh",
        status="active",
        daily_used=0,
        daily_quota=1000,
    )
    session = _FakeSession(execute_rows=[[partially_used, fresh_active], []])

    selected = asyncio.run(service.get_available_account(session, "claude-opus-4-6"))

    assert selected is fresh_active
    assert session.flush_calls == 0
    assert session.commit_calls == 0


def test_get_available_account_returns_none_when_only_exhausted_accounts_exist():
    service = AccountPoolService()
    exhausted_with_remaining = _FakeAccount(
        id="acc-exhausted",
        status="exhausted",
        daily_used=100,
        daily_quota=1000,
    )
    session = _FakeSession(execute_rows=[[exhausted_with_remaining], []])

    selected = asyncio.run(service.get_available_account(session, "claude-opus-4-6"))

    assert selected is None
    assert session.flush_calls == 0
    assert session.commit_calls == 0


def test_get_available_account_prefers_lower_inflight_when_usage_is_equal():
    service = AccountPoolService()
    now = utc_now_naive()
    busier = _FakeAccount(
        id="acc-busy",
        status="active",
        daily_used=200,
        daily_quota=1000,
        inflight_requests=3,
        inflight_updated_at=now,
    )
    less_busy = _FakeAccount(
        id="acc-less-busy",
        status="active",
        daily_used=200,
        daily_quota=1000,
        inflight_requests=1,
        inflight_updated_at=now,
    )
    session = _FakeSession(execute_rows=[[busier, less_busy], []])

    selected = asyncio.run(service.get_available_account(session, "claude-opus-4-6"))

    assert selected is less_busy


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
    # Selection phase is pure-read; exhausted state is handled in write path.
    assert blocked.status == "active"
    assert blocked.updated_at is None
    assert session.flush_calls == 0


def test_get_available_account_postgres_reserves_last_used_at():
    service = AccountPoolService()
    account = _FakeAccount(
        id="acc-1",
        status="active",
        daily_used=0,
        daily_quota=1000,
        inflight_requests=0,
        inflight_updated_at=None,
        last_used_at=None,
        updated_at=None,
    )
    session = _FakePGSession(account)

    selected = asyncio.run(service.get_available_account(session, "claude-opus-4-6"))

    assert selected is account
    assert session.execute_calls == 1
    assert account.inflight_requests == 1
    assert account.inflight_updated_at is not None
    assert account.last_used_at is not None
    assert account.updated_at is not None
    assert session.flush_calls == 1


def test_get_available_account_postgres_resets_stale_inflight_lease_before_reserving():
    service = AccountPoolService()
    stale = datetime(2024, 1, 1)
    account = _FakeAccount(
        id="acc-1",
        status="active",
        daily_used=0,
        daily_quota=1000,
        inflight_requests=7,
        inflight_updated_at=stale,
    )
    session = _FakePGSession(account)

    selected = asyncio.run(service.get_available_account(session, "claude-opus-4-6"))

    assert selected is account
    assert account.inflight_requests == 1
    assert account.inflight_updated_at is not None


def test_get_available_account_postgres_falls_back_to_legacy_candidates_when_route_query_is_empty():
    service = AccountPoolService()
    legacy_account = _FakeAccount(
        id="acc-legacy",
        status="active",
        daily_used=0,
        daily_quota=1000,
    )
    session = _FakePGSession([None, legacy_account])

    selected = asyncio.run(service.get_available_account(session, "claude-opus-4-6"))

    assert selected is legacy_account
    assert session.execute_calls == 2
    assert session.flush_calls == 1


def test_get_accounts_by_model_groups_batches_and_deduplicates_route_and_legacy_rows():
    service = AccountPoolService()
    account_1 = _FakeAccount(
        id="acc-1",
        status="active",
        daily_used=0,
        daily_quota=1000,
        model_group="BookmarkVault",
    )
    account_2 = _FakeAccount(
        id="acc-2",
        status="active",
        daily_used=0,
        daily_quota=1000,
        model_group="BookmarkVault",
    )
    account_3 = _FakeAccount(
        id="acc-3",
        status="active",
        daily_used=0,
        daily_quota=1000,
        model_group="ipfs-file-manager",
    )
    session = _BatchSession(
        [
            _BatchResult(
                fetch_rows=[
                    ("acc-2", "BookmarkVault"),
                    ("acc-1", "BookmarkVault"),
                    ("acc-2", "ipfs-file-manager"),
                ]
            ),
            _BatchResult(scalar_rows=[account_2, account_1]),
            _BatchResult(scalar_rows=[account_1, account_2, account_3]),
        ]
    )

    accounts_by_model = asyncio.run(
        service.get_accounts_by_model_groups(
            session,
            ["BookmarkVault", "ipfs-file-manager"],
        )
    )

    assert [account.id for account in accounts_by_model["BookmarkVault"]] == ["acc-1", "acc-2"]
    assert [account.id for account in accounts_by_model["ipfs-file-manager"]] == ["acc-2", "acc-3"]


def test_get_account_models_map_batches_routes_and_falls_back_to_legacy_model_group():
    service = AccountPoolService()
    account_1 = _FakeAccount(
        id="acc-1",
        status="active",
        daily_used=0,
        daily_quota=1000,
        model_group="legacy-a",
    )
    account_2 = _FakeAccount(
        id="acc-2",
        status="active",
        daily_used=0,
        daily_quota=1000,
        model_group="legacy-b",
    )
    session = _BatchSession(
        [
            _BatchResult(
                fetch_rows=[
                    ("acc-1", "model-b"),
                    ("acc-1", "model-a"),
                    ("acc-1", "model-a"),
                ]
            ),
        ]
    )

    model_map = asyncio.run(
        service.get_account_models_map(
            session,
            [account_1, account_2],
        )
    )

    assert model_map["acc-1"] == ["model-b", "model-a"]
    assert model_map["acc-2"] == ["legacy-b"]
