"""
Authentication Service
提供管理员登录认证、JWT Token 管理和登录保护功能

Requirements: 4.2, 4.3
"""

import os
import uuid
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, Any

import jwt
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Admin, LoginAttempt, get_session_factory
from app.services.crypto import CryptoService
from app.services.config import get_config_service


class AuthenticationError(Exception):
    """认证错误基类"""
    pass


class InvalidCredentialsError(AuthenticationError):
    """无效的凭证"""
    pass


class AccountLockedError(AuthenticationError):
    """账号被锁定"""
    def __init__(self, locked_until: datetime):
        self.locked_until = locked_until
        super().__init__(f"Account locked until {locked_until}")


class TokenExpiredError(AuthenticationError):
    """Token 已过期"""
    pass


class InvalidTokenError(AuthenticationError):
    """无效的 Token"""
    pass


class AuthService:
    """
    认证服务类
    
    提供管理员登录、JWT Token 生成/验证和登录保护功能
    
    Requirements:
    - 4.2: 用户提供正确的管理员凭证时创建会话
    - 4.3: 连续失败 5 次锁定 IP 15 分钟
    """
    
    def __init__(self, session_factory=None):
        """
        初始化认证服务
        
        Args:
            session_factory: SQLAlchemy 异步会话工厂
        """
        self._session_factory = session_factory
        self._config = get_config_service()
    
    @property
    def jwt_secret_key(self) -> str:
        """获取 JWT 密钥"""
        return self._config.security.jwt_secret_key
    
    @property
    def jwt_expire_hours(self) -> int:
        """获取 JWT 过期时间（小时）"""
        return self._config.security.jwt_expire_hours
    
    @property
    def max_login_attempts(self) -> int:
        """获取最大登录尝试次数"""
        return self._config.login_protection.max_attempts
    
    @property
    def lockout_minutes(self) -> int:
        """获取锁定时间（分钟）"""
        return self._config.login_protection.lockout_minutes
    
    def _get_session_factory(self):
        """获取会话工厂"""
        if self._session_factory is None:
            self._session_factory = get_session_factory()
        return self._session_factory
    
    # ==================== JWT Token 管理 ====================
    
    def generate_token(self, admin_id: str, username: str) -> str:
        """
        生成 JWT Token
        
        Args:
            admin_id: 管理员 ID
            username: 管理员用户名
            
        Returns:
            JWT Token 字符串
            
        Requirements: 4.2
        """
        now = datetime.utcnow()
        expire = now + timedelta(hours=self.jwt_expire_hours)
        
        payload = {
            "sub": admin_id,
            "username": username,
            "iat": now,
            "exp": expire,
            "type": "access"
        }
        
        token = jwt.encode(payload, self.jwt_secret_key, algorithm="HS256")
        return token
    
    def verify_token(self, token: str) -> Dict[str, Any]:
        """
        验证 JWT Token
        
        Args:
            token: JWT Token 字符串
            
        Returns:
            Token 载荷（包含 sub, username, iat, exp）
            
        Raises:
            TokenExpiredError: Token 已过期
            InvalidTokenError: Token 无效
        """
        try:
            payload = jwt.decode(
                token, 
                self.jwt_secret_key, 
                algorithms=["HS256"]
            )
            return payload
        except jwt.ExpiredSignatureError:
            raise TokenExpiredError("Token has expired")
        except jwt.InvalidTokenError as e:
            raise InvalidTokenError(f"Invalid token: {e}")
    
    def refresh_token(self, token: str) -> str:
        """
        刷新 JWT Token
        
        Args:
            token: 当前的 JWT Token
            
        Returns:
            新的 JWT Token
            
        Raises:
            TokenExpiredError: Token 已过期
            InvalidTokenError: Token 无效
        """
        payload = self.verify_token(token)
        return self.generate_token(payload["sub"], payload["username"])
    
    # ==================== 密码验证 ====================
    
    def verify_password(self, password: str, password_hash: str) -> bool:
        """
        验证密码
        
        Args:
            password: 明文密码
            password_hash: 密码哈希
            
        Returns:
            密码是否匹配
            
        Requirements: 4.2
        """
        return CryptoService.verify_password(password, password_hash)
    
    def hash_password(self, password: str) -> str:
        """
        哈希密码
        
        Args:
            password: 明文密码
            
        Returns:
            密码哈希
        """
        return CryptoService.hash_password(password)
    
    # ==================== 登录保护 ====================
    
    # 本地 IP 白名单 - 这些 IP 不受登录锁定限制
    LOCAL_IP_WHITELIST = {
        "127.0.0.1",
        "localhost",
        "::1",  # IPv6 localhost
        "0.0.0.0",
    }
    
    def _is_local_ip(self, ip: str) -> bool:
        """
        检查是否为本地 IP 地址
        
        Args:
            ip: IP 地址
            
        Returns:
            是否为本地 IP
        """
        if ip in self.LOCAL_IP_WHITELIST:
            return True
        # 检查是否为本地网络地址 (192.168.x.x, 10.x.x.x, 172.16-31.x.x)
        if ip.startswith("192.168.") or ip.startswith("10."):
            return True
        if ip.startswith("172."):
            try:
                second_octet = int(ip.split(".")[1])
                if 16 <= second_octet <= 31:
                    return True
            except (ValueError, IndexError):
                pass
        return False
    
    async def check_login_allowed(self, ip: str) -> Tuple[bool, Optional[datetime]]:
        """
        检查 IP 是否允许登录
        
        Args:
            ip: 客户端 IP 地址
            
        Returns:
            (是否允许登录, 锁定截止时间)
            
        Requirements: 4.3
        
        Note: 本地 IP 地址不受登录锁定限制
        """
        # 本地 IP 跳过锁定检查
        if self._is_local_ip(ip):
            return True, None
        
        session_factory = self._get_session_factory()
        async with session_factory() as session:
            result = await session.execute(
                select(LoginAttempt).where(LoginAttempt.ip == ip)
            )
            attempt = result.scalar_one_or_none()
            
            if attempt is None:
                return True, None
            
            # 检查是否仍在锁定期
            if attempt.locked_until and attempt.locked_until > datetime.utcnow():
                return False, attempt.locked_until
            
            # 锁定期已过，重置计数
            if attempt.locked_until and attempt.locked_until <= datetime.utcnow():
                attempt.attempts = 0
                attempt.locked_until = None
                await session.commit()
            
            return True, None
    
    async def record_login_attempt(self, ip: str, success: bool) -> Optional[datetime]:
        """
        记录登录尝试
        
        Args:
            ip: 客户端 IP 地址
            success: 是否登录成功
            
        Returns:
            如果被锁定，返回锁定截止时间；否则返回 None
            
        Requirements: 4.3
        
        Note: 本地 IP 地址不记录失败尝试，不会被锁定
        """
        # 本地 IP 跳过记录
        if self._is_local_ip(ip):
            return None
        
        session_factory = self._get_session_factory()
        async with session_factory() as session:
            result = await session.execute(
                select(LoginAttempt).where(LoginAttempt.ip == ip)
            )
            attempt = result.scalar_one_or_none()
            
            if success:
                # 登录成功，清除记录
                if attempt:
                    await session.delete(attempt)
                    await session.commit()
                return None
            
            # 登录失败
            if attempt is None:
                attempt = LoginAttempt(ip=ip, attempts=1)
                session.add(attempt)
            else:
                attempt.attempts += 1
            
            # 检查是否需要锁定
            if attempt.attempts >= self.max_login_attempts:
                attempt.locked_until = datetime.utcnow() + timedelta(
                    minutes=self.lockout_minutes
                )
                await session.commit()
                return attempt.locked_until
            
            await session.commit()
            return None
    
    async def get_remaining_attempts(self, ip: str) -> int:
        """
        获取剩余登录尝试次数
        
        Args:
            ip: 客户端 IP 地址
            
        Returns:
            剩余尝试次数
        """
        session_factory = self._get_session_factory()
        async with session_factory() as session:
            result = await session.execute(
                select(LoginAttempt).where(LoginAttempt.ip == ip)
            )
            attempt = result.scalar_one_or_none()
            
            if attempt is None:
                return self.max_login_attempts
            
            # 如果锁定期已过，返回最大次数
            if attempt.locked_until and attempt.locked_until <= datetime.utcnow():
                return self.max_login_attempts
            
            return max(0, self.max_login_attempts - attempt.attempts)
    
    # ==================== 管理员登录 ====================
    
    async def authenticate(
        self, 
        username: str, 
        password: str, 
        ip: str
    ) -> Tuple[str, Dict[str, Any]]:
        """
        管理员登录认证
        
        Args:
            username: 用户名
            password: 密码
            ip: 客户端 IP 地址
            
        Returns:
            (JWT Token, 管理员信息)
            
        Raises:
            AccountLockedError: 账号被锁定
            InvalidCredentialsError: 凭证无效
            
        Requirements: 4.2, 4.3
        """
        # 检查是否被锁定
        allowed, locked_until = await self.check_login_allowed(ip)
        if not allowed:
            raise AccountLockedError(locked_until)
        
        session_factory = self._get_session_factory()
        async with session_factory() as session:
            # 查找管理员
            result = await session.execute(
                select(Admin).where(Admin.username == username)
            )
            admin = result.scalar_one_or_none()
            
            # 验证凭证
            if admin is None or not self.verify_password(password, admin.password_hash):
                # 记录失败尝试
                locked_until = await self.record_login_attempt(ip, success=False)
                if locked_until:
                    raise AccountLockedError(locked_until)
                raise InvalidCredentialsError("Invalid username or password")
            
            # 登录成功，清除失败记录
            await self.record_login_attempt(ip, success=True)
            
            # 生成 Token
            token = self.generate_token(admin.id, admin.username)
            
            admin_info = {
                "id": admin.id,
                "username": admin.username,
                "created_at": admin.created_at.isoformat() if admin.created_at else None
            }
            
            return token, admin_info
    
    # ==================== 管理员管理 ====================
    
    async def create_admin(self, username: str, password: str) -> Dict[str, Any]:
        """
        创建管理员账号
        
        Args:
            username: 用户名
            password: 密码
            
        Returns:
            管理员信息
        """
        session_factory = self._get_session_factory()
        async with session_factory() as session:
            # 检查用户名是否已存在
            result = await session.execute(
                select(Admin).where(Admin.username == username)
            )
            if result.scalar_one_or_none():
                raise ValueError(f"Username '{username}' already exists")
            
            # 创建管理员
            admin = Admin(
                id=str(uuid.uuid4()),
                username=username,
                password_hash=self.hash_password(password),
                created_at=datetime.utcnow()
            )
            session.add(admin)
            await session.commit()
            
            return {
                "id": admin.id,
                "username": admin.username,
                "created_at": admin.created_at.isoformat()
            }
    
    async def get_admin_by_id(self, admin_id: str) -> Optional[Dict[str, Any]]:
        """
        根据 ID 获取管理员信息
        
        Args:
            admin_id: 管理员 ID
            
        Returns:
            管理员信息，如果不存在返回 None
        """
        session_factory = self._get_session_factory()
        async with session_factory() as session:
            result = await session.execute(
                select(Admin).where(Admin.id == admin_id)
            )
            admin = result.scalar_one_or_none()
            
            if admin is None:
                return None
            
            return {
                "id": admin.id,
                "username": admin.username,
                "created_at": admin.created_at.isoformat() if admin.created_at else None
            }
    
    async def change_password(
        self, 
        admin_id: str, 
        old_password: str, 
        new_password: str
    ) -> bool:
        """
        修改管理员密码
        
        Args:
            admin_id: 管理员 ID
            old_password: 旧密码
            new_password: 新密码
            
        Returns:
            是否修改成功
        """
        session_factory = self._get_session_factory()
        async with session_factory() as session:
            result = await session.execute(
                select(Admin).where(Admin.id == admin_id)
            )
            admin = result.scalar_one_or_none()
            
            if admin is None:
                return False
            
            # 验证旧密码
            if not self.verify_password(old_password, admin.password_hash):
                return False
            
            # 更新密码
            admin.password_hash = self.hash_password(new_password)
            await session.commit()
            
            return True
    
    async def ensure_default_admin(self) -> None:
        """
        确保默认管理员存在
        
        如果数据库中没有管理员，则创建默认管理员
        """
        session_factory = self._get_session_factory()
        async with session_factory() as session:
            result = await session.execute(select(Admin))
            if result.scalar_one_or_none() is None:
                # 创建默认管理员
                default_username = self._config.admin.username
                default_password = self._config.admin.password
                
                admin = Admin(
                    id=str(uuid.uuid4()),
                    username=default_username,
                    password_hash=self.hash_password(default_password),
                    created_at=datetime.utcnow()
                )
                session.add(admin)
                await session.commit()


# 全局认证服务实例
_auth_service: Optional[AuthService] = None


def get_auth_service() -> AuthService:
    """获取全局认证服务实例"""
    global _auth_service
    if _auth_service is None:
        _auth_service = AuthService()
    return _auth_service


def init_auth_service(session_factory=None) -> AuthService:
    """
    初始化全局认证服务
    
    Args:
        session_factory: SQLAlchemy 异步会话工厂
        
    Returns:
        认证服务实例
    """
    global _auth_service
    _auth_service = AuthService(session_factory)
    return _auth_service
