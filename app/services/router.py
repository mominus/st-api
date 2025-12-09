"""
Model Group Router Service
根据 model 参数选择账号池，实现模型组路由

Requirements: 2.3, 6.4
"""

import json
import logging
from typing import Optional, Tuple, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import BackendAccount, ModelGroup, APIKey

# 兼容旧名称
StackAIAccount = BackendAccount
from app.services.account_pool import AccountPoolService, get_account_pool_service
from app.services.api_key import APIKeyService, get_api_key_service

logger = logging.getLogger(__name__)


class ModelGroupRouter:
    """
    模型组路由服务
    
    根据请求的 model 参数选择对应的账号池，
    并验证 API Key 是否有权限访问该模型组。
    
    Requirements: 2.3, 6.4
    """
    
    def __init__(
        self,
        account_pool_service: Optional[AccountPoolService] = None,
        api_key_service: Optional[APIKeyService] = None
    ):
        """
        初始化路由服务
        
        Args:
            account_pool_service: 账号池服务实例（可选，默认使用全局实例）
            api_key_service: API Key 服务实例（可选，默认使用全局实例）
        """
        self._account_pool = account_pool_service
        self._api_key_service = api_key_service
    
    @property
    def account_pool(self) -> AccountPoolService:
        """获取账号池服务"""
        if self._account_pool is None:
            self._account_pool = get_account_pool_service()
        return self._account_pool
    
    @property
    def api_key_service(self) -> APIKeyService:
        """获取 API Key 服务"""
        if self._api_key_service is None:
            self._api_key_service = get_api_key_service()
        return self._api_key_service
    
    async def route_request(
        self,
        session: AsyncSession,
        model: str,
        api_key: Optional[APIKey] = None
    ) -> Tuple[Optional[StackAIAccount], Optional[str]]:
        """
        根据 model 参数路由请求到对应的账号池
        
        1. 验证 API Key 是否有权限访问该模型组（如果提供了 API Key）
        2. 从对应模型组的账号池中选择可用账号
        
        Args:
            session: 数据库会话
            model: 请求的模型名称（对应模型组名称）
            api_key: API Key 对象（可选，用于权限验证）
            
        Returns:
            (账号对象, 错误消息) - 如果成功，错误消息为 None
            
        Requirements: 2.3, 6.4
        """
        # 1. 验证 API Key 权限（如果提供了 API Key）
        if api_key is not None:
            allowed_groups = self._get_allowed_model_groups(api_key)
            if model not in allowed_groups:
                logger.warning(
                    f"API Key {api_key.key_prefix} not authorized for model '{model}'"
                )
                return None, f"Model '{model}' not authorized for this API key"
        
        # 2. 检查模型组是否存在
        model_group_exists = await self._check_model_group_exists(session, model)
        if not model_group_exists:
            # 即使模型组不存在于 model_groups 表，也尝试从账号池中查找
            # 因为账号可能直接配置了 model_group 而没有在 model_groups 表中注册
            pass
        
        # 3. 从账号池中选择可用账号
        account = await self.account_pool.get_available_account(session, model)
        
        if account is None:
            logger.warning(f"No available accounts for model group '{model}'")
            return None, f"No available accounts for model '{model}'"
        
        logger.info(
            f"Routed request for model '{model}' to account {account.id} ({account.name})"
        )
        return account, None
    
    async def validate_model_access(
        self,
        session: AsyncSession,
        model: str,
        api_key: APIKey
    ) -> Tuple[bool, Optional[str]]:
        """
        验证 API Key 是否有权限访问指定模型
        
        Args:
            session: 数据库会话
            model: 请求的模型名称
            api_key: API Key 对象
            
        Returns:
            (是否有权限, 错误消息)
            
        Requirements: 6.4
        """
        allowed_groups = self._get_allowed_model_groups(api_key)
        
        if model not in allowed_groups:
            return False, f"Model '{model}' not authorized for this API key"
        
        return True, None
    
    async def get_available_models(
        self,
        session: AsyncSession,
        api_key: Optional[APIKey] = None
    ) -> List[str]:
        """
        获取可用的模型列表
        
        如果提供了 API Key，只返回该 Key 授权的模型
        
        Args:
            session: 数据库会话
            api_key: API Key 对象（可选）
            
        Returns:
            可用模型名称列表
        """
        # 获取所有模型组
        result = await session.execute(select(ModelGroup))
        all_groups = [g.name for g in result.scalars().all()]
        
        # 如果没有 API Key 限制，返回所有模型组
        if api_key is None:
            return all_groups
        
        # 过滤出 API Key 授权的模型组
        allowed_groups = self._get_allowed_model_groups(api_key)
        return [g for g in all_groups if g in allowed_groups]
    
    async def get_model_group_config(
        self,
        session: AsyncSession,
        model: str
    ) -> Optional[ModelGroup]:
        """
        获取模型组配置
        
        Args:
            session: 数据库会话
            model: 模型名称
            
        Returns:
            ModelGroup 对象，如果不存在则返回 None
        """
        result = await session.execute(
            select(ModelGroup).where(ModelGroup.name == model)
        )
        return result.scalar_one_or_none()
    
    async def get_input_mapping(
        self,
        session: AsyncSession,
        model: str
    ) -> Optional[dict]:
        """
        获取模型组的输入映射配置
        
        Args:
            session: 数据库会话
            model: 模型名称
            
        Returns:
            输入映射字典，如果不存在则返回 None
        """
        model_group = await self.get_model_group_config(session, model)
        if model_group is None or not model_group.input_mapping:
            return None
        
        try:
            return json.loads(model_group.input_mapping)
        except json.JSONDecodeError:
            logger.warning(f"Invalid input_mapping JSON for model group '{model}'")
            return None
    
    def _get_allowed_model_groups(self, api_key: APIKey) -> List[str]:
        """
        获取 API Key 授权的模型组列表
        
        Args:
            api_key: API Key 对象
            
        Returns:
            模型组名称列表
        """
        try:
            return json.loads(api_key.model_groups)
        except (json.JSONDecodeError, TypeError):
            return []
    
    async def _check_model_group_exists(
        self,
        session: AsyncSession,
        model: str
    ) -> bool:
        """
        检查模型组是否存在于 model_groups 表中
        
        Args:
            session: 数据库会话
            model: 模型名称
            
        Returns:
            是否存在
        """
        result = await session.execute(
            select(ModelGroup).where(ModelGroup.name == model)
        )
        return result.scalar_one_or_none() is not None
    
    async def get_accounts_for_model(
        self,
        session: AsyncSession,
        model: str
    ) -> List[StackAIAccount]:
        """
        获取指定模型组的所有账号
        
        Args:
            session: 数据库会话
            model: 模型名称（模型组名称）
            
        Returns:
            账号列表
        """
        return await self.account_pool.get_accounts_by_model_group(session, model)
    
    async def get_model_group_stats(
        self,
        session: AsyncSession,
        model: str
    ) -> dict:
        """
        获取模型组的统计信息
        
        Args:
            session: 数据库会话
            model: 模型名称
            
        Returns:
            统计信息字典
        """
        return await self.account_pool.get_model_group_stats(session, model)


# 全局路由服务实例
_router_service: Optional[ModelGroupRouter] = None


def get_router_service() -> ModelGroupRouter:
    """获取全局路由服务实例"""
    global _router_service
    if _router_service is None:
        _router_service = ModelGroupRouter()
    return _router_service


def init_router_service(
    account_pool_service: Optional[AccountPoolService] = None,
    api_key_service: Optional[APIKeyService] = None
) -> ModelGroupRouter:
    """
    初始化全局路由服务
    
    Args:
        account_pool_service: 账号池服务实例（可选）
        api_key_service: API Key 服务实例（可选）
        
    Returns:
        路由服务实例
    """
    global _router_service
    _router_service = ModelGroupRouter(account_pool_service, api_key_service)
    return _router_service
