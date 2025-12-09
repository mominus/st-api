"""
Data Models
包含 SQLAlchemy 数据库模型和 Pydantic 数据模型
"""

from app.models.database import (
    Base,
    BackendAccount,
    ModelGroup,
    APIKey,
    TokenUsageHistory,
    RequestLog,
    Admin,
    LoginAttempt,
    init_database,
    close_database,
    get_session,
    get_session_factory,
)

# 兼容旧名称
StackAIAccount = BackendAccount

__all__ = [
    "Base",
    "BackendAccount",
    "StackAIAccount",  # 兼容旧名称
    "ModelGroup",
    "APIKey",
    "TokenUsageHistory",
    "RequestLog",
    "Admin",
    "LoginAttempt",
    "init_database",
    "close_database",
    "get_session",
    "get_session_factory",
]
