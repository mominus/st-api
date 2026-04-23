"""
SQLAlchemy Database Models
定义所有数据库模型和数据库连接管理
"""

import os
import logging
from datetime import datetime, date
from typing import Optional, AsyncGenerator

from sqlalchemy import (
    Column, String, Integer, Text, DateTime, Date,
    Boolean, text, UniqueConstraint
)
from sqlalchemy.ext.asyncio import (
    create_async_engine, AsyncSession, async_sessionmaker
)
from sqlalchemy.orm import declarative_base

from app.services.time_utils import utc_now_naive

# 创建基类
Base = declarative_base()
logger = logging.getLogger(__name__)

# 数据库 URL，默认使用 SQLite
DATABASE_URL = os.getenv(
    "DATABASE_URL", 
    "sqlite+aiosqlite:///./data/api_service.db"
)

SQLITE_JOURNAL_MODE = "WAL"
SQLITE_BUSY_TIMEOUT_MS = 30000


class BackendAccount(Base):
    """后端账号表"""
    __tablename__ = "backend_accounts"
    
    id = Column(String(36), primary_key=True)
    name = Column(String(255), nullable=False)
    org_id = Column(String(255), nullable=False)
    flow_id = Column(String(255), nullable=False)
    api_key_encrypted = Column(Text, nullable=False)  # 加密存储
    private_api_key_encrypted = Column(Text, nullable=True)  # Private API Key（用于监控）
    model_group = Column(String(255), nullable=False)
    daily_quota = Column(Integer, default=1000000)
    daily_used = Column(Integer, default=0)
    inflight_requests = Column(Integer, default=0, nullable=False)
    inflight_updated_at = Column(DateTime, nullable=True)
    status = Column(String(20), default="active")  # active, exhausted, disabled
    last_used_at = Column(DateTime, nullable=True)
    last_sync_at = Column(DateTime, nullable=True)  # 最后同步时间
    created_at = Column(DateTime, default=utc_now_naive)
    updated_at = Column(DateTime, default=utc_now_naive, onupdate=utc_now_naive)


class AccountModelRoute(Base):
    """账号-模型路由表（支持单账号多模型）"""
    __tablename__ = "account_model_routes"
    __table_args__ = (
        UniqueConstraint("account_id", "model_name", name="uq_account_model_route"),
    )

    id = Column(String(36), primary_key=True)
    account_id = Column(String(36), nullable=False, index=True)
    model_name = Column(String(255), nullable=False, index=True)
    enabled = Column(Boolean, default=True, nullable=False)
    weight = Column(Integer, default=100, nullable=False)
    priority = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=utc_now_naive)
    updated_at = Column(DateTime, default=utc_now_naive, onupdate=utc_now_naive)


class ModelGroup(Base):
    """模型组表"""
    __tablename__ = "model_groups"
    
    id = Column(String(36), primary_key=True)
    name = Column(String(255), unique=True, nullable=False)
    description = Column(Text, nullable=True)
    input_mapping = Column(Text, nullable=False)  # JSON: 输入字段映射配置
    capability_overrides = Column(Text, nullable=True)  # JSON: 能力矩阵覆盖配置
    created_at = Column(DateTime, default=utc_now_naive)


class APIKey(Base):
    """API Key 表"""
    __tablename__ = "api_keys"
    
    id = Column(String(36), primary_key=True)
    key_hash = Column(String(255), unique=True, nullable=False)  # 哈希存储
    key_prefix = Column(String(20), nullable=False)  # 用于显示，如 sk-xxx
    key_suffix = Column(String(10), nullable=True)  # 用于显示，如 xxxx（后4位）
    name = Column(String(255), nullable=True)
    model_groups = Column(Text, nullable=False)  # JSON: 授权的模型组列表
    quota = Column(Integer, nullable=True)  # Token 配额，NULL 表示无限制（已弃用）
    used = Column(Integer, default=0)
    request_quota = Column(Integer, nullable=True)  # 请求数配额，NULL 表示无限制
    token_quota = Column(Integer, nullable=True)  # Token 额度限制，NULL 表示无限制
    cost_limit = Column(String(20), nullable=True)  # 费用限制（美元），NULL 表示无限制
    expires_at = Column(DateTime, nullable=True)
    status = Column(String(20), default="active")  # active, revoked, exhausted
    created_at = Column(DateTime, default=utc_now_naive)
    last_used_at = Column(DateTime, nullable=True)
    # 累计统计（从创建日期开始）
    total_requests = Column(Integer, default=0)  # 总请求数
    total_tokens = Column(Integer, default=0)  # 总 Token 使用量
    total_cost = Column(String(20), default="0")  # 总费用（美元）


class TokenUsageHistory(Base):
    """Token 使用历史表"""
    __tablename__ = "token_usage_history"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, nullable=False)
    account_id = Column(String(36), nullable=True)
    api_key_id = Column(String(36), nullable=True)
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    request_count = Column(Integer, default=0)


class RequestLog(Base):
    """请求日志表"""
    __tablename__ = "request_logs"
    
    id = Column(String(36), primary_key=True)
    timestamp = Column(DateTime, default=utc_now_naive)
    api_key_prefix = Column(String(20), nullable=True)
    client_ip = Column(String(45), nullable=True)
    model = Column(String(255), nullable=True)
    account_id = Column(String(36), nullable=True)
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    response_time_ms = Column(Integer, nullable=True)
    status = Column(String(20), nullable=True)  # success, error
    error_message = Column(Text, nullable=True)


class CallLog(Base):
    """详细调用日志表"""
    __tablename__ = "call_logs"
    
    id = Column(String(36), primary_key=True)
    timestamp = Column(DateTime, default=utc_now_naive, index=True)
    
    # 调用方信息
    api_key_id = Column(String(36), nullable=True)
    api_key_name = Column(String(255), nullable=True)
    api_key_prefix = Column(String(20), nullable=True)
    client_ip = Column(String(45), nullable=True)
    
    # 账号信息
    account_id = Column(String(36), nullable=True)
    account_name = Column(String(255), nullable=True)
    model_group = Column(String(255), nullable=True)
    
    # 请求信息
    model = Column(String(255), nullable=True)
    api_type = Column(String(20), nullable=True)  # openai, anthropic, gemini
    is_stream = Column(Boolean, default=False)
    
    # 输入输出（截断存储）
    input_preview = Column(Text, nullable=True)  # 输入预览（前500字符）
    output_preview = Column(Text, nullable=True)  # 输出预览（前10行或500字符）
    
    # Token 统计
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    
    # 费用计算（美元，精确到小数点后6位）
    input_cost = Column(String(20), nullable=True)
    output_cost = Column(String(20), nullable=True)
    total_cost = Column(String(20), nullable=True)
    
    # 响应信息
    response_time_ms = Column(Integer, nullable=True)
    status = Column(String(20), nullable=True)  # success, error
    error_message = Column(Text, nullable=True)


class SystemStats(Base):
    """系统统计表 - 存储全局统计数据"""
    __tablename__ = "system_stats"
    
    id = Column(String(36), primary_key=True, default="global")  # 只有一条记录
    total_requests = Column(Integer, default=0)  # 历史总请求数
    total_tokens = Column(Integer, default=0)  # 历史总 Token 使用量
    total_input_tokens = Column(Integer, default=0)  # 历史总输入 Token
    total_output_tokens = Column(Integer, default=0)  # 历史总输出 Token
    updated_at = Column(DateTime, default=utc_now_naive, onupdate=utc_now_naive)


class Admin(Base):
    """管理员表"""
    __tablename__ = "admins"
    
    id = Column(String(36), primary_key=True)
    username = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=utc_now_naive)


class LoginAttempt(Base):
    """登录尝试表（用于防暴力破解）"""
    __tablename__ = "login_attempts"
    
    ip = Column(String(45), primary_key=True)
    attempts = Column(Integer, default=0)
    locked_until = Column(DateTime, nullable=True)


# 数据库引擎和会话管理
_engine: Optional[object] = None
_async_session_factory: Optional[async_sessionmaker] = None


def get_database_url() -> str:
    """获取数据库 URL"""
    return os.getenv("DATABASE_URL", DATABASE_URL)


def _require_sqlite_database_url(db_url: str) -> str:
    normalized = str(db_url or "").strip() or DATABASE_URL
    if "sqlite" not in normalized.lower():
        raise RuntimeError(
            "st-api now only supports SQLite. "
            "Set DATABASE_URL to a sqlite+aiosqlite URL such as "
            "'sqlite+aiosqlite:///./data/api_service.db'."
        )
    return normalized


def _build_async_engine_kwargs(db_url: str) -> dict[str, object]:
    _require_sqlite_database_url(db_url)

    engine_kwargs: dict[str, object] = {
        "echo": os.getenv("DEBUG", "false").lower() == "true",
        "future": True,
        "pool_pre_ping": True,
        "connect_args": {
            "timeout": 60,  # 增加锁等待超时时间
            "check_same_thread": False,
        },
    }
    return engine_kwargs


async def init_database() -> None:
    """初始化数据库，创建所有表"""
    global _engine, _async_session_factory
    
    db_url = _require_sqlite_database_url(get_database_url())
    engine_kwargs = _build_async_engine_kwargs(db_url)
    
    # 创建异步引擎
    _engine = create_async_engine(db_url, **engine_kwargs)
    
    # 创建会话工厂
    _async_session_factory = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False
    )
    
    # 创建所有表，并为 SQLite 启用 WAL 模式
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # 启用 WAL 模式以支持更好的并发读写
        await conn.execute(text(f"PRAGMA journal_mode={SQLITE_JOURNAL_MODE}"))
        await conn.execute(text(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}"))
        # 执行 SQLite 迁移（添加新列）
        await _migrate_sqlite_columns(conn)
        # token_usage_history 并发聚合桶唯一索引
        await _ensure_token_usage_history_bucket_index(conn)
        # 日志查询索引（高并发下后台查询/聚合性能关键）
        await _ensure_log_query_indexes(conn)
        # 账号选号与模型路由索引
        await _ensure_account_routing_indexes(conn)


async def _migrate_sqlite_columns(conn) -> None:
    """
    SQLite 迁移：为现有表添加新列
    SQLite 不支持通过 create_all 添加新列，需要手动 ALTER TABLE
    """
    # 需要添加的列：(表名, 列名, 列定义)
    migrations = [
        ("api_keys", "token_quota", "INTEGER"),
        ("api_keys", "cost_limit", "VARCHAR(20)"),
        ("api_keys", "total_cost", "VARCHAR(20) DEFAULT '0'"),
        ("model_groups", "capability_overrides", "TEXT"),
        ("backend_accounts", "inflight_requests", "INTEGER DEFAULT 0 NOT NULL"),
        ("backend_accounts", "inflight_updated_at", "DATETIME"),
    ]
    
    for table_name, column_name, column_def in migrations:
        try:
            # 检查列是否存在
            result = await conn.execute(
                text(f"SELECT {column_name} FROM {table_name} LIMIT 1")
            )
            result.close()
        except Exception:
            # 列不存在，添加它
            try:
                await conn.execute(
                    text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")
                )
                print(f"[Migration] Added column {table_name}.{column_name}")
            except Exception as e:
                print(f"[Migration] Failed to add column {table_name}.{column_name}: {e}")

    # 账号-模型路由表（单账号多模型）
    try:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS account_model_routes (
                id VARCHAR(36) PRIMARY KEY,
                account_id VARCHAR(36) NOT NULL,
                model_name VARCHAR(255) NOT NULL,
                enabled BOOLEAN DEFAULT 1 NOT NULL,
                weight INTEGER DEFAULT 100 NOT NULL,
                priority INTEGER DEFAULT 0 NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(account_id, model_name)
            )
        """))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_account_model_routes_account_id ON account_model_routes(account_id)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_account_model_routes_model_name ON account_model_routes(model_name)"
        ))

        # 回填历史数据：把 backend_accounts.model_group 同步到新路由表
        await conn.execute(text("""
            INSERT INTO account_model_routes (
                id, account_id, model_name, enabled, weight, priority, created_at, updated_at
            )
            SELECT
                lower(hex(randomblob(16))),
                b.id,
                b.model_group,
                1,
                100,
                0,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            FROM backend_accounts b
            WHERE b.model_group IS NOT NULL
              AND b.model_group <> ''
              AND NOT EXISTS (
                    SELECT 1
                    FROM account_model_routes r
                    WHERE r.account_id = b.id
                      AND r.model_name = b.model_group
              )
        """))
        print("[Migration] account_model_routes ready")
    except Exception as e:
        print(f"[Migration] Failed to migrate account_model_routes: {e}")

async def _ensure_token_usage_history_bucket_index(conn) -> None:
    """
    为 token_usage_history 建立并发聚合桶唯一索引：
    (date, COALESCE(account_id,''), COALESCE(api_key_id,''))。
    """
    try:
        await conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_token_usage_history_bucket
            ON token_usage_history (
                date,
                COALESCE(account_id, ''),
                COALESCE(api_key_id, '')
            )
        """))
    except Exception as e:
        logger.warning(
            "Failed to ensure token usage history bucket index; fallback path will be used: %s",
            e,
        )


async def _ensure_log_query_indexes(conn) -> None:
    """为 request_logs/call_logs 创建查询索引。"""
    index_statements = [
        "CREATE INDEX IF NOT EXISTS ix_request_logs_timestamp ON request_logs(timestamp)",
        "CREATE INDEX IF NOT EXISTS ix_request_logs_account_id_timestamp ON request_logs(account_id, timestamp)",
        "CREATE INDEX IF NOT EXISTS ix_request_logs_api_key_prefix_timestamp ON request_logs(api_key_prefix, timestamp)",
        "CREATE INDEX IF NOT EXISTS ix_request_logs_status_timestamp ON request_logs(status, timestamp)",
        "CREATE INDEX IF NOT EXISTS ix_request_logs_model_timestamp ON request_logs(model, timestamp)",
        "CREATE INDEX IF NOT EXISTS ix_call_logs_api_key_id_timestamp ON call_logs(api_key_id, timestamp)",
        "CREATE INDEX IF NOT EXISTS ix_call_logs_account_id_timestamp ON call_logs(account_id, timestamp)",
        "CREATE INDEX IF NOT EXISTS ix_call_logs_status_timestamp ON call_logs(status, timestamp)",
        "CREATE INDEX IF NOT EXISTS ix_call_logs_model_timestamp ON call_logs(model, timestamp)",
    ]
    for statement in index_statements:
        try:
            await conn.execute(text(statement))
        except Exception as exc:
            logger.warning("Failed to ensure index with statement [%s]: %s", statement, exc)


async def _ensure_account_routing_indexes(conn) -> None:
    """为高并发账号选号路径创建索引。"""
    index_statements = [
        (
            "CREATE INDEX IF NOT EXISTS "
            "ix_account_model_routes_model_enabled_priority_account "
            "ON account_model_routes(model_name, enabled, priority, account_id)"
        ),
        (
            "CREATE INDEX IF NOT EXISTS "
            "ix_backend_accounts_model_group_id "
            "ON backend_accounts(model_group, id)"
        ),
        (
            "CREATE INDEX IF NOT EXISTS "
            "ix_backend_accounts_status_last_used_updated_id "
            "ON backend_accounts(status, last_used_at, updated_at, id)"
        ),
        (
            "CREATE INDEX IF NOT EXISTS "
            "ix_backend_accounts_routable_order "
            "ON backend_accounts(last_used_at, updated_at, id) "
            "WHERE status IN ('active', 'exhausted')"
        ),
    ]

    for statement in index_statements:
        try:
            await conn.execute(text(statement))
        except Exception as exc:
            logger.warning("Failed to ensure routing index with statement [%s]: %s", statement, exc)


async def close_database() -> None:
    """关闭数据库连接"""
    global _engine
    if _engine:
        await _engine.dispose()
        _engine = None


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """获取数据库会话（用于依赖注入）"""
    if _async_session_factory is None:
        raise RuntimeError("Database not initialized. Call init_database() first.")
    
    async with _async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_session_factory() -> async_sessionmaker:
    """获取会话工厂"""
    if _async_session_factory is None:
        raise RuntimeError("Database not initialized. Call init_database() first.")
    return _async_session_factory


def get_sqlite_runtime_settings() -> dict[str, object]:
    """返回当前 SQLite 运行时参数，供管理后台和文档说明复用。"""
    return {
        "journal_mode": SQLITE_JOURNAL_MODE,
        "busy_timeout_ms": SQLITE_BUSY_TIMEOUT_MS,
        "busy_timeout_seconds": SQLITE_BUSY_TIMEOUT_MS / 1000,
    }
