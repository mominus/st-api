import asyncio

from app.routers import admin as admin_router
from app.services import backend_client as backend_client_module
from app.services import connection_pool as connection_pool_module
from app.services import usage_aggregator as usage_aggregator_module


class _FakeLimiter:
    def get_stats(self):
        return {
            "active_requests": 1,
            "queued_requests": 0,
            "total_requests": 5,
            "rejected_requests": 0,
            "active_streams": 0,
            "queued_streams": 0,
            "total_streams": 0,
            "rejected_streams": 0,
            "active_db_ops": 0,
            "queued_db_ops": 0,
            "total_db_ops": 0,
            "rejected_db_ops": 0,
            "avg_response_time": 10,
            "p95_response_time": 20,
            "p99_response_time": 30,
            "avg_request_wait_ms": 1,
            "p95_request_wait_ms": 2,
            "p99_request_wait_ms": 3,
            "avg_stream_wait_ms": 0,
            "p95_stream_wait_ms": 0,
            "p99_stream_wait_ms": 0,
            "avg_db_wait_ms": 0,
            "p95_db_wait_ms": 0,
            "p99_db_wait_ms": 0,
        }


class _FakeUsageAggregator:
    def stats(self):
        return {
            "queue_size": 0,
            "flushed_events": 0,
            "flushed_batches": 0,
            "dropped_events": 0,
            "configured_workers": 1,
            "active_workers": 0,
            "active_flush_workers": 0,
        }


class _FakeGatewayRuntime:
    def background_log_stats(self):
        return {
            "queue_size": 0,
            "processed_events": 0,
            "dropped_events": 0,
            "inflight_events": 0,
            "active_workers": 0,
            "queue_capacity": 100,
        }


class _FakeBackendClient:
    def stats(self):
        return {
            "configured_max_connections": 10,
            "max_connections_per_host": 10,
            "effective_max_connections": 10,
            "backend_max_concurrent_streams": 5,
            "max_keepalive_connections": 5,
            "keepalive_expiry_seconds": 15,
            "connect_timeout_seconds": 10,
            "pool_timeout_seconds": 8,
            "sync_timeout_seconds": 120,
            "stream_timeout_seconds": 300,
            "sync_retry_count": 0,
            "stream_retry_count": 0,
            "backend_slot_timeout_count": 0,
            "backend_stream_slot_timeout_count": 0,
            "backend_total_acquires": 0,
            "active_sync_requests": 0,
            "active_stream_requests": 0,
            "active_total_requests": 0,
            "avg_wait_ms": 0,
            "p95_wait_ms": 0,
            "p99_wait_ms": 0,
            "pool_timeout_count": 0,
            "connect_error_count": 0,
            "timeout_error_count": 0,
            "transport_error_count": 0,
        }


def test_performance_stats_exposes_sqlite_runtime_fields(monkeypatch):
    monkeypatch.setattr(connection_pool_module, "get_concurrency_limiter", lambda: _FakeLimiter())
    monkeypatch.setattr(usage_aggregator_module, "get_usage_aggregator", lambda: _FakeUsageAggregator())
    monkeypatch.setattr(admin_router, "get_gateway_runtime", lambda: _FakeGatewayRuntime())
    monkeypatch.setattr(backend_client_module, "get_backend_client", lambda: _FakeBackendClient())

    if hasattr(admin_router.get_performance_stats, "_start_time"):
        delattr(admin_router.get_performance_stats, "_start_time")

    result = asyncio.run(admin_router.get_performance_stats(admin={"username": "admin"}, session=None))

    assert result["success"] is True
    assert result["system"]["db_type"] == "SQLite"
    assert result["system"]["db_journal_mode"] == "WAL"
    assert result["system"]["db_busy_timeout"] == "30s"
