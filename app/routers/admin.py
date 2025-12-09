"""
Admin API Router
管理后台 API 路由，提供认证、账号管理、API Key 管理、统计功能

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 5.1, 5.4, 5.5, 5.6, 6.1, 6.5, 3.2, 3.3
"""

import json
import logging
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Request, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.database import get_session, ModelGroup, BackendAccount
from app.services.auth import (
    AuthService, get_auth_service,
    InvalidCredentialsError, AccountLockedError, TokenExpiredError, InvalidTokenError
)
from app.services.account_pool import AccountPoolService, get_account_pool_service
from app.services.api_key import APIKeyService, get_api_key_service
from app.services.stats import StatsService, get_stats_service
from app.services.analytics import AnalyticsService, get_analytics_service

logger = logging.getLogger(__name__)

# 创建路由器
router = APIRouter(prefix="/api/admin", tags=["Admin"])


# ============================================================================
# Request/Response Models
# ============================================================================

# Auth Models
class LoginRequest(BaseModel):
    """登录请求"""
    username: str = Field(..., description="用户名")
    password: str = Field(..., description="密码")


class LoginResponse(BaseModel):
    """登录响应"""
    success: bool
    token: Optional[str] = None
    admin: Optional[dict] = None
    message: Optional[str] = None


class RefreshTokenRequest(BaseModel):
    """刷新 Token 请求"""
    token: str = Field(..., description="当前 Token")


class ChangePasswordRequest(BaseModel):
    """修改密码请求"""
    old_password: str = Field(..., description="旧密码")
    new_password: str = Field(..., description="新密码")


# Account Models
class CreateAccountRequest(BaseModel):
    """创建账号请求"""
    name: str = Field(..., description="账号名称")
    org_id: str = Field(..., description="组织 ID")
    flow_id: str = Field(..., description="工作流 ID")
    api_key: str = Field(..., description="API Key")
    model_group: str = Field(..., description="所属模型组")
    daily_quota: int = Field(default=1000000, description="每日 Token 配额")
    private_api_key: Optional[str] = Field(None, description="Private API Key（用于监控）")


class UpdateAccountRequest(BaseModel):
    """更新账号请求"""
    name: Optional[str] = None
    org_id: Optional[str] = None
    flow_id: Optional[str] = None
    api_key: Optional[str] = None
    model_group: Optional[str] = None
    daily_quota: Optional[int] = None
    status: Optional[str] = None
    private_api_key: Optional[str] = None


# Model Group Models
class CreateModelGroupRequest(BaseModel):
    """创建模型组请求"""
    name: str = Field(..., description="模型组名称")
    description: Optional[str] = Field(None, description="描述")
    input_mapping: dict = Field(..., description="输入字段映射配置")


class UpdateModelGroupRequest(BaseModel):
    """更新模型组请求"""
    description: Optional[str] = None
    input_mapping: Optional[dict] = None


# API Key Models
class CreateAPIKeyRequest(BaseModel):
    """创建 API Key 请求"""
    name: Optional[str] = Field(None, description="Key 名称")
    model_groups: List[str] = Field(..., description="授权的模型组列表")
    request_quota: Optional[int] = Field(None, description="请求数配额，NULL 表示无限制")
    expires_at: Optional[datetime] = Field(None, description="过期时间")


class UpdateAPIKeyRequest(BaseModel):
    """更新 API Key 请求"""
    name: Optional[str] = Field(None, description="Key 名称")
    model_groups: Optional[List[str]] = Field(None, description="授权的模型组列表")
    request_quota: Optional[int] = Field(None, description="请求数配额，-1 表示清除限制")
    expires_at: Optional[datetime] = Field(None, description="过期时间")


# ============================================================================
# Helper Functions
# ============================================================================

def get_client_ip(request: Request) -> str:
    """获取客户端 IP"""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def verify_admin_token(
    authorization: Optional[str] = Header(None)
) -> dict:
    """
    验证管理员 Token
    
    从 Authorization header 提取并验证 JWT Token
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authorization header")
    
    # 提取 Token
    token = authorization.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")
    
    # 验证 Token
    auth_service = get_auth_service()
    try:
        payload = auth_service.verify_token(token)
        return payload
    except TokenExpiredError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


# ============================================================================
# Authentication Endpoints (Requirement 4.1, 4.2, 4.3)
# ============================================================================

@router.post("/auth/login", response_model=LoginResponse)
async def login(
    request: LoginRequest,
    http_request: Request,
    session: AsyncSession = Depends(get_session)
):
    """
    管理员登录
    
    验证用户名和密码，成功后返回 JWT Token。
    连续失败 5 次将锁定 IP 15 分钟。
    
    Requirements: 4.1, 4.2, 4.3
    """
    auth_service = get_auth_service()
    client_ip = get_client_ip(http_request)
    
    try:
        token, admin_info = await auth_service.authenticate(
            username=request.username,
            password=request.password,
            ip=client_ip
        )
        
        logger.info(f"Admin login successful: {request.username} from {client_ip}")
        
        return LoginResponse(
            success=True,
            token=token,
            admin=admin_info,
            message="Login successful"
        )
        
    except AccountLockedError as e:
        logger.warning(f"Login blocked - IP locked: {client_ip}")
        return JSONResponse(
            status_code=429,
            content={
                "success": False,
                "message": f"Account locked until {e.locked_until.isoformat()}",
                "locked_until": e.locked_until.isoformat()
            }
        )
        
    except InvalidCredentialsError:
        # 获取剩余尝试次数
        remaining = await auth_service.get_remaining_attempts(client_ip)
        logger.warning(f"Login failed for {request.username} from {client_ip}, {remaining} attempts remaining")
        
        return JSONResponse(
            status_code=401,
            content={
                "success": False,
                "message": "Invalid username or password",
                "remaining_attempts": remaining
            }
        )


@router.post("/auth/logout")
async def logout(
    admin: dict = Depends(verify_admin_token)
):
    """
    管理员登出
    
    客户端应删除本地存储的 Token。
    服务端目前不维护 Token 黑名单。
    
    Requirements: 4.1
    """
    logger.info(f"Admin logout: {admin.get('username')}")
    return {"success": True, "message": "Logout successful"}


@router.post("/auth/refresh")
async def refresh_token(
    request: RefreshTokenRequest
):
    """
    刷新 JWT Token
    
    使用当前有效的 Token 获取新的 Token。
    
    Requirements: 4.2
    """
    auth_service = get_auth_service()
    
    try:
        new_token = auth_service.refresh_token(request.token)
        return {
            "success": True,
            "token": new_token,
            "message": "Token refreshed"
        }
    except TokenExpiredError:
        return JSONResponse(
            status_code=401,
            content={"success": False, "message": "Token has expired, please login again"}
        )
    except InvalidTokenError:
        return JSONResponse(
            status_code=401,
            content={"success": False, "message": "Invalid token"}
        )


@router.get("/auth/me")
async def get_current_admin(
    admin: dict = Depends(verify_admin_token)
):
    """
    获取当前登录的管理员信息
    """
    auth_service = get_auth_service()
    admin_info = await auth_service.get_admin_by_id(admin["sub"])
    
    if not admin_info:
        raise HTTPException(status_code=404, detail="Admin not found")
    
    return {"success": True, "admin": admin_info}


@router.post("/auth/change-password")
async def change_password(
    request: ChangePasswordRequest,
    admin: dict = Depends(verify_admin_token)
):
    """
    修改管理员密码
    """
    auth_service = get_auth_service()
    
    success = await auth_service.change_password(
        admin_id=admin["sub"],
        old_password=request.old_password,
        new_password=request.new_password
    )
    
    if not success:
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "Invalid old password"}
        )
    
    logger.info(f"Password changed for admin: {admin.get('username')}")
    return {"success": True, "message": "Password changed successfully"}



# ============================================================================
# Account Management Endpoints (Requirement 4.5, 4.6, 4.7, 4.8)
# ============================================================================

def mask_api_key(encrypted_key: str) -> str:
    """
    对 API Key 进行脱敏处理
    解密后显示前8位和后4位，中间用 * 替代
    """
    try:
        from app.services.crypto import get_crypto_service
        crypto = get_crypto_service()
        decrypted = crypto.decrypt(encrypted_key)
        if len(decrypted) <= 12:
            return decrypted[:3] + "***" + decrypted[-2:] if len(decrypted) > 5 else "***"
        return decrypted[:8] + "****" + decrypted[-4:]
    except Exception:
        return "***"


@router.get("/accounts")
async def list_accounts(
    model_group: Optional[str] = Query(None, description="按模型组过滤"),
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    获取所有后端账号列表
    
    Requirements: 4.5
    """
    account_pool = get_account_pool_service()
    
    if model_group:
        accounts = await account_pool.get_accounts_by_model_group(session, model_group)
    else:
        accounts = await account_pool.get_all_accounts(session)
    
    # 转换为响应格式（包含脱敏的 API Key）
    result = []
    for account in accounts:
        usage_percentage = account_pool.get_usage_percentage(account)
        usage_status = account_pool.get_usage_status(account)
        
        # 获取脱敏的 API Key
        api_key_masked = mask_api_key(account.api_key_encrypted)
        
        result.append({
            "id": account.id,
            "name": account.name,
            "org_id": account.org_id,
            "flow_id": account.flow_id,
            "model_group": account.model_group,
            "api_key_masked": api_key_masked,
            "has_private_key": account.private_api_key_encrypted is not None,
            "daily_quota": account.daily_quota,
            "daily_used": account.daily_used,
            "usage_percentage": round(usage_percentage, 2),
            "usage_status": usage_status,
            "status": account.status,
            "last_used_at": account.last_used_at.isoformat() if account.last_used_at else None,
            "last_sync_at": account.last_sync_at.isoformat() if account.last_sync_at else None,
            "created_at": account.created_at.isoformat() if account.created_at else None,
            "updated_at": account.updated_at.isoformat() if account.updated_at else None
        })
    
    return {"success": True, "accounts": result, "total": len(result)}


@router.get("/accounts/{account_id}")
async def get_account(
    account_id: str,
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    获取单个账号详情
    
    Requirements: 4.5
    """
    account_pool = get_account_pool_service()
    account = await account_pool.get_account(session, account_id)
    
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    usage_percentage = account_pool.get_usage_percentage(account)
    usage_status = account_pool.get_usage_status(account)
    
    # 获取脱敏的 API Key
    api_key_masked = mask_api_key(account.api_key_encrypted)
    
    return {
        "success": True,
        "account": {
            "id": account.id,
            "name": account.name,
            "org_id": account.org_id,
            "flow_id": account.flow_id,
            "model_group": account.model_group,
            "api_key_masked": api_key_masked,
            "has_private_key": account.private_api_key_encrypted is not None,
            "daily_quota": account.daily_quota,
            "daily_used": account.daily_used,
            "usage_percentage": round(usage_percentage, 2),
            "usage_status": usage_status,
            "status": account.status,
            "last_used_at": account.last_used_at.isoformat() if account.last_used_at else None,
            "last_sync_at": account.last_sync_at.isoformat() if account.last_sync_at else None,
            "created_at": account.created_at.isoformat() if account.created_at else None,
            "updated_at": account.updated_at.isoformat() if account.updated_at else None
        }
    }


@router.post("/accounts")
async def create_account(
    request: CreateAccountRequest,
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    创建新的后端账号
    
    Requirements: 4.6
    """
    account_pool = get_account_pool_service()
    
    try:
        account = await account_pool.create_account(
            session=session,
            name=request.name,
            org_id=request.org_id,
            flow_id=request.flow_id,
            api_key=request.api_key,
            model_group=request.model_group,
            daily_quota=request.daily_quota,
            private_api_key=request.private_api_key
        )
        await session.commit()
        
        logger.info(f"Account created: {account.id} by {admin.get('username')}")
        
        return {
            "success": True,
            "message": "Account created successfully",
            "account": {
                "id": account.id,
                "name": account.name,
                "org_id": account.org_id,
                "flow_id": account.flow_id,
                "model_group": account.model_group,
                "daily_quota": account.daily_quota,
                "status": account.status,
                "has_private_key": account.private_api_key_encrypted is not None
            }
        }
    except ValueError as e:
        # 业务逻辑错误，可以返回具体信息
        logger.warning(f"Failed to create account (validation): {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # 其他异常，不暴露详细信息
        logger.error(f"Failed to create account: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create account")


@router.put("/accounts/{account_id}")
async def update_account(
    account_id: str,
    request: UpdateAccountRequest,
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    更新账号信息
    
    Requirements: 4.7
    """
    account_pool = get_account_pool_service()
    
    # 构建更新字段
    update_data = {}
    if request.name is not None:
        update_data["name"] = request.name
    if request.org_id is not None:
        update_data["org_id"] = request.org_id
    if request.flow_id is not None:
        update_data["flow_id"] = request.flow_id
    if request.api_key is not None:
        update_data["api_key"] = request.api_key
    if request.model_group is not None:
        update_data["model_group"] = request.model_group
    if request.daily_quota is not None:
        update_data["daily_quota"] = request.daily_quota
    if request.status is not None:
        update_data["status"] = request.status
    if request.private_api_key is not None:
        update_data["private_api_key"] = request.private_api_key if request.private_api_key else None
    
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update")
    
    account = await account_pool.update_account(session, account_id, **update_data)
    
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    await session.commit()
    
    logger.info(f"Account updated: {account_id} by {admin.get('username')}")
    
    return {
        "success": True,
        "message": "Account updated successfully",
        "account": {
            "id": account.id,
            "name": account.name,
            "org_id": account.org_id,
            "flow_id": account.flow_id,
            "model_group": account.model_group,
            "daily_quota": account.daily_quota,
            "status": account.status,
            "has_private_key": account.private_api_key_encrypted is not None
        }
    }


@router.delete("/accounts/{account_id}")
async def delete_account(
    account_id: str,
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    删除账号
    
    从账号池中移除该账号，历史数据保留。
    
    Requirements: 4.8
    """
    account_pool = get_account_pool_service()
    
    success = await account_pool.delete_account(session, account_id)
    
    if not success:
        raise HTTPException(status_code=404, detail="Account not found")
    
    await session.commit()
    
    logger.info(f"Account deleted: {account_id} by {admin.get('username')}")
    
    return {"success": True, "message": "Account deleted successfully"}


@router.post("/accounts/{account_id}/reset-usage")
async def reset_account_usage(
    account_id: str,
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    重置账号的每日使用量
    """
    account_pool = get_account_pool_service()
    
    success = await account_pool.reset_account_daily_usage(session, account_id)
    
    if not success:
        raise HTTPException(status_code=404, detail="Account not found")
    
    await session.commit()
    
    logger.info(f"Account usage reset: {account_id} by {admin.get('username')}")
    
    return {"success": True, "message": "Account usage reset successfully"}


@router.get("/accounts/{account_id}/analytics")
async def get_account_analytics(
    account_id: str,
    days: int = Query(7, ge=1, le=30, description="统计天数"),
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    获取账号的 Analytics 数据
    
    需要账号配置了 Private API Key
    """
    account_pool = get_account_pool_service()
    analytics_service = get_analytics_service()
    
    account = await account_pool.get_account(session, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    # 检查是否配置了 Private API Key
    private_key = account_pool.decrypt_private_api_key(account)
    if not private_key:
        return {
            "success": False,
            "error": "Private API Key not configured",
            "message": "请配置 Private API Key 以启用监控功能"
        }
    
    # 获取统计数据
    stats = await analytics_service.get_recent_stats(
        org_id=account.org_id,
        flow_id=account.flow_id,
        private_api_key=private_key,
        days=days
    )
    
    if stats is None:
        return {
            "success": False,
            "error": "Failed to fetch analytics",
            "message": "无法获取分析数据，请检查 Private API Key 是否有效"
        }
    
    # 更新账号的同步时间
    account.last_sync_at = datetime.utcnow()
    await session.commit()
    
    return {
        "success": True,
        "account_id": account_id,
        "account_name": account.name,
        "days": days,
        "stats": {
            "total_runs": stats.total_runs,
            "successful_runs": stats.successful_runs,
            "failed_runs": stats.failed_runs,
            "success_rate": stats.success_rate,
            "average_latency": stats.average_latency,
            "total_tokens": stats.total_tokens,
            "average_tokens": stats.average_tokens,
            "today_tokens": stats.today_tokens,
            "today_runs": stats.today_runs
        },
        "quota": {
            "daily_quota": account.daily_quota,
            "daily_used": account.daily_used,
            "remaining": max(0, account.daily_quota - account.daily_used),
            "usage_percentage": round(account.daily_used / account.daily_quota * 100, 2) if account.daily_quota > 0 else 0
        },
        "last_sync_at": account.last_sync_at.isoformat() if account.last_sync_at else None
    }


@router.post("/accounts/{account_id}/sync")
async def sync_account_usage(
    account_id: str,
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    同步账号的今日使用量（从 Analytics 获取）
    
    需要账号配置了 Private API Key
    """
    account_pool = get_account_pool_service()
    analytics_service = get_analytics_service()
    
    account = await account_pool.get_account(session, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    # 检查是否配置了 Private API Key
    private_key = account_pool.decrypt_private_api_key(account)
    if not private_key:
        return {
            "success": False,
            "error": "Private API Key not configured",
            "message": "请配置 Private API Key 以启用同步功能"
        }
    
    # 获取最近统计数据（包含总使用量）
    stats = await analytics_service.get_recent_stats(
        org_id=account.org_id,
        flow_id=account.flow_id,
        private_api_key=private_key,
        days=30
    )
    
    if stats is None:
        return {
            "success": False,
            "error": "Failed to fetch analytics",
            "message": "无法获取分析数据，请检查 Private API Key 是否有效"
        }
    
    # 更新账号的使用量
    old_used = account.daily_used
    account.daily_used = stats.today_tokens
    account.last_sync_at = datetime.utcnow()
    
    # 检查是否达到配额
    if account.daily_used >= account.daily_quota:
        account.status = "exhausted"
    elif account.status == "exhausted":
        account.status = "active"
    
    await session.commit()
    
    logger.info(f"Account {account_id} usage synced: {old_used} -> {stats.today_tokens}")
    
    return {
        "success": True,
        "message": "Usage synced successfully",
        "account_id": account_id,
        "previous_used": old_used,
        "current_used": account.daily_used,
        "today_runs": stats.today_runs,
        "total_tokens": stats.total_tokens,
        "status": account.status
    }


@router.post("/accounts/{account_id}/verify-private-key")
async def verify_private_api_key(
    account_id: str,
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    验证账号的 Private API Key 是否有效
    """
    account_pool = get_account_pool_service()
    analytics_service = get_analytics_service()
    
    account = await account_pool.get_account(session, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    private_key = account_pool.decrypt_private_api_key(account)
    if not private_key:
        return {
            "success": False,
            "valid": False,
            "message": "Private API Key not configured"
        }
    
    is_valid = await analytics_service.verify_private_api_key(
        org_id=account.org_id,
        flow_id=account.flow_id,
        private_api_key=private_key
    )
    
    return {
        "success": True,
        "valid": is_valid,
        "message": "Private API Key is valid" if is_valid else "Private API Key is invalid"
    }


# ============================================================================
# Model Group Management Endpoints (Requirement 6.1, 6.5)
# ============================================================================

@router.get("/groups")
async def list_model_groups(
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    获取所有模型组列表
    
    Requirements: 6.1
    """
    result = await session.execute(select(ModelGroup))
    groups = list(result.scalars().all())
    
    # 获取每个组的账号统计
    account_pool = get_account_pool_service()
    
    group_list = []
    for group in groups:
        accounts = await account_pool.get_accounts_by_model_group(session, group.name)
        active_count = sum(1 for a in accounts if a.status == "active")
        
        group_list.append({
            "id": group.id,
            "name": group.name,
            "description": group.description,
            "input_mapping": json.loads(group.input_mapping) if group.input_mapping else {},
            "account_count": len(accounts),
            "active_account_count": active_count,
            "created_at": group.created_at.isoformat() if group.created_at else None
        })
    
    return {"success": True, "groups": group_list, "total": len(group_list)}


@router.get("/groups/{group_id}")
async def get_model_group(
    group_id: str,
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    获取单个模型组详情
    """
    result = await session.execute(
        select(ModelGroup).where(ModelGroup.id == group_id)
    )
    group = result.scalar_one_or_none()
    
    if not group:
        raise HTTPException(status_code=404, detail="Model group not found")
    
    # 获取该组的账号统计
    account_pool = get_account_pool_service()
    stats = await account_pool.get_model_group_stats(session, group.name)
    
    return {
        "success": True,
        "group": {
            "id": group.id,
            "name": group.name,
            "description": group.description,
            "input_mapping": json.loads(group.input_mapping) if group.input_mapping else {},
            "created_at": group.created_at.isoformat() if group.created_at else None,
            "stats": stats
        }
    }


@router.post("/groups")
async def create_model_group(
    request: CreateModelGroupRequest,
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    创建新的模型组
    
    Requirements: 6.1
    """
    import uuid
    
    # 检查名称是否已存在
    result = await session.execute(
        select(ModelGroup).where(ModelGroup.name == request.name)
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Model group name already exists")
    
    group = ModelGroup(
        id=str(uuid.uuid4()),
        name=request.name,
        description=request.description,
        input_mapping=json.dumps(request.input_mapping),
        created_at=datetime.utcnow()
    )
    
    session.add(group)
    await session.commit()
    
    logger.info(f"Model group created: {group.name} by {admin.get('username')}")
    
    return {
        "success": True,
        "message": "Model group created successfully",
        "group": {
            "id": group.id,
            "name": group.name,
            "description": group.description,
            "input_mapping": request.input_mapping
        }
    }


@router.put("/groups/{group_id}")
async def update_model_group(
    group_id: str,
    request: UpdateModelGroupRequest,
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    更新模型组配置
    
    Requirements: 6.5
    """
    result = await session.execute(
        select(ModelGroup).where(ModelGroup.id == group_id)
    )
    group = result.scalar_one_or_none()
    
    if not group:
        raise HTTPException(status_code=404, detail="Model group not found")
    
    if request.description is not None:
        group.description = request.description
    if request.input_mapping is not None:
        group.input_mapping = json.dumps(request.input_mapping)
    
    await session.commit()
    
    logger.info(f"Model group updated: {group.name} by {admin.get('username')}")
    
    return {
        "success": True,
        "message": "Model group updated successfully",
        "group": {
            "id": group.id,
            "name": group.name,
            "description": group.description,
            "input_mapping": json.loads(group.input_mapping) if group.input_mapping else {}
        }
    }


@router.delete("/groups/{group_id}")
async def delete_model_group(
    group_id: str,
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    删除模型组
    
    注意：删除前应确保没有账号关联到此模型组
    """
    result = await session.execute(
        select(ModelGroup).where(ModelGroup.id == group_id)
    )
    group = result.scalar_one_or_none()
    
    if not group:
        raise HTTPException(status_code=404, detail="Model group not found")
    
    # 检查是否有账号关联
    account_pool = get_account_pool_service()
    accounts = await account_pool.get_accounts_by_model_group(session, group.name)
    
    if accounts:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete model group with {len(accounts)} associated accounts"
        )
    
    await session.delete(group)
    await session.commit()
    
    logger.info(f"Model group deleted: {group.name} by {admin.get('username')}")
    
    return {"success": True, "message": "Model group deleted successfully"}



# ============================================================================
# API Key Management Endpoints (Requirement 5.1, 5.4, 5.5, 5.6)
# ============================================================================

@router.get("/keys")
async def list_api_keys(
    include_revoked: bool = Query(False, description="是否包含已撤销的 Key"),
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    获取所有 API Key 列表
    
    显示 Key 的部分内容（脱敏）、关联模型和状态。
    
    Requirements: 5.4, 5.5
    """
    api_key_service = get_api_key_service()
    keys = await api_key_service.get_all_keys(session, include_revoked=include_revoked)
    
    key_list = []
    
    for key in keys:
        model_groups = api_key_service.get_model_groups(key)
        display_key = api_key_service.get_display_key(key)
        
        key_list.append({
            "id": key.id,
            "display_key": display_key,
            "key_prefix": key.key_prefix,
            "key_suffix": key.key_suffix or "****",
            "name": key.name,
            "model_groups": model_groups,
            "request_quota": key.request_quota,
            "expires_at": key.expires_at.isoformat() if key.expires_at else None,
            "status": key.status,
            "created_at": key.created_at.isoformat() if key.created_at else None,
            "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
            "total_requests": key.total_requests or 0,
            "total_tokens": key.total_tokens or 0
        })
    
    return {"success": True, "keys": key_list, "total": len(key_list)}


@router.get("/keys/{key_id}")
async def get_api_key(
    key_id: str,
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    获取单个 API Key 详情
    """
    api_key_service = get_api_key_service()
    key = await api_key_service.get_key_by_id(session, key_id)
    
    if not key:
        raise HTTPException(status_code=404, detail="API Key not found")
    
    model_groups = api_key_service.get_model_groups(key)
    display_key = api_key_service.get_display_key(key)
    
    return {
        "success": True,
        "key": {
            "id": key.id,
            "display_key": display_key,
            "key_prefix": key.key_prefix,
            "name": key.name,
            "model_groups": model_groups,
            "expires_at": key.expires_at.isoformat() if key.expires_at else None,
            "status": key.status,
            "created_at": key.created_at.isoformat() if key.created_at else None,
            "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None
        }
    }


@router.post("/keys")
async def create_api_key(
    request: CreateAPIKeyRequest,
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    生成新的 API Key
    
    生成唯一的 API Key 并关联到指定的模型组。
    完整的 Key 值只在创建时返回一次，请妥善保存。
    
    Requirements: 5.1, 5.4
    """
    api_key_service = get_api_key_service()
    
    # 验证模型组是否存在
    for group_name in request.model_groups:
        result = await session.execute(
            select(ModelGroup).where(ModelGroup.name == group_name)
        )
        if not result.scalar_one_or_none():
            raise HTTPException(
                status_code=400,
                detail="Invalid model group specified"
            )
    
    raw_key, api_key = await api_key_service.generate_key(
        session=session,
        model_groups=request.model_groups,
        name=request.name,
        expires_at=request.expires_at,
        request_quota=request.request_quota
    )
    
    await session.commit()
    
    logger.info(f"API Key created: {api_key.id} by {admin.get('username')}")
    
    return {
        "success": True,
        "message": "API Key created successfully. Please save the key, it will only be shown once.",
        "key": raw_key,  # 完整的 Key 只在创建时返回
        "key_info": {
            "id": api_key.id,
            "key_prefix": api_key.key_prefix,
            "name": api_key.name,
            "model_groups": request.model_groups,
            "request_quota": api_key.request_quota,
            "expires_at": api_key.expires_at.isoformat() if api_key.expires_at else None
        }
    }


@router.put("/keys/{key_id}")
async def update_api_key(
    key_id: str,
    request: UpdateAPIKeyRequest,
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    更新 API Key 属性
    
    可更新名称、模型组、请求数配额、过期时间等。
    """
    api_key_service = get_api_key_service()
    
    # 验证模型组是否存在
    if request.model_groups:
        for group_name in request.model_groups:
            result = await session.execute(
                select(ModelGroup).where(ModelGroup.name == group_name)
            )
            if not result.scalar_one_or_none():
                raise HTTPException(
                    status_code=400,
                    detail="Invalid model group specified"
                )
    
    key = await api_key_service.update_key(
        session=session,
        key_id=key_id,
        name=request.name,
        model_groups=request.model_groups,
        request_quota=request.request_quota,
        expires_at=request.expires_at
    )
    
    if not key:
        raise HTTPException(status_code=404, detail="API Key not found")
    
    await session.commit()
    
    logger.info(f"API Key updated: {key_id} by {admin.get('username')}")
    
    model_groups = api_key_service.get_model_groups(key)
    
    return {
        "success": True,
        "message": "API Key updated successfully",
        "key": {
            "id": key.id,
            "name": key.name,
            "model_groups": model_groups,
            "request_quota": key.request_quota,
            "total_requests": key.total_requests or 0,
            "status": key.status,
            "expires_at": key.expires_at.isoformat() if key.expires_at else None
        }
    }


@router.delete("/keys/{key_id}")
async def delete_api_key(
    key_id: str,
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    永久删除 API Key
    
    从数据库中删除该 Key，此操作不可恢复。
    
    Requirements: 5.6
    """
    api_key_service = get_api_key_service()
    
    success = await api_key_service.delete_key(session, key_id)
    
    if not success:
        raise HTTPException(status_code=404, detail="API Key not found")
    
    await session.commit()
    
    logger.info(f"API Key deleted: {key_id} by {admin.get('username')}")
    
    return {"success": True, "message": "API Key deleted successfully"}


@router.post("/keys/{key_id}/enable")
async def enable_api_key(
    key_id: str,
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    启用已撤销的 API Key
    """
    api_key_service = get_api_key_service()
    
    success = await api_key_service.enable_key(session, key_id)
    
    if not success:
        raise HTTPException(status_code=404, detail="API Key not found")
    
    await session.commit()
    
    logger.info(f"API Key enabled: {key_id} by {admin.get('username')}")
    
    return {"success": True, "message": "API Key enabled successfully"}


@router.post("/keys/{key_id}/revoke")
async def revoke_api_key(
    key_id: str,
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    禁用 API Key（可恢复）
    """
    logger.info(f"Attempting to revoke API Key: {key_id}")
    api_key_service = get_api_key_service()
    
    success = await api_key_service.revoke_key(session, key_id)
    
    if not success:
        logger.warning(f"API Key not found for revoke: {key_id}")
        raise HTTPException(status_code=404, detail="API Key not found")
    
    await session.commit()
    
    logger.info(f"API Key revoked: {key_id} by {admin.get('username')}")
    
    return {"success": True, "message": "API Key revoked successfully"}


@router.put("/keys/{key_id}/model-groups")
async def update_api_key_model_groups(
    key_id: str,
    model_groups: List[str],
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    更新 API Key 的授权模型组
    """
    api_key_service = get_api_key_service()
    
    # 验证模型组是否存在
    for group_name in model_groups:
        result = await session.execute(
            select(ModelGroup).where(ModelGroup.name == group_name)
        )
        if not result.scalar_one_or_none():
            raise HTTPException(
                status_code=400,
                detail="Invalid model group specified"
            )
    
    key = await api_key_service.update_model_groups(session, key_id, model_groups)
    
    if not key:
        raise HTTPException(status_code=404, detail="API Key not found")
    
    await session.commit()
    
    logger.info(f"API Key model groups updated: {key_id} by {admin.get('username')}")
    
    return {
        "success": True,
        "message": "API Key model groups updated successfully",
        "model_groups": model_groups
    }



# ============================================================================
# Statistics Endpoints (Requirement 3.2, 3.3, 4.4)
# ============================================================================

@router.get("/stats/overview")
async def get_system_overview(
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    获取系统概览统计
    
    显示系统状态概览、账号状态和 Token 使用情况。
    
    Requirements: 4.4
    """
    stats_service = get_stats_service()
    overview = await stats_service.get_system_overview(session)
    
    return {
        "success": True,
        "overview": {
            "accounts": {
                "total": overview.total_accounts,
                "active": overview.active_accounts,
                "exhausted": overview.exhausted_accounts,
                "disabled": overview.disabled_accounts
            },
            "api_keys": {
                "total": overview.total_api_keys,
                "active": overview.active_api_keys
            },
            "model_groups": {
                "total": overview.total_model_groups
            },
            "today": {
                "requests": overview.today_requests,
                "total_tokens": overview.today_tokens,
                "input_tokens": overview.today_input_tokens,
                "output_tokens": overview.today_output_tokens
            },
            "all_time": {
                "requests": overview.all_time_requests,
                "total_tokens": overview.all_time_tokens,
                "input_tokens": overview.all_time_input_tokens,
                "output_tokens": overview.all_time_output_tokens
            }
        }
    }


@router.get("/stats/accounts")
async def get_all_account_stats(
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    获取所有账号的统计信息
    
    显示每个账号的当日已用 Token、剩余 Token 和使用百分比。
    
    Requirements: 3.2
    """
    stats_service = get_stats_service()
    account_stats = await stats_service.get_all_account_stats(session)
    
    result = []
    for stats in account_stats:
        result.append({
            "id": stats.id,
            "name": stats.name,
            "model_group": stats.model_group,
            "daily_quota": stats.daily_quota,
            "daily_used": stats.daily_used,
            "daily_remaining": max(0, stats.daily_quota - stats.daily_used),
            "usage_percentage": round(stats.usage_percentage, 2),
            "status": stats.status,
            "request_count": stats.request_count,
            "total_input_tokens": stats.total_input_tokens,
            "total_output_tokens": stats.total_output_tokens
        })
    
    return {"success": True, "accounts": result, "total": len(result)}


@router.get("/stats/accounts/{account_id}")
async def get_account_stats(
    account_id: str,
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    获取单个账号的统计信息
    
    Requirements: 3.2
    """
    stats_service = get_stats_service()
    stats = await stats_service.get_account_stats(session, account_id)
    
    if not stats:
        raise HTTPException(status_code=404, detail="Account not found")
    
    return {
        "success": True,
        "account": {
            "id": stats.id,
            "name": stats.name,
            "model_group": stats.model_group,
            "daily_quota": stats.daily_quota,
            "daily_used": stats.daily_used,
            "daily_remaining": max(0, stats.daily_quota - stats.daily_used),
            "usage_percentage": round(stats.usage_percentage, 2),
            "status": stats.status,
            "request_count": stats.request_count,
            "total_input_tokens": stats.total_input_tokens,
            "total_output_tokens": stats.total_output_tokens
        }
    }


@router.get("/stats/groups")
async def get_all_model_group_stats(
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    获取所有模型组的统计信息
    
    显示每个账号池的总体使用情况。
    
    Requirements: 3.3
    """
    stats_service = get_stats_service()
    group_stats = await stats_service.get_all_model_group_stats(session)
    
    result = []
    for stats in group_stats:
        result.append({
            "name": stats.name,
            "total_accounts": stats.total_accounts,
            "active_accounts": stats.active_accounts,
            "exhausted_accounts": stats.exhausted_accounts,
            "disabled_accounts": stats.disabled_accounts,
            "total_quota": stats.total_quota,
            "total_used": stats.total_used,
            "total_remaining": max(0, stats.total_quota - stats.total_used),
            "usage_percentage": round(stats.usage_percentage, 2),
            "request_count": stats.request_count
        })
    
    return {"success": True, "groups": result, "total": len(result)}


@router.get("/stats/groups/{group_name}")
async def get_model_group_stats(
    group_name: str,
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    获取单个模型组的统计信息
    
    Requirements: 3.3
    """
    stats_service = get_stats_service()
    stats = await stats_service.get_model_group_stats(session, group_name)
    
    if not stats:
        raise HTTPException(status_code=404, detail="Model group not found or has no accounts")
    
    return {
        "success": True,
        "group": {
            "name": stats.name,
            "total_accounts": stats.total_accounts,
            "active_accounts": stats.active_accounts,
            "exhausted_accounts": stats.exhausted_accounts,
            "disabled_accounts": stats.disabled_accounts,
            "total_quota": stats.total_quota,
            "total_used": stats.total_used,
            "total_remaining": max(0, stats.total_quota - stats.total_used),
            "usage_percentage": round(stats.usage_percentage, 2),
            "request_count": stats.request_count
        }
    }


@router.get("/stats/api-keys")
async def get_all_api_key_stats(
    include_revoked: bool = Query(False, description="是否包含已撤销的 Key"),
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    获取所有 API Key 的统计信息
    """
    stats_service = get_stats_service()
    key_stats = await stats_service.get_all_api_key_stats(session, include_revoked=include_revoked)
    
    result = []
    for stats in key_stats:
        result.append({
            "id": stats.id,
            "key_prefix": stats.key_prefix,
            "name": stats.name,
            "model_groups": stats.model_groups,
            "quota": stats.quota,
            "used": stats.used,
            "remaining": max(0, stats.quota - stats.used) if stats.quota else None,
            "usage_percentage": round(stats.usage_percentage, 2) if stats.usage_percentage else None,
            "status": stats.status,
            "request_count": stats.request_count,
            "total_input_tokens": stats.total_input_tokens,
            "total_output_tokens": stats.total_output_tokens
        })
    
    return {"success": True, "keys": result, "total": len(result)}


@router.get("/stats/history")
async def get_usage_history(
    days: int = Query(30, ge=1, le=365, description="查询天数"),
    account_id: Optional[str] = Query(None, description="按账号过滤"),
    api_key_id: Optional[str] = Query(None, description="按 API Key 过滤"),
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    获取历史使用统计
    
    提供最近 N 天的 Token 使用统计。
    
    Requirements: 3.6
    """
    stats_service = get_stats_service()
    history = await stats_service.get_usage_history(
        session,
        days=days,
        account_id=account_id,
        api_key_id=api_key_id
    )
    
    result = []
    for daily in history:
        result.append({
            "date": daily.date.isoformat(),
            "input_tokens": daily.input_tokens,
            "output_tokens": daily.output_tokens,
            "total_tokens": daily.total_tokens,
            "request_count": daily.request_count
        })
    
    return {"success": True, "history": result, "days": days}


# ============================================================================
# Test Endpoint (一键测试)
# ============================================================================

class TestAccountRequest(BaseModel):
    """测试账号请求"""
    message: str = Field(default="Hello, this is a test message.", description="测试消息")


@router.post("/accounts/{account_id}/test")
async def test_account(
    account_id: str,
    request: TestAccountRequest,
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    测试后端账号连接
    
    发送一个简单的测试请求到后端，验证账号配置是否正确。
    """
    import httpx
    import time
    
    account_pool = get_account_pool_service()
    account = await account_pool.get_account(session, account_id)
    
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    # 获取解密的 API Key
    from app.services.crypto import get_crypto_service
    try:
        crypto = get_crypto_service()
        api_key = crypto.decrypt(account.api_key_encrypted)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "message": f"Failed to decrypt API key: {str(e)}",
                "error_type": "decryption_error"
            }
        )
    
    # 获取模型组的输入映射
    result = await session.execute(
        select(ModelGroup).where(ModelGroup.name == account.model_group)
    )
    model_group = result.scalar_one_or_none()
    
    input_mapping = {"user_input": "in-0"}  # 默认映射
    if model_group and model_group.input_mapping:
        try:
            input_mapping = json.loads(model_group.input_mapping)
        except json.JSONDecodeError:
            pass
    
    # 构建后端请求
    import os
    backend_base = os.getenv("BACKEND_API_URL", "https://api.stack-ai.com")
    backend_url = f"{backend_base}/inference/v0/run/{account.org_id}/{account.flow_id}"
    
    # 构建输入字段
    input_fields = {}
    if "user_input" in input_mapping:
        input_fields[input_mapping["user_input"]] = request.message
    else:
        input_fields["in-0"] = request.message
    
    payload = {
        "user_id": "test-user",
        **input_fields
    }
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    start_time = time.time()
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(backend_url, json=payload, headers=headers)
            elapsed_ms = int((time.time() - start_time) * 1000)
            
            if response.status_code == 200:
                response_data = response.json()
                # 提取输出
                outputs = response_data.get("outputs", {})
                output_text = ""
                for key, value in outputs.items():
                    if isinstance(value, str):
                        output_text = value
                        break
                
                logger.info(f"Account test successful: {account_id}")
                
                return {
                    "success": True,
                    "message": "连接测试成功",
                    "response_time_ms": elapsed_ms,
                    "output": output_text[:500] if output_text else "(无输出)",
                    "raw_response": response_data
                }
            else:
                error_detail = response.text[:500]
                logger.warning(f"Account test failed: {account_id}, status: {response.status_code}")
                
                return JSONResponse(
                    status_code=response.status_code,
                    content={
                        "success": False,
                        "message": f"后端返回错误: {response.status_code}",
                        "response_time_ms": elapsed_ms,
                        "error_detail": error_detail
                    }
                )
                
    except httpx.TimeoutException:
        elapsed_ms = int((time.time() - start_time) * 1000)
        return JSONResponse(
            status_code=504,
            content={
                "success": False,
                "message": "请求超时 (30秒)",
                "response_time_ms": elapsed_ms,
                "error_type": "timeout"
            }
        )
    except httpx.RequestError as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        return JSONResponse(
            status_code=502,
            content={
                "success": False,
                "message": f"网络错误: {str(e)}",
                "response_time_ms": elapsed_ms,
                "error_type": "network_error"
            }
        )
    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.error(f"Account test error: {account_id}, error: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "message": f"测试失败: {str(e)}",
                "response_time_ms": elapsed_ms,
                "error_type": "unknown_error"
            }
        )
