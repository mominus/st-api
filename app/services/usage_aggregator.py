"""
Async usage aggregation service.

Moves non-critical per-request persistence off the hot request path:
- token_usage_history
- api_keys cumulative stats
- system_stats counters
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Dict, Optional, Tuple

from app.models.database import get_session_factory
from app.services.account_pool import AccountPoolService, get_account_pool_service
from app.services.api_key import APIKeyService, get_api_key_service
from app.services.connection_pool import ConcurrencyLimiter, get_concurrency_limiter
from app.services.stats import StatsService, get_stats_service

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


ENABLE_ASYNC_USAGE_AGGREGATION = _env_bool("ENABLE_ASYNC_USAGE_AGGREGATION", True)
ENABLE_SYSTEM_STATS_PERSIST = _env_bool("ENABLE_SYSTEM_STATS_PERSIST", True)
ASYNC_USAGE_QUEUE_MAX_SIZE = max(100, int(os.getenv("ASYNC_USAGE_QUEUE_MAX_SIZE", "20000")))
ASYNC_USAGE_MAX_BATCH_SIZE = max(10, int(os.getenv("ASYNC_USAGE_MAX_BATCH_SIZE", "300")))
ASYNC_USAGE_FLUSH_INTERVAL_SECONDS = max(
    0.05,
    float(os.getenv("ASYNC_USAGE_FLUSH_INTERVAL_SECONDS", "0.5")),
)
# SQLite has one writer; parallel flush workers add lock contention rather
# than commit throughput. Increase batch size before increasing this value.
ASYNC_USAGE_WORKERS = max(1, int(os.getenv("ASYNC_USAGE_WORKERS", "1")))
ASYNC_USAGE_DB_ACQUIRE_TIMEOUT_SECONDS = max(
    0.1,
    float(
        os.getenv(
            "ASYNC_USAGE_DB_ACQUIRE_TIMEOUT_SECONDS",
            os.getenv("DB_OP_ACQUIRE_TIMEOUT_SECONDS", os.getenv("REQUEST_QUEUE_TIMEOUT_SECONDS", "8")),
        )
    ),
)
ASYNC_USAGE_MAX_RETRIES = max(0, int(os.getenv("ASYNC_USAGE_MAX_RETRIES", "3")))


@dataclass
class UsageUpdateEvent:
    request_id: str
    api_key_id: str
    account_id: str
    input_tokens: int
    output_tokens: int
    total_cost: str
    retries: int = 0


class AsyncUsageAggregator:
    def __init__(
        self,
        *,
        account_pool: Optional[AccountPoolService] = None,
        api_key_service: Optional[APIKeyService] = None,
        stats_service: Optional[StatsService] = None,
        db_limiter: Optional[ConcurrencyLimiter] = None,
    ) -> None:
        self.account_pool = account_pool or get_account_pool_service()
        self.api_key_service = api_key_service or get_api_key_service()
        self.stats_service = stats_service or get_stats_service()
        self.db_limiter = db_limiter or get_concurrency_limiter()

        self._loop_id: Optional[int] = None
        self._queue: Optional[asyncio.Queue[Optional[UsageUpdateEvent]]] = None
        self._worker_tasks: list[asyncio.Task[None]] = []
        self._worker_lock: Optional[asyncio.Lock] = None

        self._dropped_events = 0
        self._flushed_events = 0
        self._flushed_batches = 0
        self._active_flush_workers = 0

    def _ensure_runtime_state(self) -> None:
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
        if self._loop_id == loop_id:
            return

        self._loop_id = loop_id
        self._queue = asyncio.Queue(maxsize=ASYNC_USAGE_QUEUE_MAX_SIZE)
        self._worker_tasks = []
        self._worker_lock = asyncio.Lock()

    async def _ensure_workers_started(self) -> None:
        self._ensure_runtime_state()
        assert self._worker_lock is not None
        async with self._worker_lock:
            active_workers = [task for task in self._worker_tasks if not task.done()]
            self._worker_tasks = active_workers
            missing = max(0, ASYNC_USAGE_WORKERS - len(active_workers))
            for worker_index in range(missing):
                task = asyncio.create_task(
                    self._worker_loop(len(active_workers) + worker_index + 1)
                )
                self._worker_tasks.append(task)

    async def enqueue_usage_update(
        self,
        *,
        request_id: str,
        api_key_id: str,
        account_id: str,
        input_tokens: int,
        output_tokens: int,
        total_cost: str,
    ) -> bool:
        if not ENABLE_ASYNC_USAGE_AGGREGATION:
            return False

        await self._ensure_workers_started()
        assert self._queue is not None

        event = UsageUpdateEvent(
            request_id=request_id,
            api_key_id=api_key_id,
            account_id=account_id,
            input_tokens=max(0, int(input_tokens)),
            output_tokens=max(0, int(output_tokens)),
            total_cost=str(total_cost or "0"),
        )

        try:
            self._queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            self._dropped_events += 1
            logger.warning(
                "Usage aggregation queue full; dropping usage event: request_id=%s dropped_total=%s",
                request_id,
                self._dropped_events,
            )
            return False

    async def _build_batch(
        self,
        first_item: UsageUpdateEvent,
        *,
        max_batch_size: int,
    ) -> list[UsageUpdateEvent]:
        assert self._queue is not None
        queue = self._queue
        batch = [first_item]
        deadline = time.monotonic() + ASYNC_USAGE_FLUSH_INTERVAL_SECONDS

        while len(batch) < max(1, int(max_batch_size)):
            try:
                next_item = queue.get_nowait()
            except asyncio.QueueEmpty:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    next_item = await asyncio.wait_for(queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    break

            if next_item is None:
                queue.task_done()
                queue.put_nowait(None)
                break

            batch.append(next_item)

        return batch

    async def _worker_loop(self, worker_index: int) -> None:
        assert self._queue is not None
        queue = self._queue

        while True:
            item = await queue.get()
            if item is None:
                queue.task_done()
                return

            batch = await self._build_batch(
                item,
                max_batch_size=ASYNC_USAGE_MAX_BATCH_SIZE,
            )

            self._active_flush_workers += 1
            try:
                await self._flush_batch(batch)
                self._flushed_events += len(batch)
                self._flushed_batches += 1
            except asyncio.TimeoutError:
                logger.warning(
                    "Async usage worker timed out acquiring DB permit: worker=%s batch_size=%s",
                    worker_index,
                    len(batch),
                )
                await self._retry_or_drop(batch)
            except Exception:
                logger.exception(
                    "Async usage worker flush failed: worker=%s batch_size=%s",
                    worker_index,
                    len(batch),
                )
                await self._retry_or_drop(batch)
            finally:
                self._active_flush_workers = max(0, self._active_flush_workers - 1)
                for _ in batch:
                    queue.task_done()

    @asynccontextmanager
    async def _acquire_db_permit(self):
        permit = self.db_limiter.acquire_db(timeout=ASYNC_USAGE_DB_ACQUIRE_TIMEOUT_SECONDS)
        await permit.__aenter__()
        try:
            yield
        finally:
            await permit.__aexit__(None, None, None)

    @staticmethod
    def _parse_cost_decimal(value: str) -> Decimal:
        try:
            parsed = Decimal(str(value or "0"))
            if parsed < 0:
                return Decimal("0")
            return parsed
        except (InvalidOperation, ValueError):
            return Decimal("0")

    async def _flush_batch(self, items: list[UsageUpdateEvent]) -> None:
        account_agg: Dict[str, Dict[str, int]] = defaultdict(lambda: {"in": 0, "out": 0, "req": 0})
        key_agg: Dict[str, Dict[str, object]] = defaultdict(
            lambda: {"in": 0, "out": 0, "req": 0, "cost": Decimal("0")}
        )
        total_input = 0
        total_output = 0
        total_requests = 0

        for item in items:
            account_bucket = account_agg[item.account_id]
            account_bucket["in"] += item.input_tokens
            account_bucket["out"] += item.output_tokens
            account_bucket["req"] += 1

            key_bucket = key_agg[item.api_key_id]
            key_bucket["in"] += item.input_tokens
            key_bucket["out"] += item.output_tokens
            key_bucket["req"] += 1
            key_bucket["cost"] += self._parse_cost_decimal(item.total_cost)

            total_input += item.input_tokens
            total_output += item.output_tokens
            total_requests += 1

        session_factory = get_session_factory()
        async with session_factory() as session:
            try:
                async with self._acquire_db_permit():
                    await self.account_pool.record_usage_history_bulk(
                        session,
                        [
                            {
                                "account_id": account_id,
                                "api_key_id": None,
                                "input_tokens": int(bucket["in"]),
                                "output_tokens": int(bucket["out"]),
                                "request_count": int(bucket["req"]),
                            }
                            for account_id, bucket in account_agg.items()
                        ],
                    )

                    await self.api_key_service.update_key_stats_bulk(
                        session,
                        [
                            {
                                "key_id": key_id,
                                "input_tokens": int(bucket["in"]),
                                "output_tokens": int(bucket["out"]),
                                "cost": str(bucket["cost"]),
                                "request_count": int(bucket["req"]),
                            }
                            for key_id, bucket in key_agg.items()
                        ],
                    )

                    if ENABLE_SYSTEM_STATS_PERSIST and total_requests > 0:
                        await self.stats_service.update_system_stats(
                            session,
                            input_tokens=total_input,
                            output_tokens=total_output,
                            request_count=total_requests,
                        )

                    await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _retry_or_drop(self, items: list[UsageUpdateEvent]) -> None:
        assert self._queue is not None
        for item in items:
            item.retries += 1
            if item.retries > ASYNC_USAGE_MAX_RETRIES:
                self._dropped_events += 1
                logger.warning(
                    "Dropped usage event after retries: request_id=%s retries=%s dropped_total=%s",
                    item.request_id,
                    item.retries,
                    self._dropped_events,
                )
                continue
            try:
                self._queue.put_nowait(item)
            except asyncio.QueueFull:
                self._dropped_events += 1
                logger.warning(
                    "Usage queue full during retry; dropped event: request_id=%s dropped_total=%s",
                    item.request_id,
                    self._dropped_events,
                )

    async def flush_once(self, *, max_batch_size: int = ASYNC_USAGE_MAX_BATCH_SIZE) -> int:
        self._ensure_runtime_state()
        assert self._queue is not None

        batch: list[UsageUpdateEvent] = []
        for _ in range(max(1, int(max_batch_size))):
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is None:
                self._queue.task_done()
                self._queue.put_nowait(None)
                break
            batch.append(item)

        if not batch:
            return 0

        try:
            await self._flush_batch(batch)
            self._flushed_events += len(batch)
            self._flushed_batches += 1
            return len(batch)
        except asyncio.TimeoutError:
            logger.warning(
                "Timed out flushing async usage batch size=%s; retrying individually",
                len(batch),
            )
            await self._retry_or_drop(batch)
            return 0
        except Exception:
            logger.exception("Failed to flush async usage batch size=%s", len(batch))
            await self._retry_or_drop(batch)
            return 0
        finally:
            for _ in batch:
                self._queue.task_done()

    async def drain_pending(self) -> None:
        self._ensure_runtime_state()
        if self._queue is None:
            return
        await self._queue.join()

    async def close(self) -> None:
        self._ensure_runtime_state()
        await self.drain_pending()
        if self._queue is not None:
            workers = [task for task in self._worker_tasks if not task.done()]
            for _ in workers:
                self._queue.put_nowait(None)
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)
        self._worker_tasks = []

    def stats(self) -> Dict[str, int]:
        self._ensure_runtime_state()
        queue_size = self._queue.qsize() if self._queue is not None else 0
        return {
            "queue_size": queue_size,
            "flushed_events": self._flushed_events,
            "flushed_batches": self._flushed_batches,
            "dropped_events": self._dropped_events,
            "configured_workers": ASYNC_USAGE_WORKERS,
            "active_workers": sum(1 for task in self._worker_tasks if not task.done()),
            "active_flush_workers": self._active_flush_workers,
        }


_usage_aggregator: Optional[AsyncUsageAggregator] = None


def get_usage_aggregator() -> AsyncUsageAggregator:
    global _usage_aggregator
    if _usage_aggregator is None:
        _usage_aggregator = AsyncUsageAggregator()
    return _usage_aggregator


async def close_usage_aggregator() -> None:
    global _usage_aggregator
    if _usage_aggregator is None:
        return
    await _usage_aggregator.close()
    _usage_aggregator = None
