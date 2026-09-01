"""
API Key Management Service
API Key 生成、验证、撤销和管理

Requirements: 5.1, 5.2, 5.3, 5.5, 5.6, 5.7, 5.8, 6.3
"""

import uuid
import json
from decimal import Decimal, InvalidOperation
from datetime import datetime
from typing import Optional, List, Tuple, Dict, Any
import logging

from sqlalchemy import select, and_, update, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import APIKey, get_session_factory
from app.services.crypto import CryptoService, get_crypto_service
from app.services.time_utils import ensure_utc, utc_now, utc_now_naive

logger = logging.getLogger(__name__)


class APIKeyService:
    """
    API Key 管理服务
    
    提供 API Key 生成、验证、撤销、额度管理等功能
    """
    
    # ==================== Key Generation ====================
    # Requirements: 5.1, 5.2, 5.3
    
    async def generate_key(
        self,
        session: AsyncSession,
        model_groups: List[str],
        name: Optional[str] = None,
        quota: Optional[int] = None,
        expires_at: Optional[datetime] = None,
        request_quota: Optional[int] = None,
        token_quota: Optional[int] = None,
        cost_limit: Optional[str] = None
    ) -> Tuple[str, APIKey]:
        """
        生成新的 API Key
        
        Args:
            session: 数据库会话
            model_groups: 授权的模型组列表
            name: Key 名称（可选）
            quota: Token 使用额度限制（可选，None 表示无限制）- 已弃用
            expires_at: 过期时间（可选）
            request_quota: 请求数配额（可选，None 表示无限制）
            token_quota: Token 额度限制（可选，None 表示无限制）
            cost_limit: 费用限制（美元，可选，None 表示无限制）
            
        Returns:
            (原始 Key 值, APIKey 对象) - 原始 Key 只在创建时返回一次
        """
        # 生成唯一的 API Key (sk- 前缀)
        raw_key = CryptoService.generate_api_key(prefix="sk-")
        
        # 计算 Key 的哈希值用于存储
        key_hash = CryptoService.hash_api_key(raw_key)
        
        # 提取前缀和后缀用于显示
        key_prefix = raw_key[:7]  # "sk-" + 4 chars
        key_suffix = raw_key[-4:]  # 后4位
        
        # 创建 API Key 记录
        api_key = APIKey(
            id=str(uuid.uuid4()),
            key_hash=key_hash,
            key_prefix=key_prefix,
            key_suffix=key_suffix,
            name=name,
            model_groups=json.dumps(model_groups),
            quota=quota,
            used=0,
            request_quota=request_quota,
            token_quota=token_quota,
            cost_limit=cost_limit,
            total_cost="0",
            expires_at=expires_at,
            status="active",
            created_at=utc_now_naive()
        )
        
        session.add(api_key)
        await session.flush()
        
        logger.info(f"Generated API Key: {api_key.id} ({key_prefix}...)")
        return raw_key, api_key


    # ==================== Key Validation ====================
    # Requirements: 5.7, 6.3
    
    async def validate_key(
        self,
        session: AsyncSession,
        raw_key: str,
        requested_model: Optional[str] = None
    ) -> Tuple[bool, Optional[str], Optional[APIKey]]:
        """
        验证 API Key 的有效性、权限和额度
        
        Args:
            session: 数据库会话
            raw_key: 原始 API Key
            requested_model: 请求的模型名称（可选，用于权限检查）
            
        Returns:
            (是否有效, 错误消息, APIKey 对象)
        """
        # 清理可能的 Unicode 连字符变体（用户复制粘贴时可能引入）
        # EN DASH (U+2013), EM DASH (U+2014), MINUS SIGN (U+2212) -> ASCII hyphen (U+002D)
        raw_key = raw_key.replace('\u2013', '-').replace('\u2014', '-').replace('\u2212', '-')
        
        # 计算 Key 的哈希值
        key_hash = CryptoService.hash_api_key(raw_key)
        
        # 查找 Key
        result = await session.execute(
            select(APIKey).where(APIKey.key_hash == key_hash)
        )
        api_key = result.scalar_one_or_none()
        
        if api_key is None:
            return False, "Invalid API key", None

        total_requests = int(api_key.total_requests or 0)
        total_tokens = int(api_key.total_tokens or 0)
        legacy_used = int(api_key.used or 0)

        def _cost_limit_exceeded() -> bool:
            if api_key.cost_limit is None:
                return False
            try:
                return Decimal(api_key.total_cost or "0") >= Decimal(api_key.cost_limit)
            except (InvalidOperation, TypeError, ValueError):
                # 避免异常放大到鉴权路径，格式错误时跳过费用限制判定。
                return False

        def _quota_error_message() -> str:
            if api_key.request_quota is not None and total_requests >= api_key.request_quota:
                return "API key request quota exceeded"
            if api_key.token_quota is not None and total_tokens >= api_key.token_quota:
                return "API key token quota exceeded"
            if api_key.quota is not None and legacy_used >= api_key.quota:
                return "API key token quota exceeded"
            if _cost_limit_exceeded():
                return "API key cost limit exceeded"
            return "API key quota exceeded"

        # 检查状态
        if api_key.status == "revoked":
            return False, "API key has been revoked", None

        if api_key.status == "exhausted":
            return False, _quota_error_message(), None

        # 检查过期时间
        expires_at = ensure_utc(api_key.expires_at)
        if expires_at and expires_at < utc_now():
            return False, "API key has expired", None

        # 检查 Token 额度
        if api_key.quota is not None and legacy_used >= api_key.quota:
            return False, "API key token quota exceeded", None

        # 检查请求数额度
        if api_key.request_quota is not None and total_requests >= api_key.request_quota:
            return False, "API key request quota exceeded", None

        # 检查新版 Token 额度
        if api_key.token_quota is not None and total_tokens >= api_key.token_quota:
            return False, "API key token quota exceeded", None

        # 检查费用额度
        if _cost_limit_exceeded():
            return False, "API key cost limit exceeded", None

        # 检查模型权限
        if requested_model:
            try:
                allowed_groups = json.loads(api_key.model_groups)
            except Exception:
                return False, "API key model groups config invalid", None
            if requested_model not in allowed_groups:
                return False, f"Model '{requested_model}' not authorized for this API key", None

        return True, None, api_key
    
    async def get_key_by_hash(
        self,
        session: AsyncSession,
        key_hash: str
    ) -> Optional[APIKey]:
        """
        通过哈希值获取 API Key
        
        Args:
            session: 数据库会话
            key_hash: Key 的哈希值
            
        Returns:
            APIKey 对象，如果不存在则返回 None
        """
        result = await session.execute(
            select(APIKey).where(APIKey.key_hash == key_hash)
        )
        return result.scalar_one_or_none()

    async def get_key_by_raw(
        self,
        session: AsyncSession,
        raw_key: str
    ) -> Optional[APIKey]:
        """
        通过原始 Key 值获取 API Key。

        Args:
            session: 数据库会话
            raw_key: 原始 API Key

        Returns:
            APIKey 对象，如果不存在则返回 None
        """
        raw_key = raw_key.replace('\u2013', '-').replace('\u2014', '-').replace('\u2212', '-')
        key_hash = CryptoService.hash_api_key(raw_key)
        return await self.get_key_by_hash(session, key_hash)
    
    async def get_key_by_id(
        self,
        session: AsyncSession,
        key_id: str
    ) -> Optional[APIKey]:
        """
        通过 ID 获取 API Key
        
        Args:
            session: 数据库会话
            key_id: Key ID
            
        Returns:
            APIKey 对象，如果不存在则返回 None
        """
        logger.debug(f"Looking for API Key with id: {key_id}")
        result = await session.execute(
            select(APIKey).where(APIKey.id == key_id)
        )
        api_key = result.scalar_one_or_none()
        if api_key:
            logger.debug(f"Found API Key: {api_key.id}, status: {api_key.status}")
        else:
            logger.debug(f"API Key not found: {key_id}")
        return api_key
    
    async def get_all_keys(
        self,
        session: AsyncSession,
        include_revoked: bool = False
    ) -> List[APIKey]:
        """
        获取所有 API Key
        
        Args:
            session: 数据库会话
            include_revoked: 是否包含已撤销的 Key
            
        Returns:
            APIKey 列表
        """
        if include_revoked:
            result = await session.execute(select(APIKey))
        else:
            result = await session.execute(
                select(APIKey).where(APIKey.status == "active")
            )
        keys = list(result.scalars().all())
        print(f"[GET_ALL_KEYS] Found {len(keys)} keys (include_revoked={include_revoked})")
        for key in keys:
            print(f"[GET_ALL_KEYS]   - {key.id}: {key.name} ({key.status})")
        return keys


    # ==================== Key Revocation ====================
    # Requirements: 5.6
    
    async def revoke_key(
        self,
        session: AsyncSession,
        key_id: str
    ) -> bool:
        """
        撤销 API Key，立即使其失效
        
        Args:
            session: 数据库会话
            key_id: Key ID
            
        Returns:
            是否成功撤销
        """
        api_key = await self.get_key_by_id(session, key_id)
        if api_key is None:
            return False
        
        api_key.status = "revoked"
        await session.flush()
        
        logger.info(f"Revoked API Key: {key_id}")
        return True
    
    async def revoke_key_by_raw(
        self,
        session: AsyncSession,
        raw_key: str
    ) -> bool:
        """
        通过原始 Key 值撤销 API Key
        
        Args:
            session: 数据库会话
            raw_key: 原始 API Key
            
        Returns:
            是否成功撤销
        """
        key_hash = CryptoService.hash_api_key(raw_key)
        api_key = await self.get_key_by_hash(session, key_hash)
        if api_key is None:
            return False
        
        api_key.status = "revoked"
        await session.flush()
        
        logger.info(f"Revoked API Key: {api_key.id}")
        return True

    async def enable_key(
        self,
        session: AsyncSession,
        key_id: str
    ) -> bool:
        """
        启用已撤销的 API Key
        
        Args:
            session: 数据库会话
            key_id: Key ID
            
        Returns:
            是否成功启用
        """
        api_key = await self.get_key_by_id(session, key_id)
        if api_key is None:
            return False
        
        api_key.status = "active"
        await session.flush()
        
        logger.info(f"Enabled API Key: {key_id}")
        return True

    async def delete_key(
        self,
        session: AsyncSession,
        key_id: str
    ) -> bool:
        """
        永久删除 API Key
        
        Args:
            session: 数据库会话
            key_id: Key ID
            
        Returns:
            是否成功删除
        """
        print(f"[DELETE_KEY] Attempting to delete key: {key_id}")
        
        # 先检查 key 是否存在
        api_key = await self.get_key_by_id(session, key_id)
        if api_key is None:
            print(f"[DELETE_KEY] Key not found: {key_id}")
            return False
        
        print(f"[DELETE_KEY] Found key: {api_key.id}, name: {api_key.name}")
        
        # 删除 key
        await session.delete(api_key)
        await session.flush()
        
        print(f"[DELETE_KEY] Key deleted: {key_id}")
        return True

    # ==================== Usage Tracking ====================
    # Requirements: 5.8
    
    async def update_usage(
        self,
        session: AsyncSession,
        key_id: str,
        tokens: int
    ) -> Optional[APIKey]:
        """
        更新 API Key 的 Token 使用量
        
        Args:
            session: 数据库会话
            key_id: Key ID
            tokens: 使用的 Token 数量
            
        Returns:
            更新后的 APIKey 对象，如果不存在则返回 None
        """
        await session.execute(
            update(APIKey)
            .where(APIKey.id == key_id)
            .values(
                used=APIKey.used + max(0, int(tokens)),
                last_used_at=utc_now_naive(),
            )
        )

        api_key = await self.get_key_by_id(session, key_id)
        if api_key is None:
            return None

        logger.debug(f"Updated usage for API Key {key_id}: +{tokens} (total: {api_key.used})")
        return api_key
    
    async def update_key_stats(
        self,
        session: AsyncSession,
        key_id: str,
        input_tokens: int,
        output_tokens: int,
        cost: Optional[str] = None,
        *,
        request_count: int = 1,
    ) -> Optional[APIKey]:
        """
        更新 API Key 的累计统计（请求数、Token 使用量和费用）
        
        Args:
            session: 数据库会话
            key_id: Key ID
            input_tokens: 输入 Token 数量
            output_tokens: 输出 Token 数量
            cost: 本次调用费用（美元字符串）
            
        Returns:
            更新后的 APIKey 对象，如果不存在则返回 None
        """
        from decimal import Decimal
        
        # 并发安全：统计写入前先加行锁，避免高并发下丢增量。
        result = await session.execute(
            select(APIKey)
            .where(APIKey.id == key_id)
            .with_for_update()
        )
        api_key = result.scalar_one_or_none()
        if api_key is None:
            logger.warning(f"update_key_stats: API Key not found: {key_id}")
            return None
        
        safe_request_count = max(1, int(request_count))
        total_tokens = max(0, int(input_tokens)) + max(0, int(output_tokens))
        
        # 更新累计统计
        api_key.total_requests = (api_key.total_requests or 0) + safe_request_count
        api_key.total_tokens = (api_key.total_tokens or 0) + total_tokens
        api_key.last_used_at = utc_now_naive()
        
        # 更新费用
        if cost:
            try:
                current_cost = Decimal(api_key.total_cost or "0")
                new_cost = current_cost + Decimal(cost)
                api_key.total_cost = str(new_cost)
                logger.info(f"update_key_stats: Key {key_id} cost updated: {current_cost} + {cost} = {new_cost}")
            except Exception as e:
                logger.error(f"update_key_stats: Failed to update cost for key {key_id}: {e}")
        
        # 检查是否达到请求数限制
        if api_key.request_quota is not None and api_key.total_requests >= api_key.request_quota:
            api_key.status = "exhausted"
            logger.info(f"API Key {key_id} reached request quota limit ({api_key.request_quota})")
        
        # 检查是否达到 Token 限制
        if api_key.token_quota is not None and api_key.total_tokens >= api_key.token_quota:
            api_key.status = "exhausted"
            logger.info(f"API Key {key_id} reached token quota limit ({api_key.token_quota})")
        
        # 检查是否达到费用限制
        if api_key.cost_limit is not None and api_key.total_cost:
            try:
                limit = Decimal(api_key.cost_limit)
                current = Decimal(api_key.total_cost)
                if current >= limit:
                    api_key.status = "exhausted"
                    logger.info(f"API Key {key_id} reached cost limit ({api_key.cost_limit})")
            except Exception:
                pass
        
        await session.flush()
        
        logger.debug(
            f"Updated stats for API Key {key_id}: "
            f"requests={api_key.total_requests}, tokens={api_key.total_tokens}, cost={api_key.total_cost}"
        )
        return api_key

    async def update_key_stats_bulk(
        self,
        session: AsyncSession,
        buckets: List[Dict[str, Any]],
    ) -> None:
        """Apply aggregated key counters in one executemany round trip.

        This avoids a SELECT plus ORM UPDATE for every key in each usage batch.
        Quota checks use the post-increment values in the same atomic statement.
        """
        if not buckets:
            return
        now = utc_now_naive()
        parameters = [
            {
                "key_id": str(item["key_id"]),
                "request_count": max(1, int(item.get("request_count", 1))),
                "total_tokens": max(0, int(item.get("input_tokens", 0)))
                + max(0, int(item.get("output_tokens", 0))),
                "cost": str(item.get("cost") or "0"),
                "last_used_at": now,
            }
            for item in buckets
        ]
        await session.execute(
            text(
                """
                UPDATE api_keys SET
                    total_requests = COALESCE(total_requests, 0) + :request_count,
                    total_tokens = COALESCE(total_tokens, 0) + :total_tokens,
                    total_cost = CAST(
                        CAST(COALESCE(total_cost, '0') AS NUMERIC) + CAST(:cost AS NUMERIC)
                        AS TEXT
                    ),
                    last_used_at = :last_used_at,
                    status = CASE
                        WHEN status = 'revoked' THEN status
                        WHEN request_quota IS NOT NULL
                             AND COALESCE(total_requests, 0) + :request_count >= request_quota
                            THEN 'exhausted'
                        WHEN token_quota IS NOT NULL
                             AND COALESCE(total_tokens, 0) + :total_tokens >= token_quota
                            THEN 'exhausted'
                        WHEN cost_limit IS NOT NULL
                             AND CAST(COALESCE(total_cost, '0') AS NUMERIC) + CAST(:cost AS NUMERIC)
                                 >= CAST(cost_limit AS NUMERIC)
                            THEN 'exhausted'
                        ELSE status
                    END
                WHERE id = :key_id
                """
            ),
            parameters,
        )
    
    async def check_quota(
        self,
        session: AsyncSession,
        key_id: str
    ) -> Tuple[bool, int, Optional[int]]:
        """
        检查 API Key 的额度状态
        
        Args:
            session: 数据库会话
            key_id: Key ID
            
        Returns:
            (是否有剩余额度, 已使用量, 总额度)
        """
        api_key = await self.get_key_by_id(session, key_id)
        if api_key is None:
            return False, 0, None
        
        if api_key.quota is None:
            # 无限制
            return True, api_key.used, None
        
        has_quota = api_key.used < api_key.quota
        return has_quota, api_key.used, api_key.quota


    # ==================== Key Display Masking ====================
    # Requirements: 5.5
    
    @staticmethod
    def mask_key(raw_key: str) -> str:
        """
        对 API Key 进行脱敏显示
        
        只显示前缀（前7位）和后缀（后4位），中间用 ... 替代
        
        Args:
            raw_key: 原始 API Key
            
        Returns:
            脱敏后的 Key，格式为 "sk-xxx...xxxx"
        """
        return CryptoService.mask_api_key(raw_key)
    
    @staticmethod
    def get_display_key(api_key: APIKey) -> str:
        """
        获取 API Key 的显示格式
        
        使用存储的前缀和后缀
        
        Args:
            api_key: APIKey 对象
            
        Returns:
            显示格式的 Key，如 "sk-xxxx...xxxx"
        """
        suffix = api_key.key_suffix or "****"
        return f"{api_key.key_prefix}...{suffix}"
    
    # ==================== Model Group Management ====================
    
    def get_model_groups(self, api_key: APIKey) -> List[str]:
        """
        获取 API Key 授权的模型组列表
        
        Args:
            api_key: APIKey 对象
            
        Returns:
            模型组列表
        """
        return json.loads(api_key.model_groups)
    
    async def update_model_groups(
        self,
        session: AsyncSession,
        key_id: str,
        model_groups: List[str]
    ) -> Optional[APIKey]:
        """
        更新 API Key 的授权模型组
        
        Args:
            session: 数据库会话
            key_id: Key ID
            model_groups: 新的模型组列表
            
        Returns:
            更新后的 APIKey 对象，如果不存在则返回 None
        """
        api_key = await self.get_key_by_id(session, key_id)
        if api_key is None:
            return None
        
        api_key.model_groups = json.dumps(model_groups)
        await session.flush()
        
        logger.info(f"Updated model groups for API Key {key_id}: {model_groups}")
        return api_key
    
    async def update_key(
        self,
        session: AsyncSession,
        key_id: str,
        name: Optional[str] = None,
        model_groups: Optional[List[str]] = None,
        request_quota: Optional[int] = None,
        token_quota: Optional[int] = None,
        cost_limit: Optional[str] = None,
        expires_at: Optional[datetime] = None,
        status: Optional[str] = None
    ) -> Optional[APIKey]:
        """
        更新 API Key 的属性
        
        Args:
            session: 数据库会话
            key_id: Key ID
            name: 新名称（可选）
            model_groups: 新的模型组列表（可选）
            request_quota: 请求数配额（可选，-1 表示清除限制）
            token_quota: Token 额度限制（可选，-1 表示清除限制）
            cost_limit: 费用限制（美元，可选，None 表示不更新）
            expires_at: 过期时间（可选）
            status: 状态（可选）
            
        Returns:
            更新后的 APIKey 对象，如果不存在则返回 None
        """
        api_key = await self.get_key_by_id(session, key_id)
        if api_key is None:
            return None
        
        if name is not None:
            api_key.name = name
        
        if model_groups is not None:
            api_key.model_groups = json.dumps(model_groups)
        
        if request_quota is not None:
            # -1 表示清除限制
            api_key.request_quota = None if request_quota == -1 else request_quota
            # 如果设置了新的配额且当前状态是 exhausted，检查是否可以恢复
            if api_key.status == "exhausted" and (api_key.request_quota is None or 
                (api_key.total_requests or 0) < api_key.request_quota):
                api_key.status = "active"
        
        if token_quota is not None:
            # -1 表示清除限制
            api_key.token_quota = None if token_quota == -1 else token_quota
            # 检查是否可以恢复状态
            if api_key.status == "exhausted" and (api_key.token_quota is None or 
                (api_key.total_tokens or 0) < api_key.token_quota):
                api_key.status = "active"
        
        if cost_limit is not None:
            # 空字符串表示清除限制
            if cost_limit == "":
                api_key.cost_limit = None
                if api_key.status == "exhausted":
                    api_key.status = "active"
            else:
                api_key.cost_limit = cost_limit
                # 检查是否可以恢复状态
                if api_key.status == "exhausted":
                    try:
                        limit = float(cost_limit)
                        current = float(api_key.total_cost or "0")
                        if current < limit:
                            api_key.status = "active"
                    except ValueError:
                        pass
        
        if expires_at is not None:
            api_key.expires_at = expires_at
        
        if status is not None:
            api_key.status = status
        
        await session.flush()
        
        logger.info(f"Updated API Key {key_id}")
        return api_key


# 全局 API Key 服务实例
_api_key_service: Optional[APIKeyService] = None


def get_api_key_service() -> APIKeyService:
    """获取全局 API Key 服务实例"""
    global _api_key_service
    if _api_key_service is None:
        _api_key_service = APIKeyService()
    return _api_key_service


def init_api_key_service() -> APIKeyService:
    """初始化全局 API Key 服务"""
    global _api_key_service
    _api_key_service = APIKeyService()
    return _api_key_service
