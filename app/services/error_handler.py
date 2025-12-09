"""
Error Handler Service
将 StackAI 错误转换为标准 API 错误格式（OpenAI、Anthropic、Gemini）

Requirements: 8.1
"""

from typing import Optional, Dict, Any, Literal
from dataclasses import dataclass
from enum import Enum


# ============================================================================
# Error Types and Codes
# ============================================================================

class ErrorType(str, Enum):
    """标准错误类型"""
    # Client Errors (4xx)
    INVALID_REQUEST = "invalid_request_error"
    AUTHENTICATION = "authentication_error"
    PERMISSION = "permission_error"
    NOT_FOUND = "not_found_error"
    RATE_LIMIT = "rate_limit_error"
    QUOTA_EXCEEDED = "quota_exceeded_error"
    
    # Server Errors (5xx)
    SERVER_ERROR = "server_error"
    BACKEND_ERROR = "backend_error"
    SERVICE_UNAVAILABLE = "service_unavailable_error"


@dataclass
class APIError:
    """统一的 API 错误格式"""
    error_type: ErrorType
    message: str
    code: Optional[str] = None
    status_code: int = 500
    param: Optional[str] = None
    details: Optional[Dict[str, Any]] = None


# ============================================================================
# Error Handler Service
# ============================================================================

class ErrorHandler:
    """
    错误处理器
    
    负责将 StackAI 错误和内部错误转换为标准 API 错误格式。
    支持 OpenAI、Anthropic 和 Gemini 三种格式。
    """
    
    # StackAI 错误码到标准错误类型的映射
    STACKAI_ERROR_MAPPING: Dict[str, ErrorType] = {
        "invalid_api_key": ErrorType.AUTHENTICATION,
        "unauthorized": ErrorType.AUTHENTICATION,
        "forbidden": ErrorType.PERMISSION,
        "not_found": ErrorType.NOT_FOUND,
        "rate_limit": ErrorType.RATE_LIMIT,
        "quota_exceeded": ErrorType.QUOTA_EXCEEDED,
        "invalid_request": ErrorType.INVALID_REQUEST,
        "bad_request": ErrorType.INVALID_REQUEST,
        "server_error": ErrorType.SERVER_ERROR,
        "internal_error": ErrorType.SERVER_ERROR,
        "timeout": ErrorType.BACKEND_ERROR,
        "service_unavailable": ErrorType.SERVICE_UNAVAILABLE,
    }
    
    # HTTP 状态码映射
    ERROR_STATUS_CODES: Dict[ErrorType, int] = {
        ErrorType.INVALID_REQUEST: 400,
        ErrorType.AUTHENTICATION: 401,
        ErrorType.PERMISSION: 403,
        ErrorType.NOT_FOUND: 404,
        ErrorType.RATE_LIMIT: 429,
        ErrorType.QUOTA_EXCEEDED: 429,
        ErrorType.SERVER_ERROR: 500,
        ErrorType.BACKEND_ERROR: 502,
        ErrorType.SERVICE_UNAVAILABLE: 503,
    }
    
    # ========================================================================
    # StackAI Error Parsing
    # ========================================================================
    
    def parse_backend_error(
        self,
        backend_response: Dict[str, Any],
        status_code: int = 500
    ) -> APIError:
        """
        解析 StackAI 错误响应
        
        Args:
            backend_response: StackAI 返回的错误响应
            status_code: HTTP 状态码
            
        Returns:
            统一的 APIError 对象
        """
        # 尝试提取错误信息
        error_code = self._extract_error_code(backend_response)
        error_message = self._extract_error_message(backend_response)
        
        # 映射到标准错误类型
        error_type = self._map_error_type(error_code, status_code)
        
        # 确定最终状态码
        final_status_code = self.ERROR_STATUS_CODES.get(error_type, status_code)
        
        return APIError(
            error_type=error_type,
            message=error_message,
            code=error_code,
            status_code=final_status_code,
            details=backend_response if backend_response else None
        )
    
    def _extract_error_code(self, response: Dict[str, Any]) -> Optional[str]:
        """从响应中提取错误码"""
        if not response:
            return None
        
        # 尝试常见的错误码字段
        for key in ["code", "error_code", "type", "error_type"]:
            if key in response:
                return str(response[key]).lower()
        
        # 检查嵌套的 error 对象
        if "error" in response and isinstance(response["error"], dict):
            return self._extract_error_code(response["error"])
        
        return None
    
    def _extract_error_message(self, response: Dict[str, Any]) -> str:
        """从响应中提取错误消息"""
        if not response:
            return "An unknown error occurred"
        
        # 尝试常见的消息字段
        for key in ["message", "error_message", "detail", "description", "msg"]:
            if key in response and response[key]:
                return str(response[key])
        
        # 检查嵌套的 error 对象
        if "error" in response:
            if isinstance(response["error"], str):
                return response["error"]
            elif isinstance(response["error"], dict):
                return self._extract_error_message(response["error"])
        
        return "An unknown error occurred"
    
    def _map_error_type(
        self,
        error_code: Optional[str],
        status_code: int
    ) -> ErrorType:
        """将错误码映射到标准错误类型"""
        # 首先尝试通过错误码映射
        if error_code and error_code in self.STACKAI_ERROR_MAPPING:
            return self.STACKAI_ERROR_MAPPING[error_code]
        
        # 然后通过 HTTP 状态码映射
        if status_code == 400:
            return ErrorType.INVALID_REQUEST
        elif status_code == 401:
            return ErrorType.AUTHENTICATION
        elif status_code == 403:
            return ErrorType.PERMISSION
        elif status_code == 404:
            return ErrorType.NOT_FOUND
        elif status_code == 429:
            return ErrorType.RATE_LIMIT
        elif status_code == 502:
            return ErrorType.BACKEND_ERROR
        elif status_code == 503:
            return ErrorType.SERVICE_UNAVAILABLE
        elif status_code >= 500:
            return ErrorType.SERVER_ERROR
        else:
            return ErrorType.SERVER_ERROR
    
    # ========================================================================
    # OpenAI Error Format (Requirement 8.1)
    # ========================================================================
    
    def to_openai_error(self, error: APIError) -> Dict[str, Any]:
        """
        将错误转换为 OpenAI 格式
        
        OpenAI 错误格式:
        {
            "error": {
                "message": "Error description",
                "type": "invalid_request_error",
                "code": "invalid_api_key",
                "param": null
            }
        }
        
        Args:
            error: 统一的 APIError 对象
            
        Returns:
            OpenAI 格式的错误响应字典
        """
        return {
            "error": {
                "message": error.message,
                "type": error.error_type.value,
                "code": error.code,
                "param": error.param
            }
        }
    
    # ========================================================================
    # Anthropic Error Format (Requirement 8.1)
    # ========================================================================
    
    def to_anthropic_error(self, error: APIError) -> Dict[str, Any]:
        """
        将错误转换为 Anthropic 格式
        
        Anthropic 错误格式:
        {
            "type": "error",
            "error": {
                "type": "authentication_error",
                "message": "Error description"
            }
        }
        
        Args:
            error: 统一的 APIError 对象
            
        Returns:
            Anthropic 格式的错误响应字典
        """
        # 映射到 Anthropic 错误类型
        anthropic_type = self._to_anthropic_error_type(error.error_type)
        
        return {
            "type": "error",
            "error": {
                "type": anthropic_type,
                "message": error.message
            }
        }
    
    def _to_anthropic_error_type(self, error_type: ErrorType) -> str:
        """将标准错误类型映射到 Anthropic 错误类型"""
        mapping = {
            ErrorType.INVALID_REQUEST: "invalid_request_error",
            ErrorType.AUTHENTICATION: "authentication_error",
            ErrorType.PERMISSION: "permission_error",
            ErrorType.NOT_FOUND: "not_found_error",
            ErrorType.RATE_LIMIT: "rate_limit_error",
            ErrorType.QUOTA_EXCEEDED: "rate_limit_error",
            ErrorType.SERVER_ERROR: "api_error",
            ErrorType.BACKEND_ERROR: "api_error",
            ErrorType.SERVICE_UNAVAILABLE: "overloaded_error",
        }
        return mapping.get(error_type, "api_error")
    
    # ========================================================================
    # Gemini Error Format (Requirement 8.1)
    # ========================================================================
    
    def to_gemini_error(self, error: APIError) -> Dict[str, Any]:
        """
        将错误转换为 Gemini 格式
        
        Gemini 错误格式:
        {
            "error": {
                "code": 401,
                "message": "Error description",
                "status": "UNAUTHENTICATED"
            }
        }
        
        Args:
            error: 统一的 APIError 对象
            
        Returns:
            Gemini 格式的错误响应字典
        """
        # 映射到 Gemini 状态
        gemini_status = self._to_gemini_status(error.error_type)
        
        return {
            "error": {
                "code": error.status_code,
                "message": error.message,
                "status": gemini_status
            }
        }
    
    def _to_gemini_status(self, error_type: ErrorType) -> str:
        """将标准错误类型映射到 Gemini 状态"""
        mapping = {
            ErrorType.INVALID_REQUEST: "INVALID_ARGUMENT",
            ErrorType.AUTHENTICATION: "UNAUTHENTICATED",
            ErrorType.PERMISSION: "PERMISSION_DENIED",
            ErrorType.NOT_FOUND: "NOT_FOUND",
            ErrorType.RATE_LIMIT: "RESOURCE_EXHAUSTED",
            ErrorType.QUOTA_EXCEEDED: "RESOURCE_EXHAUSTED",
            ErrorType.SERVER_ERROR: "INTERNAL",
            ErrorType.BACKEND_ERROR: "UNAVAILABLE",
            ErrorType.SERVICE_UNAVAILABLE: "UNAVAILABLE",
        }
        return mapping.get(error_type, "INTERNAL")
    
    # ========================================================================
    # Unified Error Conversion
    # ========================================================================
    
    def convert_error(
        self,
        error: APIError,
        target_format: Literal["openai", "anthropic", "gemini"] = "openai"
    ) -> Dict[str, Any]:
        """
        根据目标格式转换错误
        
        Args:
            error: 统一的 APIError 对象
            target_format: 目标 API 格式
            
        Returns:
            转换后的错误响应字典
        """
        if target_format == "openai":
            return self.to_openai_error(error)
        elif target_format == "anthropic":
            return self.to_anthropic_error(error)
        elif target_format == "gemini":
            return self.to_gemini_error(error)
        else:
            return self.to_openai_error(error)
    
    def convert_stackai_error(
        self,
        backend_response: Dict[str, Any],
        status_code: int,
        target_format: Literal["openai", "anthropic", "gemini"] = "openai"
    ) -> Dict[str, Any]:
        """
        将 StackAI 错误直接转换为目标格式
        
        Args:
            backend_response: StackAI 返回的错误响应
            status_code: HTTP 状态码
            target_format: 目标 API 格式
            
        Returns:
            转换后的错误响应字典
        """
        error = self.parse_backend_error(backend_response, status_code)
        return self.convert_error(error, target_format)
    
    # ========================================================================
    # Common Error Creators
    # ========================================================================
    
    def create_invalid_request_error(
        self,
        message: str,
        param: Optional[str] = None
    ) -> APIError:
        """创建无效请求错误"""
        return APIError(
            error_type=ErrorType.INVALID_REQUEST,
            message=message,
            code="invalid_request",
            status_code=400,
            param=param
        )
    
    def create_authentication_error(
        self,
        message: str = "Unauthorized"
    ) -> APIError:
        """创建认证错误"""
        return APIError(
            error_type=ErrorType.AUTHENTICATION,
            message="Unauthorized",  # 始终使用简洁消息，不暴露详细信息
            code="unauthorized",
            status_code=401
        )
    
    def create_permission_error(
        self,
        message: str = "You don't have permission to access this resource"
    ) -> APIError:
        """创建权限错误"""
        return APIError(
            error_type=ErrorType.PERMISSION,
            message=message,
            code="permission_denied",
            status_code=403
        )
    
    def create_not_found_error(
        self,
        message: str = "The requested resource was not found"
    ) -> APIError:
        """创建资源未找到错误"""
        return APIError(
            error_type=ErrorType.NOT_FOUND,
            message=message,
            code="not_found",
            status_code=404
        )
    
    def create_rate_limit_error(
        self,
        message: str = "Rate limit exceeded. Please try again later"
    ) -> APIError:
        """创建速率限制错误"""
        return APIError(
            error_type=ErrorType.RATE_LIMIT,
            message=message,
            code="rate_limit_exceeded",
            status_code=429
        )
    
    def create_quota_exceeded_error(
        self,
        message: str = "API key quota exceeded"
    ) -> APIError:
        """创建配额超限错误"""
        return APIError(
            error_type=ErrorType.QUOTA_EXCEEDED,
            message=message,
            code="quota_exceeded",
            status_code=429
        )
    
    def create_server_error(
        self,
        message: str = "Internal server error"
    ) -> APIError:
        """创建服务器错误"""
        return APIError(
            error_type=ErrorType.SERVER_ERROR,
            message="Internal server error",  # 简洁消息
            code="internal_error",
            status_code=500
        )
    
    def create_backend_error(
        self,
        message: str = "Service error"
    ) -> APIError:
        """创建后端错误"""
        return APIError(
            error_type=ErrorType.BACKEND_ERROR,
            message="Service error",  # 简洁消息
            code="service_error",
            status_code=502
        )
    
    def create_service_unavailable_error(
        self,
        message: str = "Service unavailable"
    ) -> APIError:
        """创建服务不可用错误"""
        return APIError(
            error_type=ErrorType.SERVICE_UNAVAILABLE,
            message="Service unavailable",  # 简洁消息
            code="service_unavailable",
            status_code=503
        )


# ============================================================================
# Global Instance
# ============================================================================

_error_handler: Optional[ErrorHandler] = None


def get_error_handler() -> ErrorHandler:
    """获取全局错误处理器实例"""
    global _error_handler
    if _error_handler is None:
        _error_handler = ErrorHandler()
    return _error_handler
