"""
Property-Based Tests for Error Handler Service

**Feature: stackai-to-api, Property 15: Error Response Format**
**Validates: Requirements 8.1**

Tests that converting StackAI error responses to OpenAI/Anthropic/Gemini formats
produces valid error responses conforming to each API's error format specification.
"""

import pytest
from hypothesis import given, strategies as st, settings
from typing import Dict, Any, Optional

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services.error_handler import (
    ErrorHandler,
    ErrorType,
    APIError,
    get_error_handler,
)


# ============================================================================
# Strategies for generating test data
# ============================================================================

# Strategy for non-empty error messages
error_messages = st.text(min_size=1, max_size=200).filter(lambda x: len(x.strip()) > 0)

# Strategy for error codes
error_codes = st.sampled_from([
    "invalid_api_key", "unauthorized", "forbidden", "not_found",
    "rate_limit", "quota_exceeded", "invalid_request", "bad_request",
    "server_error", "internal_error", "timeout", "service_unavailable",
    "unknown_error", None
])

# Strategy for HTTP status codes
http_status_codes = st.sampled_from([400, 401, 403, 404, 429, 500, 502, 503])

# Strategy for error types
error_types = st.sampled_from(list(ErrorType))

# Strategy for target API formats
target_formats = st.sampled_from(["openai", "anthropic", "gemini"])


# Strategy for generating StackAI-like error responses
@st.composite
def stackai_error_responses(draw):
    """Generate various StackAI error response formats"""
    message = draw(error_messages)
    code = draw(error_codes)
    
    # StackAI can return errors in various formats
    format_type = draw(st.integers(min_value=0, max_value=4))
    
    if format_type == 0:
        # Simple format: {"message": "...", "code": "..."}
        response = {"message": message}
        if code:
            response["code"] = code
        return response
    elif format_type == 1:
        # Nested format: {"error": {"message": "...", "type": "..."}}
        response = {"error": {"message": message}}
        if code:
            response["error"]["type"] = code
        return response
    elif format_type == 2:
        # Alternative format: {"error_message": "...", "error_code": "..."}
        response = {"error_message": message}
        if code:
            response["error_code"] = code
        return response
    elif format_type == 3:
        # String error format: {"error": "..."}
        return {"error": message}
    else:
        # Detail format: {"detail": "..."}
        return {"detail": message}


# Strategy for generating APIError objects
@st.composite
def api_errors(draw):
    """Generate valid APIError objects"""
    error_type = draw(error_types)
    message = draw(error_messages)
    code = draw(st.one_of(st.none(), st.text(min_size=1, max_size=50).filter(lambda x: len(x.strip()) > 0)))
    param = draw(st.one_of(st.none(), st.text(min_size=1, max_size=50).filter(lambda x: len(x.strip()) > 0)))
    
    # Get appropriate status code for error type
    status_code = ErrorHandler.ERROR_STATUS_CODES.get(error_type, 500)
    
    return APIError(
        error_type=error_type,
        message=message,
        code=code,
        status_code=status_code,
        param=param
    )


# ============================================================================
# Property Tests
# ============================================================================

class TestErrorResponseFormat:
    """
    Property 15: Error Response Format
    
    *For any* StackAI error response, the converted error should conform to 
    the target API's error format (OpenAI/Anthropic/Gemini).
    
    **Validates: Requirements 8.1**
    """
    
    @given(error=api_errors())
    @settings(max_examples=100)
    def test_openai_error_format_structure(self, error: APIError):
        """
        **Feature: stackai-to-api, Property 15: Error Response Format**
        **Validates: Requirements 8.1**
        
        For any APIError, the OpenAI format conversion should produce a valid
        OpenAI error response with required fields.
        
        OpenAI error format:
        {
            "error": {
                "message": "Error description",
                "type": "invalid_request_error",
                "code": "invalid_api_key",
                "param": null
            }
        }
        """
        handler = ErrorHandler()
        result = handler.to_openai_error(error)
        
        # Must have top-level "error" key
        assert "error" in result
        assert isinstance(result["error"], dict)
        
        # Must have required fields in error object
        error_obj = result["error"]
        assert "message" in error_obj
        assert "type" in error_obj
        assert "code" in error_obj
        assert "param" in error_obj
        
        # Message must be the original message
        assert error_obj["message"] == error.message
        
        # Type must be a valid error type string
        assert isinstance(error_obj["type"], str)
        assert error_obj["type"] == error.error_type.value
    
    @given(error=api_errors())
    @settings(max_examples=100)
    def test_anthropic_error_format_structure(self, error: APIError):
        """
        **Feature: stackai-to-api, Property 15: Error Response Format**
        **Validates: Requirements 8.1**
        
        For any APIError, the Anthropic format conversion should produce a valid
        Anthropic error response with required fields.
        
        Anthropic error format:
        {
            "type": "error",
            "error": {
                "type": "authentication_error",
                "message": "Error description"
            }
        }
        """
        handler = ErrorHandler()
        result = handler.to_anthropic_error(error)
        
        # Must have top-level "type" key with value "error"
        assert "type" in result
        assert result["type"] == "error"
        
        # Must have "error" object
        assert "error" in result
        assert isinstance(result["error"], dict)
        
        # Must have required fields in error object
        error_obj = result["error"]
        assert "type" in error_obj
        assert "message" in error_obj
        
        # Message must be the original message
        assert error_obj["message"] == error.message
        
        # Type must be a valid Anthropic error type string
        assert isinstance(error_obj["type"], str)
        valid_anthropic_types = [
            "invalid_request_error", "authentication_error", "permission_error",
            "not_found_error", "rate_limit_error", "api_error", "overloaded_error"
        ]
        assert error_obj["type"] in valid_anthropic_types
    
    @given(error=api_errors())
    @settings(max_examples=100)
    def test_gemini_error_format_structure(self, error: APIError):
        """
        **Feature: stackai-to-api, Property 15: Error Response Format**
        **Validates: Requirements 8.1**
        
        For any APIError, the Gemini format conversion should produce a valid
        Gemini error response with required fields.
        
        Gemini error format:
        {
            "error": {
                "code": 401,
                "message": "Error description",
                "status": "UNAUTHENTICATED"
            }
        }
        """
        handler = ErrorHandler()
        result = handler.to_gemini_error(error)
        
        # Must have top-level "error" key
        assert "error" in result
        assert isinstance(result["error"], dict)
        
        # Must have required fields in error object
        error_obj = result["error"]
        assert "code" in error_obj
        assert "message" in error_obj
        assert "status" in error_obj
        
        # Code must be an integer (HTTP status code)
        assert isinstance(error_obj["code"], int)
        assert error_obj["code"] == error.status_code
        
        # Message must be the original message
        assert error_obj["message"] == error.message
        
        # Status must be a valid Gemini status string
        assert isinstance(error_obj["status"], str)
        valid_gemini_statuses = [
            "INVALID_ARGUMENT", "UNAUTHENTICATED", "PERMISSION_DENIED",
            "NOT_FOUND", "RESOURCE_EXHAUSTED", "INTERNAL", "UNAVAILABLE"
        ]
        assert error_obj["status"] in valid_gemini_statuses
    
    @given(
        stackai_response=stackai_error_responses(),
        status_code=http_status_codes,
        target_format=target_formats
    )
    @settings(max_examples=100)
    def test_stackai_error_conversion_produces_valid_format(
        self, 
        stackai_response: Dict[str, Any], 
        status_code: int,
        target_format: str
    ):
        """
        **Feature: stackai-to-api, Property 15: Error Response Format**
        **Validates: Requirements 8.1**
        
        For any StackAI error response, converting it to any target format
        should produce a valid error response conforming to that format.
        """
        handler = ErrorHandler()
        result = handler.convert_stackai_error(stackai_response, status_code, target_format)
        
        if target_format == "openai":
            # Validate OpenAI format
            assert "error" in result
            assert "message" in result["error"]
            assert "type" in result["error"]
            assert "code" in result["error"]
            assert "param" in result["error"]
        elif target_format == "anthropic":
            # Validate Anthropic format
            assert "type" in result
            assert result["type"] == "error"
            assert "error" in result
            assert "type" in result["error"]
            assert "message" in result["error"]
        elif target_format == "gemini":
            # Validate Gemini format
            assert "error" in result
            assert "code" in result["error"]
            assert "message" in result["error"]
            assert "status" in result["error"]
            assert isinstance(result["error"]["code"], int)
    
    @given(error=api_errors(), target_format=target_formats)
    @settings(max_examples=100)
    def test_convert_error_dispatches_correctly(self, error: APIError, target_format: str):
        """
        **Feature: stackai-to-api, Property 15: Error Response Format**
        **Validates: Requirements 8.1**
        
        For any APIError and target format, the convert_error method should
        dispatch to the correct format-specific converter.
        """
        handler = ErrorHandler()
        result = handler.convert_error(error, target_format)
        
        # Verify the result matches the expected format
        if target_format == "openai":
            expected = handler.to_openai_error(error)
        elif target_format == "anthropic":
            expected = handler.to_anthropic_error(error)
        elif target_format == "gemini":
            expected = handler.to_gemini_error(error)
        else:
            expected = handler.to_openai_error(error)
        
        assert result == expected
    
    @given(error=api_errors())
    @settings(max_examples=100)
    def test_error_message_preserved_across_formats(self, error: APIError):
        """
        **Feature: stackai-to-api, Property 15: Error Response Format**
        **Validates: Requirements 8.1**
        
        For any APIError, the error message should be preserved in all
        target formats.
        """
        handler = ErrorHandler()
        
        openai_result = handler.to_openai_error(error)
        anthropic_result = handler.to_anthropic_error(error)
        gemini_result = handler.to_gemini_error(error)
        
        # Message should be preserved in all formats
        assert openai_result["error"]["message"] == error.message
        assert anthropic_result["error"]["message"] == error.message
        assert gemini_result["error"]["message"] == error.message
    
    @given(
        stackai_response=stackai_error_responses(),
        status_code=http_status_codes
    )
    @settings(max_examples=100)
    def test_stackai_error_parsing_extracts_message(
        self, 
        stackai_response: Dict[str, Any], 
        status_code: int
    ):
        """
        **Feature: stackai-to-api, Property 15: Error Response Format**
        **Validates: Requirements 8.1**
        
        For any StackAI error response, parsing should extract a non-empty
        error message.
        """
        handler = ErrorHandler()
        api_error = handler.parse_stackai_error(stackai_response, status_code)
        
        # Should always have a message
        assert api_error.message is not None
        assert len(api_error.message) > 0
        
        # Should have a valid error type
        assert api_error.error_type in ErrorType
        
        # Should have a valid status code
        assert api_error.status_code >= 400
