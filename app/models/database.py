"""
SQLAlchemy Database Models
定义所有数据库模型和数据库连接管理
"""

import os
from datetime import datetime, date
from typing import Optional, AsyncGenerator

from sqlalchemy import (
    Column, String, Integer, Text, DateTime, Date, 
    Boolean, create_engine, event, text
)
from sqlalchemy.ext.asyncio import (
    create_async_engine, AsyncSession, async_sessionmaker
)
from sqlalchemy.orm import declarative_base

# 创建基类
Base = declarative_base()

# 数据库 URL，默认使用 SQLite
DATABASE_URL = os.getenv(
    "DATABASE_URL", 
    "sqlite+aiosqlite:///./data/api_service.db"
)


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
    status = Column(String(20), default="active")  # active, exhausted, disabled
    last_used_at = Column(DateTime, nullable=True)
    last_sync_at = Column(DateTime, nullable=True)  # 最后同步时间
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ModelGroup(Base):
    """模型组表"""
    __tablename__ = "model_groups"
    
    id = Column(String(36), primary_key=True)
    name = Column(String(255), unique=True, nullable=False)
    description = Column(Text, nullable=True)
    input_mapping = Column(Text, nullable=False)  # JSON: 输入字段映射配置
    created_at = Column(DateTime, default=datetime.utcnow)


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
    created_at = Column(DateTime, default=datetime.utcnow)
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
    timestamp = Column(DateTime, default=datetime.utcnow)
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
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    
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
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Admin(Base):
    """管理员表"""
    __tablename__ = "admins"
    
    id = Column(String(36), primary_key=True)
    username = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


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


async def init_database() -> None:
    """初始化数据库，创建所有表"""
    global _engine, _async_session_factory
    
    db_url = get_database_url()
    
    # 连接池配置
    pool_size = int(os.getenv("DB_POOL_SIZE", "20"))
    max_overflow = int(os.getenv("DB_MAX_OVERFLOW", "30"))
    pool_timeout = int(os.getenv("DB_POOL_TIMEOUT", "30"))
    pool_recycle = int(os.getenv("DB_POOL_RECYCLE", "3600"))
    
    # SQLite 连接参数：增加超时时间和启用 WAL 模式以支持更好的并发
    connect_args = {}
    engine_kwargs = {
        "echo": os.getenv("DEBUG", "false").lower() == "true",
        "future": True,
        "pool_pre_ping": True
    }
    
    if "sqlite" in db_url:
        connect_args = {
            "timeout": 60,  # 增加锁等待超时时间
            "check_same_thread": False
        }
        # SQLite 使用 NullPool 或 StaticPool 更适合
        engine_kwargs["pool_size"] = 5
        engine_kwargs["max_overflow"] = 10
    else:
        # PostgreSQL 等数据库使用完整连接池配置
        engine_kwargs["pool_size"] = pool_size
        engine_kwargs["max_overflow"] = max_overflow
        engine_kwargs["pool_timeout"] = pool_timeout
        engine_kwargs["pool_recycle"] = pool_recycle
    
    engine_kwargs["connect_args"] = connect_args
    
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
        if "sqlite" in db_url:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA busy_timeout=30000"))
            # 执行 SQLite 迁移（添加新列）
            await _migrate_sqlite_columns(conn)


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
