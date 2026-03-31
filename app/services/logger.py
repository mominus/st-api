"""
Logger Service
请求日志记录、错误日志记录和日志查询服务

Requirements: 8.2, 8.3, 8.4
"""

import uuid
import traceback
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass, field

from sqlalchemy import select, and_, desc, func, case, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import RequestLog, get_session_factory
from app.services.time_utils import to_utc_naive, utc_now_naive

# 配置标准 Python 日志
logger = logging.getLogger(__name__)


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class RequestLogEntry:
    """请求日志条目"""
    id: str
    timestamp: datetime
    api_key_prefix: Optional[str] = None
    client_ip: Optional[str] = None
    model: Optional[str] = None
    account_id: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    response_time_ms: Optional[int] = None
    status: Optional[str] = None  # success, error
    error_message: Optional[str] = None


@dataclass
class LogQueryParams:
    """日志查询参数"""
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    api_key_prefix: Optional[str] = None
    status: Optional[str] = None
    model: Optional[str] = None
    client_ip: Optional[str] = None
    limit: int = 100
    offset: int = 0


@dataclass
class LogQueryResult:
    """日志查询结果"""
    logs: List[RequestLogEntry]
    total: int
    limit: int
    offset: int


# ============================================================================
# Logger Service
# ============================================================================

class LoggerService:
    """
    日志服务
    
    提供请求日志记录、错误日志记录和日志查询功能。
    
    Requirements:
    - 8.2: 请求处理过程中发生异常时记录详细错误日志
    - 8.3: 请求完成时记录请求日志
    - 8.4: 提供日志查询和过滤功能
    """
    
    def __init__(self):
        """初始化日志服务"""
        pass
    
    # ========================================================================
    # Request Logging (Requirement 8.3)
    # ========================================================================
    
    def generate_request_id(self) -> str:
        """
        生成唯一的请求 ID
        
        Returns:
            UUID 格式的请求 ID
        """
        return str(uuid.uuid4())
    
    async def log_request(
        self,
        session: AsyncSession,
        request_id: str,
        api_key_prefix: Optional[str] = None,
        client_ip: Optional[str] = None,
        model: Optional[str] = None,
        account_id: Optional[str] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        response_time_ms: Optional[int] = None,
        status: str = "success",
        error_message: Optional[str] = None,
        timestamp: Optional[datetime] = None
    ) -> RequestLog:
        """
        记录请求日志
        
        记录请求 ID、时间、API Key（脱敏）、模型、Token 使用和响应时间。
        
        Args:
            session: 数据库会话
            request_id: 请求 ID
            api_key_prefix: API Key 前缀（脱敏后）
            client_ip: 客户端 IP
            model: 请求的模型
            account_id: 使用的账号 ID
            input_tokens: 输入 Token 数量
            output_tokens: 输出 Token 数量
            response_time_ms: 响应时间（毫秒）
            status: 状态（success/error）
            error_message: 错误消息（如果有）
            timestamp: 时间戳（默认为当前时间）
            
        Returns:
            创建的日志记录
            
        Requirements: 8.3
        """
        log_entry = RequestLog(
            id=request_id,
            timestamp=to_utc_naive(timestamp) if timestamp is not None else utc_now_naive(),
            api_key_prefix=api_key_prefix,
            client_ip=client_ip,
            model=model,
            account_id=account_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            response_time_ms=response_time_ms,
            status=status,
            error_message=error_message
        )

        session.add(log_entry)

        # 同时记录到标准日志
        log_msg = (
            f"Request {request_id}: "
            f"model={model}, "
            f"api_key={api_key_prefix}, "
            f"tokens={input_tokens}+{output_tokens}, "
            f"time={response_time_ms}ms, "
            f"status={status}"
        )
        
        if status == "success":
            logger.info(log_msg)
        else:
            logger.warning(f"{log_msg}, error={error_message}")
        
        return log_entry
    
    async def log_success(
        self,
        session: AsyncSession,
        request_id: str,
        api_key_prefix: Optional[str] = None,
        client_ip: Optional[str] = None,
        model: Optional[str] = None,
        account_id: Optional[str] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        response_time_ms: Optional[int] = None
    ) -> RequestLog:
        """
        记录成功的请求日志
        
        Args:
            session: 数据库会话
            request_id: 请求 ID
            api_key_prefix: API Key 前缀
            client_ip: 客户端 IP
            model: 请求的模型
            account_id: 使用的账号 ID
            input_tokens: 输入 Token 数量
            output_tokens: 输出 Token 数量
            response_time_ms: 响应时间（毫秒）
            
        Returns:
            创建的日志记录
            
        Requirements: 8.3
        """
        return await self.log_request(
            session=session,
            request_id=request_id,
            api_key_prefix=api_key_prefix,
            client_ip=client_ip,
            model=model,
            account_id=account_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            response_time_ms=response_time_ms,
            status="success"
        )

    # ========================================================================
    # Error Logging (Requirement 8.2)
    # ========================================================================
    
    async def log_error(
        self,
        session: AsyncSession,
        request_id: str,
        error: Exception,
        api_key_prefix: Optional[str] = None,
        client_ip: Optional[str] = None,
        model: Optional[str] = None,
        account_id: Optional[str] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        response_time_ms: Optional[int] = None
    ) -> RequestLog:
        """
        记录错误日志
        
        记录详细错误日志包含请求 ID、时间戳、错误类型和堆栈信息。
        
        Args:
            session: 数据库会话
            request_id: 请求 ID
            error: 异常对象
            api_key_prefix: API Key 前缀
            client_ip: 客户端 IP
            model: 请求的模型
            account_id: 使用的账号 ID
            input_tokens: 输入 Token 数量
            output_tokens: 输出 Token 数量
            response_time_ms: 响应时间（毫秒）
            
        Returns:
            创建的日志记录
            
        Requirements: 8.2
        """
        # 构建错误消息，包含错误类型和堆栈信息
        error_type = type(error).__name__
        error_msg = str(error)
        stack_trace = traceback.format_exc()
        
        # 组合完整的错误信息
        full_error_message = f"{error_type}: {error_msg}\n{stack_trace}"
        
        # 记录到标准日志
        logger.error(
            f"Request {request_id} failed: {error_type}: {error_msg}",
            exc_info=True
        )
        
        return await self.log_request(
            session=session,
            request_id=request_id,
            api_key_prefix=api_key_prefix,
            client_ip=client_ip,
            model=model,
            account_id=account_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            response_time_ms=response_time_ms,
            status="error",
            error_message=full_error_message
        )
    
    async def log_error_simple(
        self,
        session: AsyncSession,
        request_id: str,
        error_message: str,
        api_key_prefix: Optional[str] = None,
        client_ip: Optional[str] = None,
        model: Optional[str] = None,
        account_id: Optional[str] = None,
        response_time_ms: Optional[int] = None
    ) -> RequestLog:
        """
        记录简单错误日志（不包含堆栈信息）
        
        Args:
            session: 数据库会话
            request_id: 请求 ID
            error_message: 错误消息
            api_key_prefix: API Key 前缀
            client_ip: 客户端 IP
            model: 请求的模型
            account_id: 使用的账号 ID
            response_time_ms: 响应时间（毫秒）
            
        Returns:
            创建的日志记录
            
        Requirements: 8.2
        """
        logger.warning(f"Request {request_id} failed: {error_message}")
        
        return await self.log_request(
            session=session,
            request_id=request_id,
            api_key_prefix=api_key_prefix,
            client_ip=client_ip,
            model=model,
            account_id=account_id,
            response_time_ms=response_time_ms,
            status="error",
            error_message=error_message
        )
    
    # ========================================================================
    # Log Query (Requirement 8.4)
    # ========================================================================
    
    async def query_logs(
        self,
        session: AsyncSession,
        params: LogQueryParams
    ) -> LogQueryResult:
        """
        查询日志
        
        支持按时间、API Key、状态过滤。
        
        Args:
            session: 数据库会话
            params: 查询参数
            
        Returns:
            查询结果
            
        Requirements: 8.4
        """
        # 构建查询条件
        conditions = []
        
        if params.start_time:
            conditions.append(RequestLog.timestamp >= params.start_time)
        
        if params.end_time:
            conditions.append(RequestLog.timestamp <= params.end_time)
        
        if params.api_key_prefix:
            conditions.append(RequestLog.api_key_prefix == params.api_key_prefix)
        
        if params.status:
            conditions.append(RequestLog.status == params.status)
        
        if params.model:
            conditions.append(RequestLog.model == params.model)
        
        if params.client_ip:
            conditions.append(RequestLog.client_ip == params.client_ip)
        
        # 构建查询
        query = select(RequestLog)
        if conditions:
            query = query.where(and_(*conditions))
        
        # 按时间倒序排列
        query = query.order_by(desc(RequestLog.timestamp))
        
        # 获取总数（聚合计数，避免拉全量行）
        count_query = select(func.count(RequestLog.id))
        if conditions:
            count_query = count_query.where(and_(*conditions))
        count_result = await session.execute(count_query)
        total = int(count_result.scalar() or 0)
        
        # 应用分页
        query = query.offset(params.offset).limit(params.limit)
        
        # 执行查询
        result = await session.execute(query)
        logs = list(result.scalars().all())
        
        # 转换为数据类
        log_entries = [
            RequestLogEntry(
                id=log.id,
                timestamp=log.timestamp,
                api_key_prefix=log.api_key_prefix,
                client_ip=log.client_ip,
                model=log.model,
                account_id=log.account_id,
                input_tokens=log.input_tokens,
                output_tokens=log.output_tokens,
                response_time_ms=log.response_time_ms,
                status=log.status,
                error_message=log.error_message
            )
            for log in logs
        ]
        
        return LogQueryResult(
            logs=log_entries,
            total=total,
            limit=params.limit,
            offset=params.offset
        )
    
    async def get_log_by_id(
        self,
        session: AsyncSession,
        request_id: str
    ) -> Optional[RequestLogEntry]:
        """
        根据请求 ID 获取日志
        
        Args:
            session: 数据库会话
            request_id: 请求 ID
            
        Returns:
            日志条目，如果不存在则返回 None
        """
        result = await session.execute(
            select(RequestLog).where(RequestLog.id == request_id)
        )
        log = result.scalar_one_or_none()
        
        if log is None:
            return None
        
        return RequestLogEntry(
            id=log.id,
            timestamp=log.timestamp,
            api_key_prefix=log.api_key_prefix,
            client_ip=log.client_ip,
            model=log.model,
            account_id=log.account_id,
            input_tokens=log.input_tokens,
            output_tokens=log.output_tokens,
            response_time_ms=log.response_time_ms,
            status=log.status,
            error_message=log.error_message
        )
    
    async def get_recent_logs(
        self,
        session: AsyncSession,
        limit: int = 50
    ) -> List[RequestLogEntry]:
        """
        获取最近的日志
        
        Args:
            session: 数据库会话
            limit: 返回数量限制
            
        Returns:
            日志条目列表
        """
        result = await self.query_logs(
            session,
            LogQueryParams(limit=limit)
        )
        return result.logs
    
    async def get_error_logs(
        self,
        session: AsyncSession,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100
    ) -> List[RequestLogEntry]:
        """
        获取错误日志
        
        Args:
            session: 数据库会话
            start_time: 开始时间
            end_time: 结束时间
            limit: 返回数量限制
            
        Returns:
            错误日志条目列表
        """
        result = await self.query_logs(
            session,
            LogQueryParams(
                start_time=start_time,
                end_time=end_time,
                status="error",
                limit=limit
            )
        )
        return result.logs
    
    async def get_logs_by_api_key(
        self,
        session: AsyncSession,
        api_key_prefix: str,
        limit: int = 100
    ) -> List[RequestLogEntry]:
        """
        根据 API Key 前缀获取日志
        
        Args:
            session: 数据库会话
            api_key_prefix: API Key 前缀
            limit: 返回数量限制
            
        Returns:
            日志条目列表
        """
        result = await self.query_logs(
            session,
            LogQueryParams(
                api_key_prefix=api_key_prefix,
                limit=limit
            )
        )
        return result.logs
    
    async def get_logs_by_time_range(
        self,
        session: AsyncSession,
        start_time: datetime,
        end_time: datetime,
        limit: int = 1000
    ) -> List[RequestLogEntry]:
        """
        根据时间范围获取日志
        
        Args:
            session: 数据库会话
            start_time: 开始时间
            end_time: 结束时间
            limit: 返回数量限制
            
        Returns:
            日志条目列表
        """
        result = await self.query_logs(
            session,
            LogQueryParams(
                start_time=start_time,
                end_time=end_time,
                limit=limit
            )
        )
        return result.logs
    
    # ========================================================================
    # Statistics
    # ========================================================================
    
    async def get_log_statistics(
        self,
        session: AsyncSession,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        获取日志统计信息
        
        Args:
            session: 数据库会话
            start_time: 开始时间
            end_time: 结束时间
            
        Returns:
            统计信息字典
        """
        # 构建查询条件
        conditions = []
        if start_time:
            conditions.append(RequestLog.timestamp >= start_time)
        if end_time:
            conditions.append(RequestLog.timestamp <= end_time)
        
        where_clause = and_(*conditions) if conditions else None

        # 聚合主统计，避免把全量日志载入内存
        aggregate_query = select(
            func.count(RequestLog.id).label("total_requests"),
            func.coalesce(
                func.sum(case((RequestLog.status == "success", 1), else_=0)),
                0,
            ).label("success_count"),
            func.coalesce(
                func.sum(case((RequestLog.status == "error", 1), else_=0)),
                0,
            ).label("error_count"),
            func.coalesce(func.sum(RequestLog.input_tokens), 0).label("total_input_tokens"),
            func.coalesce(func.sum(RequestLog.output_tokens), 0).label("total_output_tokens"),
            func.coalesce(func.avg(RequestLog.response_time_ms), 0.0).label("avg_response_time_ms"),
        )
        if where_clause is not None:
            aggregate_query = aggregate_query.where(where_clause)
        aggregate_row = (await session.execute(aggregate_query)).one()

        total_requests = int(aggregate_row.total_requests or 0)
        success_count = int(aggregate_row.success_count or 0)
        error_count = int(aggregate_row.error_count or 0)
        total_input_tokens = int(aggregate_row.total_input_tokens or 0)
        total_output_tokens = int(aggregate_row.total_output_tokens or 0)
        avg_response_time = float(aggregate_row.avg_response_time_ms or 0.0)

        # 按模型统计（分组聚合）
        model_query = select(
            RequestLog.model,
            func.count(RequestLog.id),
        ).where(RequestLog.model.isnot(None))
        if where_clause is not None:
            model_query = model_query.where(where_clause)
        model_query = model_query.group_by(RequestLog.model)
        model_result = await session.execute(model_query)
        model_stats: Dict[str, int] = {
            str(model): int(count or 0)
            for model, count in model_result.fetchall()
            if model
        }
        
        return {
            "total_requests": total_requests,
            "success_count": success_count,
            "error_count": error_count,
            "success_rate": (success_count / total_requests * 100) if total_requests > 0 else 0,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_tokens": total_input_tokens + total_output_tokens,
            "avg_response_time_ms": avg_response_time,
            "model_stats": model_stats
        }
    
    async def get_error_rate(
        self,
        session: AsyncSession,
        time_window_minutes: int = 60
    ) -> float:
        """
        获取指定时间窗口内的错误率
        
        Args:
            session: 数据库会话
            time_window_minutes: 时间窗口（分钟）
            
        Returns:
            错误率（0-100）
        """
        start_time = utc_now_naive() - timedelta(minutes=time_window_minutes)
        
        stats = await self.get_log_statistics(session, start_time=start_time)
        
        total = stats["total_requests"]
        errors = stats["error_count"]
        
        if total == 0:
            return 0.0
        
        return (errors / total) * 100
    
    # ========================================================================
    # Cleanup
    # ========================================================================
    
    async def cleanup_old_logs(
        self,
        session: AsyncSession,
        days_to_keep: int = 30
    ) -> int:
        """
        清理旧日志

        Args:
            session: 数据库会话
            days_to_keep: 保留天数

        Returns:
            删除的日志数量
        """
        cutoff_date = utc_now_naive() - timedelta(days=days_to_keep)

        count_query = select(func.count(RequestLog.id)).where(RequestLog.timestamp < cutoff_date)
        count = int((await session.execute(count_query)).scalar() or 0)
        if count <= 0:
            return 0

        await session.execute(
            delete(RequestLog).where(RequestLog.timestamp < cutoff_date)
        )
        await session.flush()

        logger.info(f"Cleaned up {count} old logs (older than {days_to_keep} days)")
        return count


# ============================================================================
# Global Instance
# ============================================================================

_logger_service: Optional[LoggerService] = None


def get_logger_service() -> LoggerService:
    """获取全局日志服务实例"""
    global _logger_service
    if _logger_service is None:
        _logger_service = LoggerService()
    return _logger_service


def init_logger_service() -> LoggerService:
    """初始化全局日志服务"""
    global _logger_service
    _logger_service = LoggerService()
    return _logger_service
