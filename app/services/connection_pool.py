"""
Connection Pool Management
高并发连接池管理，优化数据库和 HTTP 客户端连接

支持:
- 数据库连接池配置
- HTTP 客户端连接池
- 信号量限流
- 并发请求管理
"""

import os
import asyncio
from typing import Optional, Dict, Any
from contextlib import asynccontextmanager
import logging

import httpx

logger = logging.getLogger(__name__)


class ConnectionPoolConfig:
    """连接池配置"""
    
    # 数据库连接池配置
    DB_POOL_SIZE: int = int(os.getenv("DB_POOL_SIZE", "20"))
    DB_MAX_OVERFLOW: int = int(os.getenv("DB_MAX_OVERFLOW", "30"))
    DB_POOL_TIMEOUT: int = int(os.getenv("DB_POOL_TIMEOUT", "30"))
    DB_POOL_RECYCLE: int = int(os.getenv("DB_POOL_RECYCLE", "3600"))
    
    # HTTP 客户端连接池配置
    HTTP_MAX_CONNECTIONS: int = int(os.getenv("HTTP_MAX_CONNECTIONS", "100"))
    HTTP_MAX_KEEPALIVE: int = int(os.getenv("HTTP_MAX_KEEPALIVE", "20"))
    HTTP_TIMEOUT: float = float(os.getenv("HTTP_TIMEOUT", "60.0"))
    HTTP_CONNECT_TIMEOUT: float = float(os.getenv("HTTP_CONNECT_TIMEOUT", "10.0"))
    
    # 并发限制
    MAX_CONCURRENT_REQUESTS: int = int(os.getenv("MAX_CONCURRENT_REQUESTS", "100"))
    MAX_CONCURRENT_DB_OPS: int = int(os.getenv("MAX_CONCURRENT_DB_OPS", "50"))


class HTTPClientPool:
    """
    HTTP 客户端连接池
    
    使用 httpx.AsyncClient 管理 HTTP 连接，支持连接复用和超时控制
    """
    
    _instance: Optional["HTTPClientPool"] = None
    _client: Optional[httpx.AsyncClient] = None
    _lock: asyncio.Lock = asyncio.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    async def get_client(self) -> httpx.AsyncClient:
        """获取 HTTP 客户端实例"""
        if self._client is None or self._client.is_closed:
            async with self._lock:
                if self._client is None or self._client.is_closed:
                    self._client = httpx.AsyncClient(
                        limits=httpx.Limits(
                            max_connections=ConnectionPoolConfig.HTTP_MAX_CONNECTIONS,
                            max_keepalive_connections=ConnectionPoolConfig.HTTP_MAX_KEEPALIVE
                        ),
                        timeout=httpx.Timeout(
                            timeout=ConnectionPoolConfig.HTTP_TIMEOUT,
                            connect=ConnectionPoolConfig.HTTP_CONNECT_TIMEOUT
                        )
                        # 注意：HTTP/2 需要额外安装 h2 包，这里默认不启用
                        # 如需启用，请运行: pip install httpx[http2]
                    )
                    logger.info(
                        f"Created HTTP client pool: "
                        f"max_connections={ConnectionPoolConfig.HTTP_MAX_CONNECTIONS}, "
                        f"max_keepalive={ConnectionPoolConfig.HTTP_MAX_KEEPALIVE}"
                    )
        return self._client
    
    async def close(self):
        """关闭 HTTP 客户端"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
            logger.info("Closed HTTP client pool")


class ConcurrencyLimiter:
    """
    并发限制器
    
    使用信号量控制并发请求数量，防止系统过载
    """
    
    _instance: Optional["ConcurrencyLimiter"] = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._request_semaphore = None
            cls._instance._db_semaphore = None
            cls._instance._active_requests = 0
            cls._instance._total_requests = 0
            cls._instance._rejected_requests = 0
            cls._instance._lock = None
            cls._instance._loop_id = None
            # 响应时间统计
            cls._instance._response_times = []
            cls._instance._max_response_samples = 1000  # 保留最近1000个样本
        return cls._instance
    
    def _ensure_initialized(self):
        """确保信号量已初始化（在当前事件循环中）"""
        try:
            current_loop = asyncio.get_running_loop()
            current_loop_id = id(current_loop)
        except RuntimeError:
            current_loop_id = None
        
        # 如果事件循环变了，重新创建信号量
        if self._loop_id != current_loop_id:
            self._request_semaphore = asyncio.Semaphore(
                ConnectionPoolConfig.MAX_CONCURRENT_REQUESTS
            )
            self._db_semaphore = asyncio.Semaphore(
                ConnectionPoolConfig.MAX_CONCURRENT_DB_OPS
            )
            self._lock = asyncio.Lock()
            self._loop_id = current_loop_id
    
    @asynccontextmanager
    async def acquire_request(self, timeout: float = 30.0):
        """
        获取请求许可
        
        Args:
            timeout: 等待超时时间（秒）
            
        Raises:
            asyncio.TimeoutError: 等待超时
        """
        self._ensure_initialized()
        
        try:
            # 尝试在超时时间内获取信号量
            await asyncio.wait_for(
                self._request_semaphore.acquire(),
                timeout=timeout
            )
            
            async with self._lock:
                self._active_requests += 1
                self._total_requests += 1
            
            try:
                yield
            finally:
                self._request_semaphore.release()
                async with self._lock:
                    self._active_requests -= 1
                    
        except asyncio.TimeoutError:
            async with self._lock:
                self._rejected_requests += 1
            logger.warning(
                f"Request rejected due to concurrency limit. "
                f"Active: {self._active_requests}, "
                f"Max: {ConnectionPoolConfig.MAX_CONCURRENT_REQUESTS}"
            )
            raise
    
    @asynccontextmanager
    async def acquire_db(self, timeout: float = 10.0):
        """
        获取数据库操作许可
        
        Args:
            timeout: 等待超时时间（秒）
        """
        self._ensure_initialized()
        
        try:
            await asyncio.wait_for(
                self._db_semaphore.acquire(),
                timeout=timeout
            )
            try:
                yield
            finally:
                self._db_semaphore.release()
        except asyncio.TimeoutError:
            logger.warning("Database operation rejected due to concurrency limit")
            raise
    
    def get_stats(self) -> Dict[str, Any]:
        """获取并发统计信息"""
        # 计算响应时间统计
        avg_response = 0
        p95_response = 0
        p99_response = 0
        
        if self._response_times:
            sorted_times = sorted(self._response_times)
            avg_response = sum(sorted_times) / len(sorted_times)
            p95_idx = int(len(sorted_times) * 0.95)
            p99_idx = int(len(sorted_times) * 0.99)
            p95_response = sorted_times[min(p95_idx, len(sorted_times) - 1)]
            p99_response = sorted_times[min(p99_idx, len(sorted_times) - 1)]
        
        return {
            "active_requests": self._active_requests,
            "total_requests": self._total_requests,
            "rejected_requests": self._rejected_requests,
            "max_concurrent_requests": ConnectionPoolConfig.MAX_CONCURRENT_REQUESTS,
            "max_concurrent_db_ops": ConnectionPoolConfig.MAX_CONCURRENT_DB_OPS,
            "avg_response_time": round(avg_response, 2),
            "p95_response_time": round(p95_response, 2),
            "p99_response_time": round(p99_response, 2),
        }
    
    def record_response_time(self, elapsed_ms: float):
        """记录响应时间（毫秒）"""
        self._response_times.append(elapsed_ms)
        # 保持样本数量在限制内
        if len(self._response_times) > self._max_response_samples:
            self._response_times = self._response_times[-self._max_response_samples:]
    
    def reset_stats(self):
        """重置统计信息"""
        self._total_requests = 0
        self._rejected_requests = 0
        self._response_times = []


class RequestQueue:
    """
    请求队列
    
    用于管理待处理的请求，支持优先级和超时
    """
    
    def __init__(self, max_size: int = 1000):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_size)
        self._processing: int = 0
        self._completed: int = 0
        self._failed: int = 0
    
    async def enqueue(self, request_data: Dict[str, Any], timeout: float = 30.0) -> bool:
        """
        将请求加入队列
        
        Args:
            request_data: 请求数据
            timeout: 入队超时时间
            
        Returns:
            是否成功入队
        """
        try:
            await asyncio.wait_for(
                self._queue.put(request_data),
                timeout=timeout
            )
            return True
        except asyncio.TimeoutError:
            logger.warning("Request queue full, request rejected")
            return False
    
    async def dequeue(self, timeout: float = 1.0) -> Optional[Dict[str, Any]]:
        """
        从队列获取请求
        
        Args:
            timeout: 出队超时时间
            
        Returns:
            请求数据，如果超时则返回 None
        """
        try:
            return await asyncio.wait_for(
                self._queue.get(),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            return None
    
    def get_stats(self) -> Dict[str, Any]:
        """获取队列统计信息"""
        return {
            "queue_size": self._queue.qsize(),
            "processing": self._processing,
            "completed": self._completed,
            "failed": self._failed
        }


# 全局实例
_http_pool: Optional[HTTPClientPool] = None
_concurrency_limiter: Optional[ConcurrencyLimiter] = None


def get_http_pool() -> HTTPClientPool:
    """获取 HTTP 客户端池实例"""
    global _http_pool
    if _http_pool is None:
        _http_pool = HTTPClientPool()
    return _http_pool


def get_concurrency_limiter() -> ConcurrencyLimiter:
    """获取并发限制器实例"""
    global _concurrency_limiter
    if _concurrency_limiter is None:
        _concurrency_limiter = ConcurrencyLimiter()
    return _concurrency_limiter


async def init_connection_pools():
    """初始化所有连接池"""
    http_pool = get_http_pool()
    await http_pool.get_client()
    logger.info("Connection pools initialized")


async def close_connection_pools():
    """关闭所有连接池"""
    global _http_pool
    if _http_pool:
        await _http_pool.close()
        _http_pool = None
    logger.info("Connection pools closed")
