#!/usr/bin/env python3
"""High-concurrency deep-conversation stress test for /v1/messages."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sqlite3
import statistics
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.time_utils import utc_now_naive


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    idx = int((len(sorted_values) - 1) * p)
    return float(sorted_values[idx])


def extract_text_from_nonstream(payload: Dict[str, Any]) -> str:
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    chunks: List[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            txt = block.get("text")
            if isinstance(txt, str):
                chunks.append(txt)
    return "\n".join(chunks).strip()


def build_deep_user_prompt(session_idx: int, turn_idx: int) -> str:
    # Keep prompts intentionally deep so each conversation turn carries context load.
    facts = []
    for i in range(max(0, turn_idx - 5), turn_idx):
        value = (session_idx * 37 + i * 19) % 997
        facts.append(f"turn-{i}: decision_code={value}")
    facts_block = "; ".join(facts) if facts else "none yet"
    scenario = (
        f"You are co-piloting project session-{session_idx:04d}. "
        f"This is turn {turn_idx}. Prior decision memory: {facts_block}. "
        "Build a concise plan with constraints, risk controls, and rollback checks. "
        "Use numbered steps and keep technical specificity high."
    )
    audit = (
        "Also include a 3-row table in markdown with columns: risk, trigger, mitigation. "
        "If assumptions are uncertain, say what to verify first."
    )
    return scenario + "\n\n" + audit


def build_project_mix_user_prompt(base_prompt: str, turn_idx: int) -> str:
    prompt = (base_prompt or "").strip()
    if not prompt:
        prompt = "@ipfs-file-manager 分析项目"

    if turn_idx == 0:
        return (
            f"{prompt}\n\n"
            "请通过 Claude Code 风格工具调用执行仓库级分析："
            "先调用 Explore，再按需调用 Read/Grep/Glob/LS。"
            "先给出工具调用，不要直接输出最终结论。"
        )

    return (
        f"{prompt}\n\n"
        f"这是第 {turn_idx + 1} 轮，请继续通过工具调用补充证据，"
        "并在信息充分后给出简明结论。"
    )


def build_default_claude_code_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": "Explore",
            "description": "Explore repository structure and identify relevant files.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Repository path to inspect"}
                },
                "required": ["path"],
            },
        },
        {
            "name": "Read",
            "description": "Read file content.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "File path"},
                    "offset": {"type": "integer", "description": "Start line offset"},
                    "limit": {"type": "integer", "description": "Max lines to read"},
                },
                "required": ["file_path"],
            },
        },
        {
            "name": "Grep",
            "description": "Search text pattern in files.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Pattern to search"},
                    "path": {"type": "string", "description": "Directory path"},
                },
                "required": ["pattern"],
            },
        },
        {
            "name": "Glob",
            "description": "Find files with glob patterns.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern"},
                    "path": {"type": "string", "description": "Directory path"},
                },
                "required": ["pattern"],
            },
        },
        {
            "name": "LS",
            "description": "List files in a directory.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Directory path"}},
                "required": ["path"],
            },
        },
    ]


def parse_sse_data_line(line: str) -> Optional[Dict[str, Any]]:
    if not line.startswith("data: "):
        return None
    raw = line[6:].strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


@dataclass
class SessionState:
    session_id: str
    prompt_group: str = "deep"
    base_prompt: str = ""
    messages: List[Dict[str, Any]] = field(default_factory=list)
    success_count: int = 0
    fail_count: int = 0


@dataclass
class ReqResult:
    session_id: str
    turn: int
    stream: bool
    queue_wait_ms: int
    ok: bool
    status_code: int
    latency_ms: int
    error_type: str
    error_message: str
    response_chars: int


class StressRunner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.rand = random.Random(args.seed)
        self.results: List[ReqResult] = []
        self.session_group_map: Dict[str, str] = {}
        self.claude_code_tools: List[Dict[str, Any]] = []
        if args.enable_claude_code_tools or args.prompt_mode == "project_mix":
            self.claude_code_tools = build_default_claude_code_tools()

    async def ensure_valid_key(self, client: httpx.AsyncClient, key: str) -> Optional[str]:
        try:
            resp = await client.get(
                f"{self.args.base_url}/v1/key/info",
                params={"key": key},
                timeout=10.0,
            )
        except Exception:
            return None
        if resp.status_code == 200:
            return key
        return None

    async def login_admin(self, client: httpx.AsyncClient) -> Optional[str]:
        payload = {
            "username": self.args.admin_username,
            "password": self.args.admin_password,
        }
        try:
            resp = await client.post(
                f"{self.args.base_url}/api/admin/auth/login",
                json=payload,
                timeout=10.0,
            )
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        data = resp.json()
        token = data.get("token")
        return token if isinstance(token, str) else None

    async def create_stress_key(self, client: httpx.AsyncClient, token: str) -> Optional[str]:
        payload = {
            "name": f"stress-{utc_now()}",
            "model_groups": [self.args.model],
            "request_quota": None,
            "token_quota": None,
            "cost_limit": None,
            "expires_at": None,
        }
        headers = {"Authorization": f"Bearer {token}"}
        try:
            resp = await client.post(
                f"{self.args.base_url}/api/admin/keys",
                headers=headers,
                json=payload,
                timeout=10.0,
            )
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        data = resp.json()
        raw_key = data.get("key")
        return raw_key if isinstance(raw_key, str) else None

    def create_stress_key_via_local_db(self) -> Optional[str]:
        # Local fallback for test environments where admin password may differ.
        try:
            from app.services.crypto import CryptoService

            raw_key = CryptoService.generate_api_key(prefix="sk-")
            key_hash = CryptoService.hash_api_key(raw_key)
            key_prefix = raw_key[:7]
            key_suffix = raw_key[-4:]
            now = utc_now_naive().isoformat(sep=" ")

            conn = sqlite3.connect(self.args.local_db_path)
            try:
                conn.execute(
                    """
                    INSERT INTO api_keys (
                        id, key_hash, key_prefix, key_suffix, name, model_groups,
                        quota, used, request_quota, token_quota, cost_limit, expires_at,
                        status, created_at, last_used_at, total_requests, total_tokens, total_cost
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        key_hash,
                        key_prefix,
                        key_suffix,
                        f"stress-local-{utc_now()}",
                        json.dumps([self.args.model], ensure_ascii=False),
                        None,
                        0,
                        None,
                        None,
                        None,
                        None,
                        "active",
                        now,
                        None,
                        0,
                        0,
                        "0",
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            return raw_key
        except Exception:
            return None

    async def resolve_api_key(self) -> str:
        existing = self.args.api_key.strip() if self.args.api_key else ""
        async with httpx.AsyncClient(timeout=10.0) as client:
            if existing:
                valid = await self.ensure_valid_key(client, existing)
                if valid:
                    return valid

            if not self.args.auto_create_key:
                raise RuntimeError("Provided API key invalid and auto-create disabled")

            token = await self.login_admin(client)
            if token:
                new_key = await self.create_stress_key(client, token)
                if new_key:
                    return new_key

        # Fallback to local DB bootstrap in case admin credentials are unknown.
        local_key = self.create_stress_key_via_local_db()
        if local_key:
            return local_key

        raise RuntimeError("Failed to create stress API key (admin API and local DB fallback both failed)")

    def make_request_payload(
        self,
        state: SessionState,
        turn_idx: int,
        stream: bool,
    ) -> Dict[str, Any]:
        if self.args.prompt_mode == "project_mix":
            user_prompt = build_project_mix_user_prompt(state.base_prompt, turn_idx)
        else:
            user_prompt = build_deep_user_prompt(
                int(state.session_id.split("-")[-1]),
                turn_idx,
            )
        state.messages.append({"role": "user", "content": user_prompt})

        # Keep long history window to preserve deep multi-turn context.
        history = state.messages[-self.args.max_history_messages :]
        payload: Dict[str, Any] = {
            "model": self.args.model,
            "max_tokens": self.args.max_tokens,
            "stream": stream,
            "system": self.args.system_prompt,
            "messages": history,
            "metadata": {"user_id": state.session_id},
        }
        if self.claude_code_tools:
            payload["tools"] = self.claude_code_tools
            payload["tool_choice"] = {"type": "auto"}
        return payload

    def build_sessions(self) -> List[SessionState]:
        session_indexes = list(range(self.args.sessions))
        if self.args.shuffle_prompt_groups:
            self.rand.shuffle(session_indexes)

        bookmark_count = min(max(self.args.bookmark_sessions, 0), self.args.sessions)
        bookmark_indexes = set(session_indexes[:bookmark_count])

        sessions: List[SessionState] = []
        for idx in range(self.args.sessions):
            session_id = f"{self.args.session_prefix}-{idx:04d}"
            if self.args.prompt_mode == "project_mix":
                if idx in bookmark_indexes:
                    prompt_group = "bookmark"
                    base_prompt = self.args.bookmark_prompt
                else:
                    prompt_group = "ipfs"
                    base_prompt = self.args.ipfs_prompt
            else:
                prompt_group = "deep"
                base_prompt = ""

            sessions.append(
                SessionState(
                    session_id=session_id,
                    prompt_group=prompt_group,
                    base_prompt=base_prompt,
                )
            )
            self.session_group_map[session_id] = prompt_group
        return sessions

    async def run_one_turn(
        self,
        client: httpx.AsyncClient,
        state: SessionState,
        turn_idx: int,
        queue_wait_ms: int,
    ) -> ReqResult:
        stream = self.rand.random() < self.args.stream_ratio
        payload = self.make_request_payload(state, turn_idx, stream)
        headers = {
            "x-api-key": self.args.api_key,
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-st-session-id": state.session_id,
        }
        started = time.perf_counter()

        if stream:
            try:
                content_parts: List[str] = []
                error_type = ""
                error_message = ""
                status_code = 0
                async with client.stream(
                    "POST",
                    f"{self.args.base_url}/v1/messages",
                    params={"beta": "true"},
                    headers=headers,
                    json=payload,
                    timeout=self.args.request_timeout,
                ) as resp:
                    status_code = resp.status_code
                    current_event = ""
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        if line.startswith("event: "):
                            current_event = line[7:].strip()
                            continue
                        data = parse_sse_data_line(line)
                        if data is None:
                            continue

                        if current_event == "content_block_delta":
                            delta = data.get("delta")
                            if isinstance(delta, dict):
                                txt = delta.get("text")
                                if isinstance(txt, str):
                                    content_parts.append(txt)
                        elif current_event == "error":
                            err = data.get("error")
                            if isinstance(err, dict):
                                error_type = str(err.get("type") or "error")
                                error_message = str(err.get("message") or "")
                            else:
                                error_type = "error"
                                error_message = str(data)

                latency_ms = int((time.perf_counter() - started) * 1000)
                ok = status_code < 400 and not error_type
                text = "".join(content_parts).strip()
                if ok:
                    state.messages.append({"role": "assistant", "content": text or "(empty)"})
                    state.success_count += 1
                    return ReqResult(
                        session_id=state.session_id,
                        turn=turn_idx,
                        stream=True,
                        queue_wait_ms=queue_wait_ms,
                        ok=True,
                        status_code=status_code,
                        latency_ms=latency_ms,
                        error_type="",
                        error_message="",
                        response_chars=len(text),
                    )

                state.fail_count += 1
                if not error_type:
                    error_type = f"http_{status_code}"
                return ReqResult(
                    session_id=state.session_id,
                    turn=turn_idx,
                    stream=True,
                    queue_wait_ms=queue_wait_ms,
                    ok=False,
                    status_code=status_code,
                    latency_ms=latency_ms,
                    error_type=error_type,
                    error_message=error_message[:400],
                    response_chars=len(text),
                )
            except Exception as exc:
                latency_ms = int((time.perf_counter() - started) * 1000)
                state.fail_count += 1
                return ReqResult(
                    session_id=state.session_id,
                    turn=turn_idx,
                    stream=True,
                    queue_wait_ms=queue_wait_ms,
                    ok=False,
                    status_code=0,
                    latency_ms=latency_ms,
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:400],
                    response_chars=0,
                )

        # non-stream
        try:
            resp = await client.post(
                f"{self.args.base_url}/v1/messages",
                params={"beta": "true"},
                headers=headers,
                json=payload,
                timeout=self.args.request_timeout,
            )
            latency_ms = int((time.perf_counter() - started) * 1000)

            if resp.status_code >= 400:
                msg = ""
                err_type = f"http_{resp.status_code}"
                try:
                    data = resp.json()
                    err = data.get("error")
                    if isinstance(err, dict):
                        msg = str(err.get("message") or "")
                        err_type = str(err.get("type") or err_type)
                    else:
                        msg = str(data)
                except Exception:
                    msg = resp.text
                state.fail_count += 1
                return ReqResult(
                    session_id=state.session_id,
                    turn=turn_idx,
                    stream=False,
                    queue_wait_ms=queue_wait_ms,
                    ok=False,
                    status_code=resp.status_code,
                    latency_ms=latency_ms,
                    error_type=err_type,
                    error_message=msg[:400],
                    response_chars=0,
                )

            data = resp.json()
            text = extract_text_from_nonstream(data)
            state.messages.append({"role": "assistant", "content": text or "(empty)"})
            state.success_count += 1
            return ReqResult(
                session_id=state.session_id,
                turn=turn_idx,
                stream=False,
                queue_wait_ms=queue_wait_ms,
                ok=True,
                status_code=resp.status_code,
                latency_ms=latency_ms,
                error_type="",
                error_message="",
                response_chars=len(text),
            )
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            state.fail_count += 1
            return ReqResult(
                session_id=state.session_id,
                turn=turn_idx,
                stream=False,
                queue_wait_ms=queue_wait_ms,
                ok=False,
                status_code=0,
                latency_ms=latency_ms,
                error_type=type(exc).__name__,
                error_message=str(exc)[:400],
                response_chars=0,
            )

    async def run(self) -> Dict[str, Any]:
        self.args.api_key = await self.resolve_api_key()

        sessions = self.build_sessions()
        sem = asyncio.Semaphore(self.args.concurrency)

        async with httpx.AsyncClient(
            timeout=self.args.request_timeout,
            limits=httpx.Limits(
                max_connections=max(self.args.concurrency * 2, 100),
                max_keepalive_connections=max(self.args.concurrency, 20),
            ),
        ) as client:
            for turn_idx in range(self.args.turns):
                round_sessions = list(sessions)
                if self.args.shuffle_request_order_each_round:
                    self.rand.shuffle(round_sessions)
                round_started = time.perf_counter()

                async def _bounded_one(state: SessionState) -> ReqResult:
                    async with sem:
                        queue_wait_ms = int((time.perf_counter() - round_started) * 1000)
                        return await self.run_one_turn(client, state, turn_idx, queue_wait_ms)

                tasks = [asyncio.create_task(_bounded_one(state)) for state in round_sessions]
                round_results = await asyncio.gather(*tasks)
                self.results.extend(round_results)

                ok_count = sum(1 for x in round_results if x.ok)
                fail_count = len(round_results) - ok_count
                print(
                    f"[round {turn_idx + 1:02d}/{self.args.turns}] "
                    f"ok={ok_count} fail={fail_count}",
                    flush=True,
                )

                if self.args.round_pause_ms > 0:
                    await asyncio.sleep(self.args.round_pause_ms / 1000.0)

        return self.build_report()

    def build_report(self) -> Dict[str, Any]:
        latencies = [x.latency_ms for x in self.results]
        queue_waits = [x.queue_wait_ms for x in self.results]
        error_counter = Counter(x.error_type for x in self.results if not x.ok)
        status_counter = Counter(str(x.status_code) for x in self.results)
        stream_counter = Counter("stream" if x.stream else "non_stream" for x in self.results)
        group_counter = Counter(self.session_group_map.values())

        request_group_counter = Counter(
            self.session_group_map.get(x.session_id, "unknown") for x in self.results
        )
        request_group_success_counter = Counter(
            self.session_group_map.get(x.session_id, "unknown") for x in self.results if x.ok
        )
        request_group_failure_counter = Counter(
            self.session_group_map.get(x.session_id, "unknown") for x in self.results if not x.ok
        )

        total = len(self.results)
        success = sum(1 for x in self.results if x.ok)
        failed = total - success
        success_rate = (success / total) if total else 0.0

        sessions_touched = Counter(x.session_id for x in self.results)
        max_turn_depth = max(sessions_touched.values()) if sessions_touched else 0

        failure_samples: List[Dict[str, Any]] = []
        for item in self.results:
            if item.ok:
                continue
            failure_samples.append(
                {
                    "session_id": item.session_id,
                    "turn": item.turn,
                    "stream": item.stream,
                    "queue_wait_ms": item.queue_wait_ms,
                    "status_code": item.status_code,
                    "error_type": item.error_type,
                    "error_message": item.error_message,
                }
            )
            if len(failure_samples) >= self.args.failure_sample_limit:
                break

        return {
            "started_at_utc": utc_now(),
            "config": {
                "base_url": self.args.base_url,
                "model": self.args.model,
                "sessions": self.args.sessions,
                "turns": self.args.turns,
                "concurrency": self.args.concurrency,
                "prompt_mode": self.args.prompt_mode,
                "bookmark_sessions": self.args.bookmark_sessions,
                "bookmark_prompt": self.args.bookmark_prompt,
                "ipfs_prompt": self.args.ipfs_prompt,
                "shuffle_prompt_groups": self.args.shuffle_prompt_groups,
                "shuffle_request_order_each_round": self.args.shuffle_request_order_each_round,
                "enable_claude_code_tools": bool(self.claude_code_tools),
                "stream_ratio": self.args.stream_ratio,
                "max_history_messages": self.args.max_history_messages,
                "max_tokens": self.args.max_tokens,
                "request_timeout_seconds": self.args.request_timeout,
            },
            "summary": {
                "total_requests": total,
                "success_requests": success,
                "failed_requests": failed,
                "success_rate": round(success_rate, 4),
                "max_turn_depth_observed": max_turn_depth,
            },
            "latency_ms": {
                "min": min(latencies) if latencies else 0,
                "max": max(latencies) if latencies else 0,
                "mean": round(statistics.fmean(latencies), 2) if latencies else 0,
                "p50": round(percentile(latencies, 0.50), 2) if latencies else 0,
                "p95": round(percentile(latencies, 0.95), 2) if latencies else 0,
                "p99": round(percentile(latencies, 0.99), 2) if latencies else 0,
            },
            "queue_wait_ms": {
                "min": min(queue_waits) if queue_waits else 0,
                "max": max(queue_waits) if queue_waits else 0,
                "mean": round(statistics.fmean(queue_waits), 2) if queue_waits else 0,
                "p50": round(percentile(queue_waits, 0.50), 2) if queue_waits else 0,
                "p95": round(percentile(queue_waits, 0.95), 2) if queue_waits else 0,
                "p99": round(percentile(queue_waits, 0.99), 2) if queue_waits else 0,
            },
            "status_code_distribution": dict(status_counter),
            "request_mode_distribution": dict(stream_counter),
            "session_group_distribution": dict(group_counter),
            "request_group_distribution": {
                group: {
                    "requests": request_group_counter.get(group, 0),
                    "success": request_group_success_counter.get(group, 0),
                    "failed": request_group_failure_counter.get(group, 0),
                }
                for group in sorted(set(request_group_counter.keys()) | set(group_counter.keys()))
            },
            "error_type_distribution": dict(error_counter),
            "failure_samples": failure_samples,
        }


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Concurrent deep-dialogue stress test")
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--model", default="claude-opus-4-6")
    parser.add_argument("--api-key", default=os.getenv("ST_API_KEY", ""))
    parser.add_argument("--auto-create-key", action="store_true", default=True)
    parser.add_argument("--admin-username", default=os.getenv("ADMIN_USERNAME", "admin"))
    parser.add_argument("--admin-password", default=os.getenv("ADMIN_PASSWORD", "admin123"))
    parser.add_argument("--local-db-path", default="data/api_service.db")

    parser.add_argument("--sessions", type=int, default=80)
    parser.add_argument("--turns", type=int, default=12)
    parser.add_argument("--concurrency", type=int, default=120)
    parser.add_argument("--prompt-mode", choices=["deep", "project_mix"], default="deep")
    parser.add_argument("--bookmark-sessions", type=int, default=5)
    parser.add_argument("--bookmark-prompt", default="@BookmarkVault 分析项目")
    parser.add_argument("--ipfs-prompt", default="@ipfs-file-manager 分析项目")
    parser.add_argument("--shuffle-prompt-groups", action="store_true", default=True)
    parser.add_argument("--no-shuffle-prompt-groups", dest="shuffle_prompt_groups", action="store_false")
    parser.add_argument("--shuffle-request-order-each-round", action="store_true", default=True)
    parser.add_argument(
        "--no-shuffle-request-order-each-round",
        dest="shuffle_request_order_each_round",
        action="store_false",
    )
    parser.add_argument("--enable-claude-code-tools", action="store_true", default=False)
    parser.add_argument("--stream-ratio", type=float, default=0.7)
    parser.add_argument("--request-timeout", type=float, default=45.0)
    parser.add_argument("--max-history-messages", type=int, default=28)
    parser.add_argument("--max-tokens", type=int, default=220)
    parser.add_argument("--round-pause-ms", type=int, default=30)
    parser.add_argument("--seed", type=int, default=20260329)
    parser.add_argument("--session-prefix", default="tenantA-user")
    parser.add_argument(
        "--system-prompt",
        default=(
            "You are a rigorous engineering copilot. "
            "For each turn provide concrete reasoning, explicit assumptions, "
            "and practical risk controls."
        ),
    )
    parser.add_argument("--output-dir", default="data/stress_reports")
    parser.add_argument("--failure-sample-limit", type=int, default=30)
    return parser.parse_args(argv)


async def async_main(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    runner = StressRunner(args)
    report = await runner.run()

    stamp = utc_now()
    report_path = output_dir / f"stress_report_{stamp}.json"
    details_path = output_dir / f"stress_details_{stamp}.jsonl"

    with report_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    with details_path.open("w", encoding="utf-8") as f:
        for item in runner.results:
            f.write(
                json.dumps(
                    {
                        "session_id": item.session_id,
                        "turn": item.turn,
                        "stream": item.stream,
                        "queue_wait_ms": item.queue_wait_ms,
                        "ok": item.ok,
                        "status_code": item.status_code,
                        "latency_ms": item.latency_ms,
                        "error_type": item.error_type,
                        "error_message": item.error_message,
                        "response_chars": item.response_chars,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    summary = report["summary"]
    print("\n=== Stress Summary ===")
    print(
        f"requests={summary['total_requests']} "
        f"success={summary['success_requests']} "
        f"failed={summary['failed_requests']} "
        f"success_rate={summary['success_rate']}"
    )
    print(f"max_turn_depth={summary['max_turn_depth_observed']}")
    print(f"latency_ms={report['latency_ms']}")
    print(f"queue_wait_ms={report.get('queue_wait_ms', {})}")
    print(f"status_distribution={report['status_code_distribution']}")
    print(f"session_group_distribution={report.get('session_group_distribution', {})}")
    print(f"request_group_distribution={report.get('request_group_distribution', {})}")
    print(f"error_types={report['error_type_distribution']}")
    print(f"report_file={report_path}")
    print(f"details_file={details_path}")
    print(f"api_key_prefix={args.api_key[:7]}..." if args.api_key else "api_key_prefix=(none)")

    return 0


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
