"""
Property-Based Tests for Configuration Persistence

**Feature: stackai-to-api, Property 16: Configuration Persistence Round-Trip**
**Validates: Requirements 9.2**

Tests that for any configuration change, after persisting and reloading,
the configuration should be equivalent to the original.
"""

import pytest
import tempfile
import os
from pathlib import Path
from hypothesis import given, strategies as st, settings, assume

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services.config import (
    ConfigService, AppConfig, ServerConfig, DatabaseConfig,
    SecurityConfig, AdminConfig, LoginProtectionConfig, LogConfig, CorsConfig
)


# Strategies for generating valid configuration values
valid_host_strategy = st.sampled_from([
    "0.0.0.0", "127.0.0.1", "localhost", "192.168.1.1", "10.0.0.1"
])

valid_port_strategy = st.integers(min_value=1, max_value=65535)

valid_log_level_strategy = st.sampled_from([
    "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"
])

# Safe text strategy that avoids problematic characters for JSON
safe_text_strategy = st.text(
    alphabet=st.characters(
        whitelist_categories=('L', 'N', 'P', 'S'),
        blacklist_characters='\x00\n\r\t\\"\''
    ),
    min_size=1,
    max_size=50
)

# Strategy for CORS origins
cors_origin_strategy = st.lists(
    st.sampled_from([
        "*", "http://localhost", "http://localhost:3000",
        "https://example.com", "http://127.0.0.1:8080"
    ]),
    min_size=1,
    max_size=5
)


class TestConfigurationPersistenceRoundTrip:
    """
    Property 16: Configuration Persistence Round-Trip
    
    *For any* configuration change, after persisting and reloading,
    the configuration should be equivalent to the original.
    
    **Validates: Requirements 9.2**
    """
    
    @given(
        host=valid_host_strategy,
        port=valid_port_strategy,
        debug=st.booleans()
    )
    @settings(max_examples=100)
    def test_server_config_round_trip(self, host: str, port: int, debug: bool):
        """
        **Feature: stackai-to-api, Property 16: Configuration Persistence Round-Trip**
        **Validates: Requirements 9.2**
        
        For any server configuration, saving and loading should preserve values.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "config.json")
            
            # Create and configure service
            service = ConfigService(config_path)
            service._config.server.host = host
            service._config.server.port = port
            service._config.server.debug = debug
            
            # Save to file
            assert service.save_to_file(), "Failed to save config"
            
            # Create new service and load
            service2 = ConfigService(config_path)
            assert service2.load_from_file(), "Failed to load config"
            
            # Verify round-trip
            assert service2.server.host == host, (
                f"Host mismatch: expected {host}, got {service2.server.host}"
            )
            assert service2.server.port == port, (
                f"Port mismatch: expected {port}, got {service2.server.port}"
            )
            assert service2.server.debug == debug, (
                f"Debug mismatch: expected {debug}, got {service2.server.debug}"
            )
    
    @given(
        jwt_expire_hours=st.integers(min_value=1, max_value=720)
    )
    @settings(max_examples=100)
    def test_security_config_round_trip(self, jwt_expire_hours: int):
        """
        **Feature: stackai-to-api, Property 16: Configuration Persistence Round-Trip**
        **Validates: Requirements 9.2**
        
        For any security configuration, saving and loading should preserve values.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "config.json")
            
            # Create and configure service
            service = ConfigService(config_path)
            service._config.security.jwt_expire_hours = jwt_expire_hours
            service._config.security.jwt_secret_key = "test-secret-key"
            service._config.security.encryption_key = "test-encryption-key"
            
            # Save to file
            assert service.save_to_file(), "Failed to save config"
            
            # Create new service and load
            service2 = ConfigService(config_path)
            assert service2.load_from_file(), "Failed to load config"
            
            # Verify round-trip
            assert service2.security.jwt_expire_hours == jwt_expire_hours, (
                f"JWT expire hours mismatch: expected {jwt_expire_hours}, "
                f"got {service2.security.jwt_expire_hours}"
            )
            assert service2.security.jwt_secret_key == "test-secret-key"
            assert service2.security.encryption_key == "test-encryption-key"
    
    @given(
        max_attempts=st.integers(min_value=1, max_value=100),
        lockout_minutes=st.integers(min_value=1, max_value=1440)
    )
    @settings(max_examples=100)
    def test_login_protection_config_round_trip(
        self, max_attempts: int, lockout_minutes: int
    ):
        """
        **Feature: stackai-to-api, Property 16: Configuration Persistence Round-Trip**
        **Validates: Requirements 9.2**
        
        For any login protection configuration, saving and loading should preserve values.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "config.json")
            
            # Create and configure service
            service = ConfigService(config_path)
            service._config.login_protection.max_attempts = max_attempts
            service._config.login_protection.lockout_minutes = lockout_minutes
            
            # Save to file
            assert service.save_to_file(), "Failed to save config"
            
            # Create new service and load
            service2 = ConfigService(config_path)
            assert service2.load_from_file(), "Failed to load config"
            
            # Verify round-trip
            assert service2.login_protection.max_attempts == max_attempts, (
                f"Max attempts mismatch: expected {max_attempts}, "
                f"got {service2.login_protection.max_attempts}"
            )
            assert service2.login_protection.lockout_minutes == lockout_minutes, (
                f"Lockout minutes mismatch: expected {lockout_minutes}, "
                f"got {service2.login_protection.lockout_minutes}"
            )
    
    @given(
        log_level=valid_log_level_strategy
    )
    @settings(max_examples=100)
    def test_log_config_round_trip(self, log_level: str):
        """
        **Feature: stackai-to-api, Property 16: Configuration Persistence Round-Trip**
        **Validates: Requirements 9.2**
        
        For any log configuration, saving and loading should preserve values.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "config.json")
            log_file = os.path.join(tmpdir, "test.log")
            
            # Create and configure service
            service = ConfigService(config_path)
            service._config.log.level = log_level
            service._config.log.file = log_file
            
            # Save to file
            assert service.save_to_file(), "Failed to save config"
            
            # Create new service and load
            service2 = ConfigService(config_path)
            assert service2.load_from_file(), "Failed to load config"
            
            # Verify round-trip
            assert service2.log.level == log_level, (
                f"Log level mismatch: expected {log_level}, got {service2.log.level}"
            )
            assert service2.log.file == log_file, (
                f"Log file mismatch: expected {log_file}, got {service2.log.file}"
            )
    
    @given(origins=cors_origin_strategy)
    @settings(max_examples=100)
    def test_cors_config_round_trip(self, origins: list):
        """
        **Feature: stackai-to-api, Property 16: Configuration Persistence Round-Trip**
        **Validates: Requirements 9.2**
        
        For any CORS configuration, saving and loading should preserve values.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "config.json")
            
            # Create and configure service
            service = ConfigService(config_path)
            service._config.cors.origins = origins
            
            # Save to file
            assert service.save_to_file(), "Failed to save config"
            
            # Create new service and load
            service2 = ConfigService(config_path)
            assert service2.load_from_file(), "Failed to load config"
            
            # Verify round-trip
            assert service2.cors.origins == origins, (
                f"CORS origins mismatch: expected {origins}, "
                f"got {service2.cors.origins}"
            )
    
    @given(
        host=valid_host_strategy,
        port=valid_port_strategy,
        debug=st.booleans(),
        jwt_expire_hours=st.integers(min_value=1, max_value=720),
        max_attempts=st.integers(min_value=1, max_value=100),
        lockout_minutes=st.integers(min_value=1, max_value=1440),
        log_level=valid_log_level_strategy,
        origins=cors_origin_strategy
    )
    @settings(max_examples=100)
    def test_full_config_round_trip(
        self,
        host: str,
        port: int,
        debug: bool,
        jwt_expire_hours: int,
        max_attempts: int,
        lockout_minutes: int,
        log_level: str,
        origins: list
    ):
        """
        **Feature: stackai-to-api, Property 16: Configuration Persistence Round-Trip**
        **Validates: Requirements 9.2**
        
        For any complete configuration, saving and loading should preserve all values.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "config.json")
            
            # Create and configure service with all settings
            service = ConfigService(config_path)
            service._config.server.host = host
            service._config.server.port = port
            service._config.server.debug = debug
            service._config.security.jwt_expire_hours = jwt_expire_hours
            service._config.security.jwt_secret_key = "test-jwt-secret"
            service._config.security.encryption_key = "test-enc-key"
            service._config.admin.username = "testadmin"
            service._config.admin.password = "testpass123"
            service._config.login_protection.max_attempts = max_attempts
            service._config.login_protection.lockout_minutes = lockout_minutes
            service._config.log.level = log_level
            service._config.log.file = "./test.log"
            service._config.cors.origins = origins
            
            # Save to file
            assert service.save_to_file(), "Failed to save config"
            
            # Create new service and load
            service2 = ConfigService(config_path)
            assert service2.load_from_file(), "Failed to load config"
            
            # Verify all values round-trip correctly
            assert service2.server.host == host
            assert service2.server.port == port
            assert service2.server.debug == debug
            assert service2.security.jwt_expire_hours == jwt_expire_hours
            assert service2.security.jwt_secret_key == "test-jwt-secret"
            assert service2.security.encryption_key == "test-enc-key"
            assert service2.admin.username == "testadmin"
            assert service2.admin.password == "testpass123"
            assert service2.login_protection.max_attempts == max_attempts
            assert service2.login_protection.lockout_minutes == lockout_minutes
            assert service2.log.level == log_level
            assert service2.log.file == "./test.log"
            assert service2.cors.origins == origins
    
    @given(
        host=valid_host_strategy,
        port=valid_port_strategy
    )
    @settings(max_examples=100)
    def test_to_dict_and_apply_round_trip(self, host: str, port: int):
        """
        **Feature: stackai-to-api, Property 16: Configuration Persistence Round-Trip**
        **Validates: Requirements 9.2**
        
        For any configuration, converting to dict and applying should preserve values.
        """
        # Create and configure service
        service = ConfigService()
        service._config.server.host = host
        service._config.server.port = port
        
        # Convert to dict
        config_dict = service.to_dict()
        
        # Create new service and apply dict
        service2 = ConfigService()
        service2._apply_config_dict(config_dict)
        
        # Verify round-trip
        assert service2.server.host == host
        assert service2.server.port == port
