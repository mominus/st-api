"""
Services Layer
包含核心业务逻辑：账号池管理、API Key 管理、请求转换等
"""

from app.services.crypto import (
    CryptoService,
    get_crypto_service,
    init_crypto_service,
)

from app.services.config import (
    ConfigService,
    AppConfig,
    ServerConfig,
    DatabaseConfig,
    SecurityConfig,
    AdminConfig,
    LoginProtectionConfig,
    LogConfig,
    CorsConfig,
    get_config_service,
    init_config_service,
    get_config,
)

from app.services.response_transformer import (
    ResponseTransformer,
    TokenUsage,
    BackendResponse,
    get_response_transformer,
)

from app.services.error_handler import (
    ErrorHandler,
    ErrorType,
    APIError,
    get_error_handler,
)

from app.services.backend_client import (
    BackendClient,
    BackendClientError,
    BackendConnectionError,
    BackendTimeoutError,
    BackendAPIError,
    get_backend_client,
    init_backend_client,
    # 兼容旧名称
    STClient,
    STClientError,
    STConnectionError,
    STTimeoutError,
    STAPIError,
    get_st_client,
    init_st_client,
)

from app.services.router import (
    ModelGroupRouter,
    get_router_service,
    init_router_service,
)

from app.services.logger import (
    LoggerService,
    RequestLogEntry,
    LogQueryParams,
    LogQueryResult,
    get_logger_service,
    init_logger_service,
)

from app.services.stats import (
    StatsService,
    AccountStats,
    APIKeyStats,
    ModelGroupStats,
    DailyUsageStats,
    SystemOverview,
    get_stats_service,
    init_stats_service,
)

from app.services.tool_registry import (
    ToolSchema,
    ToolRegistry,
    READ_TOOL,
    WRITE_TOOL,
    EDIT_TOOL,
    BASH_TOOL,
    GREP_TOOL,
    GLOB_TOOL,
    DIFF_TOOL,
    LS_TOOL,
    DEFAULT_TOOLS,
    create_default_registry,
    get_tool_registry,
    init_tool_registry,
)

from app.services.tool_serializer import (
    ToolSerializer,
)

__all__ = [
    # Crypto Service
    "CryptoService",
    "get_crypto_service",
    "init_crypto_service",
    # Config Service
    "ConfigService",
    "AppConfig",
    "ServerConfig",
    "DatabaseConfig",
    "SecurityConfig",
    "AdminConfig",
    "LoginProtectionConfig",
    "LogConfig",
    "CorsConfig",
    "get_config_service",
    "init_config_service",
    "get_config",
    # Response Transformer
    "ResponseTransformer",
    "TokenUsage",
    "BackendResponse",
    "get_response_transformer",
    # Error Handler
    "ErrorHandler",
    "ErrorType",
    "APIError",
    "get_error_handler",
    # Backend Client
    "BackendClient",
    "BackendClientError",
    "BackendConnectionError",
    "BackendTimeoutError",
    "BackendAPIError",
    "get_backend_client",
    "init_backend_client",
    # 兼容旧名称
    "STClient",
    "STClientError",
    "STConnectionError",
    "STTimeoutError",
    "STAPIError",
    "get_st_client",
    "init_st_client",
    # Model Group Router
    "ModelGroupRouter",
    "get_router_service",
    "init_router_service",
    # Logger Service
    "LoggerService",
    "RequestLogEntry",
    "LogQueryParams",
    "LogQueryResult",
    "get_logger_service",
    "init_logger_service",
    # Stats Service
    "StatsService",
    "AccountStats",
    "APIKeyStats",
    "ModelGroupStats",
    "DailyUsageStats",
    "SystemOverview",
    "get_stats_service",
    "init_stats_service",
    # Tool Registry
    "ToolSchema",
    "ToolRegistry",
    "READ_TOOL",
    "WRITE_TOOL",
    "EDIT_TOOL",
    "BASH_TOOL",
    "GREP_TOOL",
    "GLOB_TOOL",
    "DIFF_TOOL",
    "LS_TOOL",
    "DEFAULT_TOOLS",
    "create_default_registry",
    "get_tool_registry",
    "init_tool_registry",
    # Tool Serializer
    "ToolSerializer",
]
