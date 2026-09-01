import asyncio
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.services.account_pool as account_pool_module
from app.models.database import BackendAccount, Base
from app.services.account_pool import AccountPoolService


def test_account_capacity_reservation_is_atomic_across_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(account_pool_module, "ACCOUNT_MAX_INFLIGHT_REQUESTS_PER_ACCOUNT", 2)

    async def run():
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'leases.db'}",
            connect_args={"timeout": 10},
        )
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            session.add(BackendAccount(
                id="account-1", name="one", org_id="org", flow_id="flow",
                api_key_encrypted="encrypted", model_group="model", status="active",
                daily_quota=1000, daily_used=0, inflight_requests=0,
            ))
            await session.commit()

        async def reserve():
            async with factory() as session:
                selected = await AccountPoolService().get_available_account(session, "model")
                await session.commit()
                return selected is not None

        results = await asyncio.gather(*(reserve() for _ in range(8)))
        async with factory() as session:
            account = (await session.execute(select(BackendAccount))).scalar_one()
            assert sum(results) == 2
            assert account.inflight_requests == 2
        await engine.dispose()

    asyncio.run(run())


def test_stale_daily_quota_is_reactivated_on_selection(tmp_path, monkeypatch):
    monkeypatch.setattr(account_pool_module, "ACCOUNT_MAX_INFLIGHT_REQUESTS_PER_ACCOUNT", 2)

    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'daily.db'}")
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            session.add(BackendAccount(
                id="account-old", name="old", org_id="org", flow_id="flow",
                api_key_encrypted="encrypted", model_group="model", status="exhausted",
                daily_quota=1000, daily_used=1000, daily_usage_date=date(2024, 1, 1),
                inflight_requests=0,
            ))
            await session.commit()

        async with factory() as session:
            selected = await AccountPoolService().get_available_account(session, "model")
            await session.commit()
            assert selected is not None
            assert selected.status == "active"
            assert selected.daily_used == 0
            assert selected.inflight_requests == 1
        await engine.dispose()

    asyncio.run(run())
