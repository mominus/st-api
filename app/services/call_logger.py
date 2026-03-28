"""
调用日志服务

记录 API 调用的详细信息，包括输入输出预览和费用计算
"""

import uuid
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional, List, Dict, Any

from sqlalchemy import select, desc, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import CallLog, get_session_factory
from app.services.pricing import get_pricing_service

logger = logging.getLogger(__name__)

MAX_CALL_LOG_ENTRIES = 100


def _normalize_cost(value: str) -> str:
    try:
        normalized = format(Decimal(str(value)).normalize(), "f")
    except (InvalidOperation, ValueError):
        return "0"

    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def _empty_model_usage() -> Dict[str, Any]:
    return {
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "tokens": 0,
        "cost": "0",
    }


def _get_log_field(log: Any, field_name: str, default: Any = None) -> Any:
    if hasattr(log, field_name):
        return getattr(log, field_name)

    mapping = getattr(log, "_mapping", None)
    if mapping is not None:
        return mapping.get(field_name, default)

    return default


def build_api_key_usage_by_model(
    logs: List[Any],
    allowed_models: Optional[List[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    usage_by_model: Dict[str, Dict[str, Any]] = {
        model_name: _empty_model_usage()
        for model_name in (allowed_models or [])
    }

    cost_totals: Dict[str, Decimal] = {
        model_name: Decimal("0")
        for model_name in usage_by_model
    }

    for log in logs:
        model_name = _get_log_field(log, "model_group") or _get_log_field(log, "model") or ""
        if not model_name:
            continue

        usage = usage_by_model.setdefault(model_name, _empty_model_usage())
        cost_totals.setdefault(model_name, Decimal("0"))

        input_tokens = _get_log_field(log, "input_tokens", 0) or 0
        output_tokens = _get_log_field(log, "output_tokens", 0) or 0
        total_tokens_value = _get_log_field(log, "total_tokens")
        total_tokens = total_tokens_value if total_tokens_value is not None else (input_tokens + output_tokens)

        usage["requests"] += 1
        usage["input_tokens"] += input_tokens
        usage["output_tokens"] += output_tokens
        usage["tokens"] += total_tokens

        total_cost = _get_log_field(log, "total_cost")
        if total_cost:
            try:
                cost_totals[model_name] += Decimal(str(total_cost))
            except (InvalidOperation, ValueError):
                logger.warning("Invalid call log cost for model usage aggregation: %s", total_cost)

    for model_name, usage in usage_by_model.items():
        usage["cost"] = _normalize_cost(str(cost_totals[model_name]))

    return usage_by_model


def truncate_text(text: str, max_length: int = 500) -> str:
    """截断文本"""
    if not text:
        return ""
    if len(text) <= max_length:
        return text
    return text[:max_length] + "..."


def truncate_to_lines(text: str, max_lines: int = 10, max_length: int = 500) -> str:
    """截断到指定行数"""
    if not text:
        return ""
    
    lines = text.split('\n')
    if len(lines) > max_lines:
        text = '\n'.join(lines[:max_lines]) + f"\n... (共 {len(lines)} 行)"
    
    return truncate_text(text, max_length)


def extract_input_preview(messages: list, api_type: str = "openai") -> str:
    """
    从消息列表提取输入预览（只取最后一条用户消息）
    
    Args:
        messages: 消息列表
        api_type: API 类型 (openai, anthropic, gemini)
    """
    if not messages:
        return ""
    
    try:
        # 从后往前找最后一条用户消息
        last_user_msg = None
        for msg in reversed(messages):
            role = msg.get("role", "")
            if role == "user":
                last_user_msg = msg
                break
        
        # 如果没有用户消息，取最后一条消息
        if not last_user_msg:
            last_user_msg = messages[-1] if messages else None
        
        if not last_user_msg:
            return ""
        
        # 提取内容
        if api_type in ("openai", "anthropic"):
            content = last_user_msg.get("content", "")
            if isinstance(content, list):
                # 处理多模态内容
                text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                content = " ".join(text_parts)
        elif api_type == "gemini":
            parts = last_user_msg.get("parts", [])
            text_parts = [p.get("text", "") for p in parts if isinstance(p, dict) and "text" in p]
            content = " ".join(text_parts)
        else:
            content = str(last_user_msg.get("content", ""))
        
        return truncate_text(str(content), 500)
    except Exception as e:
        logger.error(f"Error extracting input preview: {e}")
        return str(messages)[:500] if messages else ""


class CallLoggerService:
    """调用日志服务"""

    async def cleanup_excess_logs(
        self,
        session: AsyncSession,
        max_logs: int = MAX_CALL_LOG_ENTRIES,
    ) -> int:
        """
        自动清理超出限制的旧日志，仅保留最近 max_logs 条。
        """
        if max_logs <= 0:
            return 0

        keep_ids_result = await session.execute(
            select(CallLog.id)
            .order_by(desc(CallLog.timestamp), desc(CallLog.id))
            .limit(max_logs)
        )
        keep_ids = [row[0] for row in keep_ids_result.all()]

        if not keep_ids:
            return 0

        delete_query = delete(CallLog).where(CallLog.id.not_in(keep_ids))
        result = await session.execute(delete_query)

        deleted_count = result.rowcount if isinstance(result.rowcount, int) else 0
        if deleted_count < 0:
            deleted_count = 0

        if deleted_count:
            logger.info(
                "Auto cleaned up %s old call logs, kept latest %s",
                deleted_count,
                max_logs,
            )

        return deleted_count
    
    async def log_call(
        self,
        session: AsyncSession,
        *,
        # 调用方信息
        api_key_id: Optional[str] = None,
        api_key_name: Optional[str] = None,
        api_key_prefix: Optional[str] = None,
        client_ip: Optional[str] = None,
        # 账号信息
        account_id: Optional[str] = None,
        account_name: Optional[str] = None,
        model_group: Optional[str] = None,
        # 请求信息
        model: Optional[str] = None,
        api_type: str = "openai",
        is_stream: bool = False,
        input_preview: Optional[str] = None,
        output_preview: Optional[str] = None,
        # Token 统计
        input_tokens: int = 0,
        output_tokens: int = 0,
        # 响应信息
        response_time_ms: Optional[int] = None,
        status: str = "success",
        error_message: Optional[str] = None,
    ) -> Optional[CallLog]:
        """
        记录一次 API 调用
        """
        try:
            # 计算费用
            pricing_service = get_pricing_service()
            input_cost, output_cost, total_cost = pricing_service.calculate(
                model or "", input_tokens, output_tokens
            )
            
            log_entry = CallLog(
                id=str(uuid.uuid4()),
                timestamp=datetime.utcnow(),
                api_key_id=api_key_id,
                api_key_name=api_key_name,
                api_key_prefix=api_key_prefix,
                client_ip=client_ip,
                account_id=account_id,
                account_name=account_name,
                model_group=model_group,
                model=model,
                api_type=api_type,
                is_stream=is_stream,
                input_preview=truncate_text(input_preview, 500) if input_preview else None,
                output_preview=truncate_to_lines(output_preview, 10, 500) if output_preview else None,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                input_cost=input_cost,
                output_cost=output_cost,
                total_cost=total_cost,
                response_time_ms=response_time_ms,
                status=status,
                error_message=error_message,
            )
            
            session.add(log_entry)
            await session.flush()
            await self.cleanup_excess_logs(session, max_logs=MAX_CALL_LOG_ENTRIES)
            # 不在这里 commit，让调用方控制事务
            
            return log_entry
        except Exception as e:
            logger.error(f"Error logging call: {e}")
            return None
    
    async def get_recent_logs(
        self,
        session: AsyncSession,
        limit: int = 50,
        api_key_id: Optional[str] = None,
        account_id: Optional[str] = None,
        model_group: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[CallLog]:
        """
        获取最近的调用日志
        
        Args:
            session: 数据库会话
            limit: 返回数量限制
            api_key_id: 按 API Key 过滤
            account_id: 按账号过滤
            model_group: 按模型组过滤
            status: 按状态过滤
        """
        query = select(CallLog).order_by(desc(CallLog.timestamp))
        
        if api_key_id:
            query = query.where(CallLog.api_key_id == api_key_id)
        if account_id:
            query = query.where(CallLog.account_id == account_id)
        if model_group:
            query = query.where(CallLog.model_group == model_group)
        if status:
            query = query.where(CallLog.status == status)
        
        query = query.limit(limit)
        
        result = await session.execute(query)
        return list(result.scalars().all())
    
    async def get_log_stats(
        self,
        session: AsyncSession,
        hours: int = 24,
    ) -> dict:
        """
        获取调用日志统计
        """
        from datetime import timedelta
        from sqlalchemy import func
        
        since = datetime.utcnow() - timedelta(hours=hours)
        
        # 总调用数
        total_query = select(func.count(CallLog.id)).where(CallLog.timestamp >= since)
        total_result = await session.execute(total_query)
        total_calls = total_result.scalar() or 0
        
        # 成功/失败数
        success_query = select(func.count(CallLog.id)).where(
            CallLog.timestamp >= since,
            CallLog.status == "success"
        )
        success_result = await session.execute(success_query)
        success_calls = success_result.scalar() or 0
        
        # 总 Token
        token_query = select(
            func.sum(CallLog.input_tokens),
            func.sum(CallLog.output_tokens),
            func.sum(CallLog.total_tokens)
        ).where(CallLog.timestamp >= since)
        token_result = await session.execute(token_query)
        token_row = token_result.one()
        
        # 总费用
        from decimal import Decimal
        cost_query = select(CallLog.total_cost).where(
            CallLog.timestamp >= since,
            CallLog.total_cost.isnot(None)
        )
        cost_result = await session.execute(cost_query)
        total_cost = sum(
            Decimal(row[0]) for row in cost_result.all() if row[0]
        )
        
        return {
            "total_calls": total_calls,
            "success_calls": success_calls,
            "error_calls": total_calls - success_calls,
            "input_tokens": token_row[0] or 0,
            "output_tokens": token_row[1] or 0,
            "total_tokens": token_row[2] or 0,
            "total_cost": str(total_cost),
        }
    
    async def delete_logs_before_date(
        self,
        session: AsyncSession,
        before_date: datetime,
    ) -> int:
        """
        删除指定日期及之前的所有日志
        
        Args:
            session: 数据库会话
            before_date: 删除该日期及之前的日志（包含当天）
            
        Returns:
            删除的日志数量
        """
        from sqlalchemy import func
        
        # 先统计要删除的数量
        count_query = select(func.count(CallLog.id)).where(CallLog.timestamp <= before_date)
        count_result = await session.execute(count_query)
        count = count_result.scalar() or 0
        
        if count > 0:
            # 执行删除
            delete_query = delete(CallLog).where(CallLog.timestamp <= before_date)
            await session.execute(delete_query)
            logger.info(f"Deleted {count} call logs before {before_date}")
        
        return count
    
    async def get_total_cost(
        self,
        session: AsyncSession,
        hours: Optional[int] = None,
    ) -> dict:
        """
        获取总费用统计
        
        Args:
            session: 数据库会话
            hours: 统计时间范围（小时），None 表示全部
            
        Returns:
            费用统计字典，包含 total, input, output
        """
        from datetime import timedelta
        from decimal import Decimal
        from sqlalchemy import func
        
        query = select(
            CallLog.input_cost,
            CallLog.output_cost,
            CallLog.total_cost
        ).where(CallLog.total_cost.isnot(None))
        
        if hours is not None:
            since = datetime.utcnow() - timedelta(hours=hours)
            query = query.where(CallLog.timestamp >= since)
        
        result = await session.execute(query)
        rows = result.all()
        
        total_input = Decimal("0")
        total_output = Decimal("0")
        total = Decimal("0")
        
        for row in rows:
            if row[0]:
                total_input += Decimal(row[0])
            if row[1]:
                total_output += Decimal(row[1])
            if row[2]:
                total += Decimal(row[2])
        
        return {
            "input": str(total_input),
            "output": str(total_output),
            "total": str(total),
        }

    async def get_api_key_usage_by_model(
        self,
        session: AsyncSession,
        api_key_id: str,
        *,
        allowed_models: Optional[List[str]] = None,
        since: Optional[datetime] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """
        获取某个 API Key 的按模型拆分使用统计。
        """
        query = select(
            CallLog.model_group,
            CallLog.model,
            CallLog.input_tokens,
            CallLog.output_tokens,
            CallLog.total_tokens,
            CallLog.total_cost,
        ).where(
            CallLog.api_key_id == api_key_id,
            CallLog.status == "success",
        )

        if since is not None:
            query = query.where(CallLog.timestamp >= since)

        result = await session.execute(query.order_by(desc(CallLog.timestamp)))
        logs = list(result.all())
        return build_api_key_usage_by_model(logs, allowed_models=allowed_models)


# 单例
_call_logger_service: Optional[CallLoggerService] = None


def get_call_logger_service() -> CallLoggerService:
    """获取调用日志服务实例"""
    global _call_logger_service
    if _call_logger_service is None:
        _call_logger_service = CallLoggerService()
    return _call_logger_service
