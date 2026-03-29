import asyncio
from types import SimpleNamespace

from app.services.gateway_runtime import GatewayAuthError, GatewayRuntime


class _FakeAPIKeyService:
    async def validate_key(self, _session, _raw_key, _model):
        return True, None, SimpleNamespace(
            id="key-1",
            key_prefix="sk-test",
            name="test",
        )


class _FakeSession:
    async def commit(self):
        return None


class _FakeAccountPool:
    def __init__(self, picks, availability):
        self._picks = list(picks)
        self._availability = availability
        self.pick_calls = 0
        self.availability_calls = 0

    async def get_available_account(self, _session, _model):
        self.pick_calls += 1
        if not self._picks:
            return None
        return self._picks.pop(0)

    async def get_model_group_availability(self, _session, _model):
        self.availability_calls += 1
        return dict(self._availability)


async def _none_input_mapping(_session, _model):
    return None


def test_resolve_request_retries_once_then_succeeds():
    account_pool = _FakeAccountPool(
        picks=[None, SimpleNamespace(id="acc-1", name="a1")],
        availability={},
    )
    runtime = GatewayRuntime(
        api_key_service=_FakeAPIKeyService(),
        account_pool=account_pool,
    )
    runtime.get_model_input_mapping = _none_input_mapping
    session = _FakeSession()
    resolved = asyncio.run(
        runtime.resolve_request(
            session=session,
            raw_key="sk-test",
            model="claude-opus-4-6",
            request_id="req_1",
        )
    )

    assert resolved.account.id == "acc-1"
    assert account_pool.pick_calls == 2
    assert account_pool.availability_calls == 0


def test_resolve_request_returns_quota_exceeded_when_all_accounts_exhausted():
    account_pool = _FakeAccountPool(
        picks=[None, None],
        availability={
            "total_accounts": 3,
            "routable_accounts": 3,
            "eligible_accounts": 0,
            "stale_exhausted_accounts": 0,
            "status_counts": {"active": 0, "exhausted": 3},
        },
    )
    runtime = GatewayRuntime(
        api_key_service=_FakeAPIKeyService(),
        account_pool=account_pool,
    )
    runtime.get_model_input_mapping = _none_input_mapping
    session = _FakeSession()
    try:
        asyncio.run(
            runtime.resolve_request(
                session=session,
                raw_key="sk-test",
                model="claude-opus-4-6",
                request_id="req_2",
            )
        )
        raise AssertionError("expected GatewayAuthError")
    except GatewayAuthError as exc:
        assert exc.error.status_code == 429
        assert exc.error.code == "quota_exceeded"
        assert "daily quota" in exc.error.message

    assert account_pool.pick_calls == 2
    assert account_pool.availability_calls == 1


def test_resolve_request_returns_service_unavailable_when_no_accounts_configured():
    account_pool = _FakeAccountPool(
        picks=[None, None],
        availability={
            "total_accounts": 0,
            "routable_accounts": 0,
            "eligible_accounts": 0,
            "stale_exhausted_accounts": 0,
            "status_counts": {},
        },
    )
    runtime = GatewayRuntime(
        api_key_service=_FakeAPIKeyService(),
        account_pool=account_pool,
    )
    runtime.get_model_input_mapping = _none_input_mapping
    session = _FakeSession()
    try:
        asyncio.run(
            runtime.resolve_request(
                session=session,
                raw_key="sk-test",
                model="claude-opus-4-6",
                request_id="req_3",
            )
        )
        raise AssertionError("expected GatewayAuthError")
    except GatewayAuthError as exc:
        assert exc.error.status_code == 503
        assert exc.error.code == "service_unavailable"
        assert exc.error.message == "Upstream service unavailable"

    assert account_pool.pick_calls == 2
    assert account_pool.availability_calls == 1
