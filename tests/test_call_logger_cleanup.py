import asyncio
from unittest.mock import AsyncMock, patch

from app.services.call_logger import CallLoggerService, MAX_CALL_LOG_ENTRIES


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _DeleteResult:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class _ExecuteOnlySession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.executed_queries = []

    async def execute(self, query):
        self.executed_queries.append(str(query))
        return self._responses.pop(0)


class _LogCallSession:
    def __init__(self):
        self.added = []
        self.flush_count = 0

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flush_count += 1


class _FakePricingService:
    def calculate(self, _model, _input_tokens, _output_tokens):
        return "0.1", "0.2", "0.3"


def test_cleanup_excess_logs_returns_zero_when_no_keep_ids():
    async def _run():
        service = CallLoggerService()
        session = _ExecuteOnlySession([_RowsResult([])])

        deleted = await service.cleanup_excess_logs(session, max_logs=100)

        assert deleted == 0
        assert len(session.executed_queries) == 1
        assert "SELECT call_logs.id" in session.executed_queries[0]

    asyncio.run(_run())


def test_cleanup_excess_logs_deletes_rows_outside_recent_window():
    async def _run():
        service = CallLoggerService()
        session = _ExecuteOnlySession(
            [
                _RowsResult([("log-129",), ("log-128",)]),
                _DeleteResult(30),
            ]
        )

        deleted = await service.cleanup_excess_logs(session, max_logs=2)

        assert deleted == 30
        assert len(session.executed_queries) == 2
        assert "SELECT call_logs.id" in session.executed_queries[0]
        assert "LIMIT" in session.executed_queries[0]
        assert "DELETE FROM call_logs" in session.executed_queries[1]
        assert "NOT IN" in session.executed_queries[1]

    asyncio.run(_run())


def test_log_call_triggers_auto_cleanup_with_100_limit():
    async def _run():
        service = CallLoggerService()
        session = _LogCallSession()
        cleanup_mock = AsyncMock(return_value=0)
        service.cleanup_excess_logs = cleanup_mock

        with patch(
            "app.services.call_logger.get_pricing_service",
            return_value=_FakePricingService(),
        ):
            log_entry = await service.log_call(
                session,
                api_key_id="key-001",
                api_key_name="key-001",
                model_group="claude-opus-4-6",
                model="claude-opus-4-6",
                api_type="openai",
                input_preview="hello",
                output_preview="world",
                input_tokens=10,
                output_tokens=20,
                status="success",
            )

        assert log_entry is not None
        assert session.flush_count == 1
        assert len(session.added) == 1
        cleanup_mock.assert_awaited_once_with(session, max_logs=MAX_CALL_LOG_ENTRIES)

    asyncio.run(_run())
