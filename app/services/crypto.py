"""
Crypto Service
提供加密/解密和密码哈希功能

Requirements: 9.4
"""

import os
import base64
import secrets
from typing import Optional

import bcrypt
from cryptography.fernet import Fernet, InvalidToken


class CryptoService:
    """加密服务类，提供 Fernet 对称加密和 bcrypt 密码哈希"""
    
    def __init__(self, encryption_key: Optional[str] = None):
        """
        初始化加密服务
        
        Args:
            encryption_key: Fernet 加密密钥，如果不提供则从环境变量获取
        """
        self._encryption_key = encryption_key or os.getenv("ENCRYPTION_KEY")
        self._fernet: Optional[Fernet] = None
        
        if self._encryption_key:
            self._init_fernet()
    
    def _init_fernet(self) -> None:
        """初始化 Fernet 实例"""
        try:
            # 尝试直接使用密钥
            key = self._encryption_key.encode() if isinstance(self._encryption_key, str) else self._encryption_key
            self._fernet = Fernet(key)
        except Exception:
            # 如果密钥格式不正确，尝试进行 base64 编码
            try:
                # 确保密钥是 32 字节，然后进行 base64 编码
                key_bytes = self._encryption_key.encode() if isinstance(self._encryption_key, str) else self._encryption_key
                if len(key_bytes) < 32:
                    key_bytes = key_bytes.ljust(32, b'\0')
                elif len(key_bytes) > 32:
                    key_bytes = key_bytes[:32]
                encoded_key = base64.urlsafe_b64encode(key_bytes)
                self._fernet = Fernet(encoded_key)
            except Exception as e:
                raise ValueError(f"Invalid encryption key: {e}")

    
    @staticmethod
    def generate_encryption_key() -> str:
        """
        生成新的 Fernet 加密密钥
        
        Returns:
            Base64 编码的 Fernet 密钥
        """
        return Fernet.generate_key().decode()
    
    def encrypt(self, plaintext: str) -> str:
        """
        加密明文字符串
        
        Args:
            plaintext: 要加密的明文
            
        Returns:
            Base64 编码的密文
            
        Raises:
            RuntimeError: 如果加密服务未正确初始化
        """
        if self._fernet is None:
            raise RuntimeError("Encryption service not initialized. Set ENCRYPTION_KEY.")
        
        plaintext_bytes = plaintext.encode('utf-8')
        encrypted_bytes = self._fernet.encrypt(plaintext_bytes)
        return encrypted_bytes.decode('utf-8')
    
    def decrypt(self, ciphertext: str) -> str:
        """
        解密密文字符串
        
        Args:
            ciphertext: Base64 编码的密文
            
        Returns:
            解密后的明文
            
        Raises:
            RuntimeError: 如果加密服务未正确初始化
            ValueError: 如果解密失败（密钥错误或数据损坏）
        """
        if self._fernet is None:
            raise RuntimeError("Encryption service not initialized. Set ENCRYPTION_KEY.")
        
        try:
            ciphertext_bytes = ciphertext.encode('utf-8')
            decrypted_bytes = self._fernet.decrypt(ciphertext_bytes)
            return decrypted_bytes.decode('utf-8')
        except InvalidToken:
            raise ValueError("Decryption failed: invalid token or corrupted data")

    
    @staticmethod
    def hash_password(password: str) -> str:
        """
        使用 bcrypt 哈希密码
        
        Args:
            password: 明文密码
            
        Returns:
            bcrypt 哈希值
            
        Note:
            bcrypt has a 72-byte limit for passwords. Longer passwords are truncated.
        """
        password_bytes = password.encode('utf-8')
        # bcrypt has a 72-byte limit, truncate if necessary
        if len(password_bytes) > 72:
            password_bytes = password_bytes[:72]
        salt = bcrypt.gensalt(rounds=12)
        hashed = bcrypt.hashpw(password_bytes, salt)
        return hashed.decode('utf-8')
    
    @staticmethod
    def verify_password(password: str, password_hash: str) -> bool:
        """
        验证密码是否匹配哈希值
        
        Args:
            password: 明文密码
            password_hash: bcrypt 哈希值
            
        Returns:
            密码是否匹配
            
        Note:
            bcrypt has a 72-byte limit for passwords. Longer passwords are truncated.
        """
        try:
            password_bytes = password.encode('utf-8')
            # bcrypt has a 72-byte limit, truncate if necessary
            if len(password_bytes) > 72:
                password_bytes = password_bytes[:72]
            hash_bytes = password_hash.encode('utf-8')
            return bcrypt.checkpw(password_bytes, hash_bytes)
        except Exception:
            return False
    
    @staticmethod
    def generate_api_key(prefix: str = "sk-") -> str:
        """
        生成随机 API Key
        
        Args:
            prefix: Key 前缀，默认为 "sk-"
            
        Returns:
            格式为 "{prefix}{random_string}" 的 API Key
        """
        # 生成 32 字节的随机数据，转换为 URL 安全的 base64
        random_bytes = secrets.token_bytes(32)
        random_str = base64.urlsafe_b64encode(random_bytes).decode('utf-8').rstrip('=')
        return f"{prefix}{random_str}"
    
    @staticmethod
    def hash_api_key(api_key: str) -> str:
        """
        对 API Key 进行哈希（用于存储）
        
        Args:
            api_key: 原始 API Key
            
        Returns:
            SHA-256 哈希值的十六进制表示
        """
        import hashlib
        return hashlib.sha256(api_key.encode('utf-8')).hexdigest()
    
    @staticmethod
    def mask_api_key(api_key: str) -> str:
        """
        对 API Key 进行脱敏显示
        
        Args:
            api_key: 原始 API Key
            
        Returns:
            脱敏后的 Key，格式为 "sk-xxx...xxx"（显示前7位和后4位）
        """
        if len(api_key) <= 11:
            return api_key[:3] + "***"
        return api_key[:7] + "..." + api_key[-4:]


# 全局加密服务实例
_crypto_service: Optional[CryptoService] = None


def get_crypto_service() -> CryptoService:
    """获取全局加密服务实例"""
    global _crypto_service
    if _crypto_service is None:
        _crypto_service = CryptoService()
    return _crypto_service


def init_crypto_service(encryption_key: Optional[str] = None) -> CryptoService:
    """初始化全局加密服务"""
    global _crypto_service
    _crypto_service = CryptoService(encryption_key)
    return _crypto_service
