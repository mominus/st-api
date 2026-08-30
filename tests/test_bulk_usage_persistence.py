import asyncio

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.database import APIKey, Base, TokenUsageHistory
from app.services.account_pool import AccountPoolService
from app.services.api_key import APIKeyService


def test_bulk_usage_upserts_reduce_writes_and_enforce_key_quota(tmp_path):
    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'usage.db'}")
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text(
                "CREATE UNIQUE INDEX uq_test_usage_bucket ON token_usage_history "
                "(date, COALESCE(account_id, ''), COALESCE(api_key_id, ''))"
            ))

        async with factory() as session:
            session.add(APIKey(
                id="key-1", key_hash="hash", key_prefix="sk-test",
                model_groups='["model"]', status="active", token_quota=10,
                total_requests=0, total_tokens=0, total_cost="0",
            ))
            await session.commit()

            account_pool = AccountPoolService()
            key_service = APIKeyService()
            buckets = [{
                "account_id": "account-1", "api_key_id": None,
                "input_tokens": 3, "output_tokens": 2, "request_count": 1,
            }]
            await account_pool.record_usage_history_bulk(session, buckets)
            await account_pool.record_usage_history_bulk(session, buckets)
            await key_service.update_key_stats_bulk(session, [{
                "key_id": "key-1", "input_tokens": 6, "output_tokens": 5,
                "request_count": 2, "cost": "0.25",
            }])
            await session.commit()

            history = (await session.execute(select(TokenUsageHistory))).scalar_one()
            key = (await session.execute(select(APIKey).where(APIKey.id == "key-1"))).scalar_one()
            assert (history.input_tokens, history.output_tokens, history.request_count) == (6, 4, 2)
            assert (key.total_requests, key.total_tokens, key.status) == (2, 11, "exhausted")
            assert float(key.total_cost) == 0.25

        await engine.dispose()

    asyncio.run(run())
