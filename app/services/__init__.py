"""
Services Layer
包含核心业务逻辑：账号池管理、API Key 管理、请求转换等。

保持包级导出兼容，但改为懒加载，避免子模块导入时触发整层服务初始化，
从而引入循环依赖。
"""

from importlib import import_module
from typing import Dict, Tuple

_EXPORTS: Dict[str, Tuple[str, str]] = {
    "CryptoService": ("app.services.crypto", "CryptoService"),
    "get_crypto_service": ("app.services.crypto", "get_crypto_service"),
    "init_crypto_service": ("app.services.crypto", "init_crypto_service"),
    "ConfigService": ("app.services.config", "ConfigService"),
    "AppConfig": ("app.services.config", "AppConfig"),
    "ServerConfig": ("app.services.config", "ServerConfig"),
    "DatabaseConfig": ("app.services.config", "DatabaseConfig"),
    "SecurityConfig": ("app.services.config", "SecurityConfig"),
    "AdminConfig": ("app.services.config", "AdminConfig"),
    "LoginProtectionConfig": ("app.services.config", "LoginProtectionConfig"),
    "LogConfig": ("app.services.config", "LogConfig"),
    "CorsConfig": ("app.services.config", "CorsConfig"),
    "get_config_service": ("app.services.config", "get_config_service"),
    "init_config_service": ("app.services.config", "init_config_service"),
    "get_config": ("app.services.config", "get_config"),
    "ResponseTransformer": ("app.services.response_transformer", "ResponseTransformer"),
    "TokenUsage": ("app.services.response_transformer", "TokenUsage"),
    "BackendResponse": ("app.services.response_transformer", "BackendResponse"),
    "get_response_transformer": ("app.services.response_transformer", "get_response_transformer"),
    "ErrorHandler": ("app.services.error_handler", "ErrorHandler"),
    "ErrorType": ("app.services.error_handler", "ErrorType"),
    "APIError": ("app.services.error_handler", "APIError"),
    "get_error_handler": ("app.services.error_handler", "get_error_handler"),
    "BackendClient": ("app.services.backend_client", "BackendClient"),
    "BackendClientError": ("app.services.backend_client", "BackendClientError"),
    "BackendConnectionError": ("app.services.backend_client", "BackendConnectionError"),
    "BackendTimeoutError": ("app.services.backend_client", "BackendTimeoutError"),
    "BackendAPIError": ("app.services.backend_client", "BackendAPIError"),
    "get_backend_client": ("app.services.backend_client", "get_backend_client"),
    "init_backend_client": ("app.services.backend_client", "init_backend_client"),
    "STClient": ("app.services.backend_client", "STClient"),
    "STClientError": ("app.services.backend_client", "STClientError"),
    "STConnectionError": ("app.services.backend_client", "STConnectionError"),
    "STTimeoutError": ("app.services.backend_client", "STTimeoutError"),
    "STAPIError": ("app.services.backend_client", "STAPIError"),
    "get_st_client": ("app.services.backend_client", "get_st_client"),
    "init_st_client": ("app.services.backend_client", "init_st_client"),
    "ModelGroupRouter": ("app.services.router", "ModelGroupRouter"),
    "get_router_service": ("app.services.router", "get_router_service"),
    "init_router_service": ("app.services.router", "init_router_service"),
    "LoggerService": ("app.services.logger", "LoggerService"),
    "RequestLogEntry": ("app.services.logger", "RequestLogEntry"),
    "LogQueryParams": ("app.services.logger", "LogQueryParams"),
    "LogQueryResult": ("app.services.logger", "LogQueryResult"),
    "get_logger_service": ("app.services.logger", "get_logger_service"),
    "init_logger_service": ("app.services.logger", "init_logger_service"),
    "StatsService": ("app.services.stats", "StatsService"),
    "AccountStats": ("app.services.stats", "AccountStats"),
    "APIKeyStats": ("app.services.stats", "APIKeyStats"),
    "ModelGroupStats": ("app.services.stats", "ModelGroupStats"),
    "DailyUsageStats": ("app.services.stats", "DailyUsageStats"),
    "SystemOverview": ("app.services.stats", "SystemOverview"),
    "get_stats_service": ("app.services.stats", "get_stats_service"),
    "init_stats_service": ("app.services.stats", "init_stats_service"),
    "ToolSchema": ("app.services.tool_registry", "ToolSchema"),
    "ToolRegistry": ("app.services.tool_registry", "ToolRegistry"),
    "READ_TOOL": ("app.services.tool_registry", "READ_TOOL"),
    "WRITE_TOOL": ("app.services.tool_registry", "WRITE_TOOL"),
    "EDIT_TOOL": ("app.services.tool_registry", "EDIT_TOOL"),
    "BASH_TOOL": ("app.services.tool_registry", "BASH_TOOL"),
    "GREP_TOOL": ("app.services.tool_registry", "GREP_TOOL"),
    "GLOB_TOOL": ("app.services.tool_registry", "GLOB_TOOL"),
    "DIFF_TOOL": ("app.services.tool_registry", "DIFF_TOOL"),
    "LS_TOOL": ("app.services.tool_registry", "LS_TOOL"),
    "DEFAULT_TOOLS": ("app.services.tool_registry", "DEFAULT_TOOLS"),
    "create_default_registry": ("app.services.tool_registry", "create_default_registry"),
    "get_tool_registry": ("app.services.tool_registry", "get_tool_registry"),
    "init_tool_registry": ("app.services.tool_registry", "init_tool_registry"),
    "ToolSerializer": ("app.services.tool_serializer", "ToolSerializer"),
}

__all__ = list(_EXPORTS.keys())


def __getattr__(name: str):
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    module = import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
