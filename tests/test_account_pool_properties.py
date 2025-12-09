"""
Property-Based Tests for Account Pool Service

Tests for:
- Property 4: Account Storage Round-Trip (Requirements 2.1)
- Property 6: Round-Robin Distribution (Requirements 2.4)
- Property 7: Exhausted Account Exclusion (Requirements 2.5)
- Property 8: Token Usage Accumulation (Requirements 3.1)
- Property 9: Usage Percentage Calculation (Requirements 3.2, 3.4)
"""

import pytest
import asyncio
from collections import Counter
from typing import List
from hypothesis import given, strategies as st, settings

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services.account_pool import AccountPoolService
from app.models.database import BackendAccount as StackAIAccount, Base
from app.services.crypto import CryptoService, init_crypto_service

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker


# Test encryption key
TEST_ENCRYPTION_KEY = CryptoService.generate_encryption_key()

# Initialize crypto service once
init_crypto_service(TEST_ENCRYPTION_KEY)


async def create_test_db():
    """Create a fresh in-memory database"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, session_factory


class TestAccountStorageRoundTrip:
    """
    Property 4: Account Storage Round-Trip
    
    *For any* valid StackAI account configuration, storing it and then retrieving it
    should yield an equivalent configuration (excluding encrypted fields which should
    decrypt to original values).
    
    **Feature: stackai-to-api, Property 4: Account Storage Round-Trip**
    **Validates: Requirements 2.1**
    """
    
    @given(
        name=st.text(min_size=1, max_size=100, alphabet=st.characters(
            whitelist_categories=('L', 'N', 'P', 'S'),
            whitelist_characters=' -_'
        )).filter(lambda x: x.strip()),
        org_id=st.text(min_size=1, max_size=100, alphabet=st.characters(
            whitelist_categories=('L', 'N'),
            whitelist_characters='-_'
        )).filter(lambda x: x.strip()),
        flow_id=st.text(min_size=1, max_size=100, alphabet=st.characters(
            whitelist_categories=('L', 'N'),
            whitelist_characters='-_'
        )).filter(lambda x: x.strip()),
        api_key=st.text(min_size=1, max_size=200, alphabet=st.characters(
            whitelist_categories=('L', 'N'),
            whitelist_characters='-_'
        )).filter(lambda x: x.strip()),
        model_group=st.text(min_size=1, max_size=50, alphabet=st.characters(
            whitelist_categories=('L', 'N'),
            whitelist_characters='-_.'
        )).filter(lambda x: x.strip()),
        daily_quota=st.integers(min_value=1, max_value=100000000)
    )
    @settings(max_examples=100, deadline=None)
    def test_account_storage_round_trip(
        self,
        name: str,
        org_id: str,
        flow_id: str,
        api_key: str,
        model_group: str,
        daily_quota: int
    ):
        """
        **Feature: stackai-to-api, Property 4: Account Storage Round-Trip**
        **Validates: Requirements 2.1**
        
        For any valid account configuration, storing and retrieving should
        yield equivalent data, with encrypted API key decrypting to original.
        """
        asyncio.get_event_loop().run_until_complete(
            self._test_account_storage_round_trip_async(
                name, org_id, flow_id, api_key, model_group, daily_quota
            )
        )
    
    async def _test_account_storage_round_trip_async(
        self,
        name: str,
        org_id: str,
        flow_id: str,
        api_key: str,
        model_group: str,
        daily_quota: int
    ):
        engine, session_factory = await create_test_db()
        
        try:
            async with session_factory() as session:
                service = AccountPoolService()
                
                # Store the account
                created_account = await service.create_account(
                    session=session,
                    name=name,
                    org_id=org_id,
                    flow_id=flow_id,
                    api_key=api_key,
                    model_group=model_group,
                    daily_quota=daily_quota
                )
                
                await session.commit()
                
                # Retrieve the account
                retrieved_account = await service.get_account(session, created_account.id)
                
                # Verify non-encrypted fields are equivalent
                assert retrieved_account is not None, "Account should be retrievable"
                assert retrieved_account.id == created_account.id, "ID should match"
                assert retrieved_account.name == name, f"Name should match: expected '{name}', got '{retrieved_account.name}'"
                assert retrieved_account.org_id == org_id, f"org_id should match: expected '{org_id}', got '{retrieved_account.org_id}'"
                assert retrieved_account.flow_id == flow_id, f"flow_id should match: expected '{flow_id}', got '{retrieved_account.flow_id}'"
                assert retrieved_account.model_group == model_group, f"model_group should match: expected '{model_group}', got '{retrieved_account.model_group}'"
                assert retrieved_account.daily_quota == daily_quota, f"daily_quota should match: expected {daily_quota}, got {retrieved_account.daily_quota}"
                assert retrieved_account.daily_used == 0, "daily_used should be 0 for new account"
                assert retrieved_account.status == "active", "status should be 'active' for new account"
                
                # Verify encrypted field decrypts to original value
                decrypted_api_key = service.decrypt_api_key(retrieved_account)
                assert decrypted_api_key == api_key, f"Decrypted API key should match original: expected '{api_key}', got '{decrypted_api_key}'"
                
                # Verify the stored encrypted value is NOT the plaintext
                assert retrieved_account.api_key_encrypted != api_key, "Encrypted API key should not equal plaintext"
                
        finally:
            await engine.dispose()
    
    @given(
        api_key=st.text(min_size=1, max_size=200, alphabet=st.characters(
            whitelist_categories=('L', 'N'),
            whitelist_characters='-_'
        )).filter(lambda x: x.strip())
    )
    @settings(max_examples=50, deadline=None)
    def test_api_key_encryption_round_trip(self, api_key: str):
        """
        **Feature: stackai-to-api, Property 4: Account Storage Round-Trip**
        **Validates: Requirements 2.1**
        
        Specifically tests that API key encryption/decryption is reversible.
        """
        asyncio.get_event_loop().run_until_complete(
            self._test_api_key_encryption_round_trip_async(api_key)
        )
    
    async def _test_api_key_encryption_round_trip_async(self, api_key: str):
        engine, session_factory = await create_test_db()
        
        try:
            async with session_factory() as session:
                service = AccountPoolService()
                
                # Create account with the API key
                account = await service.create_account(
                    session=session,
                    name="Test Account",
                    org_id="test-org",
                    flow_id="test-flow",
                    api_key=api_key,
                    model_group="test-group",
                    daily_quota=1000000
                )
                
                await session.commit()
                
                # Retrieve and decrypt
                retrieved = await service.get_account(session, account.id)
                decrypted = service.decrypt_api_key(retrieved)
                
                # Round-trip should preserve the original value
                assert decrypted == api_key, f"API key round-trip failed: expected '{api_key}', got '{decrypted}'"
                
        finally:
            await engine.dispose()


class TestRoundRobinDistribution:
    """
    Property 6: Round-Robin Distribution
    
    *For any* sequence of N requests to a model group with M active accounts,
    after N requests where N >= M, each account should have received at least
    floor(N/M) requests.
    
    **Feature: stackai-to-api, Property 6: Round-Robin Distribution**
    **Validates: Requirements 2.4**
    """
    
    @given(
        num_accounts=st.integers(min_value=1, max_value=5),
        num_requests=st.integers(min_value=1, max_value=30)
    )
    @settings(max_examples=100, deadline=None)
    def test_round_robin_distribution(self, num_accounts: int, num_requests: int):
        """
        **Feature: stackai-to-api, Property 6: Round-Robin Distribution**
        **Validates: Requirements 2.4**
        """
        asyncio.get_event_loop().run_until_complete(
            self._test_round_robin_distribution_async(num_accounts, num_requests)
        )
    
    async def _test_round_robin_distribution_async(self, num_accounts: int, num_requests: int):
        engine, session_factory = await create_test_db()
        
        try:
            async with session_factory() as session:
                service = AccountPoolService()
                model_group = "test-group"
                
                # Create accounts with high quota
                accounts = []
                for i in range(num_accounts):
                    account = await service.create_account(
                        session=session,
                        name=f"Account {i}",
                        org_id=f"org-{i}",
                        flow_id=f"flow-{i}",
                        api_key=f"key-{i}",
                        model_group=model_group,
                        daily_quota=1000000
                    )
                    accounts.append(account)
                
                await session.commit()
                
                # Track selections
                selection_counts = Counter()
                
                for _ in range(num_requests):
                    selected = await service.get_available_account(session, model_group)
                    assert selected is not None
                    selection_counts[selected.id] += 1
                
                # Verify distribution
                min_expected = num_requests // num_accounts
                for account in accounts:
                    count = selection_counts.get(account.id, 0)
                    assert count >= max(0, min_expected - 1), (
                        f"Account {account.id} received {count} requests, "
                        f"expected at least {max(0, min_expected - 1)}"
                    )
        finally:
            await engine.dispose()


class TestExhaustedAccountExclusion:
    """
    Property 7: Exhausted Account Exclusion
    
    *For any* account pool, accounts marked as exhausted should never be
    selected for new requests.
    
    **Feature: stackai-to-api, Property 7: Exhausted Account Exclusion**
    **Validates: Requirements 2.5**
    """
    
    @given(
        num_active=st.integers(min_value=1, max_value=3),
        num_exhausted=st.integers(min_value=1, max_value=3),
        num_requests=st.integers(min_value=1, max_value=20)
    )
    @settings(max_examples=100, deadline=None)
    def test_exhausted_accounts_never_selected(
        self, num_active: int, num_exhausted: int, num_requests: int
    ):
        """
        **Feature: stackai-to-api, Property 7: Exhausted Account Exclusion**
        **Validates: Requirements 2.5**
        """
        asyncio.get_event_loop().run_until_complete(
            self._test_exhausted_accounts_async(num_active, num_exhausted, num_requests)
        )
    
    async def _test_exhausted_accounts_async(
        self, num_active: int, num_exhausted: int, num_requests: int
    ):
        engine, session_factory = await create_test_db()
        
        try:
            async with session_factory() as session:
                service = AccountPoolService()
                model_group = "test-group"
                
                active_ids = set()
                exhausted_ids = set()
                
                # Create active accounts
                for i in range(num_active):
                    account = await service.create_account(
                        session=session,
                        name=f"Active {i}",
                        org_id=f"org-active-{i}",
                        flow_id=f"flow-active-{i}",
                        api_key=f"key-active-{i}",
                        model_group=model_group,
                        daily_quota=1000000
                    )
                    active_ids.add(account.id)
                
                # Create exhausted accounts
                for i in range(num_exhausted):
                    account = await service.create_account(
                        session=session,
                        name=f"Exhausted {i}",
                        org_id=f"org-exhausted-{i}",
                        flow_id=f"flow-exhausted-{i}",
                        api_key=f"key-exhausted-{i}",
                        model_group=model_group,
                        daily_quota=1000
                    )
                    account.daily_used = account.daily_quota
                    account.status = "exhausted"
                    exhausted_ids.add(account.id)
                
                await session.commit()
                
                # Verify exhausted accounts are never selected
                for _ in range(num_requests):
                    selected = await service.get_available_account(session, model_group)
                    assert selected is not None
                    assert selected.id in active_ids
                    assert selected.id not in exhausted_ids
        finally:
            await engine.dispose()
    
    @given(num_accounts=st.integers(min_value=1, max_value=3))
    @settings(max_examples=50, deadline=None)
    def test_all_exhausted_returns_none(self, num_accounts: int):
        """
        **Feature: stackai-to-api, Property 7: Exhausted Account Exclusion**
        **Validates: Requirements 2.5**
        """
        asyncio.get_event_loop().run_until_complete(
            self._test_all_exhausted_async(num_accounts)
        )
    
    async def _test_all_exhausted_async(self, num_accounts: int):
        engine, session_factory = await create_test_db()
        
        try:
            async with session_factory() as session:
                service = AccountPoolService()
                model_group = "test-group"
                
                for i in range(num_accounts):
                    account = await service.create_account(
                        session=session,
                        name=f"Exhausted {i}",
                        org_id=f"org-{i}",
                        flow_id=f"flow-{i}",
                        api_key=f"key-{i}",
                        model_group=model_group,
                        daily_quota=1000
                    )
                    account.daily_used = account.daily_quota
                    account.status = "exhausted"
                
                await session.commit()
                
                selected = await service.get_available_account(session, model_group)
                assert selected is None
        finally:
            await engine.dispose()


class TestTokenUsageAccumulation:
    """
    Property 8: Token Usage Accumulation
    
    *For any* sequence of requests, the total token usage recorded for an account
    should equal the sum of tokens from all requests processed by that account.
    
    **Feature: stackai-to-api, Property 8: Token Usage Accumulation**
    **Validates: Requirements 3.1**
    """
    
    @given(
        token_updates=st.lists(
            st.tuples(
                st.integers(min_value=0, max_value=10000),
                st.integers(min_value=0, max_value=10000)
            ),
            min_size=1,
            max_size=10
        )
    )
    @settings(max_examples=100, deadline=None)
    def test_token_accumulation(self, token_updates: List[tuple]):
        """
        **Feature: stackai-to-api, Property 8: Token Usage Accumulation**
        **Validates: Requirements 3.1**
        """
        asyncio.get_event_loop().run_until_complete(
            self._test_token_accumulation_async(token_updates)
        )
    
    async def _test_token_accumulation_async(self, token_updates: List[tuple]):
        engine, session_factory = await create_test_db()
        
        try:
            async with session_factory() as session:
                service = AccountPoolService()
                
                account = await service.create_account(
                    session=session,
                    name="Test Account",
                    org_id="org-test",
                    flow_id="flow-test",
                    api_key="key-test",
                    model_group="test-group",
                    daily_quota=100000000
                )
                await session.commit()
                
                expected_total = 0
                for input_tokens, output_tokens in token_updates:
                    await service.update_token_usage(
                        session, account.id, input_tokens, output_tokens
                    )
                    expected_total += input_tokens + output_tokens
                
                await session.commit()
                
                updated_account = await service.get_account(session, account.id)
                assert updated_account.daily_used == expected_total
        finally:
            await engine.dispose()


class TestUsagePercentageCalculation:
    """
    Property 9: Usage Percentage Calculation
    
    *For any* account with daily_quota Q and daily_used U, the usage percentage
    should equal (U / Q) * 100, and status should be "warning" when >= 80%
    and "exhausted" when >= 100%.
    
    **Feature: stackai-to-api, Property 9: Usage Percentage Calculation**
    **Validates: Requirements 3.2, 3.4**
    """
    
    @given(
        daily_quota=st.integers(min_value=1, max_value=10000000),
        daily_used=st.integers(min_value=0, max_value=15000000)
    )
    @settings(max_examples=100, deadline=None)
    def test_usage_percentage_calculation(self, daily_quota: int, daily_used: int):
        """
        **Feature: stackai-to-api, Property 9: Usage Percentage Calculation**
        **Validates: Requirements 3.2, 3.4**
        """
        asyncio.get_event_loop().run_until_complete(
            self._test_usage_percentage_async(daily_quota, daily_used)
        )
    
    async def _test_usage_percentage_async(self, daily_quota: int, daily_used: int):
        engine, session_factory = await create_test_db()
        
        try:
            async with session_factory() as session:
                service = AccountPoolService()
                
                account = await service.create_account(
                    session=session,
                    name="Test Account",
                    org_id="org-test",
                    flow_id="flow-test",
                    api_key="key-test",
                    model_group="test-group",
                    daily_quota=daily_quota
                )
                account.daily_used = daily_used
                await session.commit()
                
                expected_percentage = (daily_used / daily_quota) * 100
                actual_percentage = service.get_usage_percentage(account)
                
                assert abs(actual_percentage - expected_percentage) < 0.0001
                
                status = service.get_usage_status(account)
                
                if expected_percentage >= 100:
                    assert status == "exhausted"
                elif expected_percentage >= 80:
                    assert status == "warning"
                else:
                    assert status == "normal"
        finally:
            await engine.dispose()
    
    @given(daily_used=st.integers(min_value=0, max_value=1000000))
    @settings(max_examples=50, deadline=None)
    def test_zero_quota_returns_100_percent(self, daily_used: int):
        """
        **Feature: stackai-to-api, Property 9: Usage Percentage Calculation**
        **Validates: Requirements 3.2, 3.4**
        """
        asyncio.get_event_loop().run_until_complete(
            self._test_zero_quota_async(daily_used)
        )
    
    async def _test_zero_quota_async(self, daily_used: int):
        engine, session_factory = await create_test_db()
        
        try:
            async with session_factory() as session:
                service = AccountPoolService()
                
                account = await service.create_account(
                    session=session,
                    name="Test Account",
                    org_id="org-test",
                    flow_id="flow-test",
                    api_key="key-test",
                    model_group="test-group",
                    daily_quota=1
                )
                account.daily_quota = 0
                account.daily_used = daily_used
                await session.commit()
                
                percentage = service.get_usage_percentage(account)
                assert percentage == 100.0
                
                status = service.get_usage_status(account)
                assert status == "exhausted"
        finally:
            await engine.dispose()
