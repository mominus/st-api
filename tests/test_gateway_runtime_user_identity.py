from types import SimpleNamespace

from app.services.gateway_runtime import GatewayRuntime


def _resolved(api_key_id: str = "api-key-uuid"):
    return SimpleNamespace(api_key=SimpleNamespace(id=api_key_id))


def test_resolve_client_ip_prefers_cf_connecting_ip():
    headers = {
        "cf-connecting-ip": "203.0.113.10",
        "x-forwarded-for": "198.51.100.20, 10.0.0.1",
    }
    ip = GatewayRuntime.resolve_client_ip(headers=headers, fallback_client_ip="127.0.0.1")
    assert ip == "203.0.113.10"


def test_resolve_client_ip_uses_first_forwarded_ip():
    headers = {
        "x-forwarded-for": "198.51.100.20, 10.0.0.1",
    }
    ip = GatewayRuntime.resolve_client_ip(headers=headers, fallback_client_ip="127.0.0.1")
    assert ip == "198.51.100.20"


def test_backend_user_id_uses_session_hint_when_provided():
    runtime = GatewayRuntime()
    user_id = runtime.resolve_backend_user_id(
        resolved=_resolved(),
        request_id="req_1234567890abcdef",
        session_hint=" user:alice@example.com ",
        client_ip="198.51.100.20",
        user_agent="pytest",
    )
    assert user_id.startswith("api:api-key-uuid:session:")
    assert "req_1234567890abcdef" not in user_id


def test_backend_user_id_defaults_to_request_scoped_identity():
    runtime = GatewayRuntime()
    uid_1 = runtime.resolve_backend_user_id(
        resolved=_resolved(),
        request_id="req_a",
        session_hint=None,
        client_ip="198.51.100.20",
        user_agent="pytest",
    )
    uid_2 = runtime.resolve_backend_user_id(
        resolved=_resolved(),
        request_id="req_b",
        session_hint=None,
        client_ip="198.51.100.20",
        user_agent="pytest",
    )
    assert uid_1 != uid_2
    assert uid_1.startswith("api:api-key-uuid:req:req_a:")
    assert uid_2.startswith("api:api-key-uuid:req:req_b:")


def test_resolve_session_hint_prefers_header():
    runtime = GatewayRuntime()
    session_hint = runtime.resolve_session_hint(
        headers={"x-st-session-id": "tenant_a:user_42"},
        payload={"metadata": {"user_id": "ignored"}},
        allow_user_field=True,
    )
    assert session_hint == "tenant_a:user_42"


def test_resolve_session_hint_reads_metadata_user_id():
    runtime = GatewayRuntime()
    session_hint = runtime.resolve_session_hint(
        headers={},
        payload={"metadata": {"user_id": "customer_10086"}},
        allow_user_field=True,
    )
    assert session_hint == "customer_10086"


def test_resolve_session_hint_reads_openai_user_field():
    runtime = GatewayRuntime()
    session_hint = runtime.resolve_session_hint(
        headers={},
        payload={"user": "alice@example.com"},
        allow_user_field=True,
    )
    assert session_hint == "alice@example.com"


def test_resolve_session_hint_sanitizes_unsafe_chars():
    runtime = GatewayRuntime()
    session_hint = runtime.resolve_session_hint(
        headers={"x-st-session-id": "  user#42@@@beijing  "},
        payload=None,
        allow_user_field=True,
    )
    assert session_hint == "user-42@@@beijing"
