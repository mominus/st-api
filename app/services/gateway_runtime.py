"""
Unified gateway runtime.

Handles auth, account routing, backend payload construction, usage calculation,
and post-call persistence for all protocol adapters.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple, TypeVar

from sqlalchemy import select
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import APIKey, BackendAccount, ModelGroup, get_session_factory
from app.services.account_pool import AccountPoolService, get_account_pool_service
from app.services.api_key import APIKeyService, get_api_key_service
from app.services.backend_client import (
    BackendAPIError,
    BackendClient,
    BackendClientError,
    BackendConnectionError,
    BackendTimeoutError,
    get_backend_client,
)
from app.services.connection_pool import ConcurrencyLimiter, get_concurrency_limiter
from app.services.call_logger import CallLoggerService, get_call_logger_service
from app.services.error_handler import APIError, ErrorHandler, get_error_handler
from app.services.logger import LoggerService, get_logger_service
from app.services.pricing import PricingService, get_pricing_service
from app.services.protocol_bridge import UsageNumbers
from app.services.response_transformer import ResponseTransformer, get_response_transformer
from app.services.st_usage import STUsage, choose_better_usage, extract_run_id, extract_usage, split_total_with_fallback
from app.services.stats import StatsService, get_stats_service
from app.services.token_counter import TokenCounter, get_token_counter
from app.services.tool_context_builder import ToolContextBuilder
from app.services.usage_aggregator import AsyncUsageAggregator, get_usage_aggregator

logger = logging.getLogger(__name__)
_SESSION_HINT_ALLOWED_PATTERN = re.compile(r"[^a-zA-Z0-9._:@/-]+")
_REQUEST_ID_ALLOWED_PATTERN = re.compile(r"[^a-zA-Z0-9._:-]+")


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _parse_status_code_set(raw: Optional[str], default: str) -> set[int]:
    value = raw if raw is not None else default
    parsed: set[int] = set()
    for token in str(value).split(","):
        token = token.strip()
        if not token:
            continue
        try:
            code = int(token)
        except ValueError:
            continue
        if code > 0:
            parsed.add(code)
    if parsed:
        return parsed
    return {int(x) for x in default.split(",") if x.strip().isdigit()}


ENABLE_REQUEST_LOG_PERSIST = _env_bool("ENABLE_REQUEST_LOG_PERSIST", True)
ENABLE_CALL_LOG_PERSIST = _env_bool("ENABLE_CALL_LOG_PERSIST", True)
ENABLE_SYSTEM_STATS_PERSIST = _env_bool("ENABLE_SYSTEM_STATS_PERSIST", True)
AUTO_DISABLE_ON_PERMISSION_DENIED = _env_bool("AUTO_DISABLE_ON_PERMISSION_DENIED", False)
AUTO_COOLDOWN_ON_TRANSIENT_ERRORS = _env_bool("AUTO_COOLDOWN_ON_TRANSIENT_ERRORS", True)
ACCOUNT_TRANSIENT_ERROR_COOLDOWN_SECONDS = max(
    0.0,
    float(os.getenv("ACCOUNT_TRANSIENT_ERROR_COOLDOWN_SECONDS", "20")),
)
ENABLE_ACCOUNT_FAILOVER_RETRY = _env_bool("ENABLE_ACCOUNT_FAILOVER_RETRY", True)
ACCOUNT_FAILOVER_RETRY_STATUS_CODES = _parse_status_code_set(
    os.getenv("ACCOUNT_FAILOVER_RETRY_STATUS_CODES"),
    "402,429,500,502,503,504",
)
ACCOUNT_FAILOVER_MAX_ATTEMPTS = max(
    1,
    int(os.getenv("ACCOUNT_FAILOVER_MAX_ATTEMPTS", "2")),
)
ACCOUNT_SELECT_MAX_RETRIES = max(0, int(os.getenv("ACCOUNT_SELECT_MAX_RETRIES", "1")))
ACCOUNT_SELECT_RETRY_DELAY_SECONDS = max(
    0.0,
    float(os.getenv("ACCOUNT_SELECT_RETRY_DELAY_SECONDS", "0.08")),
)
ACCOUNT_SELECT_RETRY_JITTER_SECONDS = max(
    0.0,
    float(os.getenv("ACCOUNT_SELECT_RETRY_JITTER_SECONDS", "0.02")),
)
CALL_LOG_SAMPLE_RATE = min(
    1.0,
    max(0.0, float(os.getenv("CALL_LOG_SAMPLE_RATE", "1.0"))),
)
ENABLE_DB_OP_LIMITER = _env_bool("ENABLE_DB_OP_LIMITER", True)
DB_OP_ACQUIRE_TIMEOUT_SECONDS = max(
    0.1,
    float(
        os.getenv(
            "DB_OP_ACQUIRE_TIMEOUT_SECONDS",
            os.getenv("REQUEST_QUEUE_TIMEOUT_SECONDS", "8"),
        )
    ),
)
MODEL_INPUT_MAPPING_CACHE_TTL_SECONDS = max(
    0.0,
    float(os.getenv("MODEL_INPUT_MAPPING_CACHE_TTL_SECONDS", "60")),
)
ASYNC_NONCRITICAL_LOG_PERSIST = _env_bool("ASYNC_NONCRITICAL_LOG_PERSIST", True)
BACKGROUND_LOG_WORKERS = max(1, int(os.getenv("BACKGROUND_LOG_WORKERS", "2")))
BACKGROUND_LOG_QUEUE_MAX_SIZE = max(
    100,
    int(os.getenv("BACKGROUND_LOG_QUEUE_MAX_SIZE", "5000")),
)

T = TypeVar("T")


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
    backup_account: Optional[BackendAccount] = None


@dataclass
class BackgroundLogJob:
    label: str
    request_id: str
    log_kind: str
    operation_factory: Callable[[], Awaitable[None]]


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
        usage_aggregator: Optional[AsyncUsageAggregator] = None,
        db_limiter: Optional[ConcurrencyLimiter] = None,
        session_factory: Optional[Callable[[], Any]] = None,
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
        self.usage_aggregator = usage_aggregator or get_usage_aggregator()
        self.db_limiter = db_limiter or get_concurrency_limiter()
        self._session_factory = session_factory
        self._model_input_mapping_cache: Dict[str, Tuple[float, Optional[Dict[str, str]]]] = {}
        self._background_log_loop_id: Optional[int] = None
        self._background_log_queue: Optional[asyncio.Queue[Optional[BackgroundLogJob]]] = None
        self._background_log_workers: list[asyncio.Task[None]] = []
        self._background_log_worker_lock: Optional[asyncio.Lock] = None
        self._background_log_processed = 0
        self._background_log_dropped = 0
        self._background_log_inflight = 0

    @asynccontextmanager
    async def _acquire_db_slot(self):
        if not ENABLE_DB_OP_LIMITER or self.db_limiter is None:
            yield
            return

        permit = self.db_limiter.acquire_db(timeout=DB_OP_ACQUIRE_TIMEOUT_SECONDS)
        await permit.__aenter__()
        try:
            yield
        finally:
            await permit.__aexit__(None, None, None)

    async def run_db_guarded(
        self,
        session: AsyncSession,
        operation: Callable[[], Awaitable[T]],
        *,
        busy_message: str = "Database is busy. Please retry later.",
    ) -> T:
        try:
            async with self._acquire_db_slot():
                return await operation()
        except (asyncio.TimeoutError, SQLAlchemyTimeoutError):
            try:
                await session.rollback()
            except Exception:
                pass
            raise GatewayAuthError(
                self.error_handler.create_service_unavailable_error(busy_message)
            )

    def _resolve_session_factory(self) -> Callable[[], Any]:
        return self._session_factory or get_session_factory()

    def _ensure_background_log_runtime_state(self) -> None:
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
        if self._background_log_loop_id == loop_id and self._background_log_queue is not None:
            return

        self._background_log_loop_id = loop_id
        self._background_log_queue = asyncio.Queue(maxsize=BACKGROUND_LOG_QUEUE_MAX_SIZE)
        self._background_log_workers = []
        self._background_log_worker_lock = asyncio.Lock()
        self._background_log_inflight = 0

    async def _ensure_background_log_workers_started(self) -> None:
        self._ensure_background_log_runtime_state()
        assert self._background_log_worker_lock is not None

        async with self._background_log_worker_lock:
            active_workers = [task for task in self._background_log_workers if not task.done()]
            self._background_log_workers = active_workers
            missing = max(0, BACKGROUND_LOG_WORKERS - len(active_workers))
            for worker_index in range(missing):
                task = asyncio.create_task(
                    self._background_log_worker_loop(len(active_workers) + worker_index + 1)
                )
                self._background_log_workers.append(task)

    async def _background_log_worker_loop(self, worker_index: int) -> None:
        assert self._background_log_queue is not None
        queue = self._background_log_queue

        while True:
            job = await queue.get()
            if job is None:
                queue.task_done()
                return

            self._background_log_inflight += 1
            try:
                await job.operation_factory()
                self._background_log_processed += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Background log worker failed: worker=%s label=%s request_id=%s",
                    worker_index,
                    job.label,
                    job.request_id,
                )
            finally:
                self._background_log_inflight = max(0, self._background_log_inflight - 1)
                queue.task_done()

    async def _enqueue_background_log(
        self,
        *,
        request_id: str,
        log_kind: str,
        label: str,
        operation_factory: Callable[[], Awaitable[None]],
    ) -> str:
        try:
            await self._ensure_background_log_workers_started()
        except RuntimeError:
            logger.warning("Failed to enqueue background log without running loop: %s", label)
            return "inline"

        assert self._background_log_queue is not None
        job = BackgroundLogJob(
            label=label,
            request_id=request_id,
            log_kind=log_kind,
            operation_factory=operation_factory,
        )

        try:
            self._background_log_queue.put_nowait(job)
            return "enqueued"
        except asyncio.QueueFull:
            self._background_log_dropped += 1
            logger.warning(
                (
                    "Dropped background %s due to queue pressure: request_id=%s "
                    "queue_size=%s dropped_total=%s"
                ),
                log_kind,
                request_id,
                self._background_log_queue.qsize(),
                self._background_log_dropped,
            )
            return "dropped"

    async def drain_background_tasks(self) -> None:
        if self._background_log_queue is None:
            return
        await self._background_log_queue.join()

    async def close(self) -> None:
        if self._background_log_queue is None:
            return

        await self.drain_background_tasks()

        workers = [task for task in self._background_log_workers if not task.done()]
        if not workers:
            self._background_log_workers = []
            return

        for _ in workers:
            self._background_log_queue.put_nowait(None)
        await asyncio.gather(*workers, return_exceptions=True)
        self._background_log_workers = []

    def background_log_stats(self) -> Dict[str, int]:
        queue_size = self._background_log_queue.qsize() if self._background_log_queue is not None else 0
        active_workers = sum(1 for task in self._background_log_workers if not task.done())
        return {
            "queue_size": queue_size,
            "processed_events": self._background_log_processed,
            "dropped_events": self._background_log_dropped,
            "inflight_events": self._background_log_inflight,
            "active_workers": active_workers,
            "queue_capacity": BACKGROUND_LOG_QUEUE_MAX_SIZE,
        }

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

        try:
            async with self._acquire_db_slot():
                valid, error_msg, api_key_obj = await self.api_key_service.validate_key(
                    session,
                    raw_key,
                    model,
                )
                if not valid or api_key_obj is None:
                    raise GatewayAuthError(self._map_validation_error(error_msg or "Invalid API key"))

                account = await self._select_account_with_retry(
                    session=session,
                    model=model,
                )

                if account is None:
                    availability = await self.account_pool.get_model_group_availability(
                        session,
                        model,
                    )
                    raise GatewayAuthError(
                        self._build_no_available_account_error(model, availability)
                    )

                input_mapping = await self.get_model_input_mapping(session, model)
                resolved = ResolvedRequest(
                    request_id=request_id or uuid.uuid4().hex[:24],
                    model=model,
                    api_key=api_key_obj,
                    account=account,
                    input_mapping=input_mapping,
                )
                # Release DB connection early before long upstream inference call.
                # A fresh transaction will be opened lazily for later persistence writes.
                await session.commit()
                return resolved
        except GatewayAuthError:
            raise
        except (asyncio.TimeoutError, SQLAlchemyTimeoutError):
            try:
                await session.rollback()
            except Exception:
                pass
            raise GatewayAuthError(
                self.error_handler.create_service_unavailable_error(
                    "Database is busy. Please retry later."
                )
            )
        except Exception:
            await session.rollback()
            raise

    def _map_validation_error(self, message: str) -> APIError:
        lowered = message.lower()
        if "not authorized" in lowered:
            return self.error_handler.create_permission_error(message)
        if "quota" in lowered or "cost limit" in lowered or "limit exceeded" in lowered:
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
        active_accounts = int(
            availability.get("active_accounts")
            or availability.get("routable_accounts")
            or 0
        )
        eligible = int(availability.get("eligible_accounts") or 0)
        available = int(availability.get("available_accounts") or 0)
        concurrency_limited = int(availability.get("concurrency_limited_accounts") or 0)
        exhausted_accounts = int(
            availability.get("exhausted_accounts")
            or availability.get("stale_exhausted_accounts")
            or 0
        )
        status_counts = availability.get("status_counts") or {}

        logger.warning(
            (
                "Model '%s' unavailable after retry: total=%d active=%d eligible=%d "
                "available=%d concurrency_limited=%d exhausted=%d status_counts=%s"
            ),
            model,
            total,
            active_accounts,
            eligible,
            available,
            concurrency_limited,
            exhausted_accounts,
            status_counts,
        )

        if total <= 0:
            return self.error_handler.create_service_unavailable_error(
                f"No configured accounts for model '{model}'"
            )
        if active_accounts <= 0 and exhausted_accounts > 0:
            return self.error_handler.create_quota_exceeded_error(
                f"All accounts for model '{model}' reached daily quota"
            )
        if active_accounts <= 0:
            return self.error_handler.create_service_unavailable_error(
                f"No active accounts for model '{model}'"
            )
        if eligible > 0 and available <= 0 and concurrency_limited > 0:
            return self.error_handler.create_service_unavailable_error(
                f"All accounts for model '{model}' are busy with active requests"
            )
        if eligible <= 0:
            return self.error_handler.create_quota_exceeded_error(
                f"All active accounts for model '{model}' reached daily quota"
            )
        return self.error_handler.create_service_unavailable_error(
            f"No available accounts for model '{model}'"
        )

    @staticmethod
    def _select_retry_delay_seconds(retry_index: int) -> float:
        """
        账号选择重试延迟（指数退避 + 抖动）：
        retry_index 从 1 开始，表示第几次重试（非首次尝试）。
        """
        exp_delay = ACCOUNT_SELECT_RETRY_DELAY_SECONDS * (2 ** max(0, retry_index - 1))
        jitter = (
            random.uniform(0.0, ACCOUNT_SELECT_RETRY_JITTER_SECONDS)
            if ACCOUNT_SELECT_RETRY_JITTER_SECONDS > 0
            else 0.0
        )
        return max(0.0, exp_delay + jitter)

    async def _select_account_with_retry(
        self,
        *,
        session: AsyncSession,
        model: str,
        exclude_account_id: Optional[str] = None,
    ) -> Optional[BackendAccount]:
        max_attempts = 1 + ACCOUNT_SELECT_MAX_RETRIES
        for attempt in range(1, max_attempts + 1):
            account = await self.account_pool.get_available_account(
                session,
                model,
                exclude_account_id=exclude_account_id,
            )
            if account is not None:
                return account

            if attempt >= max_attempts:
                break

            delay = self._select_retry_delay_seconds(attempt)
            if delay > 0:
                await asyncio.sleep(delay)

        return None

    async def _persist_core_success_state(
        self,
        session: AsyncSession,
        *,
        resolved: ResolvedRequest,
        usage: UsageNumbers,
        use_db_limiter: bool,
    ) -> None:
        async def _operation() -> None:
            await self.account_pool.update_token_usage(
                session,
                resolved.account.id,
                usage.input_tokens,
                usage.output_tokens,
                record_history=False,
                fetch_account=False,
            )
            await session.commit()

        if use_db_limiter:
            async with self._acquire_db_slot():
                await _operation()
            return
        await _operation()

    async def _persist_core_error_state(
        self,
        session: AsyncSession,
        *,
        resolved: ResolvedRequest,
        error_message: str,
        use_db_limiter: bool,
    ) -> None:
        async def _operation() -> None:
            await self._adapt_account_state_on_error(
                session=session,
                resolved=resolved,
                error_message=error_message,
            )
            await session.commit()

        if use_db_limiter:
            async with self._acquire_db_slot():
                await _operation()
            return
        await _operation()

    async def get_model_input_mapping(
        self,
        session: AsyncSession,
        model: str,
    ) -> Optional[Dict[str, str]]:
        if MODEL_INPUT_MAPPING_CACHE_TTL_SECONDS > 0:
            cached = self._model_input_mapping_cache.get(model)
            if cached is not None:
                cached_at, cached_value = cached
                if (time.monotonic() - cached_at) <= MODEL_INPUT_MAPPING_CACHE_TTL_SECONDS:
                    return cached_value

        result = await session.execute(
            select(ModelGroup).where(ModelGroup.name == model)
        )
        model_group = result.scalar_one_or_none()
        if model_group is None or not model_group.input_mapping:
            parsed = None
        else:
            try:
                parsed_raw = json.loads(model_group.input_mapping)
            except Exception:
                parsed = None
            else:
                parsed = parsed_raw if isinstance(parsed_raw, dict) else None

        if MODEL_INPUT_MAPPING_CACHE_TTL_SECONDS > 0:
            self._model_input_mapping_cache[model] = (time.monotonic(), parsed)

        return parsed

    # ------------------------------------------------------------------
    # Backend IO
    # ------------------------------------------------------------------

    @staticmethod
    def resolve_client_ip(
        *,
        headers: Any,
        fallback_client_ip: Optional[str],
    ) -> Optional[str]:
        """Resolve client IP from reverse-proxy headers."""
        for header_name in ("cf-connecting-ip", "true-client-ip", "x-forwarded-for", "x-real-ip"):
            raw_value = headers.get(header_name) if headers is not None else None
            if not raw_value:
                continue
            value = str(raw_value).strip()
            if header_name == "x-forwarded-for":
                value = value.split(",", 1)[0].strip()
            if value:
                return value
        if fallback_client_ip:
            value = str(fallback_client_ip).strip()
            if value:
                return value
        return None

    @staticmethod
    def _sanitize_request_id(request_id: Optional[str]) -> Optional[str]:
        if not request_id:
            return None
        compact = _REQUEST_ID_ALLOWED_PATTERN.sub("-", str(request_id).strip())
        compact = compact.strip("-._:")
        if not compact:
            return None
        return compact[:64]

    def resolve_request_id(
        self,
        *,
        headers: Any,
    ) -> str:
        for header_name in ("x-st-request-id", "x-request-id", "x-client-request-id"):
            raw_value = headers.get(header_name) if headers is not None else None
            sanitized = self._sanitize_request_id(raw_value)
            if sanitized:
                return sanitized
        return uuid.uuid4().hex[:24]

    @staticmethod
    def _sanitize_session_hint(session_hint: Optional[str]) -> Optional[str]:
        if not session_hint:
            return None
        compact = _SESSION_HINT_ALLOWED_PATTERN.sub("-", str(session_hint).strip())
        compact = compact.strip("-._:/")
        if not compact:
            return None
        return compact[:64]

    def resolve_session_hint(
        self,
        *,
        headers: Any,
        payload: Optional[Dict[str, Any]] = None,
        allow_user_field: bool = True,
    ) -> Optional[str]:
        """
        Resolve stable client session hint from common header/body conventions.
        """
        header_candidates = (
            "x-st-session-id",
            "x-session-id",
        )
        for key in header_candidates:
            raw = headers.get(key) if headers is not None else None
            sanitized = self._sanitize_session_hint(raw)
            if sanitized:
                return sanitized

        if not isinstance(payload, dict):
            return None

        direct_candidates = (
            "st_session_id",
            "session_id",
            "sessionId",
        )
        for key in direct_candidates:
            sanitized = self._sanitize_session_hint(payload.get(key))
            if sanitized:
                return sanitized

        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            metadata_candidates = (
                "st_session_id",
                "session_id",
                "sessionId",
                "user_id",
                "user",
            )
            for key in metadata_candidates:
                sanitized = self._sanitize_session_hint(metadata.get(key))
                if sanitized:
                    return sanitized

        if allow_user_field:
            sanitized = self._sanitize_session_hint(payload.get("user"))
            if sanitized:
                return sanitized

        return None

    @staticmethod
    def _serialize_error_for_logs(
        *,
        error_message: str,
        api_error: Optional[APIError],
    ) -> str:
        if api_error is None:
            return error_message

        payload: Dict[str, Any] = {
            "message": error_message or api_error.message,
            "type": api_error.error_type.value,
            "code": api_error.code,
            "status_code": api_error.status_code,
        }
        if api_error.details is not None:
            payload["details"] = api_error.details
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def resolve_backend_user_id(
        self,
        *,
        resolved: ResolvedRequest,
        request_id: str,
        session_hint: Optional[str],
        client_ip: Optional[str],
        user_agent: Optional[str],
    ) -> str:
        """
        Build isolated upstream user_id.

        - If caller provides a stable session hint, keep context within that session.
        - Otherwise default to request-scoped identity to avoid cross-user bleed.
        """
        sanitized_hint = self._sanitize_session_hint(session_hint)
        if sanitized_hint:
            return f"api:{resolved.api_key.id}:session:{sanitized_hint}"

        # Privacy-first default: request-level isolation.
        # Keep a short fingerprint suffix for debugging observability only.
        identity_material = f"{client_ip or 'na'}|{user_agent or 'na'}|{request_id}"
        fingerprint = hashlib.sha256(identity_material.encode("utf-8")).hexdigest()[:12]
        return f"api:{resolved.api_key.id}:req:{request_id}:{fingerprint}"

    @staticmethod
    def _mapped_field_name(input_mapping: Dict[str, str], logical_name: str) -> Optional[str]:
        raw_value = input_mapping.get(logical_name)
        if not isinstance(raw_value, str):
            return None
        field_name = raw_value.strip()
        return field_name or None

    @classmethod
    def _format_chat_history_for_backend(cls, messages: list[Any]) -> str:
        formatted_messages: list[str] = []
        for message in messages:
            role, content = cls._message_role_and_content(message)
            if not isinstance(content, str) or not content.strip():
                continue
            label = "[Assistant]" if role == "assistant" else "[Human]"
            formatted_messages.append(f"{label}\n{content}")
        return "\n\n".join(formatted_messages)

    @staticmethod
    def _message_role_and_content(message: Any) -> Tuple[str, Any]:
        if isinstance(message, dict):
            role = str(message.get("role") or "user")
            content = message.get("content", "")
            return role, content
        role = str(getattr(message, "role", "user") or "user")
        content = getattr(message, "content", "")
        return role, content

    @classmethod
    def _messages_for_backend_context(cls, canonical: Optional[Any]) -> list[Any]:
        if canonical is None:
            return []

        source = str(getattr(canonical, "source", "") or "")
        raw_messages = getattr(canonical, "raw_messages", None)
        if source == "anthropic_messages" and isinstance(raw_messages, list) and raw_messages:
            return ToolContextBuilder().build_context(raw_messages)

        messages = getattr(canonical, "messages", None)
        if isinstance(messages, list):
            return messages
        return []

    @classmethod
    def _split_prompt_context(
        cls,
        canonical: Optional[Any],
        prompt_text: str,
    ) -> Tuple[str, str]:
        if canonical is None:
            return prompt_text, ""

        messages = cls._messages_for_backend_context(canonical)
        if not messages:
            return prompt_text, ""

        latest_user_index: Optional[int] = None
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            role, content = cls._message_role_and_content(message)
            if role == "user" and isinstance(content, str) and content.strip():
                latest_user_index = index
                break

        if latest_user_index is None:
            return prompt_text, cls._format_chat_history_for_backend(messages)

        _, latest_user_content = cls._message_role_and_content(messages[latest_user_index])
        if not isinstance(latest_user_content, str) or not latest_user_content.strip():
            return prompt_text, cls._format_chat_history_for_backend(messages)

        history_messages = messages[:latest_user_index]
        return latest_user_content, cls._format_chat_history_for_backend(history_messages)

    def build_backend_payload(
        self,
        *,
        resolved: ResolvedRequest,
        prompt_text: str,
        user_id: str = "anonymous",
        canonical: Optional[Any] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "user_id": user_id,
            "conversation_id": str(uuid.uuid4()),
        }

        input_mapping = resolved.input_mapping or {}

        user_field = self._mapped_field_name(input_mapping, "user_input") or "in-0"
        has_structured_context_mapping = any(
            self._mapped_field_name(input_mapping, logical_name)
            for logical_name in ("system_prompt", "chat_history")
        )

        user_input_value = prompt_text
        chat_history_value = ""
        if has_structured_context_mapping:
            user_input_value, chat_history_value = self._split_prompt_context(canonical, prompt_text)
        payload[user_field] = user_input_value

        if canonical is not None:
            system_field = self._mapped_field_name(input_mapping, "system_prompt")
            system_prompt = getattr(canonical, "system_prompt", "")
            if system_field and isinstance(system_prompt, str) and system_prompt:
                payload[system_field] = system_prompt

            chat_history_field = self._mapped_field_name(input_mapping, "chat_history")
            if chat_history_field and chat_history_value:
                payload[chat_history_field] = chat_history_value

            max_tokens_field = self._mapped_field_name(input_mapping, "max_tokens")
            max_tokens = getattr(canonical, "max_tokens", None)
            if max_tokens_field and max_tokens is not None:
                payload[max_tokens_field] = max_tokens

            temperature_field = self._mapped_field_name(input_mapping, "temperature")
            temperature = getattr(canonical, "temperature", None)
            if temperature_field and temperature is not None:
                payload[temperature_field] = temperature

            tool_choice_field = self._mapped_field_name(input_mapping, "tool_choice")
            tool_choice = getattr(canonical, "tool_choice", None)
            if tool_choice_field and tool_choice is not None:
                payload[tool_choice_field] = tool_choice

            thinking_field = self._mapped_field_name(input_mapping, "thinking")
            thinking = getattr(canonical, "thinking", None)
            if thinking_field and isinstance(thinking, dict) and thinking:
                payload[thinking_field] = thinking

            metadata_field = self._mapped_field_name(input_mapping, "metadata")
            metadata = getattr(canonical, "metadata", None)
            if metadata_field and isinstance(metadata, dict) and metadata:
                payload[metadata_field] = metadata

            anthropic_beta_field = self._mapped_field_name(input_mapping, "anthropic_beta")
            anthropic_beta = getattr(canonical, "anthropic_beta", None)
            if anthropic_beta_field and isinstance(anthropic_beta, list) and anthropic_beta:
                payload[anthropic_beta_field] = anthropic_beta

        model_field = self._mapped_field_name(input_mapping, "model_id") or self._mapped_field_name(
            input_mapping,
            "model",
        )
        if model_field:
            payload[model_field] = resolved.model

        return payload

    async def run_sync(
        self,
        *,
        resolved: ResolvedRequest,
        payload: Dict[str, Any],
        session: Optional[AsyncSession] = None,
    ) -> Dict[str, Any]:
        current_account = resolved.account
        failover_attempts = 0

        while True:
            try:
                return await self.backend_client.run_with_account(
                    account=current_account,
                    payload=payload,
                    account_pool=self.account_pool,
                )
            except Exception as exc:
                can_failover = failover_attempts < ACCOUNT_FAILOVER_MAX_ATTEMPTS
                if not can_failover:
                    raise

                failover_account = await self._pick_failover_account(
                    session=session,
                    resolved=resolved,
                    exc=exc,
                    current_account=current_account,
                )
                if failover_account is None:
                    raise

                logger.warning(
                    "Retrying sync request with backup account: from=%s to=%s model=%s attempt=%s/%s",
                    getattr(current_account, "id", None),
                    getattr(failover_account, "id", None),
                    resolved.model,
                    failover_attempts + 1,
                    ACCOUNT_FAILOVER_MAX_ATTEMPTS,
                )
                current_account = failover_account
                resolved.account = current_account
                failover_attempts += 1

    async def run_stream(
        self,
        *,
        resolved: ResolvedRequest,
        payload: Dict[str, Any],
        session: Optional[AsyncSession] = None,
    ):
        async def _stream_with_failover():
            failover_attempts = 0
            current_account = resolved.account

            while True:
                yielded_any = False
                try:
                    stream_gen = await self.backend_client.execute_with_account(
                        account=current_account,
                        payload=payload,
                        stream=True,
                        account_pool=self.account_pool,
                    )
                    async for chunk in stream_gen:
                        yielded_any = True
                        yield chunk
                    return
                except Exception as exc:
                    can_failover = (
                        failover_attempts < ACCOUNT_FAILOVER_MAX_ATTEMPTS
                        and not yielded_any
                    )
                    if not can_failover:
                        raise

                    failover_account = await self._pick_failover_account(
                        session=session,
                        resolved=resolved,
                        exc=exc,
                        current_account=current_account,
                    )
                    if failover_account is None:
                        raise

                    logger.warning(
                        "Retrying stream request with backup account before first token: from=%s to=%s model=%s attempt=%s/%s",
                        getattr(current_account, "id", None),
                        getattr(failover_account, "id", None),
                        resolved.model,
                        failover_attempts + 1,
                        ACCOUNT_FAILOVER_MAX_ATTEMPTS,
                    )
                    current_account = failover_account
                    resolved.account = current_account
                    failover_attempts += 1

        return _stream_with_failover()

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

    async def _persist_success_logs_with_session(
        self,
        session: AsyncSession,
        *,
        request_id: str,
        api_key_id: Optional[str],
        api_key_name: Optional[str],
        api_key_prefix: Optional[str],
        client_ip: Optional[str],
        account_id: Optional[str],
        account_name: Optional[str],
        model: str,
        api_type: str,
        is_stream: bool,
        input_preview: str,
        output_preview: str,
        usage: UsageNumbers,
        response_time_ms: int,
        persist_request_log: bool,
        persist_call_log: bool,
    ) -> None:
        try:
            async with self._acquire_db_slot():
                if persist_request_log:
                    await self.logger_service.log_success(
                        session,
                        request_id=request_id,
                        api_key_prefix=api_key_prefix,
                        client_ip=client_ip,
                        model=model,
                        account_id=account_id,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        response_time_ms=response_time_ms,
                    )

                if persist_call_log:
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
                        is_stream=is_stream,
                        input_preview=input_preview,
                        output_preview=output_preview,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        response_time_ms=response_time_ms,
                        status="success",
                    )

                await session.commit()
        except asyncio.TimeoutError:
            logger.warning(
                "Skipped non-critical success logs due to DB concurrency pressure: request_id=%s",
                request_id,
            )
        except Exception:
            await session.rollback()
            logger.exception("Failed to persist non-critical success logs")

    async def _persist_success_logs_background(self, **kwargs: Any) -> None:
        session_factory = self._resolve_session_factory()
        async with session_factory() as session:
            await self._persist_success_logs_with_session(session, **kwargs)

    async def _persist_error_logs_with_session(
        self,
        session: AsyncSession,
        *,
        request_id: str,
        api_key_id: Optional[str],
        api_key_name: Optional[str],
        api_key_prefix: Optional[str],
        client_ip: Optional[str],
        account_id: Optional[str],
        account_name: Optional[str],
        model: str,
        api_type: str,
        input_preview: str,
        response_time_ms: int,
        error_message: str,
        persist_request_log: bool,
        persist_call_log: bool,
    ) -> None:
        try:
            async with self._acquire_db_slot():
                if persist_request_log:
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

                if persist_call_log:
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
        except asyncio.TimeoutError:
            logger.warning(
                "Skipped non-critical error logs due to DB concurrency pressure: request_id=%s",
                request_id,
            )
        except Exception:
            await session.rollback()
            logger.exception("Failed to persist non-critical error logs")

    async def _persist_error_logs_background(self, **kwargs: Any) -> None:
        session_factory = self._resolve_session_factory()
        async with session_factory() as session:
            await self._persist_error_logs_with_session(session, **kwargs)

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
        persist_request_log = ENABLE_REQUEST_LOG_PERSIST
        persist_call_log = _should_persist_call_log()
        _input_cost, _output_cost, total_cost = self.pricing_service.calculate(
            resolved.model,
            usage.input_tokens,
            usage.output_tokens,
        )

        # 核心状态更新：必须优先提交，避免日志失败导致配额状态回滚。
        try:
            await self._persist_core_success_state(
                session,
                resolved=resolved,
                usage=usage,
                use_db_limiter=True,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Retrying core success persistence without DB gate after limiter timeout: request_id=%s",
                resolved.request_id,
            )
            try:
                await self._persist_core_success_state(
                    session,
                    resolved=resolved,
                    usage=usage,
                    use_db_limiter=False,
                )
            except Exception:
                await session.rollback()
                logger.exception("Failed to persist core success metrics after limiter fallback")
                return
        except Exception:
            await session.rollback()
            logger.exception("Failed to persist core success metrics")
            return

        usage_enqueued = False
        try:
            usage_enqueued = await self.usage_aggregator.enqueue_usage_update(
                request_id=resolved.request_id,
                api_key_id=resolved.api_key.id,
                account_id=resolved.account.id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_cost=total_cost,
            )
        except Exception:
            logger.exception(
                "Failed to enqueue async usage update; falling back to sync persistence: request_id=%s",
                resolved.request_id,
            )

        # 入队失败时回退到同步写，避免统计数据丢失。
        if not usage_enqueued:
            try:
                async with self._acquire_db_slot():
                    await self.account_pool.record_usage_history(
                        session,
                        account_id=resolved.account.id,
                        api_key_id=None,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        request_count=1,
                    )
                    await self.api_key_service.update_key_stats(
                        session,
                        resolved.api_key.id,
                        usage.input_tokens,
                        usage.output_tokens,
                        total_cost,
                        request_count=1,
                    )
                    if ENABLE_SYSTEM_STATS_PERSIST:
                        await self.stats_service.update_system_stats(
                            session,
                            usage.input_tokens,
                            usage.output_tokens,
                            request_count=1,
                        )
                    await session.commit()
            except asyncio.TimeoutError:
                logger.warning(
                    "Skipped fallback sync usage persistence due to DB concurrency pressure: request_id=%s",
                    resolved.request_id,
                )
            except Exception:
                await session.rollback()
                logger.exception(
                    "Failed fallback sync usage persistence: request_id=%s",
                    resolved.request_id,
                )

        # 非关键日志：失败只回滚日志事务，不影响核心状态。
        if not persist_request_log and not persist_call_log:
            return

        log_kwargs = {
            "request_id": resolved.request_id,
            "api_key_id": getattr(resolved.api_key, "id", None),
            "api_key_name": getattr(resolved.api_key, "name", None),
            "api_key_prefix": getattr(resolved.api_key, "key_prefix", None),
            "client_ip": client_ip,
            "account_id": getattr(resolved.account, "id", None),
            "account_name": getattr(resolved.account, "name", None),
            "model": resolved.model,
            "api_type": api_type,
            "is_stream": is_stream,
            "input_preview": input_preview,
            "output_preview": output_preview,
            "usage": usage,
            "response_time_ms": response_time_ms,
            "persist_request_log": persist_request_log,
            "persist_call_log": persist_call_log,
        }

        if ASYNC_NONCRITICAL_LOG_PERSIST:
            enqueue_result = await self._enqueue_background_log(
                request_id=resolved.request_id,
                log_kind="success-log",
                label=f"success-log:{resolved.request_id}",
                operation_factory=lambda: self._persist_success_logs_background(**log_kwargs),
            )
            if enqueue_result == "enqueued":
                return
            if enqueue_result == "dropped":
                return

        await self._persist_success_logs_with_session(session, **log_kwargs)

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
        api_error: Optional[APIError] = None,
        defer_noncritical_logs: bool = False,
    ) -> None:
        # 核心状态更新：错误自适应状态单独事务提交。
        if resolved is not None:
            try:
                await self._persist_core_error_state(
                    session,
                    resolved=resolved,
                    error_message=error_message,
                    use_db_limiter=True,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Retrying core error persistence without DB gate after limiter timeout: request_id=%s",
                    resolved.request_id,
                )
                try:
                    await self._persist_core_error_state(
                        session,
                        resolved=resolved,
                        error_message=error_message,
                        use_db_limiter=False,
                    )
                except Exception:
                    await session.rollback()
                    logger.exception("Failed to persist core error state after limiter fallback")
            except Exception:
                await session.rollback()
                logger.exception("Failed to persist core error state")

        persist_request_log = ENABLE_REQUEST_LOG_PERSIST
        persist_call_log = _should_persist_call_log()
        if not persist_request_log and not persist_call_log:
            return

        request_id = resolved.request_id if resolved else uuid.uuid4().hex[:24]
        api_key_prefix = resolved.api_key.key_prefix if resolved else None
        account_id = resolved.account.id if resolved else None
        account_name = resolved.account.name if resolved else None
        api_key_id = resolved.api_key.id if resolved else None
        api_key_name = resolved.api_key.name if resolved else None

        log_kwargs = {
            "request_id": request_id,
            "api_key_id": api_key_id,
            "api_key_name": api_key_name,
            "api_key_prefix": api_key_prefix,
            "client_ip": client_ip,
            "account_id": account_id,
            "account_name": account_name,
            "model": model,
            "api_type": api_type,
            "input_preview": input_preview,
            "response_time_ms": response_time_ms,
            "error_message": self._serialize_error_for_logs(
                error_message=error_message,
                api_error=api_error,
            ),
            "persist_request_log": persist_request_log,
            "persist_call_log": persist_call_log,
        }

        if defer_noncritical_logs or ASYNC_NONCRITICAL_LOG_PERSIST:
            enqueue_result = await self._enqueue_background_log(
                request_id=request_id,
                log_kind="error-log",
                label=f"error-log:{request_id}",
                operation_factory=lambda: self._persist_error_logs_background(**log_kwargs),
            )
            if enqueue_result == "enqueued":
                return
            if enqueue_result == "dropped":
                return

        await self._persist_error_logs_with_session(session, **log_kwargs)

    async def _adapt_account_state_on_error(
        self,
        *,
        session: AsyncSession,
        resolved: Optional[ResolvedRequest],
        error_message: str,
    ) -> None:
        """Apply defensive account state transitions for upstream hard failures."""
        if resolved is None:
            return

        try:
            await self.account_pool.release_account_request(session, resolved.account.id)
        except Exception:
            logger.exception(
                "Failed to release inflight account reservation after error: account_id=%s model=%s",
                getattr(resolved.account, "id", None),
                resolved.model,
            )

        normalized = (error_message or "").strip().lower()
        if not normalized:
            return

        try:
            quota_markers = (
                "daily token quota exceeded",
                "account daily token quota exceeded",
                "upstream account quota exceeded",
                "quota exceeded",
            )
            if any(marker in normalized for marker in quota_markers):
                changed = await self.account_pool.mark_account_exhausted(
                    session,
                    resolved.account.id,
                    enforce_quota_block=True,
                )
                if changed:
                    logger.warning(
                        "Auto-marked account exhausted after upstream quota error: account_id=%s model=%s",
                        resolved.account.id,
                        resolved.model,
                    )
                return

            if normalized == "permission denied":
                if not AUTO_DISABLE_ON_PERMISSION_DENIED:
                    logger.warning(
                        "Permission denied from upstream (auto-disable disabled): account_id=%s model=%s",
                        resolved.account.id,
                        resolved.model,
                    )
                    return
                updated = await self.account_pool.update_account(
                    session,
                    resolved.account.id,
                    status="disabled",
                )
                if updated is not None:
                    logger.warning(
                        "Auto-disabled account after upstream permission error: account_id=%s model=%s",
                        resolved.account.id,
                        resolved.model,
                    )
                return

            transient_markers = (
                "upstream service unavailable",
                "upstream request timed out",
                "failed to connect to upstream service",
                "upstream service error",
                "temporarily overloaded",
                "overloaded",
                "rate limit",
                "too many requests",
            )
            if (
                AUTO_COOLDOWN_ON_TRANSIENT_ERRORS
                and ACCOUNT_TRANSIENT_ERROR_COOLDOWN_SECONDS > 0
                and any(marker in normalized for marker in transient_markers)
            ):
                changed = await self.account_pool.defer_account_selection(
                    session,
                    resolved.account.id,
                    ACCOUNT_TRANSIENT_ERROR_COOLDOWN_SECONDS,
                )
                if changed:
                    logger.warning(
                        (
                            "Auto-deferred account after transient upstream error: "
                            "account_id=%s model=%s cooldown=%.2fs message=%s"
                        ),
                        resolved.account.id,
                        resolved.model,
                        ACCOUNT_TRANSIENT_ERROR_COOLDOWN_SECONDS,
                        error_message,
                    )
                return
        except Exception:
            logger.exception("Failed to adapt account state for upstream error")

    # ------------------------------------------------------------------
    # Error mapping
    # ------------------------------------------------------------------

    def map_backend_exception(self, exc: Exception) -> APIError:
        if isinstance(exc, BackendClientError):
            return self.error_handler.from_backend_exception(exc)
        return self.error_handler.create_server_error(str(exc))

    def _is_transient_backend_exception(self, exc: Exception) -> bool:
        if isinstance(exc, (BackendTimeoutError, BackendConnectionError)):
            return True
        if isinstance(exc, BackendAPIError):
            status = int(exc.status_code or 0)
            if status == 402:
                # Quota/payment failures are often account-specific; switch account first.
                return True
            if status in ACCOUNT_FAILOVER_RETRY_STATUS_CODES:
                return True
        return False

    def _should_failover_on_exception(
        self,
        exc: Exception,
        *,
        current_account: Optional[BackendAccount],
    ) -> bool:
        if not ENABLE_ACCOUNT_FAILOVER_RETRY:
            return False
        if current_account is None:
            return False
        return self._is_transient_backend_exception(exc)

    async def _pick_failover_account(
        self,
        *,
        session: Optional[AsyncSession],
        resolved: ResolvedRequest,
        exc: Exception,
        current_account: Optional[BackendAccount],
    ) -> Optional[BackendAccount]:
        if not self._should_failover_on_exception(exc, current_account=current_account):
            return None
        if session is None:
            return None

        try:
            async with self._acquire_db_slot():
                await self._apply_transient_account_cooldown(
                    session=session,
                    resolved=resolved,
                    exc=exc,
                )

                exclude_id = getattr(current_account, "id", None)
                max_attempts = 1 + ACCOUNT_SELECT_MAX_RETRIES
                for attempt in range(1, max_attempts + 1):
                    candidate = await self.account_pool.get_available_account(
                        session,
                        resolved.model,
                        exclude_account_id=exclude_id,
                    )
                    # 选号后立即提交，及时释放行锁，避免同事务内反复命中同一账号。
                    await session.commit()
                    if candidate is not None and getattr(candidate, "id", None) != exclude_id:
                        return candidate

                    if attempt >= max_attempts:
                        break
                    delay = self._select_retry_delay_seconds(attempt)
                    if delay > 0:
                        await asyncio.sleep(delay)
                return None
        except asyncio.TimeoutError:
            logger.warning(
                "Skip failover account selection due to DB concurrency pressure: account_id=%s model=%s",
                getattr(current_account, "id", None),
                resolved.model,
            )
            return None
        except Exception:
            await session.rollback()
            logger.exception(
                "Failed to pick failover account: exclude_account_id=%s model=%s",
                getattr(current_account, "id", None),
                resolved.model,
            )
            return None

    async def _apply_transient_account_cooldown(
        self,
        *,
        session: AsyncSession,
        resolved: ResolvedRequest,
        exc: Exception,
    ) -> None:
        try:
            api_error = self.map_backend_exception(exc)
            await self._adapt_account_state_on_error(
                session=session,
                resolved=resolved,
                error_message=api_error.message,
            )
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception(
                "Failed to apply transient cooldown before account failover: account_id=%s model=%s",
                getattr(resolved.account, "id", None),
                resolved.model,
            )

    @staticmethod
    def elapsed_ms(start_time: float) -> int:
        return int((time.time() - start_time) * 1000)


_runtime: Optional[GatewayRuntime] = None


def get_gateway_runtime() -> GatewayRuntime:
    global _runtime
    if _runtime is None:
        _runtime = GatewayRuntime()
    return _runtime


async def close_gateway_runtime() -> None:
    global _runtime
    if _runtime is None:
        return
    await _runtime.close()
    _runtime = None
