"""
Unified gateway runtime.

Handles auth, account routing, backend payload construction, usage calculation,
and post-call persistence for all protocol adapters.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import APIKey, BackendAccount, ModelGroup
from app.services.account_pool import AccountPoolService, get_account_pool_service
from app.services.api_key import APIKeyService, get_api_key_service
from app.services.backend_client import (
    BackendClient,
    BackendClientError,
    get_backend_client,
)
from app.services.call_logger import CallLoggerService, get_call_logger_service
from app.services.error_handler import APIError, ErrorHandler, get_error_handler
from app.services.logger import LoggerService, get_logger_service
from app.services.pricing import PricingService, get_pricing_service
from app.services.protocol_bridge import UsageNumbers
from app.services.response_transformer import ResponseTransformer, get_response_transformer
from app.services.st_usage import STUsage, choose_better_usage, extract_run_id, extract_usage, split_total_with_fallback
from app.services.stats import StatsService, get_stats_service
from app.services.token_counter import TokenCounter, get_token_counter

logger = logging.getLogger(__name__)
ACCOUNT_SELECT_RETRY_DELAY_SECONDS = 0.08


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


ENABLE_REQUEST_LOG_PERSIST = _env_bool("ENABLE_REQUEST_LOG_PERSIST", True)
ENABLE_CALL_LOG_PERSIST = _env_bool("ENABLE_CALL_LOG_PERSIST", True)
ENABLE_SYSTEM_STATS_PERSIST = _env_bool("ENABLE_SYSTEM_STATS_PERSIST", True)
CALL_LOG_SAMPLE_RATE = min(
    1.0,
    max(0.0, float(os.getenv("CALL_LOG_SAMPLE_RATE", "1.0"))),
)


def _should_persist_call_log() -> bool:
    if not ENABLE_CALL_LOG_PERSIST:
        return False
    if CALL_LOG_SAMPLE_RATE >= 1.0:
        return True
    return random.random() < CALL_LOG_SAMPLE_RATE


@dataclass
class ResolvedRequest:
    request_id: str
    model: str
    api_key: APIKey
    account: BackendAccount
    input_mapping: Optional[Dict[str, str]]


class GatewayAuthError(Exception):
    def __init__(self, error: APIError):
        super().__init__(error.message)
        self.error = error


class GatewayRuntime:
    def __init__(
        self,
        *,
        api_key_service: Optional[APIKeyService] = None,
        account_pool: Optional[AccountPoolService] = None,
        backend_client: Optional[BackendClient] = None,
        error_handler: Optional[ErrorHandler] = None,
        response_transformer: Optional[ResponseTransformer] = None,
        token_counter: Optional[TokenCounter] = None,
        logger_service: Optional[LoggerService] = None,
        stats_service: Optional[StatsService] = None,
        call_logger: Optional[CallLoggerService] = None,
        pricing_service: Optional[PricingService] = None,
    ) -> None:
        self.api_key_service = api_key_service or get_api_key_service()
        self.account_pool = account_pool or get_account_pool_service()
        self.backend_client = backend_client or get_backend_client()
        self.error_handler = error_handler or get_error_handler()
        self.response_transformer = response_transformer or get_response_transformer()
        self.token_counter = token_counter or get_token_counter()
        self.logger_service = logger_service or get_logger_service()
        self.stats_service = stats_service or get_stats_service()
        self.call_logger = call_logger or get_call_logger_service()
        self.pricing_service = pricing_service or get_pricing_service()

    # ------------------------------------------------------------------
    # Auth + routing
    # ------------------------------------------------------------------

    async def resolve_request(
        self,
        session: AsyncSession,
        *,
        raw_key: Optional[str],
        model: str,
        request_id: Optional[str] = None,
    ) -> ResolvedRequest:
        if not raw_key:
            raise GatewayAuthError(
                self.error_handler.create_authentication_error(
                    "Missing API key"
                )
            )

        valid, error_msg, api_key_obj = await self.api_key_service.validate_key(
            session,
            raw_key,
            model,
        )
        if not valid or api_key_obj is None:
            raise GatewayAuthError(self._map_validation_error(error_msg or "Invalid API key"))

        account = await self.account_pool.get_available_account(session, model)
        if account is None:
            # 避免并发状态更新窗口导致的瞬时误判；快速重试一次再判定失败原因。
            await asyncio.sleep(ACCOUNT_SELECT_RETRY_DELAY_SECONDS)
            account = await self.account_pool.get_available_account(session, model)

        if account is None:
            availability = await self.account_pool.get_model_group_availability(
                session,
                model,
            )
            raise GatewayAuthError(
                self._build_no_available_account_error(model, availability)
            )

        input_mapping = await self.get_model_input_mapping(session, model)
        return ResolvedRequest(
            request_id=request_id or uuid.uuid4().hex[:24],
            model=model,
            api_key=api_key_obj,
            account=account,
            input_mapping=input_mapping,
        )

    def _map_validation_error(self, message: str) -> APIError:
        lowered = message.lower()
        if "not authorized" in lowered:
            return self.error_handler.create_permission_error(message)
        if "quota" in lowered:
            return self.error_handler.create_quota_exceeded_error(message)
        if "expired" in lowered:
            return self.error_handler.create_authentication_error(message)
        return self.error_handler.create_authentication_error(message)

    def _build_no_available_account_error(
        self,
        model: str,
        availability: Dict[str, Any],
    ) -> APIError:
        total = int(availability.get("total_accounts") or 0)
        routable = int(availability.get("routable_accounts") or 0)
        eligible = int(availability.get("eligible_accounts") or 0)
        stale_exhausted = int(availability.get("stale_exhausted_accounts") or 0)
        status_counts = availability.get("status_counts") or {}

        logger.warning(
            "Model '%s' unavailable after retry: total=%d routable=%d eligible=%d stale_exhausted=%d status_counts=%s",
            model,
            total,
            routable,
            eligible,
            stale_exhausted,
            status_counts,
        )

        if total <= 0:
            return self.error_handler.create_service_unavailable_error(
                f"No configured accounts for model '{model}'"
            )
        if routable <= 0:
            return self.error_handler.create_service_unavailable_error(
                f"No enabled accounts for model '{model}'"
            )
        if eligible <= 0:
            return self.error_handler.create_quota_exceeded_error(
                f"All accounts for model '{model}' reached daily quota"
            )
        return self.error_handler.create_service_unavailable_error(
            f"No available accounts for model '{model}'"
        )

    async def get_model_input_mapping(
        self,
        session: AsyncSession,
        model: str,
    ) -> Optional[Dict[str, str]]:
        result = await session.execute(
            select(ModelGroup).where(ModelGroup.name == model)
        )
        model_group = result.scalar_one_or_none()
        if model_group is None or not model_group.input_mapping:
            return None

        try:
            parsed = json.loads(model_group.input_mapping)
        except Exception:
            return None

        return parsed if isinstance(parsed, dict) else None

    # ------------------------------------------------------------------
    # Backend IO
    # ------------------------------------------------------------------

    def build_backend_payload(
        self,
        *,
        resolved: ResolvedRequest,
        prompt_text: str,
        user_id: str = "anonymous",
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "user_id": user_id,
            "conversation_id": str(uuid.uuid4()),
        }

        input_mapping = resolved.input_mapping or {}

        user_field = str(input_mapping.get("user_input") or "in-0")
        payload[user_field] = prompt_text

        model_field = input_mapping.get("model_id") or input_mapping.get("model")
        if isinstance(model_field, str) and model_field.strip():
            payload[model_field] = resolved.model

        return payload

    async def run_sync(
        self,
        *,
        resolved: ResolvedRequest,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return await self.backend_client.run_with_account(
            account=resolved.account,
            payload=payload,
            account_pool=self.account_pool,
        )

    async def run_stream(
        self,
        *,
        resolved: ResolvedRequest,
        payload: Dict[str, Any],
    ):
        return await self.backend_client.execute_with_account(
            account=resolved.account,
            payload=payload,
            stream=True,
            account_pool=self.account_pool,
        )

    def extract_content(self, backend_response: Dict[str, Any]) -> str:
        return self.response_transformer._extract_content(backend_response)

    def parse_stream_chunk(self, raw_chunk: str) -> Tuple[Optional[str], Optional[STUsage], Optional[str]]:
        token = self.response_transformer._parse_backend_sse(raw_chunk)
        usage_candidate = extract_usage(raw_chunk, source="backend.stream")
        run_id = extract_run_id(raw_chunk)
        return token, usage_candidate, run_id

    # ------------------------------------------------------------------
    # Usage
    # ------------------------------------------------------------------

    def usage_from_sync(
        self,
        *,
        backend_response: Dict[str, Any],
        prompt_text: str,
        output_text: str,
    ) -> UsageNumbers:
        backend_usage = extract_usage(backend_response, source="backend.sync")
        return self.finalize_usage(
            preferred_usage=backend_usage,
            prompt_text=prompt_text,
            output_text=output_text,
            fallback_source="estimate.sync",
        )

    def finalize_usage(
        self,
        *,
        preferred_usage: Optional[STUsage],
        prompt_text: str,
        output_text: str,
        fallback_source: str,
    ) -> UsageNumbers:
        fallback_input = self.token_counter.count(prompt_text) if prompt_text else 0
        fallback_output = self.token_counter.count(output_text) if output_text else 0

        if preferred_usage is None:
            usage = STUsage(
                input_tokens=fallback_input,
                output_tokens=fallback_output,
                total_tokens=fallback_input + fallback_output,
                source=fallback_source,
                exact=False,
            ).normalized()
        else:
            usage = split_total_with_fallback(
                preferred_usage.normalized(),
                fallback_input_tokens=fallback_input,
                fallback_output_tokens=fallback_output,
            ).normalized()
            if usage.total_tokens <= 0 and (fallback_input > 0 or fallback_output > 0):
                usage = STUsage(
                    input_tokens=fallback_input,
                    output_tokens=fallback_output,
                    total_tokens=fallback_input + fallback_output,
                    source=fallback_source,
                    exact=False,
                ).normalized()

        return UsageNumbers(
            input_tokens=max(0, int(usage.input_tokens)),
            output_tokens=max(0, int(usage.output_tokens)),
            total_tokens=max(0, int(usage.total_tokens)),
        )

    @staticmethod
    def merge_stream_usage(current: Optional[STUsage], candidate: Optional[STUsage]) -> Optional[STUsage]:
        return choose_better_usage(current, candidate)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def persist_success(
        self,
        session: AsyncSession,
        *,
        resolved: ResolvedRequest,
        api_type: str,
        input_preview: str,
        output_preview: str,
        usage: UsageNumbers,
        response_time_ms: int,
        is_stream: bool,
        client_ip: Optional[str],
    ) -> None:
        try:
            await self.account_pool.update_token_usage(
                session,
                resolved.account.id,
                usage.input_tokens,
                usage.output_tokens,
            )

            _input_cost, _output_cost, total_cost = self.pricing_service.calculate(
                resolved.model,
                usage.input_tokens,
                usage.output_tokens,
            )

            await self.api_key_service.update_key_stats(
                session,
                resolved.api_key.id,
                usage.input_tokens,
                usage.output_tokens,
                total_cost,
            )

            if ENABLE_REQUEST_LOG_PERSIST:
                await self.logger_service.log_success(
                    session,
                    request_id=resolved.request_id,
                    api_key_prefix=resolved.api_key.key_prefix,
                    client_ip=client_ip,
                    model=resolved.model,
                    account_id=resolved.account.id,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    response_time_ms=response_time_ms,
                )

            if _should_persist_call_log():
                await self.call_logger.log_call(
                    session,
                    api_key_id=resolved.api_key.id,
                    api_key_name=resolved.api_key.name,
                    api_key_prefix=resolved.api_key.key_prefix,
                    client_ip=client_ip,
                    account_id=resolved.account.id,
                    account_name=resolved.account.name,
                    model_group=resolved.model,
                    model=resolved.model,
                    api_type=api_type,
                    is_stream=is_stream,
                    input_preview=input_preview,
                    output_preview=output_preview,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    response_time_ms=response_time_ms,
                    status="success",
                )

            if ENABLE_SYSTEM_STATS_PERSIST:
                await self.stats_service.update_system_stats(
                    session,
                    usage.input_tokens,
                    usage.output_tokens,
                )

            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("Failed to persist success metrics")

    async def persist_error(
        self,
        session: AsyncSession,
        *,
        resolved: Optional[ResolvedRequest],
        api_type: str,
        model: str,
        input_preview: str,
        response_time_ms: int,
        client_ip: Optional[str],
        error_message: str,
    ) -> None:
        try:
            request_id = resolved.request_id if resolved else uuid.uuid4().hex[:24]
            api_key_prefix = resolved.api_key.key_prefix if resolved else None
            account_id = resolved.account.id if resolved else None
            account_name = resolved.account.name if resolved else None
            api_key_id = resolved.api_key.id if resolved else None
            api_key_name = resolved.api_key.name if resolved else None

            if ENABLE_REQUEST_LOG_PERSIST:
                await self.logger_service.log_request(
                    session,
                    request_id=request_id,
                    api_key_prefix=api_key_prefix,
                    client_ip=client_ip,
                    model=model,
                    account_id=account_id,
                    input_tokens=0,
                    output_tokens=0,
                    response_time_ms=response_time_ms,
                    status="error",
                    error_message=error_message,
                )

            if _should_persist_call_log():
                await self.call_logger.log_call(
                    session,
                    api_key_id=api_key_id,
                    api_key_name=api_key_name,
                    api_key_prefix=api_key_prefix,
                    client_ip=client_ip,
                    account_id=account_id,
                    account_name=account_name,
                    model_group=model,
                    model=model,
                    api_type=api_type,
                    is_stream=False,
                    input_preview=input_preview,
                    output_preview="",
                    input_tokens=0,
                    output_tokens=0,
                    response_time_ms=response_time_ms,
                    status="error",
                    error_message=error_message,
                )

            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("Failed to persist error metrics")

    # ------------------------------------------------------------------
    # Error mapping
    # ------------------------------------------------------------------

    def map_backend_exception(self, exc: Exception) -> APIError:
        if isinstance(exc, BackendClientError):
            return self.error_handler.from_backend_exception(exc)
        return self.error_handler.create_server_error(str(exc))

    @staticmethod
    def elapsed_ms(start_time: float) -> int:
        return int((time.time() - start_time) * 1000)


_runtime: Optional[GatewayRuntime] = None


def get_gateway_runtime() -> GatewayRuntime:
    global _runtime
    if _runtime is None:
        _runtime = GatewayRuntime()
    return _runtime
