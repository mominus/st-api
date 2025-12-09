"""
Property-Based Tests for API Key Service

Tests for:
- Property 10: API Key Uniqueness (Requirements 5.1)
- Property 11: API Key Validation (Requirements 5.7, 6.3)
- Property 12: API Key Quota Enforcement (Requirements 5.8)
- Property 13: API Key Revocation (Requirements 5.6)
- Property 14: API Key Display Masking (Requirements 5.5)
"""

import pytest
import asyncio
import json
from datetime import datetime, timedelta
from typing import List, Set
from hypothesis import given, strategies as st, settings, assume

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services.api_key import APIKeyService
from app.services.crypto import CryptoService, init_crypto_service
from app.models.database import APIKey, Base

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


class TestAPIKeyUniqueness:
    """
    Property 10: API Key Uniqueness
    
    *For any* two generated API Keys, their values should be different.
    
    **Feature: stackai-to-api, Property 10: API Key Uniqueness**
    **Validates: Requirements 5.1**
    """

    
    @given(num_keys=st.integers(min_value=2, max_value=20))
    @settings(max_examples=100, deadline=None)
    def test_generated_keys_are_unique(self, num_keys: int):
        """
        **Feature: stackai-to-api, Property 10: API Key Uniqueness**
        **Validates: Requirements 5.1**
        
        For any number of generated keys, all keys should be unique.
        """
        asyncio.get_event_loop().run_until_complete(
            self._test_keys_unique_async(num_keys)
        )
    
    async def _test_keys_unique_async(self, num_keys: int):
        engine, session_factory = await create_test_db()
        
        try:
            async with session_factory() as session:
                service = APIKeyService()
                
                generated_keys: Set[str] = set()
                generated_hashes: Set[str] = set()
                
                for i in range(num_keys):
                    raw_key, api_key = await service.generate_key(
                        session=session,
                        model_groups=["test-group"],
                        name=f"Test Key {i}"
                    )
                    
                    # Check raw key uniqueness
                    assert raw_key not in generated_keys, (
                        f"Duplicate key generated: {raw_key[:20]}..."
                    )
                    generated_keys.add(raw_key)
                    
                    # Check hash uniqueness
                    assert api_key.key_hash not in generated_hashes, (
                        f"Duplicate hash generated"
                    )
                    generated_hashes.add(api_key.key_hash)
                
                await session.commit()
                
                # Verify all keys are unique
                assert len(generated_keys) == num_keys
                assert len(generated_hashes) == num_keys
        finally:
            await engine.dispose()
    
    @given(st.data())
    @settings(max_examples=50, deadline=None)
    def test_key_prefix_format(self, data):
        """
        **Feature: stackai-to-api, Property 10: API Key Uniqueness**
        **Validates: Requirements 5.1**
        
        All generated keys should have the correct sk- prefix format.
        """
        asyncio.get_event_loop().run_until_complete(
            self._test_key_prefix_async()
        )
    
    async def _test_key_prefix_async(self):
        engine, session_factory = await create_test_db()
        
        try:
            async with session_factory() as session:
                service = APIKeyService()
                
                raw_key, api_key = await service.generate_key(
                    session=session,
                    model_groups=["test-group"]
                )
                
                # Key should start with sk-
                assert raw_key.startswith("sk-"), (
                    f"Key should start with 'sk-', got: {raw_key[:10]}..."
                )
                
                # Key prefix stored should match
                assert api_key.key_prefix == raw_key[:7], (
                    f"Key prefix mismatch: stored={api_key.key_prefix}, expected={raw_key[:7]}"
                )
                
                await session.commit()
        finally:
            await engine.dispose()



class TestAPIKeyValidation:
    """
    Property 11: API Key Validation
    
    *For any* API Key with associated model groups, requests for models in those
    groups should be allowed, and requests for models outside those groups should
    be rejected with 403.
    
    **Feature: stackai-to-api, Property 11: API Key Validation**
    **Validates: Requirements 5.7, 6.3**
    """
    
    @given(
        allowed_groups=st.lists(
            st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=('L', 'N'), whitelist_characters='-_')),
            min_size=1,
            max_size=5,
            unique=True
        ),
        requested_model=st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=('L', 'N'), whitelist_characters='-_'))
    )
    @settings(max_examples=100, deadline=None)
    def test_model_group_authorization(self, allowed_groups: List[str], requested_model: str):
        """
        **Feature: stackai-to-api, Property 11: API Key Validation**
        **Validates: Requirements 5.7, 6.3**
        
        Requests for authorized models should succeed, unauthorized should fail.
        """
        asyncio.get_event_loop().run_until_complete(
            self._test_model_authorization_async(allowed_groups, requested_model)
        )
    
    async def _test_model_authorization_async(self, allowed_groups: List[str], requested_model: str):
        engine, session_factory = await create_test_db()
        
        try:
            async with session_factory() as session:
                service = APIKeyService()
                
                raw_key, api_key = await service.generate_key(
                    session=session,
                    model_groups=allowed_groups
                )
                await session.commit()
                
                is_valid, error_msg, _ = await service.validate_key(
                    session, raw_key, requested_model
                )
                
                if requested_model in allowed_groups:
                    assert is_valid, (
                        f"Authorized model '{requested_model}' should be allowed. "
                        f"Allowed groups: {allowed_groups}. Error: {error_msg}"
                    )
                else:
                    assert not is_valid, (
                        f"Unauthorized model '{requested_model}' should be rejected. "
                        f"Allowed groups: {allowed_groups}"
                    )
                    assert "not authorized" in error_msg.lower()
        finally:
            await engine.dispose()
    
    @given(st.data())
    @settings(max_examples=50, deadline=None)
    def test_invalid_key_rejected(self, data):
        """
        **Feature: stackai-to-api, Property 11: API Key Validation**
        **Validates: Requirements 5.7, 6.3**
        
        Invalid/non-existent keys should be rejected.
        """
        asyncio.get_event_loop().run_until_complete(
            self._test_invalid_key_async()
        )
    
    async def _test_invalid_key_async(self):
        engine, session_factory = await create_test_db()
        
        try:
            async with session_factory() as session:
                service = APIKeyService()
                
                # Test with a fake key
                fake_key = "sk-thisisafakekeythatdoesnotexist123456"
                
                is_valid, error_msg, api_key = await service.validate_key(
                    session, fake_key
                )
                
                assert not is_valid, "Invalid key should be rejected"
                assert api_key is None
                assert "invalid" in error_msg.lower()
        finally:
            await engine.dispose()
    
    @given(st.data())
    @settings(max_examples=50, deadline=None)
    def test_expired_key_rejected(self, data):
        """
        **Feature: stackai-to-api, Property 11: API Key Validation**
        **Validates: Requirements 5.7, 6.3**
        
        Expired keys should be rejected.
        """
        asyncio.get_event_loop().run_until_complete(
            self._test_expired_key_async()
        )
    
    async def _test_expired_key_async(self):
        engine, session_factory = await create_test_db()
        
        try:
            async with session_factory() as session:
                service = APIKeyService()
                
                # Create an expired key
                expired_time = datetime.utcnow() - timedelta(days=1)
                raw_key, api_key = await service.generate_key(
                    session=session,
                    model_groups=["test-group"],
                    expires_at=expired_time
                )
                await session.commit()
                
                is_valid, error_msg, _ = await service.validate_key(
                    session, raw_key, "test-group"
                )
                
                assert not is_valid, "Expired key should be rejected"
                assert "expired" in error_msg.lower()
        finally:
            await engine.dispose()



class TestAPIKeyQuotaEnforcement:
    """
    Property 12: API Key Quota Enforcement
    
    *For any* API Key with quota Q and usage U, when U >= Q, subsequent requests
    should be rejected with 429.
    
    **Feature: stackai-to-api, Property 12: API Key Quota Enforcement**
    **Validates: Requirements 5.8**
    """
    
    @given(
        quota=st.integers(min_value=100, max_value=10000),
        usage_tokens=st.lists(
            st.integers(min_value=1, max_value=500),
            min_size=1,
            max_size=50
        )
    )
    @settings(max_examples=100, deadline=None)
    def test_quota_enforcement(self, quota: int, usage_tokens: List[int]):
        """
        **Feature: stackai-to-api, Property 12: API Key Quota Enforcement**
        **Validates: Requirements 5.8**
        
        When usage exceeds quota, validation should fail.
        """
        asyncio.get_event_loop().run_until_complete(
            self._test_quota_enforcement_async(quota, usage_tokens)
        )
    
    async def _test_quota_enforcement_async(self, quota: int, usage_tokens: List[int]):
        engine, session_factory = await create_test_db()
        
        try:
            async with session_factory() as session:
                service = APIKeyService()
                
                raw_key, api_key = await service.generate_key(
                    session=session,
                    model_groups=["test-group"],
                    quota=quota
                )
                await session.commit()
                
                total_used = 0
                for tokens in usage_tokens:
                    await service.update_usage(session, api_key.id, tokens)
                    total_used += tokens
                    await session.commit()
                    
                    is_valid, error_msg, _ = await service.validate_key(
                        session, raw_key, "test-group"
                    )
                    
                    if total_used >= quota:
                        assert not is_valid, (
                            f"Key should be rejected when usage ({total_used}) >= quota ({quota})"
                        )
                        assert "quota" in error_msg.lower()
                    else:
                        assert is_valid, (
                            f"Key should be valid when usage ({total_used}) < quota ({quota}). "
                            f"Error: {error_msg}"
                        )
        finally:
            await engine.dispose()
    
    @given(
        usage_tokens=st.lists(
            st.integers(min_value=1, max_value=1000),
            min_size=1,
            max_size=20
        )
    )
    @settings(max_examples=50, deadline=None)
    def test_unlimited_quota_always_valid(self, usage_tokens: List[int]):
        """
        **Feature: stackai-to-api, Property 12: API Key Quota Enforcement**
        **Validates: Requirements 5.8**
        
        Keys with no quota limit should always be valid regardless of usage.
        """
        asyncio.get_event_loop().run_until_complete(
            self._test_unlimited_quota_async(usage_tokens)
        )
    
    async def _test_unlimited_quota_async(self, usage_tokens: List[int]):
        engine, session_factory = await create_test_db()
        
        try:
            async with session_factory() as session:
                service = APIKeyService()
                
                # Create key with no quota (unlimited)
                raw_key, api_key = await service.generate_key(
                    session=session,
                    model_groups=["test-group"],
                    quota=None  # Unlimited
                )
                await session.commit()
                
                for tokens in usage_tokens:
                    await service.update_usage(session, api_key.id, tokens)
                    await session.commit()
                    
                    is_valid, error_msg, _ = await service.validate_key(
                        session, raw_key, "test-group"
                    )
                    
                    assert is_valid, (
                        f"Unlimited quota key should always be valid. Error: {error_msg}"
                    )
        finally:
            await engine.dispose()



class TestAPIKeyRevocation:
    """
    Property 13: API Key Revocation
    
    *For any* revoked API Key, all subsequent requests using that key should
    be rejected.
    
    **Feature: stackai-to-api, Property 13: API Key Revocation**
    **Validates: Requirements 5.6**
    """
    
    @given(
        model_groups=st.lists(
            st.text(min_size=1, max_size=10, alphabet=st.characters(whitelist_categories=('L', 'N'))),
            min_size=1,
            max_size=3,
            unique=True
        )
    )
    @settings(max_examples=100, deadline=None)
    def test_revoked_key_rejected(self, model_groups: List[str]):
        """
        **Feature: stackai-to-api, Property 13: API Key Revocation**
        **Validates: Requirements 5.6**
        
        After revocation, the key should be rejected for all requests.
        """
        asyncio.get_event_loop().run_until_complete(
            self._test_revoked_key_async(model_groups)
        )
    
    async def _test_revoked_key_async(self, model_groups: List[str]):
        engine, session_factory = await create_test_db()
        
        try:
            async with session_factory() as session:
                service = APIKeyService()
                
                raw_key, api_key = await service.generate_key(
                    session=session,
                    model_groups=model_groups
                )
                await session.commit()
                
                # Key should be valid before revocation
                is_valid, _, _ = await service.validate_key(
                    session, raw_key, model_groups[0]
                )
                assert is_valid, "Key should be valid before revocation"
                
                # Revoke the key
                success = await service.revoke_key(session, api_key.id)
                assert success, "Revocation should succeed"
                await session.commit()
                
                # Key should be rejected after revocation
                is_valid, error_msg, _ = await service.validate_key(
                    session, raw_key, model_groups[0]
                )
                assert not is_valid, "Revoked key should be rejected"
                assert "revoked" in error_msg.lower()
        finally:
            await engine.dispose()
    
    @given(st.data())
    @settings(max_examples=50, deadline=None)
    def test_revoke_by_raw_key(self, data):
        """
        **Feature: stackai-to-api, Property 13: API Key Revocation**
        **Validates: Requirements 5.6**
        
        Revocation by raw key should work the same as by ID.
        """
        asyncio.get_event_loop().run_until_complete(
            self._test_revoke_by_raw_async()
        )
    
    async def _test_revoke_by_raw_async(self):
        engine, session_factory = await create_test_db()
        
        try:
            async with session_factory() as session:
                service = APIKeyService()
                
                raw_key, api_key = await service.generate_key(
                    session=session,
                    model_groups=["test-group"]
                )
                await session.commit()
                
                # Revoke by raw key
                success = await service.revoke_key_by_raw(session, raw_key)
                assert success, "Revocation by raw key should succeed"
                await session.commit()
                
                # Key should be rejected
                is_valid, error_msg, _ = await service.validate_key(
                    session, raw_key, "test-group"
                )
                assert not is_valid, "Revoked key should be rejected"
                assert "revoked" in error_msg.lower()
        finally:
            await engine.dispose()
    
    @given(st.data())
    @settings(max_examples=50, deadline=None)
    def test_revoke_nonexistent_key_fails(self, data):
        """
        **Feature: stackai-to-api, Property 13: API Key Revocation**
        **Validates: Requirements 5.6**
        
        Revoking a non-existent key should return False.
        """
        asyncio.get_event_loop().run_until_complete(
            self._test_revoke_nonexistent_async()
        )
    
    async def _test_revoke_nonexistent_async(self):
        engine, session_factory = await create_test_db()
        
        try:
            async with session_factory() as session:
                service = APIKeyService()
                
                # Try to revoke a non-existent key
                success = await service.revoke_key(session, "nonexistent-id")
                assert not success, "Revoking non-existent key should fail"
                
                success = await service.revoke_key_by_raw(session, "sk-nonexistent")
                assert not success, "Revoking non-existent raw key should fail"
        finally:
            await engine.dispose()



class TestAPIKeyDisplayMasking:
    """
    Property 14: API Key Display Masking
    
    *For any* API Key displayed in the admin interface, only the prefix (first 7
    characters) and suffix (last 4 characters) should be visible, with the middle
    portion masked.
    
    **Feature: stackai-to-api, Property 14: API Key Display Masking**
    **Validates: Requirements 5.5**
    """
    
    @given(st.data())
    @settings(max_examples=100, deadline=None)
    def test_key_masking_preserves_prefix_and_suffix(self, data):
        """
        **Feature: stackai-to-api, Property 14: API Key Display Masking**
        **Validates: Requirements 5.5**
        
        Masked key should show prefix (first 7 chars) and suffix (last 4 chars).
        """
        asyncio.get_event_loop().run_until_complete(
            self._test_masking_async()
        )
    
    async def _test_masking_async(self):
        engine, session_factory = await create_test_db()
        
        try:
            async with session_factory() as session:
                service = APIKeyService()
                
                raw_key, api_key = await service.generate_key(
                    session=session,
                    model_groups=["test-group"]
                )
                
                masked = service.mask_key(raw_key)
                
                # Should preserve prefix (first 7 chars)
                assert masked.startswith(raw_key[:7]), (
                    f"Masked key should start with prefix. "
                    f"Expected prefix: {raw_key[:7]}, Got: {masked[:7]}"
                )
                
                # Should preserve suffix (last 4 chars)
                assert masked.endswith(raw_key[-4:]), (
                    f"Masked key should end with suffix. "
                    f"Expected suffix: {raw_key[-4:]}, Got: {masked[-4:]}"
                )
                
                # Should contain masking indicator
                assert "..." in masked, "Masked key should contain '...'"
                
                # Masked key should be shorter than original
                assert len(masked) < len(raw_key), (
                    f"Masked key should be shorter than original. "
                    f"Original: {len(raw_key)}, Masked: {len(masked)}"
                )
        finally:
            await engine.dispose()
    
    @given(st.data())
    @settings(max_examples=100, deadline=None)
    def test_masked_key_hides_middle(self, data):
        """
        **Feature: stackai-to-api, Property 14: API Key Display Masking**
        **Validates: Requirements 5.5**
        
        The middle portion of the key should not be visible in the masked version.
        """
        asyncio.get_event_loop().run_until_complete(
            self._test_middle_hidden_async()
        )
    
    async def _test_middle_hidden_async(self):
        engine, session_factory = await create_test_db()
        
        try:
            async with session_factory() as session:
                service = APIKeyService()
                
                raw_key, api_key = await service.generate_key(
                    session=session,
                    model_groups=["test-group"]
                )
                
                masked = service.mask_key(raw_key)
                
                # Extract the middle portion of the original key
                middle_portion = raw_key[7:-4]
                
                # The middle portion should not appear in the masked key
                assert middle_portion not in masked, (
                    f"Middle portion should be hidden. "
                    f"Middle: {middle_portion[:10]}..., Masked: {masked}"
                )
        finally:
            await engine.dispose()
    
    @given(
        short_key=st.text(min_size=1, max_size=11, alphabet=st.characters(whitelist_categories=('L', 'N')))
    )
    @settings(max_examples=50)
    def test_short_key_masking(self, short_key: str):
        """
        **Feature: stackai-to-api, Property 14: API Key Display Masking**
        **Validates: Requirements 5.5**
        
        Short keys should still be masked appropriately.
        """
        service = APIKeyService()
        masked = service.mask_key(short_key)
        
        # Short keys should still be masked
        if len(short_key) <= 11:
            # For very short keys, should show first 3 chars + ***
            assert "***" in masked or "..." in masked, (
                f"Short key should be masked. Original: {short_key}, Masked: {masked}"
            )
    
    @given(st.data())
    @settings(max_examples=50, deadline=None)
    def test_display_key_format(self, data):
        """
        **Feature: stackai-to-api, Property 14: API Key Display Masking**
        **Validates: Requirements 5.5**
        
        get_display_key should return a consistent format.
        """
        asyncio.get_event_loop().run_until_complete(
            self._test_display_key_async()
        )
    
    async def _test_display_key_async(self):
        engine, session_factory = await create_test_db()
        
        try:
            async with session_factory() as session:
                service = APIKeyService()
                
                raw_key, api_key = await service.generate_key(
                    session=session,
                    model_groups=["test-group"]
                )
                
                display = service.get_display_key(api_key)
                
                # Should start with the stored prefix
                assert display.startswith(api_key.key_prefix), (
                    f"Display key should start with stored prefix. "
                    f"Prefix: {api_key.key_prefix}, Display: {display}"
                )
                
                # Should contain masking
                assert "..." in display, "Display key should contain '...'"
                assert "****" in display, "Display key should contain '****'"
        finally:
            await engine.dispose()
