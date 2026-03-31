"""
Statistics Service
账号和 API Key 使用统计、历史数据查询

Requirements: 3.2, 3.3, 3.6
"""

import logging
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field

from sqlalchemy import select, and_, func, text, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import (
    BackendAccount, AccountModelRoute, APIKey, TokenUsageHistory, RequestLog, ModelGroup, SystemStats
)
from app.services.time_utils import utc_now_naive, utc_today

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
        today = utc_today()
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
        if not accounts:
            return []

        today = utc_today()
        account_request_stats = await self._get_account_request_stats_map(
            session,
            [account.id for account in accounts],
            today,
            today,
        )
        account_model_map = await self._get_account_models_map(session, accounts)

        stats_list = []
        for account in accounts:
            usage_percentage = self._calculate_usage_percentage(
                account.daily_used, account.daily_quota
            )
            request_stats = account_request_stats.get(account.id, {})
            model_groups = account_model_map.get(
                account.id,
                [account.model_group] if account.model_group else [],
            )
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

    async def _get_account_request_stats_map(
        self,
        session: AsyncSession,
        account_ids: List[str],
        start_date: date,
        end_date: date,
    ) -> Dict[str, Dict[str, int]]:
        if not account_ids:
            return {}

        start_datetime = datetime.combine(start_date, datetime.min.time())
        end_datetime = datetime.combine(end_date, datetime.max.time())
        result = await session.execute(
            select(
                RequestLog.account_id,
                func.count(RequestLog.id),
                func.coalesce(func.sum(RequestLog.input_tokens), 0),
                func.coalesce(func.sum(RequestLog.output_tokens), 0),
            )
            .where(
                and_(
                    RequestLog.account_id.in_(account_ids),
                    RequestLog.timestamp >= start_datetime,
                    RequestLog.timestamp <= end_datetime,
                )
            )
            .group_by(RequestLog.account_id)
        )
        return {
            str(account_id): {
                "request_count": int(request_count or 0),
                "input_tokens": int(input_tokens or 0),
                "output_tokens": int(output_tokens or 0),
            }
            for account_id, request_count, input_tokens, output_tokens in result.fetchall()
            if account_id
        }

    async def _get_account_models_map(
        self,
        session: AsyncSession,
        accounts: List[STAccount],
    ) -> Dict[str, List[str]]:
        account_ids = [account.id for account in accounts if getattr(account, "id", None)]
        if not account_ids:
            return {}

        route_result = await session.execute(
            select(AccountModelRoute.account_id, AccountModelRoute.model_name)
            .where(
                and_(
                    AccountModelRoute.account_id.in_(account_ids),
                    AccountModelRoute.enabled == True,
                )
            )
            .order_by(
                AccountModelRoute.account_id.asc(),
                AccountModelRoute.priority.asc(),
                AccountModelRoute.model_name.asc(),
            )
        )
        model_map: Dict[str, List[str]] = {}
        for account_id, model_name in route_result.fetchall():
            if not account_id or not model_name:
                continue
            bucket = model_map.setdefault(str(account_id), [])
            if model_name not in bucket:
                bucket.append(str(model_name))

        for account in accounts:
            if model_map.get(account.id):
                continue
            if account.model_group:
                model_map[account.id] = [account.model_group]

        return model_map
    
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
            select(
                func.count(RequestLog.id),
                func.coalesce(func.sum(RequestLog.input_tokens), 0),
                func.coalesce(func.sum(RequestLog.output_tokens), 0),
            ).where(
                and_(
                    RequestLog.account_id == account_id,
                    RequestLog.timestamp >= start_datetime,
                    RequestLog.timestamp <= end_datetime
                )
            )
        )
        request_count, input_tokens, output_tokens = result.one()

        return {
            "request_count": int(request_count or 0),
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
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
        today = utc_today()
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
        group_account_map = await self._get_model_group_account_map(session)
        if not group_account_map:
            return []

        today = utc_today()
        request_count_map = await self._get_model_group_request_count_map(
            session,
            list(group_account_map.keys()),
            today,
            today,
        )

        stats_list = []
        for group in sorted(group_account_map):
            accounts = list(group_account_map[group].values())
            total_quota = sum(a.daily_quota for a in accounts)
            total_used = sum(a.daily_used for a in accounts)
            active_count = sum(1 for a in accounts if a.status == "active")
            exhausted_count = sum(1 for a in accounts if a.status == "exhausted")
            disabled_count = sum(1 for a in accounts if a.status == "disabled")
            stats_list.append(
                ModelGroupStats(
                    name=group,
                    total_accounts=len(accounts),
                    active_accounts=active_count,
                    exhausted_accounts=exhausted_count,
                    disabled_accounts=disabled_count,
                    total_quota=total_quota,
                    total_used=total_used,
                    usage_percentage=self._calculate_usage_percentage(total_used, total_quota),
                    request_count=int(request_count_map.get(group, 0)),
                )
            )

        return stats_list

    async def _get_model_group_account_map(
        self,
        session: AsyncSession,
    ) -> Dict[str, Dict[str, STAccount]]:
        group_account_map: Dict[str, Dict[str, STAccount]] = {}

        route_result = await session.execute(
            select(AccountModelRoute.model_name, STAccount)
            .join(STAccount, STAccount.id == AccountModelRoute.account_id)
            .where(
                and_(
                    AccountModelRoute.enabled == True,
                    AccountModelRoute.model_name.isnot(None),
                )
            )
        )
        for model_name, account in route_result.fetchall():
            if not model_name or account is None:
                continue
            group_account_map.setdefault(str(model_name), {})[account.id] = account

        legacy_result = await session.execute(
            select(STAccount).where(STAccount.model_group.isnot(None))
        )
        for account in legacy_result.scalars().all():
            if not account.model_group:
                continue
            group_account_map.setdefault(account.model_group, {}).setdefault(account.id, account)

        return group_account_map

    async def _get_model_group_request_count_map(
        self,
        session: AsyncSession,
        model_groups: List[str],
        start_date: date,
        end_date: date,
    ) -> Dict[str, int]:
        if not model_groups:
            return {}

        start_datetime = datetime.combine(start_date, datetime.min.time())
        end_datetime = datetime.combine(end_date, datetime.max.time())
        result = await session.execute(
            select(RequestLog.model, func.count(RequestLog.id))
            .where(
                and_(
                    RequestLog.model.in_(model_groups),
                    RequestLog.timestamp >= start_datetime,
                    RequestLog.timestamp <= end_datetime,
                )
            )
            .group_by(RequestLog.model)
        )
        return {
            str(model): int(request_count or 0)
            for model, request_count in result.fetchall()
            if model
        }
    
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
            select(func.count(RequestLog.id)).where(
                and_(
                    RequestLog.model == model_group,
                    RequestLog.timestamp >= start_datetime,
                    RequestLog.timestamp <= end_datetime
                )
            )
        )
        return int(result.scalar() or 0)
    
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
        today = utc_today()
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
        if not api_keys:
            return []

        today = utc_today()
        request_stats_map = await self._get_api_key_request_stats_map(
            session,
            [api_key.key_prefix for api_key in api_keys if api_key.key_prefix],
            today,
            today,
        )
        stats_list = []

        for api_key in api_keys:
            usage_percentage = None
            if api_key.quota is not None and api_key.quota > 0:
                usage_percentage = self._calculate_usage_percentage(
                    api_key.used, api_key.quota
                )

            request_stats = request_stats_map.get(api_key.key_prefix, {})

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

    async def _get_api_key_request_stats_map(
        self,
        session: AsyncSession,
        api_key_prefixes: List[str],
        start_date: date,
        end_date: date,
    ) -> Dict[str, Dict[str, int]]:
        if not api_key_prefixes:
            return {}

        start_datetime = datetime.combine(start_date, datetime.min.time())
        end_datetime = datetime.combine(end_date, datetime.max.time())
        result = await session.execute(
            select(
                RequestLog.api_key_prefix,
                func.count(RequestLog.id),
                func.coalesce(func.sum(RequestLog.input_tokens), 0),
                func.coalesce(func.sum(RequestLog.output_tokens), 0),
            )
            .where(
                and_(
                    RequestLog.api_key_prefix.in_(api_key_prefixes),
                    RequestLog.timestamp >= start_datetime,
                    RequestLog.timestamp <= end_datetime,
                )
            )
            .group_by(RequestLog.api_key_prefix)
        )
        return {
            str(api_key_prefix): {
                "request_count": int(request_count or 0),
                "input_tokens": int(input_tokens or 0),
                "output_tokens": int(output_tokens or 0),
            }
            for api_key_prefix, request_count, input_tokens, output_tokens in result.fetchall()
            if api_key_prefix
        }
    
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
            select(
                func.count(RequestLog.id),
                func.coalesce(func.sum(RequestLog.input_tokens), 0),
                func.coalesce(func.sum(RequestLog.output_tokens), 0),
            ).where(
                and_(
                    RequestLog.api_key_prefix == api_key_prefix,
                    RequestLog.timestamp >= start_datetime,
                    RequestLog.timestamp <= end_datetime
                )
            )
        )
        request_count, input_tokens, output_tokens = result.one()

        return {
            "request_count": int(request_count or 0),
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
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
        start_date = utc_today() - timedelta(days=days - 1)
        
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
        today = utc_today()
        
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
        # 账号统计（聚合）
        account_row = (
            await session.execute(
                select(
                    func.count(STAccount.id).label("total_accounts"),
                    func.coalesce(
                        func.sum(case((STAccount.status == "active", 1), else_=0)),
                        0,
                    ).label("active_accounts"),
                    func.coalesce(
                        func.sum(case((STAccount.status == "exhausted", 1), else_=0)),
                        0,
                    ).label("exhausted_accounts"),
                    func.coalesce(
                        func.sum(case((STAccount.status == "disabled", 1), else_=0)),
                        0,
                    ).label("disabled_accounts"),
                )
            )
        ).one()
        total_accounts = int(account_row.total_accounts or 0)
        active_accounts = int(account_row.active_accounts or 0)
        exhausted_accounts = int(account_row.exhausted_accounts or 0)
        disabled_accounts = int(account_row.disabled_accounts or 0)

        # API Key 统计（聚合）
        api_key_row = (
            await session.execute(
                select(
                    func.count(APIKey.id).label("total_api_keys"),
                    func.coalesce(
                        func.sum(case((APIKey.status == "active", 1), else_=0)),
                        0,
                    ).label("active_api_keys"),
                )
            )
        ).one()
        total_api_keys = int(api_key_row.total_api_keys or 0)
        active_api_keys = int(api_key_row.active_api_keys or 0)

        # 模型组统计
        total_model_groups = int(
            (await session.execute(select(func.count(ModelGroup.id)))).scalar() or 0
        )
        
        # 今日请求统计（使用 UTC 时间）
        today_utc = utc_today()
        today_start = datetime.combine(today_utc, datetime.min.time())
        today_end = datetime.combine(today_utc, datetime.max.time())
        
        today_row = (
            await session.execute(
                select(
                    func.count(RequestLog.id),
                    func.coalesce(func.sum(RequestLog.input_tokens), 0),
                    func.coalesce(func.sum(RequestLog.output_tokens), 0),
                ).where(
                    and_(
                        RequestLog.timestamp >= today_start,
                        RequestLog.timestamp <= today_end
                    )
                )
            )
        ).one()
        today_requests = int(today_row[0] or 0)
        today_input_tokens = int(today_row[1] or 0)
        today_output_tokens = int(today_row[2] or 0)
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
        output_tokens: int,
        *,
        request_count: int = 1,
    ) -> None:
        """
        更新系统累计统计（每次请求后调用）
        
        Args:
            session: 数据库会话
            input_tokens: 输入 Token 数量
            output_tokens: 输出 Token 数量
        """
        safe_input_tokens = max(0, int(input_tokens))
        safe_output_tokens = max(0, int(output_tokens))
        safe_request_count = max(1, int(request_count))
        total_tokens = safe_input_tokens + safe_output_tokens
        now = utc_now_naive()

        # 并发安全：单语句 UPSERT，避免高并发下读改写导致丢增量。
        await session.execute(
            text(
                """
                INSERT INTO system_stats (
                    id,
                    total_requests,
                    total_tokens,
                    total_input_tokens,
                    total_output_tokens,
                    updated_at
                )
                VALUES (
                    'global',
                    :request_count,
                    :total_tokens,
                    :input_tokens,
                    :output_tokens,
                    :updated_at
                )
                ON CONFLICT (id) DO UPDATE SET
                    total_requests = system_stats.total_requests + EXCLUDED.total_requests,
                    total_tokens = system_stats.total_tokens + EXCLUDED.total_tokens,
                    total_input_tokens = system_stats.total_input_tokens + EXCLUDED.total_input_tokens,
                    total_output_tokens = system_stats.total_output_tokens + EXCLUDED.total_output_tokens,
                    updated_at = EXCLUDED.updated_at
                """
            ),
            {
                "total_tokens": total_tokens,
                "input_tokens": safe_input_tokens,
                "output_tokens": safe_output_tokens,
                "request_count": safe_request_count,
                "updated_at": now,
            },
        )
        await session.flush()

        logger.debug(
            "Updated system stats atomically: +1 request, +%s tokens",
            total_tokens,
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
