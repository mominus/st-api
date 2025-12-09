"""
Property-Based Tests for StackAI Client Stream Routing Decision

**Feature: stackai-to-api, Property 2: Stream Routing Decision**
**Validates: Requirements 1.4, 1.5**

Tests that the system routes to StackAI's /stream endpoint when stream=true
and to /run endpoint when stream=false or absent.
"""

import pytest
from hypothesis import given, strategies as st, settings
from typing import Optional

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services.stackai_client import StackAIClient


# Strategy for org_id - alphanumeric strings
org_ids = st.text(
    alphabet='abcdefghijklmnopqrstuvwxyz0123456789-_',
    min_size=5,
    max_size=36
).filter(lambda x: len(x.strip()) > 0)

# Strategy for flow_id - alphanumeric strings (UUID-like)
flow_ids = st.text(
    alphabet='abcdefghijklmnopqrstuvwxyz0123456789-',
    min_size=5,
    max_size=36
).filter(lambda x: len(x.strip()) > 0)

# Strategy for base URLs
base_urls = st.sampled_from([
    "https://api.stack-ai.com",
    "https://api.stack-ai.com/",
    "http://localhost:8000",
    "http://localhost:8000/",
    "https://custom.stackai.example.com",
])


class TestStreamRoutingDecision:
    """
    Property 2: Stream Routing Decision
    
    *For any* request with a `stream` parameter, the system should route to 
    StackAI's `/stream` endpoint when `stream=true` and to `/run` endpoint 
    when `stream=false` or absent.
    
    **Validates: Requirements 1.4, 1.5**
    """
    
    @given(stream=st.booleans())
    @settings(max_examples=100)
    def test_should_use_stream_returns_correct_decision(self, stream):
        """
        **Feature: stackai-to-api, Property 2: Stream Routing Decision**
        **Validates: Requirements 1.4, 1.5**
        
        For any boolean stream value, should_use_stream should return True
        only when stream is True.
        """
        client = StackAIClient()
        
        result = client.should_use_stream(stream)
        
        # Property: should_use_stream returns True iff stream is True
        assert result == (stream is True)
    
    @given(
        org_id=org_ids,
        flow_id=flow_ids,
        base_url=base_urls
    )
    @settings(max_examples=100)
    def test_run_url_uses_run_endpoint(self, org_id, flow_id, base_url):
        """
        **Feature: stackai-to-api, Property 2: Stream Routing Decision**
        **Validates: Requirements 1.5**
        
        For any org_id and flow_id, the run URL should always contain '/run/'
        and never contain '/stream/'.
        """
        client = StackAIClient(base_url=base_url)
        
        url = client._build_run_url(org_id, flow_id)
        
        # Property: run URL contains /run/ path
        assert "/inference/v0/run/" in url
        assert "/stream/" not in url
        assert org_id in url
        assert flow_id in url
    
    @given(
        org_id=org_ids,
        flow_id=flow_ids,
        base_url=base_urls
    )
    @settings(max_examples=100)
    def test_stream_url_uses_stream_endpoint(self, org_id, flow_id, base_url):
        """
        **Feature: stackai-to-api, Property 2: Stream Routing Decision**
        **Validates: Requirements 1.4**
        
        For any org_id and flow_id, the stream URL should always contain '/stream/'
        and never contain '/run/'.
        """
        client = StackAIClient(base_url=base_url)
        
        url = client._build_stream_url(org_id, flow_id)
        
        # Property: stream URL contains /stream/ path
        assert "/inference/v0/stream/" in url
        assert "/run/" not in url
        assert org_id in url
        assert flow_id in url
    
    @given(
        org_id=org_ids,
        flow_id=flow_ids
    )
    @settings(max_examples=100)
    def test_run_and_stream_urls_differ_only_in_endpoint(self, org_id, flow_id):
        """
        **Feature: stackai-to-api, Property 2: Stream Routing Decision**
        **Validates: Requirements 1.4, 1.5**
        
        For any org_id and flow_id, the run and stream URLs should be identical
        except for the endpoint path (/run/ vs /stream/).
        """
        client = StackAIClient()
        
        run_url = client._build_run_url(org_id, flow_id)
        stream_url = client._build_stream_url(org_id, flow_id)
        
        # Property: URLs differ only in the endpoint path
        run_url_normalized = run_url.replace("/run/", "/ENDPOINT/")
        stream_url_normalized = stream_url.replace("/stream/", "/ENDPOINT/")
        
        assert run_url_normalized == stream_url_normalized
    
    @given(stream=st.just(True))
    @settings(max_examples=100)
    def test_stream_true_routes_to_stream_endpoint(self, stream):
        """
        **Feature: stackai-to-api, Property 2: Stream Routing Decision**
        **Validates: Requirements 1.4**
        
        When stream=true, should_use_stream must return True, indicating
        the /stream endpoint should be used.
        """
        client = StackAIClient()
        
        result = client.should_use_stream(stream)
        
        # Property: stream=True always routes to stream endpoint
        assert result is True
    
    @given(stream=st.just(False))
    @settings(max_examples=100)
    def test_stream_false_routes_to_run_endpoint(self, stream):
        """
        **Feature: stackai-to-api, Property 2: Stream Routing Decision**
        **Validates: Requirements 1.5**
        
        When stream=false, should_use_stream must return False, indicating
        the /run endpoint should be used.
        """
        client = StackAIClient()
        
        result = client.should_use_stream(stream)
        
        # Property: stream=False always routes to run endpoint
        assert result is False
    
    @given(
        org_id=org_ids,
        flow_id=flow_ids,
        base_url=base_urls
    )
    @settings(max_examples=100)
    def test_url_structure_is_valid(self, org_id, flow_id, base_url):
        """
        **Feature: stackai-to-api, Property 2: Stream Routing Decision**
        **Validates: Requirements 1.4, 1.5**
        
        For any valid inputs, both run and stream URLs should have valid structure
        with the base URL, inference path, and correct parameters.
        """
        client = StackAIClient(base_url=base_url)
        
        run_url = client._build_run_url(org_id, flow_id)
        stream_url = client._build_stream_url(org_id, flow_id)
        
        # Property: URLs have valid structure
        base_normalized = base_url.rstrip("/")
        
        # Run URL structure
        expected_run_suffix = f"/inference/v0/run/{org_id}/{flow_id}"
        assert run_url == f"{base_normalized}{expected_run_suffix}"
        
        # Stream URL structure
        expected_stream_suffix = f"/inference/v0/stream/{org_id}/{flow_id}"
        assert stream_url == f"{base_normalized}{expected_stream_suffix}"
    
    @given(
        stream_values=st.lists(st.booleans(), min_size=1, max_size=20)
    )
    @settings(max_examples=100)
    def test_routing_decision_is_deterministic(self, stream_values):
        """
        **Feature: stackai-to-api, Property 2: Stream Routing Decision**
        **Validates: Requirements 1.4, 1.5**
        
        For any sequence of stream values, the routing decision should be
        deterministic - the same input always produces the same output.
        """
        client = StackAIClient()
        
        for stream in stream_values:
            # Call twice with same input
            result1 = client.should_use_stream(stream)
            result2 = client.should_use_stream(stream)
            
            # Property: same input always produces same output
            assert result1 == result2
            # Property: result matches expected behavior
            assert result1 == (stream is True)

