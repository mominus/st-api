"""
Statistics Service
账号和 API Key 使用统计、历史数据查询

Requirements: 3.2, 3.3, 3.6
"""

import logging
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field

from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import (
    BackendAccount, AccountModelRoute, APIKey, TokenUsageHistory, RequestLog, ModelGroup, SystemStats
)

# 兼容旧名称
STAccount = BackendAccount

logger = logging.getLogger(__name__)


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class AccountStats:
    """账号统计信息"""
    id: str
    name: str
    model_group: str
    daily_quota: int
    daily_used: int
    usage_percentage: float
    status: str
    model_groups: List[str] = field(default_factory=list)
    request_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0


@dataclass
class APIKeyStats:
    """API Key 统计信息"""
    id: str
    key_prefix: str
    name: Optional[str]
    model_groups: List[str]
    quota: Optional[int]
    used: int
    usage_percentage: Optional[float]
    status: str
    request_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0


@dataclass
class ModelGroupStats:
    """模型组统计信息"""
    name: str
    total_accounts: int
    active_accounts: int
    exhausted_accounts: int
    disabled_accounts: int
    total_quota: int
    total_used: int
    usage_percentage: float
    request_count: int = 0


@dataclass
class DailyUsageStats:
    """每日使用统计"""
    date: date
    input_tokens: int
    output_tokens: int
    total_tokens: int
    request_count: int


@dataclass
class SystemOverview:
    """系统概览统计"""
    total_accounts: int
    active_accounts: int
    exhausted_accounts: int
    disabled_accounts: int
    total_api_keys: int
    active_api_keys: int
    total_model_groups: int
    # 今日统计
    today_requests: int
    today_tokens: int
    today_input_tokens: int
    today_output_tokens: int
    # 历史累计统计
    all_time_requests: int = 0
    all_time_tokens: int = 0
    all_time_input_tokens: int = 0
    all_time_output_tokens: int = 0


# ============================================================================
# Statistics Service
# ============================================================================

class StatsService:
    """
    统计服务
    
    提供账号和 API Key 的使用统计、历史数据查询功能。
    
    Requirements:
    - 3.2: 显示每个账号的当日已用 Token、剩余 Token 和使用百分比
    - 3.3: 显示每个账号池的总体使用情况
    - 3.6: 提供最近 30 天的 Token 使用统计
    """
    
    def __init__(self):
        """初始化统计服务"""
        pass
    
    # ========================================================================
    # Account Statistics (Requirement 3.2)
    # ========================================================================
    
    async def get_account_stats(
        self,
        session: AsyncSession,
        account_id: str
    ) -> Optional[AccountStats]:
        """
        获取单个账号的统计信息
        
        Args:
            session: 数据库会话
            account_id: 账号 ID
            
        Returns:
            账号统计信息，如果不存在则返回 None
            
        Requirements: 3.2
        """
        # 获取账号信息
        result = await session.execute(
            select(STAccount).where(STAccount.id == account_id)
        )
        account = result.scalar_one_or_none()
        
        if account is None:
            return None
        
        # 计算使用百分比
        usage_percentage = self._calculate_usage_percentage(
            account.daily_used, account.daily_quota
        )
        
        # 获取今日请求统计
        today = date.today()
        request_stats = await self._get_account_request_stats(
            session, account_id, today, today
        )
        model_groups = await self._get_account_models(session, account.id)
        primary_model_group = model_groups[0] if model_groups else account.model_group
        
        return AccountStats(
            id=account.id,
            name=account.name,
            model_group=primary_model_group,
            model_groups=model_groups,
            daily_quota=account.daily_quota,
            daily_used=account.daily_used,
            usage_percentage=usage_percentage,
            status=account.status,
            request_count=request_stats.get("request_count", 0),
            total_input_tokens=request_stats.get("input_tokens", 0),
            total_output_tokens=request_stats.get("output_tokens", 0)
        )
    
    async def get_all_account_stats(
        self,
        session: AsyncSession
    ) -> List[AccountStats]:
        """
        获取所有账号的统计信息
        
        Args:
            session: 数据库会话
            
        Returns:
            账号统计信息列表
            
        Requirements: 3.2
        """
        result = await session.execute(select(STAccount))
        accounts = list(result.scalars().all())
        
        stats_list = []
        today = date.today()
        
        for account in accounts:
            usage_percentage = self._calculate_usage_percentage(
                account.daily_used, account.daily_quota
            )
            
            request_stats = await self._get_account_request_stats(
                session, account.id, today, today
            )
            model_groups = await self._get_account_models(session, account.id)
            primary_model_group = model_groups[0] if model_groups else account.model_group
            
            stats_list.append(AccountStats(
                id=account.id,
                name=account.name,
                model_group=primary_model_group,
                model_groups=model_groups,
                daily_quota=account.daily_quota,
                daily_used=account.daily_used,
                usage_percentage=usage_percentage,
                status=account.status,
                request_count=request_stats.get("request_count", 0),
                total_input_tokens=request_stats.get("input_tokens", 0),
                total_output_tokens=request_stats.get("output_tokens", 0)
            ))
        
        return stats_list
    
    async def _get_account_request_stats(
        self,
        session: AsyncSession,
        account_id: str,
        start_date: date,
        end_date: date
    ) -> Dict[str, int]:
        """
        获取账号在指定日期范围内的请求统计
        
        Args:
            session: 数据库会话
            account_id: 账号 ID
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            统计信息字典
        """
        start_datetime = datetime.combine(start_date, datetime.min.time())
        end_datetime = datetime.combine(end_date, datetime.max.time())
        
        result = await session.execute(
            select(RequestLog).where(
                and_(
                    RequestLog.account_id == account_id,
                    RequestLog.timestamp >= start_datetime,
                    RequestLog.timestamp <= end_datetime
                )
            )
        )
        logs = list(result.scalars().all())
        
        return {
            "request_count": len(logs),
            "input_tokens": sum(log.input_tokens or 0 for log in logs),
            "output_tokens": sum(log.output_tokens or 0 for log in logs)
        }
    
    # ========================================================================
    # Model Group Statistics (Requirement 3.3)
    # ========================================================================
    
    async def get_model_group_stats(
        self,
        session: AsyncSession,
        model_group: str
    ) -> Optional[ModelGroupStats]:
        """
        获取模型组的统计信息
        
        Args:
            session: 数据库会话
            model_group: 模型组名称
            
        Returns:
            模型组统计信息，如果没有账号则返回 None
            
        Requirements: 3.3
        """
        result = await session.execute(
            select(STAccount)
            .join(
                AccountModelRoute,
                AccountModelRoute.account_id == STAccount.id
            )
            .where(
                and_(
                    AccountModelRoute.model_name == model_group,
                    AccountModelRoute.enabled == True
                )
            )
        )
        accounts = list(result.scalars().unique().all())
        account_map = {a.id: a for a in accounts}

        # 兼容旧字段兜底（补齐未迁移记录）
        legacy_result = await session.execute(
            select(STAccount).where(
                STAccount.model_group == model_group
            )
        )
        for account in legacy_result.scalars().all():
            account_map.setdefault(account.id, account)

        accounts = list(account_map.values())
        
        if not accounts:
            return None
        
        total_quota = sum(a.daily_quota for a in accounts)
        total_used = sum(a.daily_used for a in accounts)
        active_count = sum(1 for a in accounts if a.status == "active")
        exhausted_count = sum(1 for a in accounts if a.status == "exhausted")
        disabled_count = sum(1 for a in accounts if a.status == "disabled")
        
        usage_percentage = self._calculate_usage_percentage(total_used, total_quota)
        
        # 获取今日请求数
        today = date.today()
        request_count = await self._get_model_group_request_count(
            session, model_group, today, today
        )
        
        return ModelGroupStats(
            name=model_group,
            total_accounts=len(accounts),
            active_accounts=active_count,
            exhausted_accounts=exhausted_count,
            disabled_accounts=disabled_count,
            total_quota=total_quota,
            total_used=total_used,
            usage_percentage=usage_percentage,
            request_count=request_count
        )
    
    async def get_all_model_group_stats(
        self,
        session: AsyncSession
    ) -> List[ModelGroupStats]:
        """
        获取所有模型组的统计信息
        
        Args:
            session: 数据库会话
            
        Returns:
            模型组统计信息列表
            
        Requirements: 3.3
        """
        # 获取所有唯一模型（优先新路由表，兼容旧字段）
        route_result = await session.execute(
            select(AccountModelRoute.model_name).where(
                AccountModelRoute.enabled == True
            ).distinct()
        )
        legacy_result = await session.execute(
            select(STAccount.model_group).distinct()
        )

        model_groups = {
            row[0] for row in route_result.fetchall() if row[0]
        }
        model_groups.update(
            {row[0] for row in legacy_result.fetchall() if row[0]}
        )

        stats_list = []
        for group in sorted(model_groups):
            stats = await self.get_model_group_stats(session, group)
            if stats:
                stats_list.append(stats)
        
        return stats_list
    
    async def _get_model_group_request_count(
        self,
        session: AsyncSession,
        model_group: str,
        start_date: date,
        end_date: date
    ) -> int:
        """
        获取模型组在指定日期范围内的请求数
        
        Args:
            session: 数据库会话
            model_group: 模型组名称
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            请求数量
        """
        start_datetime = datetime.combine(start_date, datetime.min.time())
        end_datetime = datetime.combine(end_date, datetime.max.time())
        
        result = await session.execute(
            select(RequestLog).where(
                and_(
                    RequestLog.model == model_group,
                    RequestLog.timestamp >= start_datetime,
                    RequestLog.timestamp <= end_datetime
                )
            )
        )
        logs = list(result.scalars().all())
        return len(logs)
    
    # ========================================================================
    # API Key Statistics
    # ========================================================================
    
    async def get_api_key_stats(
        self,
        session: AsyncSession,
        key_id: str
    ) -> Optional[APIKeyStats]:
        """
        获取单个 API Key 的统计信息
        
        Args:
            session: 数据库会话
            key_id: API Key ID
            
        Returns:
            API Key 统计信息，如果不存在则返回 None
        """
        import json
        
        result = await session.execute(
            select(APIKey).where(APIKey.id == key_id)
        )
        api_key = result.scalar_one_or_none()
        
        if api_key is None:
            return None
        
        # 计算使用百分比
        usage_percentage = None
        if api_key.quota is not None and api_key.quota > 0:
            usage_percentage = self._calculate_usage_percentage(
                api_key.used, api_key.quota
            )
        
        # 获取今日请求统计
        today = date.today()
        request_stats = await self._get_api_key_request_stats(
            session, api_key.key_prefix, today, today
        )
        
        return APIKeyStats(
            id=api_key.id,
            key_prefix=api_key.key_prefix,
            name=api_key.name,
            model_groups=json.loads(api_key.model_groups),
            quota=api_key.quota,
            used=api_key.used,
            usage_percentage=usage_percentage,
            status=api_key.status,
            request_count=request_stats.get("request_count", 0),
            total_input_tokens=request_stats.get("input_tokens", 0),
            total_output_tokens=request_stats.get("output_tokens", 0)
        )
    
    async def get_all_api_key_stats(
        self,
        session: AsyncSession,
        include_revoked: bool = False
    ) -> List[APIKeyStats]:
        """
        获取所有 API Key 的统计信息
        
        Args:
            session: 数据库会话
            include_revoked: 是否包含已撤销的 Key
            
        Returns:
            API Key 统计信息列表
        """
        import json
        
        if include_revoked:
            result = await session.execute(select(APIKey))
        else:
            result = await session.execute(
                select(APIKey).where(APIKey.status == "active")
            )
        api_keys = list(result.scalars().all())
        
        stats_list = []
        today = date.today()
        
        for api_key in api_keys:
            usage_percentage = None
            if api_key.quota is not None and api_key.quota > 0:
                usage_percentage = self._calculate_usage_percentage(
                    api_key.used, api_key.quota
                )
            
            request_stats = await self._get_api_key_request_stats(
                session, api_key.key_prefix, today, today
            )
            
            stats_list.append(APIKeyStats(
                id=api_key.id,
                key_prefix=api_key.key_prefix,
                name=api_key.name,
                model_groups=json.loads(api_key.model_groups),
                quota=api_key.quota,
                used=api_key.used,
                usage_percentage=usage_percentage,
                status=api_key.status,
                request_count=request_stats.get("request_count", 0),
                total_input_tokens=request_stats.get("input_tokens", 0),
                total_output_tokens=request_stats.get("output_tokens", 0)
            ))
        
        return stats_list
    
    async def _get_api_key_request_stats(
        self,
        session: AsyncSession,
        api_key_prefix: str,
        start_date: date,
        end_date: date
    ) -> Dict[str, int]:
        """
        获取 API Key 在指定日期范围内的请求统计
        
        Args:
            session: 数据库会话
            api_key_prefix: API Key 前缀
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            统计信息字典
        """
        start_datetime = datetime.combine(start_date, datetime.min.time())
        end_datetime = datetime.combine(end_date, datetime.max.time())
        
        result = await session.execute(
            select(RequestLog).where(
                and_(
                    RequestLog.api_key_prefix == api_key_prefix,
                    RequestLog.timestamp >= start_datetime,
                    RequestLog.timestamp <= end_datetime
                )
            )
        )
        logs = list(result.scalars().all())
        
        return {
            "request_count": len(logs),
            "input_tokens": sum(log.input_tokens or 0 for log in logs),
            "output_tokens": sum(log.output_tokens or 0 for log in logs)
        }
    
    # ========================================================================
    # Historical Data (Requirement 3.6)
    # ========================================================================
    
    async def get_usage_history(
        self,
        session: AsyncSession,
        days: int = 30,
        account_id: Optional[str] = None,
        api_key_id: Optional[str] = None
    ) -> List[DailyUsageStats]:
        """
        获取历史使用统计
        
        Args:
            session: 数据库会话
            days: 查询天数（默认 30 天）
            account_id: 账号 ID（可选，用于过滤）
            api_key_id: API Key ID（可选，用于过滤）
            
        Returns:
            每日使用统计列表
            
        Requirements: 3.6
        """
        start_date = date.today() - timedelta(days=days - 1)
        
        # 构建查询条件
        conditions = [TokenUsageHistory.date >= start_date]
        
        if account_id:
            conditions.append(TokenUsageHistory.account_id == account_id)
        
        if api_key_id:
            conditions.append(TokenUsageHistory.api_key_id == api_key_id)
        
        result = await session.execute(
            select(TokenUsageHistory).where(and_(*conditions))
        )
        history_records = list(result.scalars().all())
        
        # 按日期聚合
        daily_stats: Dict[date, DailyUsageStats] = {}
        
        for record in history_records:
            if record.date not in daily_stats:
                daily_stats[record.date] = DailyUsageStats(
                    date=record.date,
                    input_tokens=0,
                    output_tokens=0,
                    total_tokens=0,
                    request_count=0
                )
            
            stats = daily_stats[record.date]
            stats.input_tokens += record.input_tokens
            stats.output_tokens += record.output_tokens
            stats.total_tokens = stats.input_tokens + stats.output_tokens
            stats.request_count += record.request_count
        
        # 填充缺失的日期
        result_list = []
        current_date = start_date
        today = date.today()
        
        while current_date <= today:
            if current_date in daily_stats:
                result_list.append(daily_stats[current_date])
            else:
                result_list.append(DailyUsageStats(
                    date=current_date,
                    input_tokens=0,
                    output_tokens=0,
                    total_tokens=0,
                    request_count=0
                ))
            current_date += timedelta(days=1)
        
        return result_list
    
    async def get_account_usage_history(
        self,
        session: AsyncSession,
        account_id: str,
        days: int = 30
    ) -> List[DailyUsageStats]:
        """
        获取账号的历史使用统计
        
        Args:
            session: 数据库会话
            account_id: 账号 ID
            days: 查询天数
            
        Returns:
            每日使用统计列表
            
        Requirements: 3.6
        """
        return await self.get_usage_history(
            session, days=days, account_id=account_id
        )
    
    async def get_api_key_usage_history(
        self,
        session: AsyncSession,
        api_key_id: str,
        days: int = 30
    ) -> List[DailyUsageStats]:
        """
        获取 API Key 的历史使用统计
        
        Args:
            session: 数据库会话
            api_key_id: API Key ID
            days: 查询天数
            
        Returns:
            每日使用统计列表
            
        Requirements: 3.6
        """
        return await self.get_usage_history(
            session, days=days, api_key_id=api_key_id
        )
    
    # ========================================================================
    # System Overview
    # ========================================================================
    
    async def get_system_overview(
        self,
        session: AsyncSession
    ) -> SystemOverview:
        """
        获取系统概览统计
        
        Args:
            session: 数据库会话
            
        Returns:
            系统概览统计
        """
        # 账号统计
        accounts_result = await session.execute(select(STAccount))
        accounts = list(accounts_result.scalars().all())
        
        total_accounts = len(accounts)
        active_accounts = sum(1 for a in accounts if a.status == "active")
        exhausted_accounts = sum(1 for a in accounts if a.status == "exhausted")
        disabled_accounts = sum(1 for a in accounts if a.status == "disabled")
        
        # API Key 统计
        keys_result = await session.execute(select(APIKey))
        api_keys = list(keys_result.scalars().all())
        
        total_api_keys = len(api_keys)
        active_api_keys = sum(1 for k in api_keys if k.status == "active")
        
        # 模型组统计
        groups_result = await session.execute(select(ModelGroup))
        total_model_groups = len(list(groups_result.scalars().all()))
        
        # 今日请求统计（使用 UTC 时间）
        now_utc = datetime.utcnow()
        today_utc = now_utc.date()
        today_start = datetime.combine(today_utc, datetime.min.time())
        today_end = datetime.combine(today_utc, datetime.max.time())
        
        logs_result = await session.execute(
            select(RequestLog).where(
                and_(
                    RequestLog.timestamp >= today_start,
                    RequestLog.timestamp <= today_end
                )
            )
        )
        today_logs = list(logs_result.scalars().all())
        
        today_requests = len(today_logs)
        today_input_tokens = sum(log.input_tokens or 0 for log in today_logs)
        today_output_tokens = sum(log.output_tokens or 0 for log in today_logs)
        today_tokens = today_input_tokens + today_output_tokens
        
        # 获取历史累计统计
        system_stats_result = await session.execute(
            select(SystemStats).where(SystemStats.id == "global")
        )
        system_stats = system_stats_result.scalar_one_or_none()
        
        all_time_requests = system_stats.total_requests if system_stats else 0
        all_time_tokens = system_stats.total_tokens if system_stats else 0
        all_time_input_tokens = system_stats.total_input_tokens if system_stats else 0
        all_time_output_tokens = system_stats.total_output_tokens if system_stats else 0
        
        return SystemOverview(
            total_accounts=total_accounts,
            active_accounts=active_accounts,
            exhausted_accounts=exhausted_accounts,
            disabled_accounts=disabled_accounts,
            total_api_keys=total_api_keys,
            active_api_keys=active_api_keys,
            total_model_groups=total_model_groups,
            today_requests=today_requests,
            today_tokens=today_tokens,
            today_input_tokens=today_input_tokens,
            today_output_tokens=today_output_tokens,
            all_time_requests=all_time_requests,
            all_time_tokens=all_time_tokens,
            all_time_input_tokens=all_time_input_tokens,
            all_time_output_tokens=all_time_output_tokens
        )
    
    # ========================================================================
    # Utility Methods
    # ========================================================================
    
    def _calculate_usage_percentage(self, used: int, quota: int) -> float:
        """
        计算使用百分比
        
        Args:
            used: 已使用量
            quota: 总配额
            
        Returns:
            使用百分比 (0-100+)
        """
        if quota <= 0:
            return 100.0 if used > 0 else 0.0
        return (used / quota) * 100

    async def _get_account_models(
        self,
        session: AsyncSession,
        account_id: str
    ) -> List[str]:
        """获取账号可路由模型列表（优先新路由表，回退旧字段）。"""
        route_result = await session.execute(
            select(AccountModelRoute).where(
                and_(
                    AccountModelRoute.account_id == account_id,
                    AccountModelRoute.enabled == True
                )
            ).order_by(AccountModelRoute.priority.asc(), AccountModelRoute.model_name.asc())
        )
        routes = list(route_result.scalars().all())
        models = [r.model_name for r in routes if r.model_name]
        if models:
            return models

        account_result = await session.execute(
            select(STAccount).where(STAccount.id == account_id)
        )
        account = account_result.scalar_one_or_none()
        if account and account.model_group:
            return [account.model_group]
        return []
    
    def get_usage_status(self, percentage: float) -> str:
        """
        根据使用百分比获取状态
        
        Args:
            percentage: 使用百分比
            
        Returns:
            状态字符串: "normal", "warning", "exhausted"
        """
        if percentage >= 100:
            return "exhausted"
        elif percentage >= 80:
            return "warning"
        else:
            return "normal"
    
    # ========================================================================
    # System Stats Update Methods
    # ========================================================================
    
    async def update_system_stats(
        self,
        session: AsyncSession,
        input_tokens: int,
        output_tokens: int
    ) -> None:
        """
        更新系统累计统计（每次请求后调用）
        
        Args:
            session: 数据库会话
            input_tokens: 输入 Token 数量
            output_tokens: 输出 Token 数量
        """
        total_tokens = input_tokens + output_tokens
        
        # 获取或创建系统统计记录
        result = await session.execute(
            select(SystemStats).where(SystemStats.id == "global")
        )
        system_stats = result.scalar_one_or_none()
        
        if system_stats is None:
            # 创建新记录
            system_stats = SystemStats(
                id="global",
                total_requests=1,
                total_tokens=total_tokens,
                total_input_tokens=input_tokens,
                total_output_tokens=output_tokens,
                updated_at=datetime.utcnow()
            )
            session.add(system_stats)
        else:
            # 更新现有记录
            system_stats.total_requests = (system_stats.total_requests or 0) + 1
            system_stats.total_tokens = (system_stats.total_tokens or 0) + total_tokens
            system_stats.total_input_tokens = (system_stats.total_input_tokens or 0) + input_tokens
            system_stats.total_output_tokens = (system_stats.total_output_tokens or 0) + output_tokens
            system_stats.updated_at = datetime.utcnow()
        
        await session.flush()
        
        logger.debug(
            f"Updated system stats: +1 request, +{total_tokens} tokens "
            f"(total: {system_stats.total_requests} requests, {system_stats.total_tokens} tokens)"
        )
    
    async def get_system_stats(
        self,
        session: AsyncSession
    ) -> Optional[SystemStats]:
        """
        获取系统累计统计
        
        Args:
            session: 数据库会话
            
        Returns:
            SystemStats 对象，如果不存在则返回 None
        """
        result = await session.execute(
            select(SystemStats).where(SystemStats.id == "global")
        )
        return result.scalar_one_or_none()


# ============================================================================
# Global Instance
# ============================================================================

_stats_service: Optional[StatsService] = None


def get_stats_service() -> StatsService:
    """获取全局统计服务实例"""
    global _stats_service
    if _stats_service is None:
        _stats_service = StatsService()
    return _stats_service


def init_stats_service() -> StatsService:
    """初始化全局统计服务"""
    global _stats_service
    _stats_service = StatsService()
    return _stats_service
