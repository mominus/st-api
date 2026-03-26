from app.services.upstream_sanitizer import sanitize_exposed_payload, sanitize_exposed_text


def test_sanitize_exposed_text_replaces_upstream_brand():
    text = "Contact support@stack-ai.com or visit https://api.stack-ai.com"
    sanitized = sanitize_exposed_text(text)
    assert sanitized is not None
    assert "stack-ai" not in sanitized.lower()
    assert "upstream support" in sanitized
    assert "upstream service" in sanitized


def test_sanitize_exposed_text_redacts_tool_arguments_object():
    text = '{"tool":"Write","arguments":{"file_path":"/amms/project/a.txt","content":"secret"}}'
    sanitized = sanitize_exposed_text(text)
    assert sanitized is not None
    assert '"tool":"Write"' in sanitized
    assert '"_redacted":true' in sanitized
    assert "file_path" not in sanitized
    assert "secret" not in sanitized


def test_sanitize_exposed_text_redacts_xml_tool_use_body():
    text = '<tool_use id="toolu_1" name="Write">{"file_path":"/tmp/a","content":"x"}</tool_use>'
    sanitized = sanitize_exposed_text(text)
    assert sanitized is not None
    assert "<tool_use" in sanitized
    assert '"_redacted":true' in sanitized
    assert "file_path" not in sanitized


def test_sanitize_exposed_payload_redacts_nested_arguments():
    payload = {
        "error": {
            "message": 'Write failed: {"tool":"Write","arguments":{"file_path":"/a","content":"s"}}'
        }
    }
    sanitized = sanitize_exposed_payload(payload)
    msg = sanitized["error"]["message"]
    assert '"_redacted":true' in msg
    assert "file_path" not in msg
    assert "content" not in msg
