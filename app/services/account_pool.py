"""
Account Pool Management Service
多账号池化管理，支持 CRUD、轮询负载均衡、Token 使用追踪
"""

import uuid
from datetime import datetime, date
from typing import Optional, List, Dict, Any, Tuple
from collections import defaultdict
import logging

from sqlalchemy import select, update, delete, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import (
    BackendAccount, AccountModelRoute, TokenUsageHistory, get_session_factory
)
from app.services.crypto import get_crypto_service

logger = logging.getLogger(__name__)

# 兼容旧名称
STAccount = BackendAccount


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
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
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
        now = datetime.utcnow()
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
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            ))

        # 兼容旧字段：保留主模型
        account.model_group = normalized[0]
        account.updated_at = datetime.utcnow()
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
        # 新路由表命中
        route_result = await session.execute(
            select(AccountModelRoute.account_id).where(
                and_(
                    AccountModelRoute.model_name == model_group,
                    AccountModelRoute.enabled == True
                )
            )
        )
        route_account_ids = [row[0] for row in route_result.fetchall()]

        accounts_by_id: Dict[str, STAccount] = {}
        if route_account_ids:
            result = await session.execute(
                select(STAccount).where(STAccount.id.in_(route_account_ids))
            )
            for account in result.scalars().all():
                accounts_by_id[account.id] = account

        # 旧字段兜底（兼容历史数据）
        legacy_result = await session.execute(
            select(STAccount).where(STAccount.model_group == model_group)
        )
        for account in legacy_result.scalars().all():
            accounts_by_id.setdefault(account.id, account)

        return sorted(accounts_by_id.values(), key=lambda a: a.id)
    
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
            account.updated_at = datetime.utcnow()
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
        now = datetime.utcnow()

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
        routable_accounts = 0
        eligible_accounts = 0
        stale_exhausted_accounts = 0

        for account in accounts:
            status = str(account.status or "unknown")
            status_counts[status] += 1

            if status in {"active", "exhausted"}:
                routable_accounts += 1
                if account.daily_used < account.daily_quota:
                    eligible_accounts += 1
                    if status == "exhausted":
                        stale_exhausted_accounts += 1

        return {
            "model_group": model_group,
            "total_accounts": len(accounts),
            "routable_accounts": routable_accounts,
            "eligible_accounts": eligible_accounts,
            "stale_exhausted_accounts": stale_exhausted_accounts,
            "status_counts": dict(sorted(status_counts.items())),
        }

    async def get_available_account(
        self,
        session: AsyncSession,
        model_group: str
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

        routable_accounts = [
            account for account in accounts
            if account.status in {"active", "exhausted"}
        ]

        if not routable_accounts:
            status_counts: Dict[str, int] = defaultdict(int)
            for account in accounts:
                key = str(account.status or "unknown")
                status_counts[key] += 1
            logger.warning(
                "No routable accounts in model group '%s' (total=%d, status_counts=%s)",
                model_group,
                len(accounts),
                dict(sorted(status_counts.items())),
            )
            return None

        eligible_accounts = [
            account for account in routable_accounts
            if account.daily_used < account.daily_quota
        ]

        if not eligible_accounts:
            logger.warning(
                "All routable accounts quota-blocked in model group '%s' (routable=%d, quota_blocked=%d)",
                model_group,
                len(routable_accounts),
                len(routable_accounts),
            )
            return None

        # 获取当前轮询索引
        current_index = self._round_robin_index[model_group]

        # 尝试找到一个可用账号（最多尝试 len(eligible_accounts) 次）
        for _ in range(len(eligible_accounts)):
            # 使用模运算确保索引在有效范围内
            index = current_index % len(eligible_accounts)
            account = eligible_accounts[index]

            # 更新轮询索引
            current_index += 1
            self._round_robin_index[model_group] = current_index

            # 账号选择阶段保持纯读，避免在高并发下放大 SQLite 写锁竞争。
            # 状态与使用时间统一在请求收口（persist_success -> update_token_usage）时落库。

            logger.debug(f"Selected account {account.id} for group {model_group}")
            return account

        return None
    
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
        account = await self.get_account(session, account_id)
        if account is None:
            return False
        
        account.status = "exhausted"
        if enforce_quota_block and account.daily_used < account.daily_quota:
            # Prevent immediate re-selection when upstream already reports quota exceeded
            # but local counters are still lagging.
            account.daily_used = account.daily_quota
        account.updated_at = datetime.utcnow()
        await session.flush()
        
        logger.info(f"Marked account {account_id} as exhausted")
        return True

    # ==================== Token Usage Tracking ====================
    # Requirements: 3.1, 3.4, 3.5
    
    async def update_token_usage(
        self,
        session: AsyncSession,
        account_id: str,
        input_tokens: int,
        output_tokens: int
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
        account = await self.get_account(session, account_id)
        if account is None:
            return None
        
        total_tokens = input_tokens + output_tokens
        account.daily_used += total_tokens
        now = datetime.utcnow()
        account.last_used_at = now
        account.updated_at = now
        
        # 检查是否达到配额
        if account.daily_used >= account.daily_quota:
            account.status = "exhausted"
            logger.info(f"Account {account_id} reached quota limit")
        elif account.status == "exhausted":
            # 容错：陈旧 exhausted 状态在成功请求后自动恢复
            account.status = "active"
        
        # 记录到历史表
        await self._record_usage_history(
            session, account_id, None, input_tokens, output_tokens
        )
        
        await session.flush()
        
        logger.debug(
            f"Updated token usage for account {account_id}: "
            f"+{total_tokens} (total: {account.daily_used}/{account.daily_quota})"
        )
        return account
    
    async def _record_usage_history(
        self,
        session: AsyncSession,
        account_id: Optional[str],
        api_key_id: Optional[str],
        input_tokens: int,
        output_tokens: int
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
        today = date.today()
        
        # 查找今天的记录
        result = await session.execute(
            select(TokenUsageHistory).where(
                and_(
                    TokenUsageHistory.date == today,
                    TokenUsageHistory.account_id == account_id,
                    TokenUsageHistory.api_key_id == api_key_id
                )
            )
        )
        history = result.scalar_one_or_none()
        
        if history:
            # 更新现有记录
            history.input_tokens += input_tokens
            history.output_tokens += output_tokens
            history.request_count += 1
        else:
            # 创建新记录
            history = TokenUsageHistory(
                date=today,
                account_id=account_id,
                api_key_id=api_key_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                request_count=1
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
            
            account.updated_at = datetime.utcnow()
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
        account.updated_at = datetime.utcnow()
        
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
