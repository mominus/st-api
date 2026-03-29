"""
Backend Client Service
与后端 API 通信的客户端，支持同步和流式请求
"""

import asyncio
import os
import logging
import random
from typing import Optional, Dict, Any, AsyncGenerator, Set

import httpx

from app.services.account_pool import AccountPoolService, get_account_pool_service

logger = logging.getLogger(__name__)

# 后端 API 基础 URL（从环境变量读取）
BACKEND_BASE_URL = os.getenv("BACKEND_API_URL", "https://api.stack-ai.com")

# 默认超时设置（秒）
DEFAULT_TIMEOUT = float(os.getenv("BACKEND_TIMEOUT_SECONDS", "120.0"))
DEFAULT_STREAM_TIMEOUT = float(os.getenv("BACKEND_STREAM_TIMEOUT_SECONDS", "300.0"))


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
STClientError = BackendClientError
STConnectionError = BackendConnectionError
STTimeoutError = BackendTimeoutError
STAPIError = BackendAPIError


def _parse_retry_status_codes(raw: Optional[str]) -> Set[int]:
    if not raw:
        return {429, 500, 502, 503, 504}

    parsed: Set[int] = set()
    for token in str(raw).split(","):
        token = token.strip()
        if not token:
            continue
        try:
            value = int(token)
        except ValueError:
            continue
        if value > 0:
            parsed.add(value)

    if parsed:
        return parsed
    return {429, 500, 502, 503, 504}


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
        self.connect_timeout = float(os.getenv("HTTP_CONNECT_TIMEOUT", "10.0"))
        self.http_max_connections = int(os.getenv("HTTP_MAX_CONNECTIONS", "100"))
        self.http_max_keepalive = int(os.getenv("HTTP_MAX_KEEPALIVE", "20"))
        self.http_trust_env = os.getenv("HTTP_TRUST_ENV", "false").lower() == "true"

        self.sync_max_retries = max(0, int(os.getenv("BACKEND_SYNC_MAX_RETRIES", "2")))
        self.stream_max_retries = max(0, int(os.getenv("BACKEND_STREAM_MAX_RETRIES", "1")))
        self.retry_base_delay_seconds = max(0.0, float(os.getenv("BACKEND_RETRY_BASE_DELAY_SECONDS", "0.25")))
        self.retry_max_delay_seconds = max(
            self.retry_base_delay_seconds,
            float(os.getenv("BACKEND_RETRY_MAX_DELAY_SECONDS", "2.0")),
        )
        self.retry_jitter_seconds = max(0.0, float(os.getenv("BACKEND_RETRY_JITTER_SECONDS", "0.10")))
        self.retry_on_connect_error = os.getenv("BACKEND_RETRY_ON_CONNECT_ERROR", "true").lower() == "true"
        self.retry_on_timeout = os.getenv("BACKEND_RETRY_ON_TIMEOUT", "false").lower() == "true"
        self.retry_on_transport_error = os.getenv("BACKEND_RETRY_ON_TRANSPORT_ERROR", "true").lower() == "true"
        self.retryable_status_codes = _parse_retry_status_codes(
            os.getenv("BACKEND_RETRY_STATUS_CODES", "429,500,502,503,504")
        )

        self._client: Optional[httpx.AsyncClient] = None
        self._client_lock: Optional[asyncio.Lock] = None
        self._loop_id: Optional[int] = None

    def _ensure_runtime_state(self) -> None:
        """确保当前事件循环下的锁和客户端状态可用。"""
        loop_id = id(asyncio.get_running_loop())
        if self._loop_id != loop_id:
            # 跨事件循环时丢弃旧客户端引用，避免 loop 绑定问题。
            self._client = None
            self._client_lock = asyncio.Lock()
            self._loop_id = loop_id

    async def _get_client(self) -> httpx.AsyncClient:
        self._ensure_runtime_state()
        if self._client is None or self._client.is_closed:
            assert self._client_lock is not None
            async with self._client_lock:
                if self._client is None or self._client.is_closed:
                    self._client = httpx.AsyncClient(
                        trust_env=self.http_trust_env,
                        limits=httpx.Limits(
                            max_connections=self.http_max_connections,
                            max_keepalive_connections=min(
                                self.http_max_keepalive,
                                self.http_max_connections
                            ),
                        )
                    )
                    logger.info(
                        (
                            "Backend HTTP client initialized: max_connections=%s, max_keepalive=%s, "
                            "trust_env=%s, sync_retries=%s, stream_retries=%s"
                        ),
                        self.http_max_connections,
                        min(self.http_max_keepalive, self.http_max_connections),
                        self.http_trust_env,
                        self.sync_max_retries,
                        self.stream_max_retries,
                    )
        return self._client

    def _sync_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            timeout=self.timeout,
            connect=self.connect_timeout,
        )

    def _stream_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect_timeout,
            read=self.stream_timeout,
            write=self.timeout,
            pool=self.timeout,
        )

    def _is_retryable_status_code(self, status_code: int) -> bool:
        return status_code in self.retryable_status_codes

    def _retry_delay_seconds(
        self,
        retry_index: int,
        *,
        response_headers: Optional[Dict[str, str]] = None,
    ) -> float:
        # Prefer upstream retry-after when present.
        if response_headers:
            raw_retry_after = response_headers.get("retry-after") or response_headers.get("Retry-After")
            if raw_retry_after:
                try:
                    retry_after = float(raw_retry_after)
                    if retry_after > 0:
                        return min(retry_after, self.retry_max_delay_seconds)
                except (TypeError, ValueError):
                    pass

        exp_delay = self.retry_base_delay_seconds * (2 ** max(0, retry_index - 1))
        jitter = random.uniform(0.0, self.retry_jitter_seconds) if self.retry_jitter_seconds > 0 else 0.0
        return min(self.retry_max_delay_seconds, exp_delay + jitter)

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

    @staticmethod
    def _parse_error_response_data(response: httpx.Response) -> Dict[str, Any]:
        try:
            return response.json()
        except Exception:
            return {"raw": response.text}

    @staticmethod
    def _parse_stream_error_body(error_body: bytes) -> Dict[str, Any]:
        try:
            return {"raw": error_body.decode("utf-8")}
        except Exception:
            return {"raw": str(error_body)}

    async def _sleep_before_retry(
        self,
        *,
        mode: str,
        attempt: int,
        max_attempts: int,
        reason: str,
        response_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        delay = self._retry_delay_seconds(
            attempt,
            response_headers=response_headers,
        )
        logger.warning(
            "Backend %s retry %s/%s in %.2fs due to %s",
            mode,
            attempt,
            max_attempts,
            delay,
            reason,
        )
        await asyncio.sleep(delay)

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            logger.info("Backend HTTP client closed")
        self._client = None

    async def run(
        self,
        org_id: str,
        flow_id: str,
        api_key: str,
        payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """同步执行工作流（带重试）。"""
        url = self._build_run_url(org_id, flow_id)
        headers = self._build_headers(api_key)

        logger.debug(f"Sending sync request to backend: {url}")

        max_attempts = self.sync_max_retries + 1
        for attempt in range(1, max_attempts + 1):
            is_last_attempt = attempt >= max_attempts
            try:
                client = await self._get_client()
                response = await client.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=self._sync_timeout(),
                )

                if response.status_code >= 400:
                    error_data = self._parse_error_response_data(response)
                    if (not is_last_attempt) and self._is_retryable_status_code(response.status_code):
                        await self._sleep_before_retry(
                            mode="sync",
                            attempt=attempt,
                            max_attempts=max_attempts,
                            reason=f"HTTP {response.status_code}",
                            response_headers=dict(response.headers),
                        )
                        continue

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
                if (not is_last_attempt) and self.retry_on_connect_error:
                    await self._sleep_before_retry(
                        mode="sync",
                        attempt=attempt,
                        max_attempts=max_attempts,
                        reason=f"connect error: {e}",
                    )
                    continue
                logger.error(f"Failed to connect to backend: {e}")
                raise BackendConnectionError(message=f"Failed to connect to backend: {e}")

            except httpx.TimeoutException as e:
                if (not is_last_attempt) and self.retry_on_timeout:
                    await self._sleep_before_retry(
                        mode="sync",
                        attempt=attempt,
                        max_attempts=max_attempts,
                        reason=f"timeout: {e}",
                    )
                    continue
                logger.error(f"Backend request timed out: {e}")
                raise BackendTimeoutError(message=f"Backend request timed out after {self.timeout}s")

            except httpx.TransportError as e:
                if (not is_last_attempt) and self.retry_on_transport_error:
                    await self._sleep_before_retry(
                        mode="sync",
                        attempt=attempt,
                        max_attempts=max_attempts,
                        reason=f"transport error: {e}",
                    )
                    continue
                logger.error(f"Backend transport error: {e}")
                raise BackendConnectionError(message=f"Failed to connect to backend: {e}")

            except BackendClientError:
                raise

            except Exception as e:
                logger.error(f"Unexpected error calling backend: {e}")
                raise BackendClientError(message=f"Unexpected error: {e}")

        raise BackendClientError(message="Unexpected retry loop exit")

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
        """流式执行工作流（在未输出内容前允许重试）。"""
        url = self._build_stream_url(org_id, flow_id)
        headers = self._build_stream_headers(api_key)

        logger.debug(f"Sending stream request to backend: {url}")

        yielded_any = False
        max_attempts = self.stream_max_retries + 1
        for attempt in range(1, max_attempts + 1):
            is_last_attempt = attempt >= max_attempts
            try:
                client = await self._get_client()
                async with client.stream(
                    "POST",
                    url,
                    headers=headers,
                    json=payload,
                    timeout=self._stream_timeout(),
                ) as response:
                    if response.status_code >= 400:
                        error_body = await response.aread()
                        error_data = self._parse_stream_error_body(error_body)

                        if (
                            (not is_last_attempt)
                            and (not yielded_any)
                            and self._is_retryable_status_code(response.status_code)
                        ):
                            await self._sleep_before_retry(
                                mode="stream",
                                attempt=attempt,
                                max_attempts=max_attempts,
                                reason=f"HTTP {response.status_code}",
                                response_headers=dict(response.headers),
                            )
                            continue

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
                                yielded_any = True
                                yield line

                    if buffer.strip():
                        yielded_any = True
                        yield buffer.strip()

                    logger.debug("Backend stream completed")
                    return

            except httpx.ConnectError as e:
                if (not is_last_attempt) and (not yielded_any) and self.retry_on_connect_error:
                    await self._sleep_before_retry(
                        mode="stream",
                        attempt=attempt,
                        max_attempts=max_attempts,
                        reason=f"connect error: {e}",
                    )
                    continue
                logger.error(f"Failed to connect to backend: {e}")
                raise BackendConnectionError(message=f"Failed to connect to backend: {e}")

            except httpx.TimeoutException as e:
                if (not is_last_attempt) and (not yielded_any) and self.retry_on_timeout:
                    await self._sleep_before_retry(
                        mode="stream",
                        attempt=attempt,
                        max_attempts=max_attempts,
                        reason=f"timeout: {e}",
                    )
                    continue
                logger.error(f"Backend stream timed out: {e}")
                raise BackendTimeoutError(message=f"Backend stream timed out after {self.stream_timeout}s")

            except httpx.TransportError as e:
                if (not is_last_attempt) and (not yielded_any) and self.retry_on_transport_error:
                    await self._sleep_before_retry(
                        mode="stream",
                        attempt=attempt,
                        max_attempts=max_attempts,
                        reason=f"transport error: {e}",
                    )
                    continue
                logger.error(f"Backend stream transport error: {e}")
                raise BackendConnectionError(message=f"Failed to connect to backend: {e}")

            except BackendClientError:
                raise

            except Exception as e:
                logger.error(f"Unexpected error in backend stream: {e}")
                raise BackendClientError(message=f"Unexpected error: {e}")

        raise BackendClientError(message="Unexpected stream retry loop exit")

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


async def close_backend_client() -> None:
    """关闭全局后端客户端连接池。"""
    global _backend_client
    if _backend_client is not None:
        await _backend_client.close()
        _backend_client = None


# 兼容旧名称的别名
STClient = BackendClient
get_st_client = get_backend_client
init_st_client = init_backend_client
