"""
Property-Based Tests for Model Group Router Service

Tests for:
- Property 5: Model Group Routing (Requirements 2.3, 6.4)
"""

import pytest
import asyncio
import json
import uuid
from typing import List, Set
from hypothesis import given, strategies as st, settings, assume

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services.router import ModelGroupRouter
from app.services.account_pool import AccountPoolService
from app.services.api_key import APIKeyService
from app.services.crypto import CryptoService, init_crypto_service
from app.models.database import BackendAccount as StackAIAccount, ModelGroup, APIKey, Base

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


# Strategy for generating valid model group names
model_group_name_strategy = st.text(
    min_size=1, 
    max_size=20, 
    alphabet=st.characters(whitelist_categories=('L', 'N'), whitelist_characters='-_')
).filter(lambda x: len(x.strip()) > 0)


class TestModelGroupRouting:
    """
    Property 5: Model Group Routing
    
    *For any* request with a model parameter, the system should select an account
    from the corresponding model group's account pool.
    
    **Feature: stackai-to-api, Property 5: Model Group Routing**
    **Validates: Requirements 2.3, 6.4**
    """
    
    @given(
        model_groups=st.lists(
            model_group_name_strategy,
            min_size=1,
            max_size=3,
            unique=True
        ),
        accounts_per_group=st.integers(min_value=1, max_value=3)
    )
    @settings(max_examples=100, deadline=None)
    def test_route_request_selects_from_correct_model_group(
        self, model_groups: List[str], accounts_per_group: int
    ):
        """
        **Feature: stackai-to-api, Property 5: Model Group Routing**
        **Validates: Requirements 2.3, 6.4**
        
        For any model parameter, the router should select an account
        that belongs to the corresponding model group.
        """
        asyncio.get_event_loop().run_until_complete(
            self._test_route_selects_correct_group_async(model_groups, accounts_per_group)
        )
    
    async def _test_route_selects_correct_group_async(
        self, model_groups: List[str], accounts_per_group: int
    ):
        engine, session_factory = await create_test_db()
        
        try:
            async with session_factory() as session:
                account_pool_service = AccountPoolService()
                router = ModelGroupRouter(account_pool_service=account_pool_service)
                
                # Create accounts for each model group
                group_account_ids = {}
                for group in model_groups:
                    group_account_ids[group] = set()
                    for i in range(accounts_per_group):
                        account = await account_pool_service.create_account(
                            session=session,
                            name=f"Account {group}-{i}",
                            org_id=f"org-{group}-{i}",
                            flow_id=f"flow-{group}-{i}",
                            api_key=f"key-{group}-{i}",
                            model_group=group,
                            daily_quota=1000000
                        )
                        group_account_ids[group].add(account.id)
                
                await session.commit()
                
                # Test routing for each model group
                for target_group in model_groups:
                    account, error = await router.route_request(
                        session=session,
                        model=target_group,
                        api_key=None  # No API key restriction
                    )
                    
                    assert account is not None, (
                        f"Should find an account for model group '{target_group}'. "
                        f"Error: {error}"
                    )
                    assert account.model_group == target_group, (
                        f"Selected account should belong to model group '{target_group}', "
                        f"but got '{account.model_group}'"
                    )
                    assert account.id in group_account_ids[target_group], (
                        f"Selected account ID {account.id} should be in the "
                        f"expected set for group '{target_group}'"
                    )
        finally:
            await engine.dispose()
    
    @given(
        model_group=model_group_name_strategy,
        num_requests=st.integers(min_value=1, max_value=10)
    )
    @settings(max_examples=100, deadline=None)
    def test_route_request_only_selects_from_target_group(
        self, model_group: str, num_requests: int
    ):
        """
        **Feature: stackai-to-api, Property 5: Model Group Routing**
        **Validates: Requirements 2.3, 6.4**
        
        Multiple requests for the same model should always select
        accounts from that model group only.
        """
        asyncio.get_event_loop().run_until_complete(
            self._test_multiple_requests_same_group_async(model_group, num_requests)
        )
    
    async def _test_multiple_requests_same_group_async(
        self, model_group: str, num_requests: int
    ):
        engine, session_factory = await create_test_db()
        
        try:
            async with session_factory() as session:
                account_pool_service = AccountPoolService()
                router = ModelGroupRouter(account_pool_service=account_pool_service)
                
                # Create accounts for target group
                target_account_ids = set()
                for i in range(3):
                    account = await account_pool_service.create_account(
                        session=session,
                        name=f"Target Account {i}",
                        org_id=f"org-target-{i}",
                        flow_id=f"flow-target-{i}",
                        api_key=f"key-target-{i}",
                        model_group=model_group,
                        daily_quota=1000000
                    )
                    target_account_ids.add(account.id)
                
                # Create accounts for a different group
                other_group = f"other-{model_group}"
                for i in range(2):
                    await account_pool_service.create_account(
                        session=session,
                        name=f"Other Account {i}",
                        org_id=f"org-other-{i}",
                        flow_id=f"flow-other-{i}",
                        api_key=f"key-other-{i}",
                        model_group=other_group,
                        daily_quota=1000000
                    )
                
                await session.commit()
                
                # Make multiple requests and verify all select from target group
                for _ in range(num_requests):
                    account, error = await router.route_request(
                        session=session,
                        model=model_group,
                        api_key=None
                    )
                    
                    assert account is not None, f"Should find account. Error: {error}"
                    assert account.model_group == model_group, (
                        f"Account should be from '{model_group}', got '{account.model_group}'"
                    )
                    assert account.id in target_account_ids, (
                        f"Account {account.id} should be in target group accounts"
                    )
        finally:
            await engine.dispose()
    
    @given(model_group=model_group_name_strategy)
    @settings(max_examples=50, deadline=None)
    def test_route_request_returns_none_for_empty_group(self, model_group: str):
        """
        **Feature: stackai-to-api, Property 5: Model Group Routing**
        **Validates: Requirements 2.3, 6.4**
        
        When no accounts exist for a model group, routing should return None.
        """
        asyncio.get_event_loop().run_until_complete(
            self._test_empty_group_returns_none_async(model_group)
        )
    
    async def _test_empty_group_returns_none_async(self, model_group: str):
        engine, session_factory = await create_test_db()
        
        try:
            async with session_factory() as session:
                account_pool_service = AccountPoolService()
                router = ModelGroupRouter(account_pool_service=account_pool_service)
                
                # Don't create any accounts for the target group
                # Create accounts for a different group
                other_group = f"other-{model_group}"
                await account_pool_service.create_account(
                    session=session,
                    name="Other Account",
                    org_id="org-other",
                    flow_id="flow-other",
                    api_key="key-other",
                    model_group=other_group,
                    daily_quota=1000000
                )
                await session.commit()
                
                # Request for the empty group should return None
                account, error = await router.route_request(
                    session=session,
                    model=model_group,
                    api_key=None
                )
                
                assert account is None, (
                    f"Should return None for empty model group '{model_group}'"
                )
                assert error is not None, "Should return an error message"
        finally:
            await engine.dispose()


class TestModelGroupRoutingWithAPIKeyAuthorization:
    """
    Property 5: Model Group Routing with API Key Authorization
    
    *For any* request with a model parameter and API Key, the system should:
    1. Verify the API Key is authorized for the requested model group
    2. Select an account from the corresponding model group's account pool
    
    **Feature: stackai-to-api, Property 5: Model Group Routing**
    **Validates: Requirements 2.3, 6.4**
    """
    
    @given(
        authorized_groups=st.lists(
            model_group_name_strategy,
            min_size=1,
            max_size=3,
            unique=True
        ),
        requested_model=model_group_name_strategy
    )
    @settings(max_examples=100, deadline=None)
    def test_api_key_authorization_for_model_routing(
        self, authorized_groups: List[str], requested_model: str
    ):
        """
        **Feature: stackai-to-api, Property 5: Model Group Routing**
        **Validates: Requirements 2.3, 6.4**
        
        Routing should succeed only if the API Key is authorized for the model.
        """
        asyncio.get_event_loop().run_until_complete(
            self._test_api_key_authorization_async(authorized_groups, requested_model)
        )
    
    async def _test_api_key_authorization_async(
        self, authorized_groups: List[str], requested_model: str
    ):
        engine, session_factory = await create_test_db()
        
        try:
            async with session_factory() as session:
                account_pool_service = AccountPoolService()
                api_key_service = APIKeyService()
                router = ModelGroupRouter(
                    account_pool_service=account_pool_service,
                    api_key_service=api_key_service
                )
                
                # Create accounts for all groups (authorized + requested if different)
                all_groups = set(authorized_groups)
                all_groups.add(requested_model)
                
                for group in all_groups:
                    await account_pool_service.create_account(
                        session=session,
                        name=f"Account for {group}",
                        org_id=f"org-{group}",
                        flow_id=f"flow-{group}",
                        api_key=f"key-{group}",
                        model_group=group,
                        daily_quota=1000000
                    )
                
                # Create API Key with authorized groups
                raw_key, api_key = await api_key_service.generate_key(
                    session=session,
                    model_groups=authorized_groups
                )
                await session.commit()
                
                # Route request with API Key
                account, error = await router.route_request(
                    session=session,
                    model=requested_model,
                    api_key=api_key
                )
                
                if requested_model in authorized_groups:
                    # Should succeed - model is authorized
                    assert account is not None, (
                        f"Routing should succeed for authorized model '{requested_model}'. "
                        f"Authorized groups: {authorized_groups}. Error: {error}"
                    )
                    assert account.model_group == requested_model, (
                        f"Selected account should be from '{requested_model}'"
                    )
                else:
                    # Should fail - model is not authorized
                    assert account is None, (
                        f"Routing should fail for unauthorized model '{requested_model}'. "
                        f"Authorized groups: {authorized_groups}"
                    )
                    assert error is not None, "Should return an error message"
                    assert "not authorized" in error.lower(), (
                        f"Error should mention authorization. Got: {error}"
                    )
        finally:
            await engine.dispose()
    
    @given(
        model_groups=st.lists(
            model_group_name_strategy,
            min_size=2,
            max_size=4,
            unique=True
        )
    )
    @settings(max_examples=50, deadline=None)
    def test_api_key_can_access_all_authorized_groups(self, model_groups: List[str]):
        """
        **Feature: stackai-to-api, Property 5: Model Group Routing**
        **Validates: Requirements 2.3, 6.4**
        
        An API Key should be able to route to any of its authorized model groups.
        """
        asyncio.get_event_loop().run_until_complete(
            self._test_access_all_authorized_async(model_groups)
        )
    
    async def _test_access_all_authorized_async(self, model_groups: List[str]):
        engine, session_factory = await create_test_db()
        
        try:
            async with session_factory() as session:
                account_pool_service = AccountPoolService()
                api_key_service = APIKeyService()
                router = ModelGroupRouter(
                    account_pool_service=account_pool_service,
                    api_key_service=api_key_service
                )
                
                # Create accounts for all groups
                for group in model_groups:
                    await account_pool_service.create_account(
                        session=session,
                        name=f"Account for {group}",
                        org_id=f"org-{group}",
                        flow_id=f"flow-{group}",
                        api_key=f"key-{group}",
                        model_group=group,
                        daily_quota=1000000
                    )
                
                # Create API Key authorized for all groups
                raw_key, api_key = await api_key_service.generate_key(
                    session=session,
                    model_groups=model_groups
                )
                await session.commit()
                
                # Verify routing works for each authorized group
                for group in model_groups:
                    account, error = await router.route_request(
                        session=session,
                        model=group,
                        api_key=api_key
                    )
                    
                    assert account is not None, (
                        f"Should route to authorized group '{group}'. Error: {error}"
                    )
                    assert account.model_group == group, (
                        f"Account should be from '{group}', got '{account.model_group}'"
                    )
        finally:
            await engine.dispose()
    
    @given(
        authorized_group=model_group_name_strategy,
        unauthorized_group=model_group_name_strategy
    )
    @settings(max_examples=50, deadline=None)
    def test_api_key_cannot_access_unauthorized_group(
        self, authorized_group: str, unauthorized_group: str
    ):
        """
        **Feature: stackai-to-api, Property 5: Model Group Routing**
        **Validates: Requirements 2.3, 6.4**
        
        An API Key should not be able to route to unauthorized model groups.
        """
        # Skip if groups are the same
        assume(authorized_group != unauthorized_group)
        
        asyncio.get_event_loop().run_until_complete(
            self._test_cannot_access_unauthorized_async(authorized_group, unauthorized_group)
        )
    
    async def _test_cannot_access_unauthorized_async(
        self, authorized_group: str, unauthorized_group: str
    ):
        engine, session_factory = await create_test_db()
        
        try:
            async with session_factory() as session:
                account_pool_service = AccountPoolService()
                api_key_service = APIKeyService()
                router = ModelGroupRouter(
                    account_pool_service=account_pool_service,
                    api_key_service=api_key_service
                )
                
                # Create accounts for both groups
                for group in [authorized_group, unauthorized_group]:
                    await account_pool_service.create_account(
                        session=session,
                        name=f"Account for {group}",
                        org_id=f"org-{group}",
                        flow_id=f"flow-{group}",
                        api_key=f"key-{group}",
                        model_group=group,
                        daily_quota=1000000
                    )
                
                # Create API Key authorized only for one group
                raw_key, api_key = await api_key_service.generate_key(
                    session=session,
                    model_groups=[authorized_group]
                )
                await session.commit()
                
                # Should succeed for authorized group
                account, error = await router.route_request(
                    session=session,
                    model=authorized_group,
                    api_key=api_key
                )
                assert account is not None, (
                    f"Should route to authorized group '{authorized_group}'"
                )
                
                # Should fail for unauthorized group
                account, error = await router.route_request(
                    session=session,
                    model=unauthorized_group,
                    api_key=api_key
                )
                assert account is None, (
                    f"Should not route to unauthorized group '{unauthorized_group}'"
                )
                assert error is not None
                assert "not authorized" in error.lower()
        finally:
            await engine.dispose()
