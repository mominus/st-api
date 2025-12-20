"""
Concurrency Management Service
高并发控制服务，提供连接池管理、信号量控制、请求队列等功能
"""

import asyncio
import time
import logging
from typing import Optional, Dict, Any, Callable, TypeVar, Coroutine
from dataclasses import dataclass, field
from collections import defaultdict
from contextlib import asynccontextmanager
from functools import wraps

logger = logging.getLogger(__name__)

T = TypeVar('T')


@dataclass
class ConcurrencyStats:
    """并发统计信息"""
    total_requests: int = 0
    active_requests: int = 0
    queued_requests: int = 0
    completed_requests: int = 0
    failed_requests: int = 0
    total_wait_time_ms: float = 0
    total_process_time_ms: float = 0
    peak_concurrent: int = 0
    
    @property
    def avg_wait_time_ms(self) -> float:
        if self.completed_requests == 0:
            return 0
        return self.total_wait_time_ms / self.completed_requests
    
    @property
    def avg_process_time_ms(self) -> float:
        if self.completed_requests == 0:
            return 0
        return self.total_process_time_ms / self.completed_requests


@dataclass
class RequestContext:
    """请求上下文"""
    request_id: str
    model_group: str
    start_time: float = field(default_factory=time.time)
    queue_time: float = 0
    process_start_time: float = 0


class ConcurrencyManager:
    """
    并发管理器
    
    提供以下功能：
    1. 全局并发限制 - 限制同时处理的请求数
    2. 模型组级别并发限制 - 按模型组限制并发
    3. 请求队列 - 超出限制时排队等待
    4. 超时控制 - 防止请求无限等待
    5. 统计信息 - 监控并发状态
    """
    
    def __init__(
        self,
        max_concurrent: int = 100,
        max_per_model_group: int = 20,
        queue_timeout: float = 30.0,
        enable_queue: bool = True
    ):
        """
        初始化并发管理器
        
        Args:
            max_concurrent: 全局最大并发数
            max_per_model_group: 每个模型组最大并发数
            queue_timeout: 队列等待超时时间（秒）
            enable_queue: 是否启用请求队列
        """
        self.max_concurrent = max_concurrent
        self.max_per_model_group = max_per_model_group
        self.queue_timeout = queue_timeout
        self.enable_queue = enable_queue
        
        # 全局信号量
        self._global_semaphore = asyncio.Semaphore(max_concurrent)
        
        # 模型组级别信号量
        self._model_group_semaphores: Dict[str, asyncio.Semaphore] = {}
        self._semaphore_lock = asyncio.Lock()
        
        # 统计信息
        self._stats = ConcurrencyStats()
        self._model_group_stats: Dict[str, ConcurrencyStats] = defaultdict(ConcurrencyStats)
        
        # 活跃请求追踪
        self._active_requests: Dict[str, RequestContext] = {}
        self._requests_lock = asyncio.Lock()
    
    async def _get_model_group_semaphore(self, model_group: str) -> asyncio.Semaphore:
        """获取或创建模型组信号量"""
        async with self._semaphore_lock:
            if model_group not in self._model_group_semaphores:
                self._model_group_semaphores[model_group] = asyncio.Semaphore(
                    self.max_per_model_group
                )
            return self._model_group_semaphores[model_group]
    
    @asynccontextmanager
    async def acquire(self, request_id: str, model_group: str):
        """
        获取并发许可
        
        使用上下文管理器确保许可正确释放
        
        Args:
            request_id: 请求 ID
            model_group: 模型组名称
            
        Yields:
            RequestContext: 请求上下文
            
        Raises:
            asyncio.TimeoutError: 等待超时
            ConcurrencyLimitError: 并发限制（队列禁用时）
        """
        context = RequestContext(
            request_id=request_id,
            model_group=model_group
        )
        
        # 更新统计
        self._stats.total_requests += 1
        self._stats.queued_requests += 1
        self._model_group_stats[model_group].total_requests += 1
        self._model_group_stats[model_group].queued_requests += 1
        
        model_semaphore = await self._get_model_group_semaphore(model_group)
        
        try:
            if self.enable_queue:
                # 带超时的等待
                try:
                    await asyncio.wait_for(
                        self._acquire_both(model_semaphore),
                        timeout=self.queue_timeout
                    )
                except asyncio.TimeoutError:
                    self._stats.queued_requests -= 1
                    self._stats.failed_requests += 1
                    self._model_group_stats[model_group].queued_requests -= 1
                    self._model_group_stats[model_group].failed_requests += 1
                    raise
            else:
                # 不等待，直接尝试获取
                if not self._global_semaphore.locked() and not model_semaphore.locked():
                    await self._acquire_both(model_semaphore)
                else:
                    self._stats.queued_requests -= 1
                    self._stats.failed_requests += 1
                    self._model_group_stats[model_group].queued_requests -= 1
                    self._model_group_stats[model_group].failed_requests += 1
                    raise ConcurrencyLimitError(
                        f"Concurrency limit reached for model group: {model_group}"
                    )
            
            # 记录等待时间
            context.queue_time = time.time() - context.start_time
            context.process_start_time = time.time()
            
            # 更新统计
            self._stats.queued_requests -= 1
            self._stats.active_requests += 1
            self._stats.peak_concurrent = max(
                self._stats.peak_concurrent, 
                self._stats.active_requests
            )
            self._model_group_stats[model_group].queued_requests -= 1
            self._model_group_stats[model_group].active_requests += 1
            
            # 追踪活跃请求
            async with self._requests_lock:
                self._active_requests[request_id] = context
            
            logger.debug(
                f"Request {request_id} acquired permit "
                f"(wait: {context.queue_time*1000:.1f}ms, "
                f"active: {self._stats.active_requests})"
            )
            
            yield context
            
        finally:
            # 释放信号量
            self._global_semaphore.release()
            model_semaphore.release()
            
            # 计算处理时间
            process_time = time.time() - context.process_start_time if context.process_start_time else 0
            
            # 更新统计
            self._stats.active_requests -= 1
            self._stats.completed_requests += 1
            self._stats.total_wait_time_ms += context.queue_time * 1000
            self._stats.total_process_time_ms += process_time * 1000
            
            self._model_group_stats[model_group].active_requests -= 1
            self._model_group_stats[model_group].completed_requests += 1
            self._model_group_stats[model_group].total_wait_time_ms += context.queue_time * 1000
            self._model_group_stats[model_group].total_process_time_ms += process_time * 1000
            
            # 移除活跃请求追踪
            async with self._requests_lock:
                self._active_requests.pop(request_id, None)
            
            logger.debug(
                f"Request {request_id} released permit "
                f"(process: {process_time*1000:.1f}ms)"
            )
    
    async def _acquire_both(self, model_semaphore: asyncio.Semaphore):
        """同时获取全局和模型组信号量"""
        await self._global_semaphore.acquire()
        try:
            await model_semaphore.acquire()
        except:
            self._global_semaphore.release()
            raise
    
    def get_stats(self) -> Dict[str, Any]:
        """获取全局统计信息"""
        return {
            "total_requests": self._stats.total_requests,
            "active_requests": self._stats.active_requests,
            "queued_requests": self._stats.queued_requests,
            "completed_requests": self._stats.completed_requests,
            "failed_requests": self._stats.failed_requests,
            "peak_concurrent": self._stats.peak_concurrent,
            "avg_wait_time_ms": round(self._stats.avg_wait_time_ms, 2),
            "avg_process_time_ms": round(self._stats.avg_process_time_ms, 2),
            "max_concurrent": self.max_concurrent,
            "max_per_model_group": self.max_per_model_group
        }
    
    def get_model_group_stats(self, model_group: str) -> Dict[str, Any]:
        """获取模型组统计信息"""
        stats = self._model_group_stats[model_group]
        return {
            "model_group": model_group,
            "total_requests": stats.total_requests,
            "active_requests": stats.active_requests,
            "queued_requests": stats.queued_requests,
            "completed_requests": stats.completed_requests,
            "failed_requests": stats.failed_requests,
            "avg_wait_time_ms": round(stats.avg_wait_time_ms, 2),
            "avg_process_time_ms": round(stats.avg_process_time_ms, 2)
        }
    
    async def get_active_requests(self) -> Dict[str, Dict[str, Any]]:
        """获取所有活跃请求"""
        async with self._requests_lock:
            return {
                req_id: {
                    "request_id": ctx.request_id,
                    "model_group": ctx.model_group,
                    "elapsed_ms": (time.time() - ctx.process_start_time) * 1000
                }
                for req_id, ctx in self._active_requests.items()
            }
    
    def reset_stats(self):
        """重置统计信息"""
        self._stats = ConcurrencyStats()
        self._model_group_stats.clear()


class ConcurrencyLimitError(Exception):
    """并发限制错误"""
    pass


class RateLimiter:
    """
    速率限制器
    
    使用令牌桶算法实现请求速率限制
    """
    
    def __init__(
        self,
        rate: float = 100.0,  # 每秒请求数
        burst: int = 200      # 突发容量
    ):
        """
        初始化速率限制器
        
        Args:
            rate: 每秒允许的请求数
            burst: 突发容量（令牌桶大小）
        """
        self.rate = rate
        self.burst = burst
        self._tokens = float(burst)
        self._last_update = time.time()
        self._lock = asyncio.Lock()
    
    async def acquire(self, tokens: int = 1) -> bool:
        """
        尝试获取令牌
        
        Args:
            tokens: 需要的令牌数
            
        Returns:
            是否成功获取
        """
        async with self._lock:
            now = time.time()
            elapsed = now - self._last_update
            self._last_update = now
            
            # 补充令牌
            self._tokens = min(
                self.burst,
                self._tokens + elapsed * self.rate
            )
            
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False
    
    async def wait_for_token(self, tokens: int = 1, timeout: float = 10.0) -> bool:
        """
        等待获取令牌
        
        Args:
            tokens: 需要的令牌数
            timeout: 最大等待时间
            
        Returns:
            是否成功获取
        """
        start = time.time()
        while time.time() - start < timeout:
            if await self.acquire(tokens):
                return True
            await asyncio.sleep(0.01)  # 10ms 间隔重试
        return False
    
    @property
    def available_tokens(self) -> float:
        """当前可用令牌数"""
        elapsed = time.time() - self._last_update
        return min(self.burst, self._tokens + elapsed * self.rate)


class ConnectionPool:
    """
    HTTP 连接池管理器
    
    管理到后端服务的 HTTP 连接，支持连接复用和健康检查
    """
    
    def __init__(
        self,
        max_connections: int = 100,
        max_connections_per_host: int = 20,
        keepalive_timeout: float = 30.0
    ):
        """
        初始化连接池
        
        Args:
            max_connections: 最大连接数
            max_connections_per_host: 每个主机最大连接数
            keepalive_timeout: 连接保活超时时间
        """
        self.max_connections = max_connections
        self.max_connections_per_host = max_connections_per_host
        self.keepalive_timeout = keepalive_timeout
        self._client = None
    
    async def get_client(self):
        """获取 HTTP 客户端"""
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(
                limits=httpx.Limits(
                    max_connections=self.max_connections,
                    max_keepalive_connections=self.max_connections_per_host,
                    keepalive_expiry=self.keepalive_timeout
                ),
                timeout=httpx.Timeout(
                    connect=10.0,
                    read=120.0,
                    write=30.0,
                    pool=30.0
                )
            )
        return self._client
    
    async def close(self):
        """关闭连接池"""
        if self._client:
            await self._client.aclose()
            self._client = None


# 全局实例
_concurrency_manager: Optional[ConcurrencyManager] = None
_rate_limiter: Optional[RateLimiter] = None
_connection_pool: Optional[ConnectionPool] = None


def get_concurrency_manager() -> ConcurrencyManager:
    """获取全局并发管理器"""
    global _concurrency_manager
    if _concurrency_manager is None:
        import os
        max_concurrent = int(os.getenv("MAX_CONCURRENT_REQUESTS", "100"))
        max_per_group = int(os.getenv("MAX_CONCURRENT_PER_MODEL_GROUP", "20"))
        queue_timeout = float(os.getenv("QUEUE_TIMEOUT_SECONDS", "30"))
        
        _concurrency_manager = ConcurrencyManager(
            max_concurrent=max_concurrent,
            max_per_model_group=max_per_group,
            queue_timeout=queue_timeout
        )
    return _concurrency_manager


def get_rate_limiter() -> RateLimiter:
    """获取全局速率限制器"""
    global _rate_limiter
    if _rate_limiter is None:
        import os
        rate = float(os.getenv("RATE_LIMIT_PER_SECOND", "100"))
        burst = int(os.getenv("RATE_LIMIT_BURST", "200"))
        _rate_limiter = RateLimiter(rate=rate, burst=burst)
    return _rate_limiter


def get_connection_pool() -> ConnectionPool:
    """获取全局连接池"""
    global _connection_pool
    if _connection_pool is None:
        import os
        max_conn = int(os.getenv("HTTP_MAX_CONNECTIONS", "100"))
        max_per_host = int(os.getenv("HTTP_MAX_CONNECTIONS_PER_HOST", "20"))
        _connection_pool = ConnectionPool(
            max_connections=max_conn,
            max_connections_per_host=max_per_host
        )
    return _connection_pool


def init_concurrency_services(
    max_concurrent: int = 100,
    max_per_model_group: int = 20,
    rate_limit: float = 100.0,
    rate_burst: int = 200
):
    """初始化所有并发服务"""
    global _concurrency_manager, _rate_limiter, _connection_pool
    
    _concurrency_manager = ConcurrencyManager(
        max_concurrent=max_concurrent,
        max_per_model_group=max_per_model_group
    )
    _rate_limiter = RateLimiter(rate=rate_limit, burst=rate_burst)
    _connection_pool = ConnectionPool()
    
    return _concurrency_manager, _rate_limiter, _connection_pool


async def cleanup_concurrency_services():
    """清理并发服务资源"""
    global _connection_pool
    if _connection_pool:
        await _connection_pool.close()
