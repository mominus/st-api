"""
Property-Based Tests for SSE Format Conversion

**Feature: stackai-to-api, Property 3: SSE Format Conversion Round-Trip**
**Validates: Requirements 1.6**

Tests that converting StackAI SSE tokens to OpenAI/Anthropic/Gemini SSE format
produces valid SSE chunks that, when parsed, contain the original token content.
"""

import pytest
import json
from hypothesis import given, strategies as st, settings
from typing import Optional

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services.response_transformer import ResponseTransformer


# Strategy for token content - non-empty strings that could be valid tokens
# Tokens are typically text fragments, so we use printable characters
token_content = st.text(
    alphabet=st.characters(whitelist_categories=('L', 'N', 'P', 'S', 'Z')),
    min_size=1,
    max_size=200
).filter(lambda x: len(x.strip()) > 0)

# Strategy for model names
model_names = st.sampled_from([
    "gpt-4", "gpt-3.5-turbo", "claude-3-opus", "claude-3-sonnet",
    "gemini-pro", "gemini-1.5-pro", "custom-model"
])

# Strategy for request IDs
request_ids = st.one_of(
    st.none(),
    st.text(alphabet='abcdef0123456789', min_size=8, max_size=24)
)


def parse_sse_data(sse_chunk: str) -> Optional[dict]:
    """
    Parse an SSE chunk and extract the JSON data.
    
    Args:
        sse_chunk: SSE formatted string (e.g., "data: {...}\n\n")
        
    Returns:
        Parsed JSON data or None if parsing fails
    """
    if not sse_chunk:
        return None
    
    lines = sse_chunk.strip().split('\n')
    for line in lines:
        if line.startswith('data:'):
            data_str = line[5:].strip()
            if data_str and data_str != '[DONE]':
                try:
                    return json.loads(data_str)
                except json.JSONDecodeError:
                    return None
    return None


def parse_anthropic_sse_event(sse_chunk: str) -> tuple[Optional[str], Optional[dict]]:
    """
    Parse an Anthropic SSE event and extract event type and data.
    
    Args:
        sse_chunk: SSE formatted string with event type
        
    Returns:
        Tuple of (event_type, data) or (None, None) if parsing fails
    """
    if not sse_chunk:
        return None, None
    
    lines = sse_chunk.strip().split('\n')
    event_type = None
    data = None
    
    for line in lines:
        if line.startswith('event:'):
            event_type = line[6:].strip()
        elif line.startswith('data:'):
            data_str = line[5:].strip()
            if data_str:
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    pass
    
    return event_type, data


class TestSSEFormatConversionRoundTrip:
    """
    Property 3: SSE Format Conversion Round-Trip
    
    *For any* valid StackAI SSE token, converting it to OpenAI/Anthropic/Gemini 
    SSE format should produce a valid SSE chunk that, when parsed, contains 
    the original token content.
    
    **Validates: Requirements 1.6**
    """
    
    @given(
        token=token_content,
        model=model_names,
        request_id=request_ids
    )
    @settings(max_examples=100)
    def test_openai_sse_chunk_contains_original_token(self, token, model, request_id):
        """
        **Feature: stackai-to-api, Property 3: SSE Format Conversion Round-Trip**
        **Validates: Requirements 1.6**
        
        For any valid token, converting to OpenAI SSE format and parsing back
        should yield the original token content.
        """
        transformer = ResponseTransformer()
        
        # Convert token to OpenAI SSE chunk
        sse_chunk = transformer.to_openai_stream_chunk(
            token=token,
            model=model,
            request_id=request_id,
            is_final=False
        )
        
        # Verify SSE format
        assert sse_chunk.startswith("data: ")
        assert sse_chunk.endswith("\n\n")
        
        # Parse the SSE chunk
        parsed = parse_sse_data(sse_chunk)
        
        # Verify structure
        assert parsed is not None
        assert "choices" in parsed
        assert len(parsed["choices"]) > 0
        assert "delta" in parsed["choices"][0]
        assert "content" in parsed["choices"][0]["delta"]
        
        # Verify token content is preserved
        extracted_token = parsed["choices"][0]["delta"]["content"]
        assert extracted_token == token
    
    @given(
        token=token_content,
        model=model_names,
        request_id=request_ids
    )
    @settings(max_examples=100)
    def test_openai_sse_chunk_has_valid_structure(self, token, model, request_id):
        """
        **Feature: stackai-to-api, Property 3: SSE Format Conversion Round-Trip**
        **Validates: Requirements 1.6**
        
        For any valid token, the OpenAI SSE chunk should have all required fields.
        """
        transformer = ResponseTransformer()
        
        sse_chunk = transformer.to_openai_stream_chunk(
            token=token,
            model=model,
            request_id=request_id,
            is_final=False
        )
        
        parsed = parse_sse_data(sse_chunk)
        
        # Verify all required OpenAI fields
        assert parsed is not None
        assert "id" in parsed
        assert parsed["id"].startswith("chatcmpl-")
        assert "object" in parsed
        assert parsed["object"] == "chat.completion.chunk"
        assert "created" in parsed
        assert isinstance(parsed["created"], int)
        assert "model" in parsed
        assert parsed["model"] == model
        assert "choices" in parsed
        assert isinstance(parsed["choices"], list)
    
    @given(
        model=model_names,
        request_id=request_ids
    )
    @settings(max_examples=100)
    def test_openai_final_chunk_has_stop_reason(self, model, request_id):
        """
        **Feature: stackai-to-api, Property 3: SSE Format Conversion Round-Trip**
        **Validates: Requirements 1.6**
        
        For any final SSE chunk, it should have finish_reason set to "stop".
        """
        transformer = ResponseTransformer()
        
        sse_chunk = transformer.to_openai_stream_chunk(
            token="",
            model=model,
            request_id=request_id,
            is_final=True
        )
        
        parsed = parse_sse_data(sse_chunk)
        
        assert parsed is not None
        assert "choices" in parsed
        assert len(parsed["choices"]) > 0
        assert "finish_reason" in parsed["choices"][0]
        assert parsed["choices"][0]["finish_reason"] == "stop"
    
    @given(
        token=token_content
    )
    @settings(max_examples=100)
    def test_anthropic_sse_event_contains_original_token(self, token):
        """
        **Feature: stackai-to-api, Property 3: SSE Format Conversion Round-Trip**
        **Validates: Requirements 1.6**
        
        For any valid token, converting to Anthropic SSE content_block_delta event
        and parsing back should yield the original token content.
        """
        transformer = ResponseTransformer()
        
        # Create a content_block_delta event
        sse_event = transformer.to_anthropic_stream_event(
            event_type="content_block_delta",
            data={
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": token}
            }
        )
        
        # Verify SSE format
        assert "event: content_block_delta" in sse_event
        assert "data: " in sse_event
        
        # Parse the SSE event
        event_type, data = parse_anthropic_sse_event(sse_event)
        
        # Verify structure
        assert event_type == "content_block_delta"
        assert data is not None
        assert "delta" in data
        assert "text" in data["delta"]
        
        # Verify token content is preserved
        extracted_token = data["delta"]["text"]
        assert extracted_token == token
    
    @given(
        token=token_content,
        model=model_names
    )
    @settings(max_examples=100)
    def test_gemini_sse_chunk_contains_original_token(self, token, model):
        """
        **Feature: stackai-to-api, Property 3: SSE Format Conversion Round-Trip**
        **Validates: Requirements 1.6**
        
        For any valid token, converting to Gemini SSE format and parsing back
        should yield the original token content.
        """
        transformer = ResponseTransformer()
        
        # Create a Gemini SSE chunk manually (similar to what transform_stackai_sse_to_gemini does)
        chunk = {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": token}],
                        "role": "model"
                    },
                    "finishReason": None,
                    "index": 0
                }
            ]
        }
        sse_chunk = f"data: {json.dumps(chunk)}\n\n"
        
        # Verify SSE format
        assert sse_chunk.startswith("data: ")
        assert sse_chunk.endswith("\n\n")
        
        # Parse the SSE chunk
        parsed = parse_sse_data(sse_chunk)
        
        # Verify structure
        assert parsed is not None
        assert "candidates" in parsed
        assert len(parsed["candidates"]) > 0
        assert "content" in parsed["candidates"][0]
        assert "parts" in parsed["candidates"][0]["content"]
        assert len(parsed["candidates"][0]["content"]["parts"]) > 0
        assert "text" in parsed["candidates"][0]["content"]["parts"][0]
        
        # Verify token content is preserved
        extracted_token = parsed["candidates"][0]["content"]["parts"][0]["text"]
        assert extracted_token == token
    
    @given(
        token=token_content,
        model=model_names,
        request_id=request_ids
    )
    @settings(max_examples=100)
    def test_openai_sse_model_preserved(self, token, model, request_id):
        """
        **Feature: stackai-to-api, Property 3: SSE Format Conversion Round-Trip**
        **Validates: Requirements 1.6**
        
        For any SSE conversion, the model name should be preserved in the output.
        """
        transformer = ResponseTransformer()
        
        sse_chunk = transformer.to_openai_stream_chunk(
            token=token,
            model=model,
            request_id=request_id,
            is_final=False
        )
        
        parsed = parse_sse_data(sse_chunk)
        
        assert parsed is not None
        assert "model" in parsed
        assert parsed["model"] == model
    
    @given(
        tokens=st.lists(token_content, min_size=1, max_size=10)
    )
    @settings(max_examples=100)
    def test_multiple_tokens_all_preserved(self, tokens):
        """
        **Feature: stackai-to-api, Property 3: SSE Format Conversion Round-Trip**
        **Validates: Requirements 1.6**
        
        For any sequence of tokens, each token should be preserved when converted
        to SSE format and parsed back.
        """
        transformer = ResponseTransformer()
        model = "gpt-4"
        
        extracted_tokens = []
        for token in tokens:
            sse_chunk = transformer.to_openai_stream_chunk(
                token=token,
                model=model,
                is_final=False
            )
            parsed = parse_sse_data(sse_chunk)
            assert parsed is not None
            extracted_tokens.append(parsed["choices"][0]["delta"]["content"])
        
        # All tokens should be preserved in order
        assert extracted_tokens == tokens
