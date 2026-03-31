"""
Configuration Management Service
加载环境变量和配置，支持配置热更新

Requirements: 9.1, 9.2
"""

import os
import json
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any, List, Callable
from dataclasses import dataclass, field
from datetime import datetime
import logging

from app.services.time_utils import utc_now

logger = logging.getLogger(__name__)


@dataclass
class ServerConfig:
    """服务器配置"""
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False


@dataclass
class DatabaseConfig:
    """数据库配置"""
    url: str = "sqlite+aiosqlite:///./data/api_service.db"


@dataclass
class SecurityConfig:
    """安全配置"""
    jwt_secret_key: str = ""
    jwt_expire_hours: int = 24
    encryption_key: str = ""


@dataclass
class AdminConfig:
    """管理员配置"""
    username: str = "admin"
    password: str = "admin123"


@dataclass
class LoginProtectionConfig:
    """登录保护配置"""
    max_attempts: int = 5
    lockout_minutes: int = 15


@dataclass
class LogConfig:
    """日志配置"""
    level: str = "INFO"
    file: str = "./data/api_service.log"


@dataclass
class CorsConfig:
    """CORS 配置"""
    origins: List[str] = field(default_factory=lambda: ["*"])


@dataclass
class AppConfig:
    """应用配置"""
    server: ServerConfig = field(default_factory=ServerConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    admin: AdminConfig = field(default_factory=AdminConfig)
    login_protection: LoginProtectionConfig = field(default_factory=LoginProtectionConfig)
    log: LogConfig = field(default_factory=LogConfig)
    cors: CorsConfig = field(default_factory=CorsConfig)
    
    # 配置文件路径
    config_file_path: Optional[str] = None
    
    # 最后加载时间
    last_loaded_at: Optional[datetime] = None


class ConfigService:
    """
    配置管理服务
    
    支持从环境变量和配置文件加载配置，并提供配置热更新功能
    """
    
    def __init__(self, config_file_path: Optional[str] = None):
        """
        初始化配置服务
        
        Args:
            config_file_path: 配置文件路径，如果不提供则使用默认路径
        """
        self._config: AppConfig = AppConfig()
        self._config_file_path = config_file_path or os.getenv(
            "CONFIG_FILE", 
            "./data/config.json"
        )
        self._change_callbacks: List[Callable[[AppConfig], None]] = []
        self._file_watch_task: Optional[asyncio.Task] = None
        self._last_file_mtime: Optional[float] = None
        
    @property
    def config(self) -> AppConfig:
        """获取当前配置"""
        return self._config
    
    @property
    def server(self) -> ServerConfig:
        """获取服务器配置"""
        return self._config.server
    
    @property
    def database(self) -> DatabaseConfig:
        """获取数据库配置"""
        return self._config.database
    
    @property
    def security(self) -> SecurityConfig:
        """获取安全配置"""
        return self._config.security
    
    @property
    def admin(self) -> AdminConfig:
        """获取管理员配置"""
        return self._config.admin
    
    @property
    def login_protection(self) -> LoginProtectionConfig:
        """获取登录保护配置"""
        return self._config.login_protection
    
    @property
    def log(self) -> LogConfig:
        """获取日志配置"""
        return self._config.log
    
    @property
    def cors(self) -> CorsConfig:
        """获取 CORS 配置"""
        return self._config.cors

    def _parse_jwt_expire_hours(self, value: Any) -> int:
        """解析并校验 JWT 过期小时数，非法值回退到 24 小时。"""
        try:
            hours = int(value)
        except (TypeError, ValueError):
            logger.warning(
                f"Invalid JWT_EXPIRE_HOURS value '{value}', fallback to 24"
            )
            return 24

        if hours <= 0:
            logger.warning(
                f"JWT_EXPIRE_HOURS must be > 0, got {hours}, fallback to 24"
            )
            return 24

        return hours
    
    def load_from_env(self) -> None:
        """从环境变量加载配置"""
        # 服务器配置
        self._config.server.host = os.getenv("HOST", "0.0.0.0")
        self._config.server.port = int(os.getenv("PORT", "8000"))
        self._config.server.debug = os.getenv("DEBUG", "false").lower() == "true"
        
        # 数据库配置
        self._config.database.url = os.getenv(
            "DATABASE_URL", 
            "sqlite+aiosqlite:///./data/api_service.db"
        )
        
        # 安全配置
        self._config.security.jwt_secret_key = os.getenv(
            "JWT_SECRET_KEY", 
            "your-super-secret-jwt-key-change-this-in-production"
        )
        self._config.security.jwt_expire_hours = self._parse_jwt_expire_hours(
            os.getenv("JWT_EXPIRE_HOURS", "24")
        )
        self._config.security.encryption_key = os.getenv(
            "ENCRYPTION_KEY", 
            ""
        )
        
        # 管理员配置
        self._config.admin.username = os.getenv("ADMIN_USERNAME", "admin")
        self._config.admin.password = os.getenv("ADMIN_PASSWORD", "admin123")
        
        # 登录保护配置
        self._config.login_protection.max_attempts = int(
            os.getenv("MAX_LOGIN_ATTEMPTS", "5")
        )
        self._config.login_protection.lockout_minutes = int(
            os.getenv("LOGIN_LOCKOUT_MINUTES", "15")
        )
        
        # 日志配置
        self._config.log.level = os.getenv("LOG_LEVEL", "INFO")
        self._config.log.file = os.getenv("LOG_FILE", "./data/api_service.log")
        
        # CORS 配置
        cors_origins = os.getenv("CORS_ORIGINS", "*")
        if cors_origins == "*":
            self._config.cors.origins = ["*"]
        else:
            self._config.cors.origins = [
                origin.strip() for origin in cors_origins.split(",")
            ]
        
        self._config.last_loaded_at = utc_now()
        logger.info("Configuration loaded from environment variables")
    
    def load_from_file(self, file_path: Optional[str] = None) -> bool:
        """
        从配置文件加载配置
        
        Args:
            file_path: 配置文件路径，如果不提供则使用默认路径
            
        Returns:
            是否成功加载
        """
        path = Path(file_path or self._config_file_path)
        
        if not path.exists():
            logger.debug(f"Config file not found: {path}")
            return False
        
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            self._apply_config_dict(data)
            self._config.config_file_path = str(path)
            self._config.last_loaded_at = utc_now()
            self._last_file_mtime = path.stat().st_mtime
            
            logger.info(f"Configuration loaded from file: {path}")
            return True
            
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in config file: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to load config file: {e}")
            return False
    
    def _apply_config_dict(self, data: Dict[str, Any]) -> None:
        """应用配置字典到当前配置"""
        # 服务器配置
        if "server" in data:
            server = data["server"]
            if "host" in server:
                self._config.server.host = server["host"]
            if "port" in server:
                self._config.server.port = int(server["port"])
            if "debug" in server:
                self._config.server.debug = bool(server["debug"])
        
        # 数据库配置
        if "database" in data:
            db = data["database"]
            if "url" in db:
                self._config.database.url = db["url"]
        
        # 安全配置
        if "security" in data:
            sec = data["security"]
            if "jwt_secret_key" in sec:
                self._config.security.jwt_secret_key = sec["jwt_secret_key"]
            if "jwt_expire_hours" in sec:
                self._config.security.jwt_expire_hours = self._parse_jwt_expire_hours(
                    sec["jwt_expire_hours"]
                )
            if "encryption_key" in sec:
                self._config.security.encryption_key = sec["encryption_key"]
        
        # 管理员配置
        if "admin" in data:
            admin = data["admin"]
            if "username" in admin:
                self._config.admin.username = admin["username"]
            if "password" in admin:
                self._config.admin.password = admin["password"]
        
        # 登录保护配置
        if "login_protection" in data:
            lp = data["login_protection"]
            if "max_attempts" in lp:
                self._config.login_protection.max_attempts = int(lp["max_attempts"])
            if "lockout_minutes" in lp:
                self._config.login_protection.lockout_minutes = int(lp["lockout_minutes"])
        
        # 日志配置
        if "log" in data:
            log = data["log"]
            if "level" in log:
                self._config.log.level = log["level"]
            if "file" in log:
                self._config.log.file = log["file"]
        
        # CORS 配置
        if "cors" in data:
            cors = data["cors"]
            if "origins" in cors:
                origins = cors["origins"]
                if isinstance(origins, str):
                    if origins == "*":
                        self._config.cors.origins = ["*"]
                    else:
                        self._config.cors.origins = [
                            o.strip() for o in origins.split(",")
                        ]
                elif isinstance(origins, list):
                    self._config.cors.origins = origins
    
    def save_to_file(self, file_path: Optional[str] = None) -> bool:
        """
        保存配置到文件
        
        Args:
            file_path: 配置文件路径，如果不提供则使用默认路径
            
        Returns:
            是否成功保存
        """
        path = Path(file_path or self._config_file_path)
        
        # 确保目录存在
        path.parent.mkdir(parents=True, exist_ok=True)
        
        try:
            data = self.to_dict()
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            
            self._config.config_file_path = str(path)
            self._last_file_mtime = path.stat().st_mtime
            
            logger.info(f"Configuration saved to file: {path}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to save config file: {e}")
            return False
    
    def to_dict(self) -> Dict[str, Any]:
        """将配置转换为字典（不包含敏感信息的完整值）"""
        return {
            "server": {
                "host": self._config.server.host,
                "port": self._config.server.port,
                "debug": self._config.server.debug,
            },
            "database": {
                "url": self._config.database.url,
            },
            "security": {
                "jwt_secret_key": self._config.security.jwt_secret_key,
                "jwt_expire_hours": self._config.security.jwt_expire_hours,
                "encryption_key": self._config.security.encryption_key,
            },
            "admin": {
                "username": self._config.admin.username,
                "password": self._config.admin.password,
            },
            "login_protection": {
                "max_attempts": self._config.login_protection.max_attempts,
                "lockout_minutes": self._config.login_protection.lockout_minutes,
            },
            "log": {
                "level": self._config.log.level,
                "file": self._config.log.file,
            },
            "cors": {
                "origins": self._config.cors.origins,
            },
        }
    
    def register_change_callback(self, callback: Callable[[AppConfig], None]) -> None:
        """
        注册配置变更回调
        
        Args:
            callback: 配置变更时调用的回调函数
        """
        self._change_callbacks.append(callback)
    
    def unregister_change_callback(self, callback: Callable[[AppConfig], None]) -> None:
        """
        取消注册配置变更回调
        
        Args:
            callback: 要取消的回调函数
        """
        if callback in self._change_callbacks:
            self._change_callbacks.remove(callback)
    
    def _notify_change(self) -> None:
        """通知所有回调配置已变更"""
        for callback in self._change_callbacks:
            try:
                callback(self._config)
            except Exception as e:
                logger.error(f"Config change callback error: {e}")
    
    async def start_file_watch(self, interval: float = 5.0) -> None:
        """
        启动配置文件监控
        
        Args:
            interval: 检查间隔（秒）
        """
        if self._file_watch_task is not None:
            return
        
        self._file_watch_task = asyncio.create_task(
            self._watch_config_file(interval)
        )
        logger.info(f"Started config file watch with {interval}s interval")
    
    async def stop_file_watch(self) -> None:
        """停止配置文件监控"""
        if self._file_watch_task is not None:
            self._file_watch_task.cancel()
            try:
                await self._file_watch_task
            except asyncio.CancelledError:
                pass
            self._file_watch_task = None
            logger.info("Stopped config file watch")
    
    async def _watch_config_file(self, interval: float) -> None:
        """监控配置文件变更"""
        while True:
            try:
                await asyncio.sleep(interval)
                
                path = Path(self._config_file_path)
                if not path.exists():
                    continue
                
                current_mtime = path.stat().st_mtime
                if self._last_file_mtime is None:
                    self._last_file_mtime = current_mtime
                    continue
                
                if current_mtime > self._last_file_mtime:
                    logger.info("Config file changed, reloading...")
                    if self.load_from_file():
                        self._notify_change()
                        
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error watching config file: {e}")
    
    def reload(self) -> bool:
        """
        重新加载配置
        
        先从环境变量加载，然后从配置文件覆盖
        
        Returns:
            是否成功重新加载
        """
        self.load_from_env()
        file_loaded = self.load_from_file()
        self._notify_change()
        return True


# 全局配置服务实例
_config_service: Optional[ConfigService] = None


def get_config_service() -> ConfigService:
    """获取全局配置服务实例"""
    global _config_service
    if _config_service is None:
        _config_service = ConfigService()
        _config_service.load_from_env()
        _config_service.load_from_file()
    return _config_service


def init_config_service(config_file_path: Optional[str] = None) -> ConfigService:
    """
    初始化全局配置服务
    
    Args:
        config_file_path: 配置文件路径
        
    Returns:
        配置服务实例
    """
    global _config_service
    _config_service = ConfigService(config_file_path)
    _config_service.load_from_env()
    _config_service.load_from_file()
    return _config_service


def get_config() -> AppConfig:
    """获取当前应用配置的快捷方法"""
    return get_config_service().config
