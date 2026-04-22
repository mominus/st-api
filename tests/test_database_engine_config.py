from app.models.database import _build_async_engine_kwargs


def test_build_async_engine_kwargs_skips_queue_pool_args_for_sqlite(monkeypatch):
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.setenv("DB_POOL_SIZE", "8")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "2")
    monkeypatch.setenv("DB_POOL_TIMEOUT", "5")
    monkeypatch.setenv("DB_POOL_RECYCLE", "1800")
    monkeypatch.setenv("SQLITE_POOL_SIZE", "12")
    monkeypatch.setenv("SQLITE_MAX_OVERFLOW", "4")
    monkeypatch.setenv("SQLITE_POOL_TIMEOUT", "9")
    monkeypatch.setenv("SQLITE_POOL_RECYCLE", "3600")

    engine_kwargs = _build_async_engine_kwargs("sqlite+aiosqlite:///./data/api_service.db")

    assert engine_kwargs["echo"] is True
    assert engine_kwargs["future"] is True
    assert engine_kwargs["pool_pre_ping"] is True
    assert engine_kwargs["connect_args"] == {
        "timeout": 60,
        "check_same_thread": False,
    }
    assert "pool_size" not in engine_kwargs
    assert "max_overflow" not in engine_kwargs
    assert "pool_timeout" not in engine_kwargs
    assert "pool_recycle" not in engine_kwargs


def test_build_async_engine_kwargs_keeps_pool_args_for_postgres(monkeypatch):
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("DB_POOL_SIZE", "10")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "3")
    monkeypatch.setenv("DB_POOL_TIMEOUT", "7")
    monkeypatch.setenv("DB_POOL_RECYCLE", "900")
    monkeypatch.setenv("POSTGRES_CONNECT_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("POSTGRES_COMMAND_TIMEOUT_SECONDS", "15")
    monkeypatch.setenv("POSTGRES_STATEMENT_TIMEOUT_MS", "45000")
    monkeypatch.setenv("POSTGRES_LOCK_TIMEOUT_MS", "6000")
    monkeypatch.setenv("POSTGRES_APPLICATION_NAME", "st-api-test")

    engine_kwargs = _build_async_engine_kwargs(
        "postgresql+asyncpg://user:pass@localhost:5432/dbname"
    )

    assert engine_kwargs["echo"] is False
    assert engine_kwargs["future"] is True
    assert engine_kwargs["pool_pre_ping"] is True
    assert engine_kwargs["pool_size"] == 10
    assert engine_kwargs["max_overflow"] == 3
    assert engine_kwargs["pool_timeout"] == 7
    assert engine_kwargs["pool_recycle"] == 900
    assert engine_kwargs["connect_args"] == {
        "timeout": 2.5,
        "command_timeout": 15.0,
        "server_settings": {
            "statement_timeout": "45000",
            "lock_timeout": "6000",
            "application_name": "st-api-test",
        },
    }
