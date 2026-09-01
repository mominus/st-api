import asyncio

import httpx

from app.services.backend_client import BackendAPIError, BackendClient


def test_sync_backend_response_size_is_bounded(monkeypatch):
    monkeypatch.setenv("BACKEND_MAX_RESPONSE_BYTES", "1024")

    async def run():
        client = BackendClient()
        client._ensure_runtime_state()
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 1025)),
        )
        try:
            await client.run("org", "flow", "secret", {"in-0": "hi"})
            raise AssertionError("expected BackendAPIError")
        except BackendAPIError as exc:
            assert "size limit" in exc.message
        finally:
            await client.close()

    asyncio.run(run())


def test_stream_backend_line_size_is_bounded(monkeypatch):
    monkeypatch.setenv("BACKEND_MAX_STREAM_LINE_BYTES", "1024")

    async def run():
        client = BackendClient()
        client._ensure_runtime_state()
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 1025 + b"\n")),
        )
        try:
            async for _ in client.stream("org", "flow", "secret", {"in-0": "hi"}):
                pass
            raise AssertionError("expected BackendAPIError")
        except BackendAPIError as exc:
            assert "size limit" in exc.message
        finally:
            await client.close()

    asyncio.run(run())
