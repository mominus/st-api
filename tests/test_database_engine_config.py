import pytest

from app.models.database import _build_async_engine_kwargs, get_sqlite_runtime_settings


def test_build_async_engine_kwargs_returns_sqlite_defaults(monkeypatch):
    monkeypatch.setenv("DEBUG", "true")

    engine_kwargs = _build_async_engine_kwargs("sqlite+aiosqlite:///./data/api_service.db")

    assert engine_kwargs["echo"] is True
    assert engine_kwargs["future"] is True
    assert engine_kwargs["pool_pre_ping"] is True
    assert engine_kwargs["connect_args"] == {
        "timeout": 60,
        "check_same_thread": False,
    }


def test_build_async_engine_kwargs_rejects_non_sqlite_database_url():
    with pytest.raises(RuntimeError, match="only supports SQLite"):
        _build_async_engine_kwargs("mysql://user:pass@localhost/dbname")


def test_get_sqlite_runtime_settings_matches_admin_display_contract():
    runtime_settings = get_sqlite_runtime_settings()

    assert runtime_settings == {
        "journal_mode": "WAL",
        "busy_timeout_ms": 30000,
        "busy_timeout_seconds": 30.0,
        "synchronous": "NORMAL",
        "wal_autocheckpoint_pages": 1000,
        "cache_size_kib": 32768,
    }
