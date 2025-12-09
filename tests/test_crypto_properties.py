"""
Property-Based Tests for Crypto Service

**Feature: stackai-to-api, Property 17: Sensitive Data Encryption**
**Validates: Requirements 9.4**

Tests that sensitive data (API keys, passwords) when stored, 
the stored value does not equal the original plaintext value.
"""

import pytest
from hypothesis import given, strategies as st, settings

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services.crypto import CryptoService


# Generate a valid Fernet key for testing
TEST_ENCRYPTION_KEY = CryptoService.generate_encryption_key()


class TestSensitiveDataEncryption:
    """
    Property 17: Sensitive Data Encryption
    
    *For any* stored sensitive data (API keys, passwords), 
    the stored value should not equal the original plaintext value.
    
    **Validates: Requirements 9.4**
    """
    
    @given(st.text(min_size=1, max_size=1000))
    @settings(max_examples=100)
    def test_encrypted_value_differs_from_plaintext(self, plaintext: str):
        """
        **Feature: stackai-to-api, Property 17: Sensitive Data Encryption**
        **Validates: Requirements 9.4**
        
        For any non-empty plaintext, the encrypted value should differ from the original.
        """
        crypto = CryptoService(TEST_ENCRYPTION_KEY)
        encrypted = crypto.encrypt(plaintext)
        
        # The encrypted value must not equal the plaintext
        assert encrypted != plaintext, (
            f"Encrypted value should differ from plaintext. "
            f"Plaintext: {plaintext[:50]}..."
        )

    
    @given(st.text(min_size=1, max_size=100))
    @settings(max_examples=100, deadline=None)  # bcrypt is intentionally slow
    def test_password_hash_differs_from_plaintext(self, password: str):
        """
        **Feature: stackai-to-api, Property 17: Sensitive Data Encryption**
        **Validates: Requirements 9.4**
        
        For any password, the bcrypt hash should differ from the original password.
        """
        hashed = CryptoService.hash_password(password)
        
        # The hash must not equal the plaintext password
        assert hashed != password, (
            f"Password hash should differ from plaintext. "
            f"Password: {password[:20]}..."
        )
    
    @given(st.text(min_size=1, max_size=1000))
    @settings(max_examples=100)
    def test_encryption_round_trip(self, plaintext: str):
        """
        **Feature: stackai-to-api, Property 17: Sensitive Data Encryption**
        **Validates: Requirements 9.4**
        
        For any plaintext, encrypting then decrypting should return the original value.
        This ensures the encryption is reversible (for API keys that need to be used).
        """
        crypto = CryptoService(TEST_ENCRYPTION_KEY)
        encrypted = crypto.encrypt(plaintext)
        decrypted = crypto.decrypt(encrypted)
        
        assert decrypted == plaintext, (
            f"Round-trip encryption failed. "
            f"Original: {plaintext[:50]}..., Decrypted: {decrypted[:50]}..."
        )
    
    @given(st.text(min_size=1, max_size=100))
    @settings(max_examples=100, deadline=None)  # bcrypt is intentionally slow
    def test_password_verification_works(self, password: str):
        """
        **Feature: stackai-to-api, Property 17: Sensitive Data Encryption**
        **Validates: Requirements 9.4**
        
        For any password, hashing and then verifying should succeed.
        """
        hashed = CryptoService.hash_password(password)
        
        assert CryptoService.verify_password(password, hashed), (
            f"Password verification failed for password: {password[:20]}..."
        )
    
    @given(st.text(min_size=1, max_size=50), st.text(min_size=1, max_size=50))
    @settings(max_examples=100, deadline=None)  # bcrypt is intentionally slow
    def test_different_passwords_have_different_hashes(self, password1: str, password2: str):
        """
        **Feature: stackai-to-api, Property 17: Sensitive Data Encryption**
        **Validates: Requirements 9.4**
        
        For any two different passwords, their hashes should be different.
        """
        # Skip if passwords are the same
        if password1 == password2:
            return
        
        hash1 = CryptoService.hash_password(password1)
        hash2 = CryptoService.hash_password(password2)
        
        # Different passwords should produce different hashes
        assert hash1 != hash2, (
            f"Different passwords should have different hashes. "
            f"Password1: {password1[:20]}..., Password2: {password2[:20]}..."
        )
    
    @given(st.text(min_size=43, max_size=100))
    @settings(max_examples=100)
    def test_api_key_hash_differs_from_original(self, api_key: str):
        """
        **Feature: stackai-to-api, Property 17: Sensitive Data Encryption**
        **Validates: Requirements 9.4**
        
        For any API key, the SHA-256 hash should differ from the original key.
        """
        hashed = CryptoService.hash_api_key(api_key)
        
        assert hashed != api_key, (
            f"API key hash should differ from original. "
            f"Key: {api_key[:20]}..."
        )
