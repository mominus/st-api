import asyncio
import re
import uuid
from types import SimpleNamespace
from typing import Dict, List, Tuple

from app.services.gateway_runtime import GatewayRuntime


USER_MARKER_RE = re.compile(r"U(\d{3})-R(\d{2})")


def _resolved_shared_key():
    # Deliberately simulate all callers sharing one API key.
    return SimpleNamespace(api_key=SimpleNamespace(id="shared-key-id"))


def _build_request_material(user_index: int, mode: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    session_value = f"tenantA:user{user_index:03d}"
    if mode == "header":
        return {"x-st-session-id": session_value}, {"model": "claude-opus-4-6"}
    if mode == "metadata":
        return {}, {"model": "claude-opus-4-6", "metadata": {"user_id": session_value}}
    if mode == "openai_user":
        return {}, {"model": "claude-opus-4-6", "user": session_value}
    raise ValueError(f"unsupported mode: {mode}")


class _FakeUpstreamMemory:
    def __init__(self) -> None:
        self._store: Dict[str, List[str]] = {}
        self._lock = asyncio.Lock()

    async def chat(self, *, backend_user_id: str, marker: str) -> str:
        # Mimic upstream stateful memory keyed by user_id.
        async with self._lock:
            history = self._store.setdefault(backend_user_id, [])
            history.append(f"[Human]{marker}")
            history.append(f"[Assistant]ACK:{marker}")
            return "\n".join(history)


async def _run_stress(
    *,
    user_count: int,
    rounds: int,
    shared_session_id: bool,
) -> List[Tuple[int, int, str, str]]:
    runtime = GatewayRuntime()
    upstream = _FakeUpstreamMemory()
    resolved = _resolved_shared_key()
    results: List[Tuple[int, int, str, str]] = []

    async def send_one(user_index: int, round_index: int):
        marker = f"U{user_index:03d}-R{round_index:02d}"
        if shared_session_id:
            headers = {"x-st-session-id": "global-shared-session"}
            payload = {"model": "claude-opus-4-6"}
        else:
            mode = ("header", "metadata", "openai_user")[user_index % 3]
            headers, payload = _build_request_material(user_index, mode)

        session_hint = runtime.resolve_session_hint(
            headers=headers,
            payload=payload,
            allow_user_field=True,
        )
        backend_user_id = runtime.resolve_backend_user_id(
            resolved=resolved,
            request_id=uuid.uuid4().hex[:24],
            session_hint=session_hint,
            client_ip="203.0.113.9",
            user_agent="pytest-load",
        )
        transcript = await upstream.chat(backend_user_id=backend_user_id, marker=marker)
        return user_index, round_index, marker, transcript

    for round_index in range(rounds):
        batch = [asyncio.create_task(send_one(user_index, round_index)) for user_index in range(user_count)]
        results.extend(await asyncio.gather(*batch))

    return results


def _extract_user_ids(transcript: str) -> List[int]:
    return [int(m.group(1)) for m in USER_MARKER_RE.finditer(transcript)]


def test_session_isolation_stress_64_users_12_rounds_no_cross_talk():
    """
    64 users × 12 rounds = 768 requests.
    Each user sends multi-turn requests while sharing one API key.
    Assertion: no transcript contains another user's marker.
    """
    user_count = 64
    rounds = 12
    results = asyncio.run(
        _run_stress(user_count=user_count, rounds=rounds, shared_session_id=False)
    )
    assert len(results) == user_count * rounds

    # Verify every response window contains only the caller's own markers.
    for user_index, _round_index, _marker, transcript in results:
        seen_users = set(_extract_user_ids(transcript))
        assert seen_users == {user_index}

    # Verify each user accumulated all rounds in their own window.
    latest_by_user: Dict[int, str] = {}
    for user_index, round_index, _marker, transcript in results:
        if round_index == rounds - 1:
            latest_by_user[user_index] = transcript
    assert len(latest_by_user) == user_count
    for user_index, transcript in latest_by_user.items():
        expected_markers = {f"U{user_index:03d}-R{idx:02d}" for idx in range(rounds)}
        found_markers = {m.group(0) for m in USER_MARKER_RE.finditer(transcript)}
        assert expected_markers.issubset(found_markers)


def test_session_isolation_stress_120_users_10_rounds_no_cross_talk():
    """
    120 users × 10 rounds = 1200 requests.
    Heavier pressure profile for cross-session leakage detection.
    """
    user_count = 120
    rounds = 10
    results = asyncio.run(
        _run_stress(user_count=user_count, rounds=rounds, shared_session_id=False)
    )
    assert len(results) == user_count * rounds

    for user_index, _round_index, _marker, transcript in results:
        seen_users = set(_extract_user_ids(transcript))
        assert seen_users == {user_index}


def test_session_isolation_control_shared_session_id_will_mix_histories():
    """
    Control group to prove test sensitivity:
    when all users share the same session id, histories should mix.
    """
    results = asyncio.run(
        _run_stress(user_count=56, rounds=8, shared_session_id=True)
    )
    leaked = 0
    for user_index, _round_index, _marker, transcript in results:
        seen_users = set(_extract_user_ids(transcript))
        if seen_users != {user_index}:
            leaked += 1
    assert leaked > 0
