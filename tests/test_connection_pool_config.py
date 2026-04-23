import asyncio

from app.services.connection_pool import ConcurrencyLimiter


def test_concurrency_limiter_tracks_active_streams():
    async def _run():
        limiter = ConcurrencyLimiter()
        limiter._loop_id = id(asyncio.get_running_loop())
        limiter._request_semaphore = asyncio.Semaphore(1)
        limiter._stream_semaphore = asyncio.Semaphore(1)
        limiter._db_semaphore = asyncio.Semaphore(1)
        limiter._lock = asyncio.Lock()
        limiter._active_requests = 0
        limiter._active_streams = 0
        limiter.reset_stats()

        async with limiter.acquire_stream(timeout=0.01):
            stats = limiter.get_stats()
            assert stats["active_streams"] == 1
            assert stats["total_streams"] == 1

        stats = limiter.get_stats()
        assert stats["active_streams"] == 0
        assert stats["total_streams"] == 1

    asyncio.run(_run())


def test_concurrency_limiter_tracks_request_wait_and_queue_depth():
    async def _run():
        limiter = ConcurrencyLimiter()
        limiter._loop_id = id(asyncio.get_running_loop())
        limiter._request_semaphore = asyncio.Semaphore(0)
        limiter._stream_semaphore = asyncio.Semaphore(1)
        limiter._db_semaphore = asyncio.Semaphore(1)
        limiter._lock = asyncio.Lock()
        limiter._active_requests = 0
        limiter._queued_requests = 0
        limiter.reset_stats()

        async def _waiter():
            async with limiter.acquire_request(timeout=0.1):
                stats_inside = limiter.get_stats()
                assert stats_inside["active_requests"] == 1
                assert stats_inside["queued_requests"] == 0

        task = asyncio.create_task(_waiter())
        await asyncio.sleep(0.01)

        stats_waiting = limiter.get_stats()
        assert stats_waiting["queued_requests"] == 1
        assert stats_waiting["active_requests"] == 0

        limiter._request_semaphore.release()
        await task

        stats = limiter.get_stats()
        assert stats["total_requests"] == 1
        assert stats["queued_requests"] == 0
        assert stats["avg_request_wait_ms"] > 0
        assert stats["p95_request_wait_ms"] >= stats["avg_request_wait_ms"]

    asyncio.run(_run())


def test_concurrency_limiter_counts_rejected_streams():
    async def _run():
        limiter = ConcurrencyLimiter()
        limiter._loop_id = id(asyncio.get_running_loop())
        limiter._request_semaphore = asyncio.Semaphore(1)
        limiter._stream_semaphore = asyncio.Semaphore(0)
        limiter._db_semaphore = asyncio.Semaphore(1)
        limiter._lock = asyncio.Lock()
        limiter._active_requests = 0
        limiter._active_streams = 0
        limiter.reset_stats()

        try:
            async with limiter.acquire_stream(timeout=0.01):
                raise AssertionError("expected timeout")
        except asyncio.TimeoutError:
            pass

        stats = limiter.get_stats()
        assert stats["rejected_streams"] == 1
        assert stats["active_streams"] == 0

    asyncio.run(_run())


def test_concurrency_limiter_tracks_db_wait_and_rejections():
    async def _run():
        limiter = ConcurrencyLimiter()
        limiter._loop_id = id(asyncio.get_running_loop())
        limiter._request_semaphore = asyncio.Semaphore(1)
        limiter._stream_semaphore = asyncio.Semaphore(1)
        limiter._db_semaphore = asyncio.Semaphore(0)
        limiter._lock = asyncio.Lock()
        limiter._active_db_ops = 0
        limiter._queued_db_ops = 0
        limiter.reset_stats()

        async def _db_waiter():
            async with limiter.acquire_db(timeout=0.1):
                stats_inside = limiter.get_stats()
                assert stats_inside["active_db_ops"] == 1
                assert stats_inside["queued_db_ops"] == 0

        task = asyncio.create_task(_db_waiter())
        await asyncio.sleep(0.01)

        stats_waiting = limiter.get_stats()
        assert stats_waiting["queued_db_ops"] == 1
        limiter._db_semaphore.release()
        await task
        limiter._db_semaphore = asyncio.Semaphore(0)

        try:
            async with limiter.acquire_db(timeout=0.01):
                raise AssertionError("expected timeout")
        except asyncio.TimeoutError:
            pass

        stats = limiter.get_stats()
        assert stats["total_db_ops"] == 1
        assert stats["rejected_db_ops"] == 1
        assert stats["active_db_ops"] == 0
        assert stats["queued_db_ops"] == 0
        assert stats["avg_db_wait_ms"] > 0

    asyncio.run(_run())
