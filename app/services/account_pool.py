"""
Account Pool Management Service
多账号池化管理，支持 CRUD、轮询负载均衡、Token 使用追踪
"""

import os
import uuid
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict, Any, Tuple
from collections import defaultdict
import logging

from sqlalchemy import select, update, delete, and_, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import (
    BackendAccount, AccountModelRoute, TokenUsageHistory
)
from app.services.crypto import get_crypto_service
from app.services.time_utils import to_utc_naive, utc_now_naive, utc_today

logger = logging.getLogger(__name__)

# 兼容旧名称
STAccount = BackendAccount
ACCOUNT_MAX_INFLIGHT_REQUESTS_PER_ACCOUNT = max(
    0,
    int(os.getenv("ACCOUNT_MAX_INFLIGHT_REQUESTS_PER_ACCOUNT", "8")),
)
ACCOUNT_INFLIGHT_LEASE_SECONDS = max(
    30.0,
    float(os.getenv("ACCOUNT_INFLIGHT_LEASE_SECONDS", "900")),
)


class AccountPoolService:
    """
    账号池管理服务
    
    提供账号 CRUD、轮询负载均衡、Token 使用追踪等功能
    """
    
    def __init__(self):
        """初始化账号池服务"""
        # 轮询索引，按模型组分别维护
        self._round_robin_index: Dict[str, int] = defaultdict(int)

    @staticmethod
    def _chunked(values: List[str], chunk_size: int = 500) -> List[List[str]]:
        """按固定大小切片，避免 SQLite 变量数量限制。"""
        if chunk_size <= 0:
            return [values]
        return [values[i:i + chunk_size] for i in range(0, len(values), chunk_size)]

    @staticmethod
    def _reservation_stale_before(now: Optional[datetime] = None) -> datetime:
        anchor = now or utc_now_naive()
        return anchor - timedelta(seconds=ACCOUNT_INFLIGHT_LEASE_SECONDS)

    @classmethod
    def _effective_inflight_expr(cls, stale_before: datetime):
        return case(
            (
                and_(
                    STAccount.inflight_requests.isnot(None),
                    STAccount.inflight_requests > 0,
                    STAccount.inflight_updated_at.isnot(None),
                    STAccount.inflight_updated_at >= stale_before,
                ),
                STAccount.inflight_requests,
            ),
            else_=0,
        )

    @classmethod
    def _decrement_inflight_expr(cls, stale_before: datetime):
        effective = cls._effective_inflight_expr(stale_before)
        return case(
            (effective > 0, effective - 1),
            else_=0,
        )

    @staticmethod
    def _reservation_enabled() -> bool:
        return ACCOUNT_MAX_INFLIGHT_REQUESTS_PER_ACCOUNT > 0

    @classmethod
    def _effective_inflight_requests(
        cls,
        account: STAccount,
        *,
        now: Optional[datetime] = None,
    ) -> int:
        if not cls._reservation_enabled():
            return 0
        inflight = max(0, int(getattr(account, "inflight_requests", 0) or 0))
        if inflight <= 0:
            return 0
        updated_at = getattr(account, "inflight_updated_at", None)
        if updated_at is None:
            return 0
        normalized_updated_at = to_utc_naive(updated_at)
        if normalized_updated_at < cls._reservation_stale_before(now):
            return 0
        return inflight

    @classmethod
    def _has_inflight_capacity(
        cls,
        account: STAccount,
        *,
        now: Optional[datetime] = None,
    ) -> bool:
        if not cls._reservation_enabled():
            return True
        return cls._effective_inflight_requests(account, now=now) < ACCOUNT_MAX_INFLIGHT_REQUESTS_PER_ACCOUNT
    
    # ==================== CRUD Operations ====================
    # Requirements: 2.1, 2.2
    
    async def create_account(
        self,
        session: AsyncSession,
        name: str,
        org_id: str,
        flow_id: str,
        api_key: str,
        model_group: str,
        model_groups: Optional[List[str]] = None,
        daily_quota: int = 1000000,
        private_api_key: Optional[str] = None
    ) -> BackendAccount:
        """
        创建新的后端账号
        
        Args:
            session: 数据库会话
            name: 账号名称
            org_id: 组织 ID
            flow_id: 工作流 ID
            api_key: API Key（明文，将被加密存储）
            model_group: 主模型组（兼容旧字段）
            model_groups: 可路由模型列表（可选）
            daily_quota: 每日 Token 配额
            private_api_key: Private API Key（用于监控，可选）
            
        Returns:
            创建的账号对象
        """
        crypto = get_crypto_service()
        encrypted_key = crypto.encrypt(api_key)
        encrypted_private_key = crypto.encrypt(private_api_key) if private_api_key else None
        
        normalized_groups = self._normalize_model_names(model_groups or [])
        primary_group = str(model_group or "").strip()
        if primary_group:
            normalized_groups = [primary_group] + [m for m in normalized_groups if m != primary_group]

        if not normalized_groups:
            raise ValueError("model_group or model_groups is required")

        account = BackendAccount(
            id=str(uuid.uuid4()),
            name=name,
            org_id=org_id,
            flow_id=flow_id,
            api_key_encrypted=encrypted_key,
            private_api_key_encrypted=encrypted_private_key,
            model_group=normalized_groups[0],
            daily_quota=daily_quota,
            daily_used=0,
            status="active",
            created_at=utc_now_naive(),
            updated_at=utc_now_naive()
        )

        session.add(account)
        await session.flush()

        # 同步账号-模型路由（支持单账号多模型）
        await self.set_account_models(session, account.id, normalized_groups)

        logger.info(
            f"Created account: {account.id} ({name}) with models={normalized_groups}"
        )
        return account

    async def create_accounts_bulk(
        self,
        session: AsyncSession,
        accounts_data: List[Dict[str, Any]]
    ) -> List[BackendAccount]:
        """
        批量创建账号（单事务、单次 flush）。

        Args:
            session: 数据库会话
            accounts_data: 账号数据列表

        Returns:
            创建的账号列表
        """
        if not accounts_data:
            return []

        crypto = get_crypto_service()
        now = utc_now_naive()
        accounts: List[BackendAccount] = []
        routes: List[AccountModelRoute] = []

        for item in accounts_data:
            name = str(item.get("name") or "").strip()
            org_id = str(item.get("org_id") or "").strip()
            flow_id = str(item.get("flow_id") or "").strip()
            api_key = str(item.get("api_key") or "").strip()
            private_api_key_raw = item.get("private_api_key")
            private_api_key = (
                str(private_api_key_raw).strip()
                if private_api_key_raw is not None
                else None
            )
            daily_quota = int(item.get("daily_quota") or 1000000)

            normalized_groups = self._normalize_model_names(
                item.get("model_groups") or []
            )
            primary_group = str(item.get("model_group") or "").strip()
            if primary_group:
                normalized_groups = [primary_group] + [
                    m for m in normalized_groups if m != primary_group
                ]

            if not name:
                raise ValueError("name is required")
            if not org_id:
                raise ValueError("org_id is required")
            if not flow_id:
                raise ValueError("flow_id is required")
            if not api_key:
                raise ValueError("api_key is required")
            if not normalized_groups:
                raise ValueError("model_group or model_groups is required")

            account_id = str(uuid.uuid4())
            encrypted_key = crypto.encrypt(api_key)
            encrypted_private_key = (
                crypto.encrypt(private_api_key) if private_api_key else None
            )

            account = BackendAccount(
                id=account_id,
                name=name,
                org_id=org_id,
                flow_id=flow_id,
                api_key_encrypted=encrypted_key,
                private_api_key_encrypted=encrypted_private_key,
                model_group=normalized_groups[0],
                daily_quota=daily_quota,
                daily_used=0,
                status="active",
                created_at=now,
                updated_at=now,
            )
            accounts.append(account)

            for idx, model_name in enumerate(normalized_groups):
                routes.append(
                    AccountModelRoute(
                        id=str(uuid.uuid4()),
                        account_id=account_id,
                        model_name=model_name,
                        enabled=True,
                        weight=100,
                        priority=idx,
                        created_at=now,
                        updated_at=now,
                    )
                )

        session.add_all(accounts)
        session.add_all(routes)
        await session.flush()

        logger.info(f"Bulk created {len(accounts)} accounts")
        return accounts

    async def get_account(
        self,
        session: AsyncSession,
        account_id: str
    ) -> Optional[STAccount]:
        """
        获取单个账号
        
        Args:
            session: 数据库会话
            account_id: 账号 ID
            
        Returns:
            账号对象，如果不存在则返回 None
        """
        result = await session.execute(
            select(STAccount).where(STAccount.id == account_id)
        )
        return result.scalar_one_or_none()
    
    async def get_all_accounts(
        self,
        session: AsyncSession
    ) -> List[STAccount]:
        """
        获取所有账号
        
        Args:
            session: 数据库会话
            
        Returns:
            账号列表
        """
        result = await session.execute(select(STAccount))
        return list(result.scalars().all())

    async def get_account_model_routes(
        self,
        session: AsyncSession,
        account_id: str,
        enabled_only: bool = True
    ) -> List[AccountModelRoute]:
        """获取账号的模型路由配置。"""
        conditions = [AccountModelRoute.account_id == account_id]
        if enabled_only:
            conditions.append(AccountModelRoute.enabled == True)

        result = await session.execute(
            select(AccountModelRoute).where(and_(*conditions)).order_by(
                AccountModelRoute.priority.asc(),
                AccountModelRoute.model_name.asc()
            )
        )
        return list(result.scalars().all())

    async def get_account_models(
        self,
        session: AsyncSession,
        account_id: str,
        enabled_only: bool = True
    ) -> List[str]:
        """获取账号支持的模型列表（优先新路由表，回退旧字段）。"""
        routes = await self.get_account_model_routes(
            session, account_id, enabled_only=enabled_only
        )
        models = [r.model_name for r in routes if r.model_name]
        if models:
            return models

        account = await self.get_account(session, account_id)
        if account and account.model_group:
            return [account.model_group]
        return []

    async def get_account_models_map(
        self,
        session: AsyncSession,
        accounts: List[STAccount],
        enabled_only: bool = True,
    ) -> Dict[str, List[str]]:
        """
        批量获取账号支持的模型列表，避免逐账号查询路由表。
        """
        account_ids = [
            str(account.id)
            for account in accounts
            if getattr(account, "id", None)
        ]
        if not account_ids:
            return {}

        model_map: Dict[str, List[str]] = {}
        for chunk in self._chunked(account_ids):
            route_result = await session.execute(
                select(AccountModelRoute.account_id, AccountModelRoute.model_name)
                .where(
                    and_(
                        AccountModelRoute.account_id.in_(chunk),
                        AccountModelRoute.enabled == True if enabled_only else True,
                    )
                )
                .order_by(
                    AccountModelRoute.account_id.asc(),
                    AccountModelRoute.priority.asc(),
                    AccountModelRoute.model_name.asc(),
                )
            )
            for account_id, model_name in route_result.fetchall():
                if not account_id or not model_name:
                    continue
                bucket = model_map.setdefault(str(account_id), [])
                normalized_model = str(model_name)
                if normalized_model not in bucket:
                    bucket.append(normalized_model)

        for account in accounts:
            account_id = getattr(account, "id", None)
            if not account_id:
                continue
            if model_map.get(account_id):
                continue
            if account.model_group:
                model_map[str(account_id)] = [account.model_group]

        return model_map

    async def set_account_models(
        self,
        session: AsyncSession,
        account_id: str,
        model_groups: List[str]
    ) -> None:
        """覆盖账号可路由模型列表。"""
        normalized = self._normalize_model_names(model_groups)
        if not normalized:
            raise ValueError("model_groups cannot be empty")

        account = await self.get_account(session, account_id)
        if account is None:
            raise ValueError("Account not found")

        await session.execute(
            delete(AccountModelRoute).where(AccountModelRoute.account_id == account_id)
        )

        for idx, model_name in enumerate(normalized):
            session.add(AccountModelRoute(
                id=str(uuid.uuid4()),
                account_id=account_id,
                model_name=model_name,
                enabled=True,
                weight=100,
                priority=idx,
                created_at=utc_now_naive(),
                updated_at=utc_now_naive(),
            ))

        # 兼容旧字段：保留主模型
        account.model_group = normalized[0]
        account.updated_at = utc_now_naive()
        await session.flush()
    
    async def get_accounts_by_model_group(
        self,
        session: AsyncSession,
        model_group: str
    ) -> List[STAccount]:
        """
        按模型组获取账号列表
        
        Args:
            session: 数据库会话
            model_group: 模型组名称
            
        Returns:
            该模型组的账号列表
        """
        accounts_by_model = await self.get_accounts_by_model_groups(session, [model_group])
        return accounts_by_model.get(str(model_group or "").strip(), [])

    async def get_accounts_by_model_groups(
        self,
        session: AsyncSession,
        model_groups: List[str],
    ) -> Dict[str, List[STAccount]]:
        """
        批量按模型组获取账号列表，避免逐模型 N+1 查询。
        """
        normalized = self._normalize_model_names(model_groups)
        if not normalized:
            return {}

        accounts_by_model: Dict[str, Dict[str, STAccount]] = {
            model_name: {}
            for model_name in normalized
        }

        route_rows: List[Tuple[str, str]] = []
        for chunk in self._chunked(normalized):
            route_result = await session.execute(
                select(AccountModelRoute.account_id, AccountModelRoute.model_name).where(
                    and_(
                        AccountModelRoute.model_name.in_(chunk),
                        AccountModelRoute.enabled == True,
                    )
                )
            )
            route_rows.extend(
                (row[0], row[1])
                for row in route_result.fetchall()
                if row[0] and row[1]
            )

        route_account_ids = sorted({account_id for account_id, _model_name in route_rows})
        route_accounts: Dict[str, STAccount] = {}
        for chunk in self._chunked(route_account_ids):
            result = await session.execute(
                select(STAccount).where(STAccount.id.in_(chunk))
            )
            for account in result.scalars().all():
                route_accounts[account.id] = account

        for account_id, model_name in route_rows:
            account = route_accounts.get(account_id)
            if account is not None and model_name in accounts_by_model:
                accounts_by_model[model_name][account.id] = account

        for chunk in self._chunked(normalized):
            legacy_result = await session.execute(
                select(STAccount).where(STAccount.model_group.in_(chunk))
            )
            for account in legacy_result.scalars().all():
                model_name = str(account.model_group or "").strip()
                if model_name in accounts_by_model:
                    accounts_by_model[model_name].setdefault(account.id, account)

        return {
            model_name: sorted(model_accounts.values(), key=lambda account: account.id)
            for model_name, model_accounts in accounts_by_model.items()
        }
    
    async def update_account(
        self,
        session: AsyncSession,
        account_id: str,
        **kwargs
    ) -> Optional[STAccount]:
        """
        更新账号信息
        
        Args:
            session: 数据库会话
            account_id: 账号 ID
            **kwargs: 要更新的字段
            
        Returns:
            更新后的账号对象，如果不存在则返回 None
        """
        account = await self.get_account(session, account_id)
        if account is None:
            return None

        marker = object()
        model_groups_input = kwargs.pop("model_groups", marker)
        model_group_input = kwargs.pop("model_group", marker)
        resolved_model_groups: Optional[List[str]] = None

        if model_groups_input is not marker:
            resolved_model_groups = self._normalize_model_names(model_groups_input or [])

        if model_group_input is not marker:
            primary = str(model_group_input or "").strip()
            if primary:
                if resolved_model_groups is None:
                    resolved_model_groups = [primary]
                else:
                    resolved_model_groups = [primary] + [m for m in resolved_model_groups if m != primary]

        if resolved_model_groups is not None and not resolved_model_groups:
            raise ValueError("model_groups cannot be empty")
        
        # 如果更新 API Key，需要加密
        if "api_key" in kwargs:
            crypto = get_crypto_service()
            kwargs["api_key_encrypted"] = crypto.encrypt(kwargs.pop("api_key"))
        
        # 如果更新 Private API Key，需要加密
        if "private_api_key" in kwargs:
            crypto = get_crypto_service()
            private_key = kwargs.pop("private_api_key")
            kwargs["private_api_key_encrypted"] = crypto.encrypt(private_key) if private_key else None
        
        # 更新字段
        for key, value in kwargs.items():
            if hasattr(account, key):
                setattr(account, key, value)

        if resolved_model_groups is not None:
            await self.set_account_models(session, account_id, resolved_model_groups)
        else:
            account.updated_at = utc_now_naive()
            await session.flush()

        logger.info(f"Updated account: {account_id}")
        return account
    
    async def delete_account(
        self,
        session: AsyncSession,
        account_id: str
    ) -> bool:
        """
        删除账号
        
        Args:
            session: 数据库会话
            account_id: 账号 ID
            
        Returns:
            是否成功删除
        """
        account = await self.get_account(session, account_id)
        if account is None:
            return False

        await session.execute(
            delete(AccountModelRoute).where(AccountModelRoute.account_id == account_id)
        )

        await session.delete(account)
        await session.flush()
        
        logger.info(f"Deleted account: {account_id}")
        return True

    async def bulk_update_account_status(
        self,
        session: AsyncSession,
        account_ids: List[str],
        status: str
    ) -> Dict[str, Any]:
        """
        批量更新账号状态。

        Args:
            session: 数据库会话
            account_ids: 账号 ID 列表
            status: 目标状态（active / disabled）

        Returns:
            操作结果统计
        """
        unique_ids = [str(x).strip() for x in dict.fromkeys(account_ids or []) if str(x).strip()]
        if not unique_ids:
            return {"updated_count": 0, "not_found_ids": []}

        accounts: List[STAccount] = []
        found_ids = set()
        for chunk in self._chunked(unique_ids):
            result = await session.execute(
                select(STAccount).where(STAccount.id.in_(chunk))
            )
            chunk_accounts = list(result.scalars().all())
            accounts.extend(chunk_accounts)
            found_ids.update(acc.id for acc in chunk_accounts)
        now = utc_now_naive()

        for account in accounts:
            account.status = status
            account.updated_at = now

        await session.flush()

        not_found_ids = [acc_id for acc_id in unique_ids if acc_id not in found_ids]
        logger.info(
            f"Bulk updated account status: updated={len(accounts)}, "
            f"status={status}, not_found={len(not_found_ids)}"
        )
        return {"updated_count": len(accounts), "not_found_ids": not_found_ids}

    async def bulk_delete_accounts(
        self,
        session: AsyncSession,
        account_ids: List[str]
    ) -> Dict[str, Any]:
        """
        批量删除账号。

        Args:
            session: 数据库会话
            account_ids: 账号 ID 列表

        Returns:
            操作结果统计
        """
        unique_ids = [str(x).strip() for x in dict.fromkeys(account_ids or []) if str(x).strip()]
        if not unique_ids:
            return {"deleted_count": 0, "not_found_ids": []}

        found_ids: List[str] = []
        for chunk in self._chunked(unique_ids):
            result = await session.execute(
                select(STAccount.id).where(STAccount.id.in_(chunk))
            )
            found_ids.extend(row[0] for row in result.fetchall())
        found_id_set = set(found_ids)

        if found_ids:
            for chunk in self._chunked(found_ids):
                await session.execute(
                    delete(AccountModelRoute).where(AccountModelRoute.account_id.in_(chunk))
                )
            for chunk in self._chunked(found_ids):
                await session.execute(
                    delete(STAccount).where(STAccount.id.in_(chunk))
                )
            await session.flush()

        not_found_ids = [acc_id for acc_id in unique_ids if acc_id not in found_id_set]
        logger.info(
            f"Bulk deleted accounts: deleted={len(found_ids)}, not_found={len(not_found_ids)}"
        )
        return {"deleted_count": len(found_ids), "not_found_ids": not_found_ids}

    # ==================== Round-Robin Load Balancing ====================
    # Requirements: 2.3, 2.4, 2.5

    async def _list_model_group_accounts(
        self,
        session: AsyncSession,
        model_group: str
    ) -> List[STAccount]:
        """收集模型组关联账号（新路由优先 + 旧字段兜底，去重）。"""
        routed_result = await session.execute(
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
            .order_by(
                AccountModelRoute.priority.asc(),
                STAccount.id.asc()
            )
        )
        routed_accounts = list(routed_result.scalars().unique().all())

        legacy_result = await session.execute(
            select(STAccount).where(
                STAccount.model_group == model_group
            ).order_by(STAccount.id)
        )

        accounts: List[STAccount] = []
        existing_ids = set()
        for account in routed_accounts:
            if account.id in existing_ids:
                continue
            existing_ids.add(account.id)
            accounts.append(account)
        for legacy_account in legacy_result.scalars().all():
            if legacy_account.id in existing_ids:
                continue
            existing_ids.add(legacy_account.id)
            accounts.append(legacy_account)
        return accounts

    async def get_model_group_availability(
        self,
        session: AsyncSession,
        model_group: str
    ) -> Dict[str, Any]:
        """
        返回模型组账号可用性快照，用于精确诊断无可用账号原因。
        """
        accounts = await self._list_model_group_accounts(session, model_group)
        status_counts: Dict[str, int] = defaultdict(int)
        active_accounts = 0
        eligible_accounts = 0
        exhausted_accounts = 0
        concurrency_limited_accounts = 0
        available_accounts = 0
        now = utc_now_naive()

        for account in accounts:
            status = str(account.status or "unknown")
            status_counts[status] += 1

            if status == "active":
                active_accounts += 1
                if account.daily_used < account.daily_quota:
                    eligible_accounts += 1
                    if self._has_inflight_capacity(account, now=now):
                        available_accounts += 1
                    else:
                        concurrency_limited_accounts += 1
            elif status == "exhausted":
                exhausted_accounts += 1

        return {
            "model_group": model_group,
            "total_accounts": len(accounts),
            "active_accounts": active_accounts,
            # Backward-compatible alias; only active accounts are routable now.
            "routable_accounts": active_accounts,
            "eligible_accounts": eligible_accounts,
            "exhausted_accounts": exhausted_accounts,
            "stale_exhausted_accounts": 0,
            "available_accounts": available_accounts,
            "concurrency_limited_accounts": concurrency_limited_accounts,
            "status_counts": dict(sorted(status_counts.items())),
        }

    async def get_available_account(
        self,
        session: AsyncSession,
        model_group: str,
        exclude_account_id: Optional[str] = None,
    ) -> Optional[STAccount]:
        """
        使用轮询算法获取可用账号
        
        从指定模型组中选择一个可用（非耗尽、非禁用）的账号。
        使用 Round-Robin 算法确保请求均匀分布。
        
        Args:
            session: 数据库会话
            model_group: 模型组名称
            
        Returns:
            可用的账号对象，如果没有可用账号则返回 None
        """
        accounts = await self._list_model_group_accounts(session, model_group)

        if not accounts:
            logger.warning(
                "No routed accounts configured in model group: %s",
                model_group
            )
            return None

        active_accounts = [
            account for account in accounts
            if account.status == "active"
            and getattr(account, "id", None) != exclude_account_id
        ]

        if not active_accounts:
            status_counts: Dict[str, int] = defaultdict(int)
            for account in accounts:
                key = str(account.status or "unknown")
                status_counts[key] += 1
            logger.warning(
                "No active accounts in model group '%s' (total=%d, status_counts=%s)",
                model_group,
                len(accounts),
                dict(sorted(status_counts.items())),
            )
            return None

        active_eligible_accounts = [
            account for account in active_accounts
            if account.daily_used < account.daily_quota
            and self._has_inflight_capacity(account)
        ]

        if active_eligible_accounts:
            account = self._select_prioritized_active_account(
                model_group,
                active_eligible_accounts,
            )
            now = utc_now_naive()
            if self._reservation_enabled():
                account.inflight_requests = self._effective_inflight_requests(account, now=now) + 1
                account.inflight_updated_at = now
            account.last_used_at = now
            account.updated_at = now
            await session.flush()
            logger.debug(
                "Selected active account %s for group %s (used=%s quota=%s remaining=%s)",
                getattr(account, "id", None),
                model_group,
                getattr(account, "daily_used", None),
                getattr(account, "daily_quota", None),
                self._remaining_quota_value(account),
            )
            return account

        logger.debug(
            "All active accounts are quota-blocked or inflight-limited for group %s (active=%d)",
            model_group,
            len(active_accounts),
        )
        return None

    def _select_round_robin_account(
        self,
        model_group: str,
        eligible_accounts: List[STAccount],
    ) -> STAccount:
        """按模型组轮询选择账号。"""
        if not eligible_accounts:
            raise ValueError("eligible_accounts cannot be empty")

        current_index = self._round_robin_index[model_group]
        index = current_index % len(eligible_accounts)
        account = eligible_accounts[index]
        self._round_robin_index[model_group] = current_index + 1
        return account

    @staticmethod
    def _remaining_quota_value(account: STAccount) -> int:
        daily_quota = int(getattr(account, "daily_quota", 0) or 0)
        daily_used = int(getattr(account, "daily_used", 0) or 0)
        return max(0, daily_quota - daily_used)

    def _select_prioritized_active_account(
        self,
        model_group: str,
        eligible_accounts: List[STAccount],
    ) -> STAccount:
        """
        账号优选策略：
        1. 先选今日使用量更低的 active 账号（未使用/满额度账号优先）
        2. 再选当前 inflight 更低的账号
        3. 最后在同优先级集合内做轮询，避免单账号长期独占
        """
        if not eligible_accounts:
            raise ValueError("eligible_accounts cannot be empty")

        min_daily_used = min(int(getattr(account, "daily_used", 0) or 0) for account in eligible_accounts)
        candidates = [
            account for account in eligible_accounts
            if int(getattr(account, "daily_used", 0) or 0) == min_daily_used
        ]

        min_effective_inflight = min(self._effective_inflight_requests(account) for account in candidates)
        candidates = [
            account for account in candidates
            if self._effective_inflight_requests(account) == min_effective_inflight
        ]

        return self._select_round_robin_account(model_group, candidates)
    async def release_account_request(
        self,
        session: AsyncSession,
        account_id: str,
    ) -> bool:
        """
        释放账号飞行中请求占位；重复释放会被钳制到 0。
        """
        if not self._reservation_enabled():
            return True

        now = utc_now_naive()
        result = await session.execute(
            update(STAccount)
            .where(STAccount.id == account_id)
            .values(
                inflight_requests=self._decrement_inflight_expr(self._reservation_stale_before(now)),
                inflight_updated_at=now,
                updated_at=now,
            )
        )
        rowcount = getattr(result, "rowcount", None)
        return rowcount != 0 if rowcount is not None else True
    
    def get_round_robin_index(self, model_group: str) -> int:
        """
        获取指定模型组的当前轮询索引（用于测试）
        
        Args:
            model_group: 模型组名称
            
        Returns:
            当前轮询索引
        """
        return self._round_robin_index[model_group]
    
    def reset_round_robin_index(self, model_group: Optional[str] = None) -> None:
        """
        重置轮询索引
        
        Args:
            model_group: 模型组名称，如果为 None 则重置所有
        """
        if model_group is None:
            self._round_robin_index.clear()
        else:
            self._round_robin_index[model_group] = 0
    
    async def mark_account_exhausted(
        self,
        session: AsyncSession,
        account_id: str,
        enforce_quota_block: bool = True,
    ) -> bool:
        """
        标记账号配额耗尽
        
        Args:
            session: 数据库会话
            account_id: 账号 ID
            
        Returns:
            是否成功标记
        """
        exists = await self.get_account(session, account_id)
        if exists is None:
            return False

        values: Dict[str, Any] = {
            "status": "exhausted",
            "updated_at": utc_now_naive(),
        }
        if enforce_quota_block:
            # Prevent immediate re-selection when upstream already reports quota exceeded
            # but local counters are still lagging.
            values["daily_used"] = case(
                (
                    STAccount.daily_used < STAccount.daily_quota,
                    STAccount.daily_quota,
                ),
                else_=STAccount.daily_used,
            )

        await session.execute(
            update(STAccount)
            .where(STAccount.id == account_id)
            .values(**values)
        )

        logger.info(f"Marked account {account_id} as exhausted")
        return True

    async def defer_account_selection(
        self,
        session: AsyncSession,
        account_id: str,
        defer_seconds: float,
    ) -> bool:
        """
        将账号短暂后置（通过推进 last_used_at），用于暂时性上游故障降噪。
        """
        if defer_seconds <= 0:
            return False

        exists = await self.get_account(session, account_id)
        if exists is None:
            return False

        now = utc_now_naive()
        deferred_until = now + timedelta(seconds=max(0.0, float(defer_seconds)))
        await session.execute(
            update(STAccount)
            .where(STAccount.id == account_id)
            .values(
                last_used_at=case(
                    (STAccount.last_used_at.is_(None), deferred_until),
                    (STAccount.last_used_at < deferred_until, deferred_until),
                    else_=STAccount.last_used_at,
                ),
                updated_at=now,
            )
        )
        logger.info(
            "Deferred account selection: account_id=%s defer_seconds=%.2f",
            account_id,
            defer_seconds,
        )
        return True

    # ==================== Token Usage Tracking ====================
    # Requirements: 3.1, 3.4, 3.5
    
    async def update_token_usage(
        self,
        session: AsyncSession,
        account_id: str,
        input_tokens: int,
        output_tokens: int,
        *,
        record_history: bool = True,
        fetch_account: bool = True,
    ) -> Optional[STAccount]:
        """
        更新账号的 Token 使用量
        
        Args:
            session: 数据库会话
            account_id: 账号 ID
            input_tokens: 输入 Token 数量
            output_tokens: 输出 Token 数量
            
        Returns:
            更新后的账号对象，如果不存在则返回 None
        """
        total_tokens = max(0, int(input_tokens)) + max(0, int(output_tokens))
        now = utc_now_naive()
        values: Dict[str, Any] = {
            "daily_used": STAccount.daily_used + total_tokens,
            "last_used_at": now,
            "updated_at": now,
            "status": case(
                (STAccount.status == "disabled", "disabled"),
                (
                    (STAccount.daily_used + total_tokens) >= STAccount.daily_quota,
                    "exhausted",
                ),
                else_=STAccount.status,
            ),
        }
        if self._reservation_enabled():
            values["inflight_requests"] = self._decrement_inflight_expr(
                self._reservation_stale_before(now)
            )
            values["inflight_updated_at"] = now

        await session.execute(
            update(STAccount)
            .where(STAccount.id == account_id)
            .values(**values)
        )

        # 记录到历史表（可选，用于高并发下改为后台批量写入）
        if record_history:
            await self._record_usage_history(
                session, account_id, None, input_tokens, output_tokens
            )

        if not fetch_account:
            logger.debug(
                "Updated token usage for account %s: +%s tokens (fetch skipped)",
                account_id,
                total_tokens,
            )
            return None

        account = await self.get_account(session, account_id)
        if account is None:
            return None

        if account.daily_used >= account.daily_quota:
            logger.info(f"Account {account_id} reached quota limit")

        logger.debug(
            f"Updated token usage for account {account_id}: "
            f"+{total_tokens} (total: {account.daily_used}/{account.daily_quota})"
        )
        return account

    async def record_usage_history(
        self,
        session: AsyncSession,
        account_id: Optional[str],
        api_key_id: Optional[str],
        input_tokens: int,
        output_tokens: int,
        *,
        request_count: int = 1,
    ) -> None:
        """仅记录 token_usage_history，用于后台批量聚合写。"""
        await self._record_usage_history(
            session,
            account_id,
            api_key_id,
            input_tokens,
            output_tokens,
            request_count=request_count,
        )
    
    async def _record_usage_history(
        self,
        session: AsyncSession,
        account_id: Optional[str],
        api_key_id: Optional[str],
        input_tokens: int,
        output_tokens: int,
        *,
        request_count: int = 1,
    ) -> None:
        """
        记录 Token 使用历史
        
        Args:
            session: 数据库会话
            account_id: 账号 ID
            api_key_id: API Key ID
            input_tokens: 输入 Token 数量
            output_tokens: 输出 Token 数量
        """
        today = utc_today()
        safe_request_count = max(1, int(request_count))
        
        conditions = [TokenUsageHistory.date == today]
        if account_id is None:
            conditions.append(TokenUsageHistory.account_id.is_(None))
        else:
            conditions.append(TokenUsageHistory.account_id == account_id)

        if api_key_id is None:
            conditions.append(TokenUsageHistory.api_key_id.is_(None))
        else:
            conditions.append(TokenUsageHistory.api_key_id == api_key_id)

        result = await session.execute(
            select(TokenUsageHistory)
            .where(and_(*conditions))
            .with_for_update()
        )
        rows = list(result.scalars().all())

        if rows:
            history = rows[0]
            if len(rows) > 1:
                # 历史重复桶在写入时合并，防止后续 scalar_one_or_none 冲突。
                for duplicate in rows[1:]:
                    history.input_tokens += duplicate.input_tokens
                    history.output_tokens += duplicate.output_tokens
                    history.request_count += duplicate.request_count
                    await session.delete(duplicate)
            history.input_tokens += input_tokens
            history.output_tokens += output_tokens
            history.request_count += safe_request_count
            return

        history = TokenUsageHistory(
            date=today,
            account_id=account_id,
            api_key_id=api_key_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            request_count=safe_request_count
        )
        session.add(history)
    
    def get_usage_percentage(self, account: STAccount) -> float:
        """
        计算账号的使用百分比
        
        Args:
            account: 账号对象
            
        Returns:
            使用百分比 (0-100+)
        """
        if account.daily_quota <= 0:
            return 100.0
        return (account.daily_used / account.daily_quota) * 100
    
    def get_usage_status(self, account: STAccount) -> str:
        """
        获取账号的使用状态
        
        Args:
            account: 账号对象
            
        Returns:
            状态字符串: "normal", "warning", "exhausted"
        """
        percentage = self.get_usage_percentage(account)
        
        if percentage >= 100:
            return "exhausted"
        elif percentage >= 80:
            return "warning"
        else:
            return "normal"
    
    async def check_quota_status(
        self,
        session: AsyncSession,
        account_id: str
    ) -> Tuple[bool, str, float]:
        """
        检查账号配额状态
        
        Args:
            session: 数据库会话
            account_id: 账号 ID
            
        Returns:
            (是否可用, 状态, 使用百分比)
        """
        account = await self.get_account(session, account_id)
        if account is None:
            return False, "not_found", 0.0
        
        percentage = self.get_usage_percentage(account)
        status = self.get_usage_status(account)
        is_available = account.status == "active" and percentage < 100
        
        return is_available, status, percentage

    # ==================== Daily Reset ====================
    # Requirements: 2.7
    
    async def reset_daily_usage(
        self,
        session: AsyncSession
    ) -> int:
        """
        重置所有账号的每日使用量（UTC 0:00 调用）
        
        Args:
            session: 数据库会话
            
        Returns:
            重置的账号数量
        """
        # 获取所有账号
        result = await session.execute(select(STAccount))
        accounts = list(result.scalars().all())
        
        reset_count = 0
        for account in accounts:
            # 重置每日使用量
            account.daily_used = 0
            
            # 如果账号之前是耗尽状态，恢复为活跃
            if account.status == "exhausted":
                account.status = "active"
            
            account.updated_at = utc_now_naive()
            reset_count += 1
        
        await session.flush()
        
        logger.info(f"Reset daily usage for {reset_count} accounts")
        return reset_count
    
    async def reset_account_daily_usage(
        self,
        session: AsyncSession,
        account_id: str
    ) -> bool:
        """
        重置单个账号的每日使用量
        
        Args:
            session: 数据库会话
            account_id: 账号 ID
            
        Returns:
            是否成功重置
        """
        account = await self.get_account(session, account_id)
        if account is None:
            return False
        
        account.daily_used = 0
        if account.status == "exhausted":
            account.status = "active"
        account.updated_at = utc_now_naive()
        
        await session.flush()
        
        logger.info(f"Reset daily usage for account {account_id}")
        return True
    
    # ==================== Utility Methods ====================
    
    def decrypt_api_key(self, account: STAccount) -> str:
        """
        解密账号的 API Key
        
        Args:
            account: 账号对象
            
        Returns:
            解密后的 API Key
        """
        crypto = get_crypto_service()
        return crypto.decrypt(account.api_key_encrypted)
    
    def decrypt_private_api_key(self, account: STAccount) -> Optional[str]:
        """
        解密账号的 Private API Key
        
        Args:
            account: 账号对象
            
        Returns:
            解密后的 Private API Key，如果未设置则返回 None
        """
        if not account.private_api_key_encrypted:
            return None
        crypto = get_crypto_service()
        return crypto.decrypt(account.private_api_key_encrypted)
    
    async def get_model_group_stats(
        self,
        session: AsyncSession,
        model_group: str
    ) -> Dict[str, Any]:
        """
        获取模型组的统计信息
        
        Args:
            session: 数据库会话
            model_group: 模型组名称
            
        Returns:
            统计信息字典
        """
        accounts = await self.get_accounts_by_model_group(session, model_group)
        
        total_quota = sum(a.daily_quota for a in accounts)
        total_used = sum(a.daily_used for a in accounts)
        active_count = sum(1 for a in accounts if a.status == "active")
        exhausted_count = sum(1 for a in accounts if a.status == "exhausted")
        disabled_count = sum(1 for a in accounts if a.status == "disabled")
        
        return {
            "model_group": model_group,
            "total_accounts": len(accounts),
            "active_accounts": active_count,
            "exhausted_accounts": exhausted_count,
            "disabled_accounts": disabled_count,
            "total_quota": total_quota,
            "total_used": total_used,
            "usage_percentage": (total_used / total_quota * 100) if total_quota > 0 else 0
        }

    def _normalize_model_names(self, model_groups: Any) -> List[str]:
        """标准化模型名列表：去空、去重、保序。"""
        if model_groups is None:
            return []

        if isinstance(model_groups, (str, bytes)):
            raw_items = [model_groups]
        elif isinstance(model_groups, (list, tuple, set)):
            raw_items = list(model_groups)
        else:
            raw_items = [model_groups]

        normalized: List[str] = []
        seen = set()

        for item in raw_items:
            value = str(item or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            normalized.append(value)

        return normalized


# 全局账号池服务实例
_account_pool_service: Optional[AccountPoolService] = None


def get_account_pool_service() -> AccountPoolService:
    """获取全局账号池服务实例"""
    global _account_pool_service
    if _account_pool_service is None:
        _account_pool_service = AccountPoolService()
    return _account_pool_service


def init_account_pool_service() -> AccountPoolService:
    """初始化全局账号池服务"""
    global _account_pool_service
    _account_pool_service = AccountPoolService()
    return _account_pool_service
