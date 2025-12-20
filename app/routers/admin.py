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
    token_quota: Optional[int] = Field(None, description="Token 额度限制，NULL 表示无限制")
    cost_limit: Optional[float] = Field(None, description="费用限制（美元），NULL 表示无限制")
    expires_at: Optional[datetime] = Field(None, description="过期时间")


class UpdateAPIKeyRequest(BaseModel):
    """更新 API Key 请求"""
    name: Optional[str] = Field(None, description="Key 名称")
    model_groups: Optional[List[str]] = Field(None, description="授权的模型组列表")
    request_quota: Optional[int] = Field(None, description="请求数配额，-1 表示清除限制")
    token_quota: Optional[int] = Field(None, description="Token 额度限制，-1 表示清除限制")
    cost_limit: Optional[float] = Field(None, description="费用限制（美元），-1 表示清除限制")
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
    from app.services.pricing import get_pricing_service
    
    result = await session.execute(select(ModelGroup))
    groups = list(result.scalars().all())
    
    # 获取每个组的账号统计
    account_pool = get_account_pool_service()
    pricing_service = get_pricing_service()
    
    group_list = []
    for group in groups:
        accounts = await account_pool.get_accounts_by_model_group(session, group.name)
        active_count = sum(1 for a in accounts if a.status == "active")
        
        # 计算可用额度（所有活跃账号的剩余额度总和）
        total_quota = 0
        total_used = 0
        for acc in accounts:
            if acc.status == "active":
                total_quota += acc.daily_quota
                total_used += acc.daily_used
        available_quota = max(0, total_quota - total_used)
        
        # 获取模型定价
        pricing = pricing_service.get_pricing(group.name)
        
        group_list.append({
            "id": group.id,
            "name": group.name,
            "description": group.description,
            "input_mapping": json.loads(group.input_mapping) if group.input_mapping else {},
            "account_count": len(accounts),
            "active_account_count": active_count,
            "pricing": {
                "input": pricing["input"],
                "output": pricing["output"]
            },
            "quota": {
                "total": total_quota,
                "used": total_used,
                "available": available_quota
            },
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
            "token_quota": key.token_quota,
            "cost_limit": key.cost_limit,
            "expires_at": key.expires_at.isoformat() if key.expires_at else None,
            "status": key.status,
            "created_at": key.created_at.isoformat() if key.created_at else None,
            "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
            "total_requests": key.total_requests or 0,
            "total_tokens": key.total_tokens or 0,
            "total_cost": key.total_cost or "0"
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
            "key_suffix": key.key_suffix or "****",
            "name": key.name,
            "model_groups": model_groups,
            "request_quota": key.request_quota,
            "token_quota": key.token_quota,
            "cost_limit": key.cost_limit,
            "expires_at": key.expires_at.isoformat() if key.expires_at else None,
            "status": key.status,
            "created_at": key.created_at.isoformat() if key.created_at else None,
            "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
            "total_requests": key.total_requests or 0,
            "total_tokens": key.total_tokens or 0,
            "total_cost": key.total_cost or "0"
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
    
    # 处理费用限制
    cost_limit_str = None
    if request.cost_limit is not None:
        cost_limit_str = str(request.cost_limit)
    
    raw_key, api_key = await api_key_service.generate_key(
        session=session,
        model_groups=request.model_groups,
        name=request.name,
        expires_at=request.expires_at,
        request_quota=request.request_quota,
        token_quota=request.token_quota,
        cost_limit=cost_limit_str
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
            "token_quota": api_key.token_quota,
            "cost_limit": api_key.cost_limit,
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
    
    # 处理费用限制：-1 表示清除限制，None 表示不更新
    cost_limit_str = None
    if request.cost_limit is not None:
        if request.cost_limit == -1:
            cost_limit_str = ""  # 空字符串表示清除限制
        else:
            cost_limit_str = str(request.cost_limit)
    
    key = await api_key_service.update_key(
        session=session,
        key_id=key_id,
        name=request.name,
        model_groups=request.model_groups,
        request_quota=request.request_quota,
        token_quota=request.token_quota,
        cost_limit=cost_limit_str,
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
            "token_quota": key.token_quota,
            "cost_limit": key.cost_limit,
            "total_requests": key.total_requests or 0,
            "total_tokens": key.total_tokens or 0,
            "total_cost": key.total_cost or "0",
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
    from app.services.call_logger import get_call_logger_service
    
    stats_service = get_stats_service()
    overview = await stats_service.get_system_overview(session)
    
    # 获取费用统计
    call_logger = get_call_logger_service()
    today_cost = await call_logger.get_total_cost(session, hours=24)
    all_time_cost = await call_logger.get_total_cost(session, hours=None)
    
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
                "output_tokens": overview.today_output_tokens,
                "cost": today_cost
            },
            "all_time": {
                "requests": overview.all_time_requests,
                "total_tokens": overview.all_time_tokens,
                "input_tokens": overview.all_time_input_tokens,
                "output_tokens": overview.all_time_output_tokens,
                "cost": all_time_cost
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


# ============================================================================
# Call Logs Endpoint (调用日志)
# ============================================================================

@router.get("/logs/calls")
async def get_call_logs(
    limit: int = Query(50, ge=1, le=200, description="返回数量"),
    api_key_id: Optional[str] = Query(None, description="按 API Key ID 过滤"),
    account_id: Optional[str] = Query(None, description="按账号 ID 过滤"),
    model_group: Optional[str] = Query(None, description="按模型组过滤"),
    status: Optional[str] = Query(None, description="按状态过滤 (success/error)"),
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    获取最近的调用日志
    
    返回最新的 API 调用记录，包括输入输出预览和费用信息。
    """
    from app.services.call_logger import get_call_logger_service
    
    call_logger = get_call_logger_service()
    logs = await call_logger.get_recent_logs(
        session,
        limit=limit,
        api_key_id=api_key_id,
        account_id=account_id,
        model_group=model_group,
        status=status,
    )
    
    result = []
    for log in logs:
        result.append({
            "id": log.id,
            "timestamp": log.timestamp.isoformat() if log.timestamp else None,
            "api_key": {
                "id": log.api_key_id,
                "name": log.api_key_name,
                "prefix": log.api_key_prefix,
            },
            "account": {
                "id": log.account_id,
                "name": log.account_name,
                "model_group": log.model_group,
            },
            "request": {
                "model": log.model,
                "api_type": log.api_type,
                "is_stream": log.is_stream,
            },
            "input_preview": log.input_preview,
            "output_preview": log.output_preview,
            "tokens": {
                "input": log.input_tokens,
                "output": log.output_tokens,
                "total": log.total_tokens,
            },
            "cost": {
                "input": log.input_cost,
                "output": log.output_cost,
                "total": log.total_cost,
            },
            "response_time_ms": log.response_time_ms,
            "status": log.status,
            "error_message": log.error_message,
            "client_ip": log.client_ip,
        })
    
    return {"success": True, "logs": result, "total": len(result)}


@router.get("/logs/stats")
async def get_call_log_stats(
    hours: int = Query(24, ge=1, le=168, description="统计时间范围（小时）"),
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    获取调用日志统计
    
    返回指定时间范围内的调用统计信息。
    """
    from app.services.call_logger import get_call_logger_service
    from app.services.pricing import format_cost_display
    
    call_logger = get_call_logger_service()
    stats = await call_logger.get_log_stats(session, hours=hours)
    
    return {
        "success": True,
        "stats": {
            "hours": hours,
            "total_calls": stats["total_calls"],
            "success_calls": stats["success_calls"],
            "error_calls": stats["error_calls"],
            "success_rate": round(stats["success_calls"] / stats["total_calls"] * 100, 1) if stats["total_calls"] > 0 else 0,
            "tokens": {
                "input": stats["input_tokens"],
                "output": stats["output_tokens"],
                "total": stats["total_tokens"],
            },
            "cost": {
                "total": stats["total_cost"],
                "display": format_cost_display(stats["total_cost"]),
            },
        }
    }


@router.get("/pricing")
async def get_model_pricing(
    admin: dict = Depends(verify_admin_token)
):
    """
    获取所有模型的定价信息
    """
    from app.services.pricing import get_pricing_service
    
    pricing_service = get_pricing_service()
    pricing = pricing_service.get_all_pricing()
    
    return {
        "success": True,
        "pricing": pricing,
        "unit": "USD per 1M tokens"
    }


class DeleteLogsRequest(BaseModel):
    """删除日志请求"""
    before_date: str = Field(..., description="删除该日期及之前的日志 (YYYY-MM-DD)")


@router.delete("/logs/calls")
async def delete_call_logs(
    request: DeleteLogsRequest,
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    按日期删除调用日志
    
    删除指定日期及之前的所有调用日志。此操作不可恢复！
    """
    from datetime import datetime as dt
    from app.services.call_logger import get_call_logger_service
    
    try:
        # 解析日期，设置为当天 23:59:59
        date_obj = dt.strptime(request.before_date, "%Y-%m-%d")
        before_datetime = date_obj.replace(hour=23, minute=59, second=59)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
    
    call_logger = get_call_logger_service()
    deleted_count = await call_logger.delete_logs_before_date(session, before_datetime)
    
    await session.commit()
    
    logger.info(f"Deleted {deleted_count} call logs before {request.before_date} by {admin.get('username')}")
    
    return {
        "success": True,
        "message": f"成功删除 {deleted_count} 条日志",
        "deleted_count": deleted_count,
        "before_date": request.before_date
    }


# ============================================================================
# API Key Cost Recalculation
# ============================================================================

@router.post("/keys/{key_id}/recalculate-cost")
async def recalculate_api_key_cost(
    key_id: str,
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    追溯计算 API Key 的费用
    
    根据调用日志重新计算该 Key 从创建日期开始的所有费用。
    """
    from decimal import Decimal
    from app.models.database import CallLog, APIKey
    from app.services.pricing import get_pricing_service
    
    # 获取 API Key
    api_key_service = get_api_key_service()
    api_key = await api_key_service.get_key_by_id(session, key_id)
    
    if not api_key:
        raise HTTPException(status_code=404, detail="API Key not found")
    
    # 获取该 Key 创建日期之后的所有调用日志
    created_at = api_key.created_at
    
    result = await session.execute(
        select(CallLog).where(
            CallLog.api_key_id == key_id,
            CallLog.timestamp >= created_at,
            CallLog.status == "success"
        )
    )
    logs = list(result.scalars().all())
    
    # 计算费用
    pricing_service = get_pricing_service()
    total_cost = Decimal("0")
    total_requests = 0
    total_tokens = 0
    
    for log in logs:
        input_tokens = log.input_tokens or 0
        output_tokens = log.output_tokens or 0
        model = log.model or log.model_group or ""
        
        _, _, cost = pricing_service.calculate(model, input_tokens, output_tokens)
        total_cost += Decimal(cost)
        total_requests += 1
        total_tokens += input_tokens + output_tokens
    
    # 更新 API Key 的统计
    api_key.total_cost = str(total_cost)
    api_key.total_requests = total_requests
    api_key.total_tokens = total_tokens
    
    await session.commit()
    
    logger.info(
        f"Recalculated cost for API Key {key_id}: "
        f"requests={total_requests}, tokens={total_tokens}, cost=${total_cost}"
    )
    
    return {
        "success": True,
        "message": f"费用重新计算完成",
        "key_id": key_id,
        "logs_processed": len(logs),
        "total_requests": total_requests,
        "total_tokens": total_tokens,
        "total_cost": str(total_cost)
    }


@router.post("/keys/recalculate-all-costs")
async def recalculate_all_api_key_costs(
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    追溯计算所有 API Key 的费用
    
    根据调用日志重新计算所有 Key 从各自创建日期开始的费用。
    """
    from decimal import Decimal
    from app.models.database import CallLog, APIKey
    from app.services.pricing import get_pricing_service
    
    # 获取所有 API Key
    api_key_service = get_api_key_service()
    keys = await api_key_service.get_all_keys(session, include_revoked=True)
    
    pricing_service = get_pricing_service()
    results = []
    
    for api_key in keys:
        # 获取该 Key 创建日期之后的所有调用日志
        created_at = api_key.created_at
        
        result = await session.execute(
            select(CallLog).where(
                CallLog.api_key_id == api_key.id,
                CallLog.timestamp >= created_at,
                CallLog.status == "success"
            )
        )
        logs = list(result.scalars().all())
        
        # 计算费用
        total_cost = Decimal("0")
        total_requests = 0
        total_tokens = 0
        
        for log in logs:
            input_tokens = log.input_tokens or 0
            output_tokens = log.output_tokens or 0
            model = log.model or log.model_group or ""
            
            _, _, cost = pricing_service.calculate(model, input_tokens, output_tokens)
            total_cost += Decimal(cost)
            total_requests += 1
            total_tokens += input_tokens + output_tokens
        
        # 更新 API Key 的统计
        api_key.total_cost = str(total_cost)
        api_key.total_requests = total_requests
        api_key.total_tokens = total_tokens
        
        results.append({
            "key_id": api_key.id,
            "key_name": api_key.name,
            "logs_processed": len(logs),
            "total_requests": total_requests,
            "total_tokens": total_tokens,
            "total_cost": str(total_cost)
        })
    
    await session.commit()
    
    logger.info(f"Recalculated costs for {len(keys)} API Keys")
    
    return {
        "success": True,
        "message": f"已重新计算 {len(keys)} 个 API Key 的费用",
        "keys_processed": len(keys),
        "results": results
    }


# ============================================================================
# Performance Monitoring Endpoints
# ============================================================================

class PerformanceConfigRequest(BaseModel):
    """性能配置请求"""
    rpm_limit: Optional[int] = Field(None, description="每分钟请求数限制，0 表示无限制")
    max_concurrent_requests: Optional[int] = Field(None, description="最大并发请求数")
    max_concurrent_db_ops: Optional[int] = Field(None, description="最大数据库并发操作数")
    http_max_connections: Optional[int] = Field(None, description="HTTP 连接池大小")
    http_timeout: Optional[float] = Field(None, description="HTTP 请求超时（秒）")


@router.get("/performance/stats")
async def get_performance_stats(
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    获取性能统计信息
    
    返回当前的并发状态、QPS、响应时间等指标
    """
    import os
    import time
    from app.services.connection_pool import (
        get_concurrency_limiter, 
        ConnectionPoolConfig
    )
    
    limiter = get_concurrency_limiter()
    stats = limiter.get_stats()
    
    # 获取系统配置
    db_url = os.getenv("DATABASE_URL", "sqlite")
    db_type = "PostgreSQL" if "postgresql" in db_url else "SQLite"
    
    # 计算服务运行时间
    start_time = getattr(get_performance_stats, '_start_time', None)
    if start_time is None:
        get_performance_stats._start_time = time.time()
        start_time = get_performance_stats._start_time
    
    uptime_seconds = int(time.time() - start_time)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{hours}h {minutes}m {seconds}s"
    
    # 计算实时 QPS（基于最近的请求统计）
    total_requests = stats.get("total_requests", 0)
    current_qps = total_requests / max(uptime_seconds, 1)
    
    # 计算成功率
    rejected = stats.get("rejected_requests", 0)
    success_rate = ((total_requests - rejected) / max(total_requests, 1)) * 100
    
    # 最大可支持的 RPM（基于最大并发数和平均响应时间估算）
    # 假设平均响应时间 1 秒，最大 RPM = 最大并发数 * 60
    max_rpm = ConnectionPoolConfig.MAX_CONCURRENT_REQUESTS * 60
    
    return {
        "success": True,
        "stats": {
            "active_requests": stats.get("active_requests", 0),
            "total_requests": total_requests,
            "rejected_requests": rejected,
            "current_qps": round(current_qps, 2),
            "success_rate": round(success_rate, 2),
            "max_concurrent_requests": ConnectionPoolConfig.MAX_CONCURRENT_REQUESTS,
            "max_concurrent_db_ops": ConnectionPoolConfig.MAX_CONCURRENT_DB_OPS,
        },
        "config": {
            "rpm_limit": int(os.getenv("RPM_LIMIT", "0")),  # 0 表示无限制
            "max_rpm": max_rpm,
            "max_concurrent_requests": ConnectionPoolConfig.MAX_CONCURRENT_REQUESTS,
            "max_concurrent_db_ops": ConnectionPoolConfig.MAX_CONCURRENT_DB_OPS,
            "http_max_connections": ConnectionPoolConfig.HTTP_MAX_CONNECTIONS,
            "http_max_keepalive": ConnectionPoolConfig.HTTP_MAX_KEEPALIVE,
            "http_timeout": ConnectionPoolConfig.HTTP_TIMEOUT,
            "http_connect_timeout": ConnectionPoolConfig.HTTP_CONNECT_TIMEOUT,
            "db_pool_size": int(os.getenv("DB_POOL_SIZE", "20")),
            "db_max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "30")),
        },
        "system": {
            "db_type": db_type,
            "db_pool": f"{os.getenv('DB_POOL_SIZE', '20')} + {os.getenv('DB_MAX_OVERFLOW', '30')}",
            "http_timeout": f"{ConnectionPoolConfig.HTTP_TIMEOUT}s",
            "uptime": uptime_str,
        },
        "response_times": {
            "avg": stats.get("avg_response_time", 0),
            "p95": stats.get("p95_response_time", 0),
            "p99": stats.get("p99_response_time", 0),
        }
    }


@router.post("/performance/config")
async def update_performance_config(
    request: PerformanceConfigRequest,
    admin: dict = Depends(verify_admin_token),
    session: AsyncSession = Depends(get_session)
):
    """
    更新性能配置
    
    注意：配置更改需要重启服务才能生效
    """
    import os
    from pathlib import Path
    
    # 读取现有的 .env 文件
    env_path = Path(".env")
    env_content = {}
    
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    env_content[key.strip()] = value.strip()
    
    # 更新配置
    updated = []
    
    if request.rpm_limit is not None:
        env_content["RPM_LIMIT"] = str(request.rpm_limit)
        updated.append(f"RPM_LIMIT={request.rpm_limit}")
    
    if request.max_concurrent_requests is not None:
        env_content["MAX_CONCURRENT_REQUESTS"] = str(request.max_concurrent_requests)
        updated.append(f"MAX_CONCURRENT_REQUESTS={request.max_concurrent_requests}")
    
    if request.max_concurrent_db_ops is not None:
        env_content["MAX_CONCURRENT_DB_OPS"] = str(request.max_concurrent_db_ops)
        updated.append(f"MAX_CONCURRENT_DB_OPS={request.max_concurrent_db_ops}")
    
    if request.http_max_connections is not None:
        env_content["HTTP_MAX_CONNECTIONS"] = str(request.http_max_connections)
        updated.append(f"HTTP_MAX_CONNECTIONS={request.http_max_connections}")
    
    if request.http_timeout is not None:
        env_content["HTTP_TIMEOUT"] = str(request.http_timeout)
        updated.append(f"HTTP_TIMEOUT={request.http_timeout}")
    
    # 写回 .env 文件
    if updated:
        # 保留原有的注释和格式，只更新值
        lines = []
        if env_path.exists():
            with open(env_path, "r", encoding="utf-8") as f:
                original_lines = f.readlines()
            
            updated_keys = set()
            for line in original_lines:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    key = stripped.split("=", 1)[0].strip()
                    if key in env_content:
                        lines.append(f"{key}={env_content[key]}\n")
                        updated_keys.add(key)
                    else:
                        lines.append(line)
                else:
                    lines.append(line)
            
            # 添加新的配置项
            for key, value in env_content.items():
                if key not in updated_keys:
                    lines.append(f"{key}={value}\n")
        else:
            for key, value in env_content.items():
                lines.append(f"{key}={value}\n")
        
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        
        logger.info(f"Performance config updated by {admin.get('username')}: {updated}")
    
    return {
        "success": True,
        "message": "配置已保存，重启服务后生效",
        "updated": updated,
        "restart_required": True
    }


@router.post("/performance/reset-stats")
async def reset_performance_stats(
    admin: dict = Depends(verify_admin_token)
):
    """
    重置性能统计
    """
    from app.services.connection_pool import get_concurrency_limiter
    
    limiter = get_concurrency_limiter()
    limiter.reset_stats()
    
    logger.info(f"Performance stats reset by {admin.get('username')}")
    
    return {
        "success": True,
        "message": "统计已重置"
    }
