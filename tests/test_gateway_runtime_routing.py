import asyncio
import json
import uuid
from types import SimpleNamespace

from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

import app.services.gateway_runtime as gateway_runtime_module
from app.services.gateway_runtime import GatewayAuthError, GatewayRuntime
from app.services.api_key import APIKeyService
from app.services.backend_client import BackendAPIError
from app.services.crypto import CryptoService
from app.services.error_handler import ErrorHandler
from app.models.database import APIKey


class _FakeAPIKeyService:
    async def validate_key(self, _session, _raw_key, _model):
        return True, None, SimpleNamespace(
            id="key-1",
            key_prefix="sk-test",
            name="test",
        )


class _FakeTimeoutAPIKeyService:
    async def validate_key(self, _session, _raw_key, _model):
        raise SQLAlchemyTimeoutError("QueuePool timeout")


class _FakeSession:
    async def commit(self):
        return None


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _FakeValidateSession:
    def __init__(self, row):
        self._row = row
        self.flush_calls = 0
        self.execute_calls = 0

    async def execute(self, _query):
        self.execute_calls += 1
        return _FakeResult(self._row)

    async def flush(self):
        self.flush_calls += 1


class _FakeCommitSession:
    def __init__(self):
        self.commit_calls = 0
        self.rollback_calls = 0

    async def commit(self):
        self.commit_calls += 1

    async def rollback(self):
        self.rollback_calls += 1


class _FakeSessionFactorySession(_FakeCommitSession):
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _TimeoutPermit:
    async def __aenter__(self):
        raise asyncio.TimeoutError()

    async def __aexit__(self, *_args):
        return False


class _FakeBusyDBLimiter:
    def acquire_db(self, timeout=10.0):
        return _TimeoutPermit()


class _FakeAccountPool:
    def __init__(self, picks, availability):
        self._picks = list(picks)
        self._availability = availability
        self.pick_calls = 0
        self.availability_calls = 0
        self.last_exclude_account_id = None

    async def get_available_account(self, _session, _model, exclude_account_id=None):
        self.pick_calls += 1
        self.last_exclude_account_id = exclude_account_id
        if not self._picks:
            return None
        return self._picks.pop(0)

    async def get_model_group_availability(self, _session, _model):
        self.availability_calls += 1
        return dict(self._availability)


class _FakeAdaptiveAccountPool:
    def __init__(self, failover_picks=None):
        self.defer_calls = 0
        self.last_defer_args = None
        self.exhaust_calls = 0
        self.disable_calls = 0
        self.release_calls = 0
        self.pick_calls = 0
        self.exclude_account_ids = []
        self._failover_picks = list(failover_picks or [])

    async def release_account_request(self, *_args, **_kwargs):
        self.release_calls += 1
        return True

    async def defer_account_selection(self, _session, account_id, defer_seconds):
        self.defer_calls += 1
        self.last_defer_args = (account_id, defer_seconds)
        return True

    async def mark_account_exhausted(self, *_args, **_kwargs):
        self.exhaust_calls += 1
        return True

    async def update_account(self, *_args, **_kwargs):
        self.disable_calls += 1
        return SimpleNamespace(id="acc-1")

    async def get_available_account(self, _session, _model, exclude_account_id=None):
        self.pick_calls += 1
        self.exclude_account_ids.append(exclude_account_id)
        if not self._failover_picks:
            return None
        return self._failover_picks.pop(0)


class _FakeFailoverBackendClient:
    def __init__(self, failure_status: int = 500, failure_response_data=None):
        self.sync_calls = []
        self.stream_calls = []
        self.failure_status = failure_status
        self.failure_response_data = failure_response_data if failure_response_data is not None else {}

    async def run_with_account(self, *, account, payload, account_pool):
        self.sync_calls.append(account.id)
        if len(self.sync_calls) == 1:
            raise BackendAPIError(
                message=f"Backend API returned error: {self.failure_status}",
                status_code=self.failure_status,
                response_data=self.failure_response_data,
            )
        return {"ok": True, "account_id": account.id, "payload": payload}

    async def execute_with_account(self, *, account, payload, stream, account_pool):
        self.stream_calls.append(account.id)
        attempt = len(self.stream_calls)

        async def _gen():
            if attempt == 1:
                raise BackendAPIError(
                    message=f"Backend API returned error: {self.failure_status}",
                    status_code=self.failure_status,
                    response_data=self.failure_response_data,
                )
            yield "data: ok"

        return _gen()


class _TrackingPersistAccountPool:
    def __init__(self):
        self.update_calls = 0
        self.history_calls = 0
        self.release_calls = 0

    async def update_token_usage(self, *_args, **_kwargs):
        self.update_calls += 1
        return SimpleNamespace(id="acc-1")

    async def record_usage_history(self, *_args, **_kwargs):
        self.history_calls += 1

    async def release_account_request(self, *_args, **_kwargs):
        self.release_calls += 1
        return True


class _TrackingPersistAPIKeyService:
    def __init__(self):
        self.update_calls = 0

    async def update_key_stats(self, *_args, **_kwargs):
        self.update_calls += 1
        return SimpleNamespace(id="key-1")


class _TrackingPersistStatsService:
    def __init__(self):
        self.update_calls = 0

    async def update_system_stats(self, *_args, **_kwargs):
        self.update_calls += 1


class _TrackingLoggerService:
    def __init__(self):
        self.success_calls = 0
        self.request_calls = 0

    async def log_success(self, session, **_kwargs):
        self.success_calls += 1
        return session

    async def log_request(self, session, **_kwargs):
        self.request_calls += 1
        return session


class _BlockingLoggerService(_TrackingLoggerService):
    def __init__(self, started_event: asyncio.Event, release_event: asyncio.Event):
        super().__init__()
        self.started_event = started_event
        self.release_event = release_event

    async def log_request(self, session, **_kwargs):
        self.request_calls += 1
        self.started_event.set()
        await self.release_event.wait()
        return session


class _CapturingLoggerService(_TrackingLoggerService):
    def __init__(self):
        super().__init__()
        self.last_request_kwargs = None

    async def log_request(self, session, **kwargs):
        self.request_calls += 1
        self.last_request_kwargs = kwargs
        return session


class _TrackingCallLoggerService:
    def __init__(self):
        self.calls = 0

    async def log_call(self, session, **_kwargs):
        self.calls += 1
        return session


class _CapturingCallLoggerService(_TrackingCallLoggerService):
    def __init__(self):
        super().__init__()
        self.last_kwargs = None

    async def log_call(self, session, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        return session


class _FakeSessionFactory:
    def __init__(self):
        self.sessions = []

    def __call__(self):
        session = _FakeSessionFactorySession()
        self.sessions.append(session)
        return session


class _FakePricingService:
    def calculate(self, *_args, **_kwargs):
        return "0", "0", "0"


class _TrackingUsageAggregator:
    def __init__(self, enqueue_result: bool = True):
        self.enqueue_result = enqueue_result
        self.enqueue_calls = 0

    async def enqueue_usage_update(self, **_kwargs):
        self.enqueue_calls += 1
        return self.enqueue_result


async def _none_input_mapping(_session, _model):
    return None


def _build_api_key(**overrides) -> APIKey:
    raw_key = str(overrides.pop("raw_key", "sk-test-key"))
    key_hash = CryptoService.hash_api_key(raw_key)
    data = {
        "id": str(uuid.uuid4()),
        "key_hash": key_hash,
        "key_prefix": "sk-test",
        "key_suffix": "1234",
        "name": "test",
        "model_groups": '["claude-opus-4-6"]',
        "status": "active",
        "used": 0,
        "total_requests": 0,
        "total_tokens": 0,
        "total_cost": "0",
    }
    data.update(overrides)
    return APIKey(**data)


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
    assert account_pool.last_exclude_account_id is None


def test_resolve_request_returns_quota_exceeded_when_all_accounts_exhausted():
    account_pool = _FakeAccountPool(
        picks=[None, None],
        availability={
            "total_accounts": 3,
            "active_accounts": 0,
            "routable_accounts": 0,
            "eligible_accounts": 0,
            "exhausted_accounts": 3,
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
            "active_accounts": 0,
            "routable_accounts": 0,
            "eligible_accounts": 0,
            "exhausted_accounts": 0,
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


def test_resolve_request_returns_service_unavailable_when_all_accounts_are_concurrency_limited():
    account_pool = _FakeAccountPool(
        picks=[None, None],
        availability={
            "total_accounts": 3,
            "active_accounts": 3,
            "routable_accounts": 3,
            "eligible_accounts": 3,
            "available_accounts": 0,
            "concurrency_limited_accounts": 3,
            "exhausted_accounts": 0,
            "stale_exhausted_accounts": 0,
            "status_counts": {"active": 3},
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
                request_id="req_busy_accounts",
            )
        )
        raise AssertionError("expected GatewayAuthError")
    except GatewayAuthError as exc:
        assert exc.error.status_code == 503
        assert exc.error.code == "service_unavailable"

    assert account_pool.pick_calls == 2
    assert account_pool.availability_calls == 1


def test_resolve_request_returns_service_unavailable_when_db_gate_busy():
    account_pool = _FakeAccountPool(
        picks=[SimpleNamespace(id="acc-1", name="a1")],
        availability={},
    )
    runtime = GatewayRuntime(
        api_key_service=_FakeAPIKeyService(),
        account_pool=account_pool,
        db_limiter=_FakeBusyDBLimiter(),
    )
    runtime.get_model_input_mapping = _none_input_mapping
    session = _FakeSession()

    try:
        asyncio.run(
            runtime.resolve_request(
                session=session,
                raw_key="sk-test",
                model="claude-opus-4-6",
                request_id="req_busy_db",
            )
        )
        raise AssertionError("expected GatewayAuthError")
    except GatewayAuthError as exc:
        assert exc.error.status_code == 503
        assert exc.error.code == "service_unavailable"

    assert account_pool.pick_calls == 0
    assert account_pool.availability_calls == 0


def test_resolve_request_maps_sqlalchemy_pool_timeout_to_503():
    runtime = GatewayRuntime(
        api_key_service=_FakeTimeoutAPIKeyService(),
        account_pool=_FakeAccountPool(picks=[], availability={}),
    )
    runtime.get_model_input_mapping = _none_input_mapping
    session = _FakeCommitSession()

    try:
        asyncio.run(
            runtime.resolve_request(
                session=session,
                raw_key="sk-test",
                model="claude-opus-4-6",
                request_id="req_sqlalchemy_timeout",
            )
        )
        raise AssertionError("expected GatewayAuthError")
    except GatewayAuthError as exc:
        assert exc.error.status_code == 503
        assert exc.error.code == "service_unavailable"

    assert session.rollback_calls >= 1


def test_run_db_guarded_maps_sqlalchemy_timeout_to_503():
    runtime = GatewayRuntime()
    session = _FakeCommitSession()

    async def _op():
        raise SQLAlchemyTimeoutError("QueuePool timeout")

    try:
        asyncio.run(runtime.run_db_guarded(session, _op))
        raise AssertionError("expected GatewayAuthError")
    except GatewayAuthError as exc:
        assert exc.error.status_code == 503
        assert exc.error.code == "service_unavailable"

    assert session.rollback_calls >= 1


def test_get_model_input_mapping_uses_cache():
    runtime = GatewayRuntime()
    session = _FakeValidateSession(SimpleNamespace(input_mapping='{"user_input":"in-0"}'))

    first = asyncio.run(runtime.get_model_input_mapping(session, "claude-opus-4-6"))
    second = asyncio.run(runtime.get_model_input_mapping(session, "claude-opus-4-6"))

    assert first == {"user_input": "in-0"}
    assert second == {"user_input": "in-0"}
    assert session.execute_calls == 1


def test_persist_success_degrades_gracefully_when_db_gate_busy():
    account_pool = _TrackingPersistAccountPool()
    api_key_service = _TrackingPersistAPIKeyService()
    stats_service = _TrackingPersistStatsService()
    usage_aggregator = _TrackingUsageAggregator(enqueue_result=True)
    logger_service = _TrackingLoggerService()
    call_logger = _TrackingCallLoggerService()
    session_factory = _FakeSessionFactory()
    runtime = GatewayRuntime(
        account_pool=account_pool,
        api_key_service=api_key_service,
        stats_service=stats_service,
        usage_aggregator=usage_aggregator,
        logger_service=logger_service,
        call_logger=call_logger,
        pricing_service=_FakePricingService(),
        db_limiter=_FakeBusyDBLimiter(),
        session_factory=session_factory,
    )
    session = _FakeCommitSession()

    resolved = SimpleNamespace(
        request_id="req_persist_busy",
        model="claude-opus-4-6",
        account=SimpleNamespace(id="acc-1", name="acc"),
        api_key=SimpleNamespace(id="key-1", key_prefix="sk-test", name="k"),
    )

    async def _run():
        await runtime.persist_success(
            session=session,
            resolved=resolved,
            api_type="openai",
            input_preview="i",
            output_preview="o",
            usage=SimpleNamespace(input_tokens=10, output_tokens=20, total_tokens=30),
            response_time_ms=120,
            is_stream=False,
            client_ip="127.0.0.1",
        )
        await runtime.drain_background_tasks()
        await runtime.close()

    asyncio.run(_run())

    assert account_pool.update_calls == 1
    assert api_key_service.update_calls == 0
    assert stats_service.update_calls == 0
    assert usage_aggregator.enqueue_calls == 1
    assert session.commit_calls == 1
    assert logger_service.success_calls == 0
    assert call_logger.calls == 0
    assert len(session_factory.sessions) == 1


def test_persist_success_stream_defers_noncritical_logs_to_background_session():
    account_pool = _TrackingPersistAccountPool()
    api_key_service = _TrackingPersistAPIKeyService()
    stats_service = _TrackingPersistStatsService()
    usage_aggregator = _TrackingUsageAggregator(enqueue_result=True)
    logger_service = _TrackingLoggerService()
    call_logger = _TrackingCallLoggerService()
    session_factory = _FakeSessionFactory()
    runtime = GatewayRuntime(
        account_pool=account_pool,
        api_key_service=api_key_service,
        stats_service=stats_service,
        usage_aggregator=usage_aggregator,
        logger_service=logger_service,
        call_logger=call_logger,
        pricing_service=_FakePricingService(),
        session_factory=session_factory,
    )
    session = _FakeCommitSession()
    resolved = SimpleNamespace(
        request_id="req_stream_success",
        model="claude-opus-4-6",
        account=SimpleNamespace(id="acc-1", name="acc"),
        api_key=SimpleNamespace(id="key-1", key_prefix="sk-test", name="k"),
    )

    async def _run():
        await runtime.persist_success(
            session=session,
            resolved=resolved,
            api_type="openai",
            input_preview="i",
            output_preview="o",
            usage=SimpleNamespace(input_tokens=10, output_tokens=20, total_tokens=30),
            response_time_ms=120,
            is_stream=True,
            client_ip="127.0.0.1",
        )
        await runtime.drain_background_tasks()
        await runtime.close()

    asyncio.run(_run())

    assert account_pool.update_calls == 1
    assert account_pool.history_calls == 0
    assert api_key_service.update_calls == 0
    assert stats_service.update_calls == 0
    assert usage_aggregator.enqueue_calls == 1
    assert session.commit_calls == 1
    assert logger_service.success_calls == 1
    assert call_logger.calls == 1
    assert len(session_factory.sessions) == 1
    assert session_factory.sessions[0].commit_calls == 1


def test_persist_error_can_defer_noncritical_logs_to_background_session():
    logger_service = _TrackingLoggerService()
    call_logger = _TrackingCallLoggerService()
    session_factory = _FakeSessionFactory()
    runtime = GatewayRuntime(
        logger_service=logger_service,
        call_logger=call_logger,
        session_factory=session_factory,
    )
    session = _FakeCommitSession()
    resolved = SimpleNamespace(
        request_id="req_stream_error",
        model="claude-opus-4-6",
        account=SimpleNamespace(id="acc-1", name="acc"),
        api_key=SimpleNamespace(id="key-1", key_prefix="sk-test", name="k"),
    )

    async def _run():
        await runtime.persist_error(
            session=session,
            resolved=resolved,
            api_type="openai",
            model="claude-opus-4-6",
            input_preview="i",
            response_time_ms=88,
            client_ip="127.0.0.1",
            error_message="upstream failed",
            defer_noncritical_logs=True,
        )
        await runtime.drain_background_tasks()
        await runtime.close()

    asyncio.run(_run())

    assert session.commit_calls == 1
    assert logger_service.request_calls == 1
    assert call_logger.calls == 1
    assert len(session_factory.sessions) == 1
    assert session_factory.sessions[0].commit_calls == 1


def test_persist_error_drops_background_logs_when_queue_is_full():
    original_workers = gateway_runtime_module.BACKGROUND_LOG_WORKERS
    original_queue_size = gateway_runtime_module.BACKGROUND_LOG_QUEUE_MAX_SIZE
    original_sample_rate = gateway_runtime_module.CALL_LOG_SAMPLE_RATE
    gateway_runtime_module.BACKGROUND_LOG_WORKERS = 1
    gateway_runtime_module.BACKGROUND_LOG_QUEUE_MAX_SIZE = 100
    gateway_runtime_module.CALL_LOG_SAMPLE_RATE = 0.0

    session_factory = _FakeSessionFactory()

    async def _run():
        started_event = asyncio.Event()
        release_event = asyncio.Event()
        logger_service = _BlockingLoggerService(started_event, release_event)
        runtime = GatewayRuntime(
            logger_service=logger_service,
            call_logger=_TrackingCallLoggerService(),
            session_factory=session_factory,
        )
        session = _FakeCommitSession()

        await runtime.persist_error(
            session=session,
            resolved=None,
            api_type="openai",
            model="claude-opus-4-6",
            input_preview="i1",
            response_time_ms=10,
            client_ip="127.0.0.1",
            error_message="blocked-1",
            defer_noncritical_logs=True,
        )
        await started_event.wait()

        queued_results = await asyncio.gather(*[
            runtime.persist_error(
                session=session,
                resolved=None,
                api_type="openai",
                model="claude-opus-4-6",
                input_preview=f"i{index}",
                response_time_ms=10,
                client_ip="127.0.0.1",
                error_message=f"blocked-{index}",
                defer_noncritical_logs=True,
            )
            for index in range(2, 103)
        ])

        assert queued_results == [None] * 101
        stats_before_release = runtime.background_log_stats()
        assert stats_before_release["dropped_events"] >= 1
        assert stats_before_release["queue_size"] >= 1

        release_event.set()
        await runtime.drain_background_tasks()

        stats_after_drain = runtime.background_log_stats()
        await runtime.close()
        return logger_service.request_calls, stats_after_drain

    try:
        request_calls, stats_after_drain = asyncio.run(_run())
    finally:
        gateway_runtime_module.BACKGROUND_LOG_WORKERS = original_workers
        gateway_runtime_module.BACKGROUND_LOG_QUEUE_MAX_SIZE = original_queue_size
        gateway_runtime_module.CALL_LOG_SAMPLE_RATE = original_sample_rate

    assert request_calls < 102
    assert request_calls >= 2
    assert stats_after_drain["queue_size"] == 0
    assert stats_after_drain["processed_events"] == request_calls
    assert stats_after_drain["dropped_events"] == 102 - request_calls


def test_map_validation_error_cost_limit_is_quota_exceeded():
    runtime = GatewayRuntime()
    err = runtime._map_validation_error("API key cost limit exceeded")
    assert err.status_code == 429
    assert err.code == "quota_exceeded"


def test_validate_key_blocks_token_quota_without_mutating():
    service = APIKeyService()
    api_key = _build_api_key(token_quota=100, total_tokens=100)
    session = _FakeValidateSession(api_key)

    valid, message, _obj = asyncio.run(
        service.validate_key(
            session,
            "sk-test-key",
            requested_model="claude-opus-4-6",
        )
    )

    assert not valid
    assert message == "API key token quota exceeded"
    assert session.flush_calls == 0


def test_validate_key_blocks_cost_limit():
    service = APIKeyService()
    api_key = _build_api_key(cost_limit="0.50", total_cost="0.50")
    session = _FakeValidateSession(api_key)

    valid, message, _obj = asyncio.run(
        service.validate_key(
            session,
            "sk-test-key",
            requested_model="claude-opus-4-6",
        )
    )

    assert not valid
    assert message == "API key cost limit exceeded"


def test_validate_key_handles_invalid_model_groups_json():
    service = APIKeyService()
    api_key = _build_api_key(model_groups="not-json")
    session = _FakeValidateSession(api_key)

    valid, message, _obj = asyncio.run(
        service.validate_key(
            session,
            "sk-test-key",
            requested_model="claude-opus-4-6",
        )
    )

    assert not valid
    assert message == "API key model groups config invalid"


def test_error_handler_maps_upstream_500_to_service_unavailable():
    handler = ErrorHandler()
    exc = BackendAPIError(
        message="Backend API returned error: 500",
        status_code=500,
        response_data={},
    )
    api_error = handler.from_backend_exception(exc)
    assert api_error.status_code == 503
    assert api_error.code == "service_unavailable"


def test_error_handler_maps_upstream_402_to_quota_exceeded():
    handler = ErrorHandler()
    exc = BackendAPIError(
        message="Backend API returned error: 402",
        status_code=402,
        response_data={},
    )
    api_error = handler.from_backend_exception(exc)
    assert api_error.status_code == 429
    assert api_error.code == "quota_exceeded"


def test_error_handler_hides_details_in_api_formats_but_keeps_logs():
    handler = ErrorHandler()
    api_error = handler.parse_backend_error(
        {
            "message": "Tool arguments invalid",
            "raw": "contact support@stack-ai.com",
        },
        status_code=400,
    )

    openai_payload = handler.to_openai_error(api_error)
    anthropic_payload = handler.to_anthropic_error(api_error)
    gemini_payload = handler.to_gemini_error(api_error)

    assert "details" not in openai_payload["error"]
    assert "details" not in anthropic_payload["error"]
    assert "details" not in gemini_payload["error"]
    assert api_error.details is not None
    assert api_error.details["raw"] == "contact upstream support"


def test_runtime_resolve_request_id_prefers_client_header():
    runtime = GatewayRuntime()
    request_id = runtime.resolve_request_id(headers={"x-st-request-id": "tenantA-user-0001-s03"})
    assert request_id == "tenantA-user-0001-s03"


def test_persist_error_serializes_api_error_context_to_logs():
    logger_service = _CapturingLoggerService()
    call_logger = _CapturingCallLoggerService()
    session_factory = _FakeSessionFactory()
    runtime = GatewayRuntime(
        logger_service=logger_service,
        call_logger=call_logger,
        session_factory=session_factory,
    )
    session = _FakeCommitSession()
    handler = ErrorHandler()
    api_error = handler.parse_backend_error(
        {
            "message": "Tool arguments invalid",
            "raw": "upstream rejected request body",
        },
        status_code=400,
    )

    async def _run():
        await runtime.persist_error(
            session=session,
            resolved=None,
            api_type="openai",
            model="claude-opus-4-6",
            input_preview="i",
            response_time_ms=42,
            client_ip="127.0.0.1",
            error_message=api_error.message,
            api_error=api_error,
            defer_noncritical_logs=True,
        )
        await runtime.drain_background_tasks()
        await runtime.close()

    asyncio.run(_run())

    assert logger_service.last_request_kwargs is not None
    assert call_logger.last_kwargs is not None
    logged_payload = json.loads(logger_service.last_request_kwargs["error_message"])
    assert logged_payload["message"] == "Invalid request"
    assert logged_payload["type"] == "invalid_request_error"
    assert logged_payload["status_code"] == 400
    assert logged_payload["details"]["message"] == "Tool arguments invalid"


def test_adapt_account_state_on_transient_error_defers_account():
    account_pool = _FakeAdaptiveAccountPool()
    runtime = GatewayRuntime(account_pool=account_pool)
    resolved = SimpleNamespace(
        account=SimpleNamespace(id="acc-1"),
        model="claude-opus-4-6",
    )

    asyncio.run(
        runtime._adapt_account_state_on_error(
            session=object(),
            resolved=resolved,
            error_message="Upstream service unavailable",
        )
    )

    assert account_pool.defer_calls == 1
    assert account_pool.release_calls == 1
    assert account_pool.last_defer_args is not None
    assert account_pool.last_defer_args[0] == "acc-1"
    assert account_pool.last_defer_args[1] > 0
    assert account_pool.exhaust_calls == 0
    assert account_pool.disable_calls == 0


def test_run_sync_failover_to_backup_account_on_transient_error():
    backup = SimpleNamespace(id="acc-backup")
    account_pool = _FakeAdaptiveAccountPool(failover_picks=[backup])
    backend_client = _FakeFailoverBackendClient()
    runtime = GatewayRuntime(account_pool=account_pool, backend_client=backend_client)
    session = _FakeCommitSession()
    primary = SimpleNamespace(id="acc-primary")
    resolved = SimpleNamespace(
        account=primary,
        model="claude-opus-4-6",
    )

    result = asyncio.run(
        runtime.run_sync(
            resolved=resolved,
            payload={"k": "v"},
            session=session,
        )
    )

    assert result["ok"] is True
    assert result["account_id"] == "acc-backup"
    assert backend_client.sync_calls == ["acc-primary", "acc-backup"]
    assert resolved.account is backup
    assert account_pool.defer_calls == 1
    assert account_pool.pick_calls == 1
    assert account_pool.exclude_account_ids == ["acc-primary"]
    assert session.commit_calls >= 1


def test_run_stream_failover_to_backup_account_before_first_token():
    backup = SimpleNamespace(id="acc-backup")
    account_pool = _FakeAdaptiveAccountPool(failover_picks=[backup])
    backend_client = _FakeFailoverBackendClient()
    runtime = GatewayRuntime(account_pool=account_pool, backend_client=backend_client)
    session = _FakeCommitSession()
    primary = SimpleNamespace(id="acc-primary")
    resolved = SimpleNamespace(
        account=primary,
        model="claude-opus-4-6",
    )

    async def _run():
        stream = await runtime.run_stream(
            resolved=resolved,
            payload={"stream": True},
            session=session,
        )
        chunks = []
        async for chunk in stream:
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(_run())

    assert chunks == ["data: ok"]
    assert backend_client.stream_calls == ["acc-primary", "acc-backup"]
    assert resolved.account is backup
    assert account_pool.defer_calls == 1
    assert account_pool.pick_calls == 1
    assert account_pool.exclude_account_ids == ["acc-primary"]
    assert session.commit_calls >= 1


def test_run_sync_failover_on_upstream_402_marks_account_exhausted():
    backup = SimpleNamespace(id="acc-backup")
    account_pool = _FakeAdaptiveAccountPool(failover_picks=[backup])
    backend_client = _FakeFailoverBackendClient(failure_status=402, failure_response_data={})
    runtime = GatewayRuntime(account_pool=account_pool, backend_client=backend_client)
    session = _FakeCommitSession()
    primary = SimpleNamespace(id="acc-primary")
    resolved = SimpleNamespace(
        account=primary,
        model="claude-opus-4-6",
    )

    result = asyncio.run(
        runtime.run_sync(
            resolved=resolved,
            payload={"k": "v"},
            session=session,
        )
    )

    assert result["ok"] is True
    assert result["account_id"] == "acc-backup"
    assert backend_client.sync_calls == ["acc-primary", "acc-backup"]
    assert resolved.account is backup
    assert account_pool.exhaust_calls == 1
    assert account_pool.defer_calls == 0
    assert account_pool.pick_calls == 1
    assert account_pool.exclude_account_ids == ["acc-primary"]
    assert session.commit_calls >= 1
