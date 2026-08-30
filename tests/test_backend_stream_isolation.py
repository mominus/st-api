import asyncio

from app.services.backend_client import BackendClient


def test_waiting_stream_does_not_hold_shared_connection_slot():
    async def run():
        client = BackendClient()
        client._ensure_runtime_state()
        client._backend_slot_semaphore = asyncio.Semaphore(2)
        client._backend_stream_slot_semaphore = asyncio.Semaphore(1)
        client.http_pool_timeout = 0.5

        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        second_entered = asyncio.Event()

        async def first_stream():
            async with client._acquire_backend_slot(kind="stream"):
                first_entered.set()
                await release_first.wait()

        async def second_stream():
            await first_entered.wait()
            async with client._acquire_backend_slot(kind="stream"):
                second_entered.set()

        first = asyncio.create_task(first_stream())
        second = asyncio.create_task(second_stream())
        await first_entered.wait()
        await asyncio.sleep(0.02)

        # The second stream is queued at the stream gate, so the shared slot is
        # still available to a short synchronous request.
        async with client._acquire_backend_slot(kind="sync"):
            assert second_entered.is_set() is False

        release_first.set()
        await asyncio.gather(first, second)

    asyncio.run(run())
