"""
Property-Based Tests for Request Transformer Service

**Feature: stackai-to-api, Property 1: Request Format Conversion Preserves Content**
**Validates: Requirements 1.1, 1.2, 1.3**

Tests that converting requests from OpenAI/Anthropic/Gemini formats to StackAI format
preserves the original user message content.
"""

import pytest
from hypothesis import given, strategies as st, settings
from typing import List, Optional

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services.transformer import (
    RequestTransformer,
    OpenAIChatRequest,
    OpenAIMessage,
    AnthropicRequest,
    AnthropicMessage,
    GeminiRequest,
    GeminiContent,
    GeminiPart,
)


# Strategy for non-empty text content
non_empty_text = st.text(min_size=1, max_size=500).filter(lambda x: len(x.strip()) > 0)

# Strategy for model names
model_names = st.sampled_from([
    "gpt-4", "gpt-3.5-turbo", "claude-3-opus", "claude-3-sonnet",
    "gemini-pro", "gemini-1.5-pro", "custom-model"
])


def make_openai_message(role, content):
    return OpenAIMessage(role=role, content=content)


def make_anthropic_message(role, content):
    return AnthropicMessage(role=role, content=content)


def make_gemini_content(role, texts):
    parts = [GeminiPart(text=t) for t in texts]
    return GeminiContent(role=role, parts=parts)



class TestRequestFormatConversionPreservesContent:
    """
    Property 1: Request Format Conversion Preserves Content
    
    *For any* valid OpenAI/Anthropic/Gemini format request, converting it to 
    StackAI format and then extracting the user input should yield the same 
    content as the original request's user message.
    
    **Validates: Requirements 1.1, 1.2, 1.3**
    """
    
    @given(
        model=model_names,
        user_content=non_empty_text,
        stream=st.booleans()
    )
    @settings(max_examples=100)
    def test_openai_request_preserves_user_content(self, model, user_content, stream):
        """
        **Feature: stackai-to-api, Property 1: Request Format Conversion Preserves Content**
        **Validates: Requirements 1.1**
        
        For any valid OpenAI request, the user input should be preserved after conversion.
        """
        request = OpenAIChatRequest(
            model=model,
            messages=[make_openai_message("user", user_content)],
            stream=stream
        )
        
        transformer = RequestTransformer()
        unified = transformer.parse_openai_request(request)
        stackai_payload = transformer.to_stackai_dict(unified)
        
        assert unified.user_input == user_content
        assert "in-0" in stackai_payload
        assert stackai_payload["in-0"] == user_content
    
    @given(
        model=model_names,
        user_content=non_empty_text,
        system_content=st.one_of(st.none(), non_empty_text),
        stream=st.booleans(),
        max_tokens=st.integers(min_value=1, max_value=4096)
    )
    @settings(max_examples=100)
    def test_anthropic_request_preserves_user_content(self, model, user_content, system_content, stream, max_tokens):
        """
        **Feature: stackai-to-api, Property 1: Request Format Conversion Preserves Content**
        **Validates: Requirements 1.2**
        
        For any valid Anthropic request, the user input should be preserved after conversion.
        """
        request = AnthropicRequest(
            model=model,
            messages=[make_anthropic_message("user", user_content)],
            max_tokens=max_tokens,
            stream=stream,
            system=system_content
        )
        
        transformer = RequestTransformer()
        unified = transformer.parse_anthropic_request(request)
        stackai_payload = transformer.to_stackai_dict(unified)
        
        assert unified.user_input == user_content
        assert "in-0" in stackai_payload
        assert stackai_payload["in-0"] == user_content
    
    @given(
        model=model_names,
        user_content=non_empty_text,
        stream=st.booleans()
    )
    @settings(max_examples=100)
    def test_gemini_request_preserves_user_content(self, model, user_content, stream):
        """
        **Feature: stackai-to-api, Property 1: Request Format Conversion Preserves Content**
        **Validates: Requirements 1.3**
        
        For any valid Gemini request, the user input should be preserved after conversion.
        """
        request = GeminiRequest(
            contents=[make_gemini_content("user", [user_content])]
        )
        
        transformer = RequestTransformer()
        unified = transformer.parse_gemini_request(request, model, stream)
        stackai_payload = transformer.to_stackai_dict(unified)
        
        assert unified.user_input == user_content
        assert "in-0" in stackai_payload
        assert stackai_payload["in-0"] == user_content


    @given(
        model=model_names,
        user_content=non_empty_text,
        system_content=non_empty_text,
        stream=st.booleans()
    )
    @settings(max_examples=100)
    def test_openai_system_prompt_preserved(self, model, user_content, system_content, stream):
        """
        **Feature: stackai-to-api, Property 1: Request Format Conversion Preserves Content**
        **Validates: Requirements 1.1**
        
        For any valid OpenAI request with a system message, the system prompt 
        should be preserved after conversion.
        """
        request = OpenAIChatRequest(
            model=model,
            messages=[
                make_openai_message("system", system_content),
                make_openai_message("user", user_content)
            ],
            stream=stream
        )
        
        transformer = RequestTransformer()
        unified = transformer.parse_openai_request(request)
        
        assert unified.system_prompt == system_content
    
    @given(
        model=model_names,
        user_content=non_empty_text,
        system_content=non_empty_text,
        stream=st.booleans(),
        max_tokens=st.integers(min_value=1, max_value=4096)
    )
    @settings(max_examples=100)
    def test_anthropic_system_prompt_preserved(self, model, user_content, system_content, stream, max_tokens):
        """
        **Feature: stackai-to-api, Property 1: Request Format Conversion Preserves Content**
        **Validates: Requirements 1.2**
        
        For any valid Anthropic request with a system field, the system prompt 
        should be preserved after conversion.
        """
        request = AnthropicRequest(
            model=model,
            messages=[make_anthropic_message("user", user_content)],
            max_tokens=max_tokens,
            stream=stream,
            system=system_content
        )
        
        transformer = RequestTransformer()
        unified = transformer.parse_anthropic_request(request)
        
        assert unified.system_prompt == system_content
    
    @given(
        model=model_names,
        user_content=non_empty_text,
        stream=st.booleans()
    )
    @settings(max_examples=100)
    def test_openai_model_preserved(self, model, user_content, stream):
        """
        **Feature: stackai-to-api, Property 1: Request Format Conversion Preserves Content**
        **Validates: Requirements 1.1**
        
        For any valid OpenAI request, the model name should be preserved after conversion.
        """
        request = OpenAIChatRequest(
            model=model,
            messages=[make_openai_message("user", user_content)],
            stream=stream
        )
        
        transformer = RequestTransformer()
        unified = transformer.parse_openai_request(request)
        
        assert unified.model == model
    
    @given(
        model=model_names,
        user_content=non_empty_text,
        stream=st.booleans(),
        max_tokens=st.integers(min_value=1, max_value=4096)
    )
    @settings(max_examples=100)
    def test_anthropic_model_preserved(self, model, user_content, stream, max_tokens):
        """
        **Feature: stackai-to-api, Property 1: Request Format Conversion Preserves Content**
        **Validates: Requirements 1.2**
        
        For any valid Anthropic request, the model name should be preserved after conversion.
        """
        request = AnthropicRequest(
            model=model,
            messages=[make_anthropic_message("user", user_content)],
            max_tokens=max_tokens,
            stream=stream
        )
        
        transformer = RequestTransformer()
        unified = transformer.parse_anthropic_request(request)
        
        assert unified.model == model
    
    @given(
        model=model_names,
        user_content=non_empty_text,
        stream=st.booleans()
    )
    @settings(max_examples=100)
    def test_gemini_model_preserved(self, model, user_content, stream):
        """
        **Feature: stackai-to-api, Property 1: Request Format Conversion Preserves Content**
        **Validates: Requirements 1.3**
        
        For any valid Gemini request, the model name should be preserved after conversion.
        """
        request = GeminiRequest(
            contents=[make_gemini_content("user", [user_content])]
        )
        
        transformer = RequestTransformer()
        unified = transformer.parse_gemini_request(request, model, stream)
        
        assert unified.model == model


    @given(
        model=model_names,
        user_content=non_empty_text,
        stream=st.booleans()
    )
    @settings(max_examples=100)
    def test_openai_stream_flag_preserved(self, model, user_content, stream):
        """
        **Feature: stackai-to-api, Property 1: Request Format Conversion Preserves Content**
        **Validates: Requirements 1.1**
        
        For any valid OpenAI request, the stream flag should be preserved after conversion.
        """
        request = OpenAIChatRequest(
            model=model,
            messages=[make_openai_message("user", user_content)],
            stream=stream
        )
        
        transformer = RequestTransformer()
        unified = transformer.parse_openai_request(request)
        
        assert unified.stream == stream
    
    @given(
        model=model_names,
        user_content=non_empty_text,
        stream=st.booleans(),
        max_tokens=st.integers(min_value=1, max_value=4096)
    )
    @settings(max_examples=100)
    def test_anthropic_stream_flag_preserved(self, model, user_content, stream, max_tokens):
        """
        **Feature: stackai-to-api, Property 1: Request Format Conversion Preserves Content**
        **Validates: Requirements 1.2**
        
        For any valid Anthropic request, the stream flag should be preserved after conversion.
        """
        request = AnthropicRequest(
            model=model,
            messages=[make_anthropic_message("user", user_content)],
            max_tokens=max_tokens,
            stream=stream
        )
        
        transformer = RequestTransformer()
        unified = transformer.parse_anthropic_request(request)
        
        assert unified.stream == stream
    
    @given(
        model=model_names,
        user_content=non_empty_text,
        stream=st.booleans()
    )
    @settings(max_examples=100)
    def test_gemini_stream_flag_preserved(self, model, user_content, stream):
        """
        **Feature: stackai-to-api, Property 1: Request Format Conversion Preserves Content**
        **Validates: Requirements 1.3**
        
        For any valid Gemini request, the stream flag should be preserved after conversion.
        """
        request = GeminiRequest(
            contents=[make_gemini_content("user", [user_content])]
        )
        
        transformer = RequestTransformer()
        unified = transformer.parse_gemini_request(request, model, stream)
        
        assert unified.stream == stream
