"""
Backend Client Service
与后端 API 通信的客户端，支持同步和流式请求
"""

import os
import logging
from typing import Optional, Dict, Any, AsyncGenerator

import httpx

from app.services.account_pool import AccountPoolService, get_account_pool_service

logger = logging.getLogger(__name__)

# 后端 API 基础 URL（从环境变量读取）
BACKEND_BASE_URL = os.getenv("BACKEND_API_URL", "https://api.stack-ai.com")

# 默认超时设置（秒）
DEFAULT_TIMEOUT = 120.0
DEFAULT_STREAM_TIMEOUT = 300.0


class BackendClientError(Exception):
    """后端客户端错误基类"""
    
    def __init__(self, message: str, status_code: Optional[int] = None, response_data: Optional[Dict] = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response_data = response_data


class BackendConnectionError(BackendClientError):
    """连接错误"""
    pass


class BackendTimeoutError(BackendClientError):
    """超时错误"""
    pass


class BackendAPIError(BackendClientError):
    """API 返回错误"""
    pass


# 兼容旧名称的别名
StackAIClientError = BackendClientError
StackAIConnectionError = BackendConnectionError
StackAITimeoutError = BackendTimeoutError
StackAIAPIError = BackendAPIError


class BackendClient:
    """
    后端 API 客户端
    
    提供与后端工作流 API 通信的功能，支持同步请求和流式请求。
    """
    
    def __init__(
        self,
        base_url: str = BACKEND_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        stream_timeout: float = DEFAULT_STREAM_TIMEOUT
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.stream_timeout = stream_timeout

    def _build_run_url(self, org_id: str, flow_id: str) -> str:
        return f"{self.base_url}/inference/v0/run/{org_id}/{flow_id}"
    
    def _build_stream_url(self, org_id: str, flow_id: str) -> str:
        return f"{self.base_url}/inference/v0/stream/{org_id}/{flow_id}"
    
    def _build_headers(self, api_key: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
    
    def _build_stream_headers(self, api_key: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream"
        }

    async def run(
        self,
        org_id: str,
        flow_id: str,
        api_key: str,
        payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """同步执行工作流"""
        url = self._build_run_url(org_id, flow_id)
        headers = self._build_headers(api_key)
        
        logger.debug(f"Sending sync request to backend: {url}")
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, headers=headers, json=payload)
                
                if response.status_code >= 400:
                    error_data = None
                    try:
                        error_data = response.json()
                    except Exception:
                        error_data = {"raw": response.text}
                    
                    logger.error(f"Backend API error: status={response.status_code}")
                    raise BackendAPIError(
                        message=f"Backend API returned error: {response.status_code}",
                        status_code=response.status_code,
                        response_data=error_data
                    )
                
                result = response.json()
                logger.debug(f"Backend sync response received: {len(str(result))} bytes")
                return result
                
        except httpx.ConnectError as e:
            logger.error(f"Failed to connect to backend: {e}")
            raise BackendConnectionError(message=f"Failed to connect to backend: {e}")
        except httpx.TimeoutException as e:
            logger.error(f"Backend request timed out: {e}")
            raise BackendTimeoutError(message=f"Backend request timed out after {self.timeout}s")
        except BackendClientError:
            raise
        except Exception as e:
            logger.error(f"Unexpected error calling backend: {e}")
            raise BackendClientError(message=f"Unexpected error: {e}")

    async def run_with_account(
        self,
        account,
        payload: Dict[str, Any],
        account_pool: Optional[AccountPoolService] = None
    ) -> Dict[str, Any]:
        """使用账号对象执行同步请求"""
        if account_pool is None:
            account_pool = get_account_pool_service()
        
        api_key = account_pool.decrypt_api_key(account)
        
        return await self.run(
            org_id=account.org_id,
            flow_id=account.flow_id,
            api_key=api_key,
            payload=payload
        )

    async def stream(
        self,
        org_id: str,
        flow_id: str,
        api_key: str,
        payload: Dict[str, Any]
    ) -> AsyncGenerator[str, None]:
        """流式执行工作流"""
        url = self._build_stream_url(org_id, flow_id)
        headers = self._build_stream_headers(api_key)
        
        logger.debug(f"Sending stream request to backend: {url}")
        
        try:
            async with httpx.AsyncClient(timeout=self.stream_timeout) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    if response.status_code >= 400:
                        error_body = await response.aread()
                        error_data = None
                        try:
                            error_data = {"raw": error_body.decode("utf-8")}
                        except Exception:
                            error_data = {"raw": str(error_body)}
                        
                        logger.error(f"Backend API error: status={response.status_code}")
                        raise BackendAPIError(
                            message=f"Backend API returned error: {response.status_code}",
                            status_code=response.status_code,
                            response_data=error_data
                        )
                    
                    buffer = ""
                    async for chunk in response.aiter_text():
                        buffer += chunk
                        
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()
                            if line:
                                yield line
                    
                    if buffer.strip():
                        yield buffer.strip()
                    
                    logger.debug("Backend stream completed")
                    
        except httpx.ConnectError as e:
            logger.error(f"Failed to connect to backend: {e}")
            raise BackendConnectionError(message=f"Failed to connect to backend: {e}")
        except httpx.TimeoutException as e:
            logger.error(f"Backend stream timed out: {e}")
            raise BackendTimeoutError(message=f"Backend stream timed out after {self.stream_timeout}s")
        except BackendClientError:
            raise
        except Exception as e:
            logger.error(f"Unexpected error in backend stream: {e}")
            raise BackendClientError(message=f"Unexpected error: {e}")
    
    async def stream_with_account(
        self,
        account,
        payload: Dict[str, Any],
        account_pool: Optional[AccountPoolService] = None
    ) -> AsyncGenerator[str, None]:
        """使用账号对象执行流式请求"""
        if account_pool is None:
            account_pool = get_account_pool_service()
        
        api_key = account_pool.decrypt_api_key(account)
        
        async for chunk in self.stream(
            org_id=account.org_id,
            flow_id=account.flow_id,
            api_key=api_key,
            payload=payload
        ):
            yield chunk

    def should_use_stream(self, stream: bool) -> bool:
        return stream is True
    
    async def execute(
        self,
        org_id: str,
        flow_id: str,
        api_key: str,
        payload: Dict[str, Any],
        stream: bool = False
    ):
        """执行工作流（自动选择同步或流式）"""
        if self.should_use_stream(stream):
            async def stream_wrapper():
                async for chunk in self.stream(org_id, flow_id, api_key, payload):
                    yield chunk
            return stream_wrapper()
        else:
            return await self.run(org_id, flow_id, api_key, payload)
    
    async def execute_with_account(
        self,
        account,
        payload: Dict[str, Any],
        stream: bool = False,
        account_pool: Optional[AccountPoolService] = None
    ):
        """使用账号对象执行工作流（自动选择同步或流式）"""
        if account_pool is None:
            account_pool = get_account_pool_service()
        
        api_key = account_pool.decrypt_api_key(account)
        
        if stream:
            async def stream_wrapper():
                async for chunk in self.stream(
                    org_id=account.org_id,
                    flow_id=account.flow_id,
                    api_key=api_key,
                    payload=payload
                ):
                    yield chunk
            return stream_wrapper()
        else:
            return await self.run(
                org_id=account.org_id,
                flow_id=account.flow_id,
                api_key=api_key,
                payload=payload
            )


# 全局实例
_backend_client: Optional[BackendClient] = None


def get_backend_client() -> BackendClient:
    """获取全局后端客户端实例"""
    global _backend_client
    if _backend_client is None:
        _backend_client = BackendClient()
    return _backend_client


def init_backend_client(
    base_url: str = BACKEND_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
    stream_timeout: float = DEFAULT_STREAM_TIMEOUT
) -> BackendClient:
    """初始化全局后端客户端"""
    global _backend_client
    _backend_client = BackendClient(
        base_url=base_url,
        timeout=timeout,
        stream_timeout=stream_timeout
    )
    return _backend_client


# 兼容旧名称的别名
StackAIClient = BackendClient
get_stackai_client = get_backend_client
init_stackai_client = init_backend_client
