from app.services.auth import AuthService


def test_local_login_lockout_bypass_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ALLOW_LOCAL_LOGIN_LOCKOUT_BYPASS", raising=False)
    service = AuthService(session_factory=object())

    assert service._local_lockout_bypass_enabled("127.0.0.1") is False


def test_local_login_lockout_bypass_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("ALLOW_LOCAL_LOGIN_LOCKOUT_BYPASS", "true")
    service = AuthService(session_factory=object())

    assert service._local_lockout_bypass_enabled("127.0.0.1") is True
    assert service._local_lockout_bypass_enabled("203.0.113.10") is False
