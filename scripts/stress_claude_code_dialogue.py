#!/usr/bin/env python3
"""Concurrent Claude Code style dialogue stress test with real tool loops."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sqlite3
import statistics
import subprocess
import sys
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
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


def clamp_int(value: Any, default: int, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    return max(min_value, min(parsed, max_value))


def build_default_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": "Explore",
            "description": "Explore repository structure and identify relevant files.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Repository path to inspect"},
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
                    "max_count": {"type": "integer", "description": "Max matches"},
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
                    "limit": {"type": "integer", "description": "Max results"},
                },
                "required": ["pattern"],
            },
        },
        {
            "name": "LS",
            "description": "List files in a directory.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path"},
                },
                "required": ["path"],
            },
        },
    ]


@dataclass
class SessionScenario:
    session_id: str
    prompt_group: str
    prompt: str


@dataclass
class RequestTrace:
    session_id: str
    prompt_group: str
    step: int
    request_id: str
    response_request_id: str
    request_latency_ms: int
    status_code: int
    ok: bool
    stop_reason: str
    tool_calls: int
    error_type: str
    error_code: str
    error_message: str
    error_details: Optional[Dict[str, Any]] = None
    error_payload: Optional[Dict[str, Any]] = None
    logged_status: str = ""
    logged_account_id: str = ""
    logged_account_name: str = ""
    logged_account_status: str = ""
    logged_daily_used: Optional[int] = None
    logged_daily_quota: Optional[int] = None
    logged_error_message: str = ""
    logged_error_payload: Optional[Dict[str, Any]] = None
    logged_timestamp: str = ""


@dataclass
class SessionTrace:
    session_id: str
    prompt_group: str
    prompt: str
    ok: bool
    start_wait_ms: int
    session_latency_ms: int
    request_count: int
    tool_calls: int
    final_status_code: int
    final_request_id: str
    final_response_request_id: str
    final_error_type: str
    final_error_code: str
    final_error_message: str
    final_error_details: Optional[Dict[str, Any]] = None
    final_error_payload: Optional[Dict[str, Any]] = None
    logged_status: str = ""
    logged_account_id: str = ""
    logged_account_name: str = ""
    logged_account_status: str = ""
    logged_daily_used: Optional[int] = None
    logged_daily_quota: Optional[int] = None
    logged_error_message: str = ""
    logged_error_payload: Optional[Dict[str, Any]] = None
    logged_timestamp: str = ""


class DialogueRunner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.rand = random.Random(args.seed)
        self.run_tag = utc_now()
        self.tools = build_default_tools()
        self.workspace_root = Path(args.tool_workspace_root).resolve()
        self.session_results: List[SessionTrace] = []
        self.request_results: List[RequestTrace] = []

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
        try:
            resp = await client.post(
                f"{self.args.base_url}/api/admin/auth/login",
                json={"username": self.args.admin_username, "password": self.args.admin_password},
                timeout=10.0,
            )
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        token = resp.json().get("token")
        return token if isinstance(token, str) else None

    async def create_stress_key(self, client: httpx.AsyncClient, token: str) -> Optional[str]:
        payload = {
            "name": f"stress-cc-dialogue-{utc_now()}",
            "model_groups": [self.args.model],
            "request_quota": None,
            "token_quota": None,
            "cost_limit": None,
            "expires_at": None,
        }
        try:
            resp = await client.post(
                f"{self.args.base_url}/api/admin/keys",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
                timeout=10.0,
            )
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        key = resp.json().get("key")
        return key if isinstance(key, str) else None

    def create_stress_key_via_local_db(self) -> Optional[str]:
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
                        f"stress-cc-dialogue-local-{utc_now()}",
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

            token = await self.login_admin(client)
            if token:
                created = await self.create_stress_key(client, token)
                if created:
                    return created

        fallback = self.create_stress_key_via_local_db()
        if fallback:
            return fallback
        raise RuntimeError("Failed to resolve API key for dialogue stress run")

    def build_scenarios(self) -> List[SessionScenario]:
        bookmark_count = min(max(self.args.bookmark_sessions, 0), self.args.sessions)
        ids = list(range(self.args.sessions))
        if self.args.shuffle_prompt_groups:
            self.rand.shuffle(ids)
        bookmark_ids = set(ids[:bookmark_count])

        scenarios: List[SessionScenario] = []
        for i in range(self.args.sessions):
            if i in bookmark_ids:
                group = "bookmark"
                prompt = self.args.bookmark_prompt
            else:
                group = "ipfs"
                prompt = self.args.ipfs_prompt
            scenarios.append(
                SessionScenario(
                    session_id=f"{self.args.session_prefix}-{i:04d}",
                    prompt_group=group,
                    prompt=prompt,
                )
            )
        return scenarios

    def _resolve_workspace_path(self, raw_path: str) -> Tuple[Optional[Path], str]:
        token = (raw_path or ".").strip() or "."
        candidate = Path(token)
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate

        try:
            resolved = candidate.resolve()
        except Exception as exc:
            return None, f"resolve_failed: {exc}"

        try:
            resolved.relative_to(self.workspace_root)
        except ValueError:
            return None, "path_outside_workspace_root"

        if resolved.exists():
            return resolved, ""

        # Best-effort case-insensitive fallback for common path mismatch.
        parent = resolved.parent
        if parent.exists():
            lowered = resolved.name.lower()
            for child in parent.iterdir():
                if child.name.lower() == lowered:
                    return child.resolve(), ""

        return None, "path_not_found"

    @staticmethod
    def _truncate_text(text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + f"\n... [truncated {len(text) - max_chars} chars]"

    def _tool_explore(self, tool_input: Dict[str, Any]) -> str:
        raw_path = str(tool_input.get("path", "."))
        path, err = self._resolve_workspace_path(raw_path)
        if err or path is None:
            return f"Explore failed: path={raw_path!r} error={err}"
        if not path.is_dir():
            return f"Explore failed: {path} is not a directory"

        entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        limit = clamp_int(tool_input.get("limit", 80), 80, 10, 200)
        lines = [f"Explore path: {path}"]
        for item in entries[:limit]:
            suffix = "/" if item.is_dir() else ""
            try:
                rel = item.relative_to(self.workspace_root)
                lines.append(f"- {rel}{suffix}")
            except Exception:
                lines.append(f"- {item}{suffix}")
        lines.append(f"entries_shown={min(len(entries), limit)} total_entries={len(entries)}")
        return "\n".join(lines)

    def _tool_ls(self, tool_input: Dict[str, Any]) -> str:
        raw_path = str(tool_input.get("path", "."))
        path, err = self._resolve_workspace_path(raw_path)
        if err or path is None:
            return f"LS failed: path={raw_path!r} error={err}"
        if not path.is_dir():
            return f"LS failed: {path} is not a directory"
        entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        limit = clamp_int(tool_input.get("limit", 120), 120, 10, 400)
        lines = [f"LS path: {path}"]
        for item in entries[:limit]:
            marker = "d" if item.is_dir() else "f"
            lines.append(f"{marker} {item.name}")
        lines.append(f"entries_shown={min(len(entries), limit)} total_entries={len(entries)}")
        return "\n".join(lines)

    def _tool_read(self, tool_input: Dict[str, Any]) -> str:
        raw_path = str(tool_input.get("file_path", "")).strip()
        path, err = self._resolve_workspace_path(raw_path)
        if err or path is None:
            return f"Read failed: path={raw_path!r} error={err}"
        if not path.is_file():
            return f"Read failed: {path} is not a file"

        offset = clamp_int(tool_input.get("offset", 0), 0, 0, 200000)
        # Keep tool results bounded so dialogue payloads stay realistic under load.
        limit = clamp_int(tool_input.get("limit", 120), 120, 1, 160)
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as exc:
            return f"Read failed: {exc}"

        sliced = lines[offset : offset + limit]
        body = "\n".join(f"{offset + idx + 1:5d}: {line}" for idx, line in enumerate(sliced))
        result = (
            f"Read file: {path}\n"
            f"offset={offset} limit={limit} lines_shown={len(sliced)} total_lines={len(lines)}\n"
            f"{body}"
        )
        return self._truncate_text(result, self.args.max_tool_result_chars)

    def _tool_glob(self, tool_input: Dict[str, Any]) -> str:
        pattern = str(tool_input.get("pattern", "")).strip()
        if not pattern:
            return "Glob failed: missing pattern"
        raw_path = str(tool_input.get("path", "."))
        base, err = self._resolve_workspace_path(raw_path)
        if err or base is None:
            return f"Glob failed: path={raw_path!r} error={err}"
        if not base.is_dir():
            return f"Glob failed: {base} is not a directory"

        limit = clamp_int(tool_input.get("limit", 120), 120, 10, 500)
        try:
            matches = sorted(base.glob(pattern))
        except Exception as exc:
            return f"Glob failed: {exc}"

        lines = [f"Glob base: {base}", f"pattern={pattern}"]
        for item in matches[:limit]:
            suffix = "/" if item.is_dir() else ""
            try:
                rel = item.relative_to(self.workspace_root)
                lines.append(f"- {rel}{suffix}")
            except Exception:
                lines.append(f"- {item}{suffix}")
        lines.append(f"matches_shown={min(len(matches), limit)} total_matches={len(matches)}")
        return "\n".join(lines)

    def _tool_grep(self, tool_input: Dict[str, Any]) -> str:
        pattern = str(tool_input.get("pattern", "")).strip()
        if not pattern:
            return "Grep failed: missing pattern"
        raw_path = str(tool_input.get("path", "."))
        base, err = self._resolve_workspace_path(raw_path)
        if err or base is None:
            return f"Grep failed: path={raw_path!r} error={err}"
        if not base.exists():
            return f"Grep failed: {base} does not exist"

        max_count = clamp_int(tool_input.get("max_count", 40), 40, 1, 400)
        cmd = [
            "rg",
            "-n",
            "--max-count",
            str(max_count),
            pattern,
            str(base),
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=8.0,
                check=False,
            )
        except FileNotFoundError:
            return "Grep failed: rg_not_found"
        except subprocess.TimeoutExpired:
            return "Grep failed: timeout"
        except Exception as exc:
            return f"Grep failed: {exc}"

        if proc.returncode == 1:
            return f"Grep pattern={pattern!r} path={base}\n(no matches)"
        if proc.returncode != 0:
            stderr = proc.stderr.strip()
            return f"Grep failed: rc={proc.returncode} stderr={stderr}"

        out = proc.stdout.strip()
        result = f"Grep pattern={pattern!r} path={base}\n{out}"
        return self._truncate_text(result, self.args.max_tool_result_chars)

    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]) -> str:
        name = (tool_name or "").strip()
        if name == "Explore":
            return self._tool_explore(tool_input)
        if name == "LS":
            return self._tool_ls(tool_input)
        if name == "Read":
            return self._tool_read(tool_input)
        if name == "Glob":
            return self._tool_glob(tool_input)
        if name == "Grep":
            return self._tool_grep(tool_input)
        return f"Unsupported tool: {name}"

    @staticmethod
    def _trim_json_value(value: Any, max_chars: int = 4000) -> Any:
        if value is None:
            return None
        try:
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            text = str(value)
            if len(text) <= max_chars:
                return text
            return text[:max_chars] + f"... [truncated {len(text) - max_chars} chars]"

        if len(encoded) <= max_chars:
            return value

        return {
            "_truncated": True,
            "preview": encoded[:max_chars],
            "original_length": len(encoded),
        }

    @staticmethod
    def _parse_logged_error_payload(raw_error_message: str) -> Optional[Dict[str, Any]]:
        text = (raw_error_message or "").strip()
        if not text or not text.startswith("{"):
            return None
        try:
            parsed = json.loads(text)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else {"value": parsed}

    @classmethod
    def extract_error(
        cls,
        resp: httpx.Response,
    ) -> Tuple[str, str, str, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        err_type = f"http_{resp.status_code}"
        err_code = ""
        msg = ""
        details: Optional[Dict[str, Any]] = None
        payload: Optional[Dict[str, Any]] = None
        try:
            data = resp.json()
            payload = data if isinstance(data, dict) else {"raw": data}
            err = data.get("error") if isinstance(data, dict) else None
            if isinstance(err, dict):
                err_type = str(err.get("type") or err.get("status") or err_type)
                err_code = str(err.get("code") or "")
                msg = str(err.get("message") or "")
                raw_details = err.get("details")
                if isinstance(raw_details, dict):
                    details = raw_details
                elif raw_details is not None:
                    details = {"value": raw_details}
            else:
                msg = str(data)
        except Exception:
            text = resp.text or ""
            payload = {"raw": text[:4000]} if text else None
            msg = resp.text or ""

        if not msg and isinstance(details, dict):
            detail_msg = details.get("message") or details.get("raw")
            if detail_msg:
                msg = str(detail_msg)

        return (
            err_type[:120],
            err_code[:120],
            msg[:400],
            cls._trim_json_value(details, max_chars=2000),
            cls._trim_json_value(payload, max_chars=4000),
        )

    def enrich_failures_from_local_db(self) -> None:
        failure_request_ids = sorted({
            item.request_id
            for item in self.request_results
            if not item.ok and item.request_id
        })
        if not failure_request_ids:
            return

        placeholders = ",".join("?" for _ in failure_request_ids)
        query = f"""
            SELECT
                rl.id AS request_id,
                rl.timestamp AS logged_timestamp,
                rl.status AS logged_status,
                rl.account_id AS logged_account_id,
                rl.error_message AS logged_error_message,
                ba.name AS logged_account_name,
                ba.status AS logged_account_status,
                ba.daily_used AS logged_daily_used,
                ba.daily_quota AS logged_daily_quota
            FROM request_logs AS rl
            LEFT JOIN backend_accounts AS ba
                ON ba.id = rl.account_id
            WHERE rl.id IN ({placeholders})
        """

        by_request_id: Dict[str, Dict[str, Any]] = {}
        try:
            conn = sqlite3.connect(self.args.local_db_path)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(query, failure_request_ids).fetchall()
            finally:
                conn.close()
        except Exception:
            return

        for row in rows:
            request_id = str(row["request_id"] or "")
            raw_error_message = str(row["logged_error_message"] or "")
            parsed_error_payload = self._parse_logged_error_payload(raw_error_message)
            by_request_id[request_id] = {
                "logged_status": str(row["logged_status"] or ""),
                "logged_account_id": str(row["logged_account_id"] or ""),
                "logged_account_name": str(row["logged_account_name"] or ""),
                "logged_account_status": str(row["logged_account_status"] or ""),
                "logged_daily_used": row["logged_daily_used"],
                "logged_daily_quota": row["logged_daily_quota"],
                "logged_error_message": raw_error_message[:4000],
                "logged_error_payload": self._trim_json_value(parsed_error_payload, max_chars=4000),
                "logged_timestamp": str(row["logged_timestamp"] or ""),
            }

        for item in self.request_results:
            if item.ok or not item.request_id:
                continue
            info = by_request_id.get(item.request_id)
            if not info:
                continue
            item.logged_status = info["logged_status"]
            item.logged_account_id = info["logged_account_id"]
            item.logged_account_name = info["logged_account_name"]
            item.logged_account_status = info["logged_account_status"]
            item.logged_daily_used = info["logged_daily_used"]
            item.logged_daily_quota = info["logged_daily_quota"]
            item.logged_error_message = info["logged_error_message"]
            item.logged_error_payload = info["logged_error_payload"]
            item.logged_timestamp = info["logged_timestamp"]

        for item in self.session_results:
            if item.ok or not item.final_request_id:
                continue
            info = by_request_id.get(item.final_request_id)
            if not info:
                continue
            item.logged_status = info["logged_status"]
            item.logged_account_id = info["logged_account_id"]
            item.logged_account_name = info["logged_account_name"]
            item.logged_account_status = info["logged_account_status"]
            item.logged_daily_used = info["logged_daily_used"]
            item.logged_daily_quota = info["logged_daily_quota"]
            item.logged_error_message = info["logged_error_message"]
            item.logged_error_payload = info["logged_error_payload"]
            item.logged_timestamp = info["logged_timestamp"]

    async def run_session(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        scenario: SessionScenario,
        queued_at: float,
    ) -> SessionTrace:
        async with sem:
            started_at = time.perf_counter()
            start_wait_ms = int((started_at - queued_at) * 1000)

            messages: List[Dict[str, Any]] = [{"role": "user", "content": scenario.prompt}]
            request_count = 0
            total_tool_calls = 0
            final_status_code = 0
            final_request_id = ""
            final_response_request_id = ""
            final_error_type = ""
            final_error_code = ""
            final_error_message = ""
            final_error_details: Optional[Dict[str, Any]] = None
            final_error_payload: Optional[Dict[str, Any]] = None
            ok = False

            for step in range(1, self.args.max_steps + 1):
                request_count += 1
                client_request_id = f"{self.run_tag}-{scenario.session_id}-s{step:02d}"
                payload: Dict[str, Any] = {
                    "model": self.args.model,
                    "stream": False,
                    "max_tokens": self.args.max_tokens,
                    "system": self.args.system_prompt,
                    "messages": messages[-self.args.max_history_messages :],
                    "tools": self.tools,
                    "tool_choice": {"type": "auto"},
                    "metadata": {
                        "user_id": scenario.session_id,
                        "stress_request_id": client_request_id,
                    },
                }
                headers = {
                    "x-api-key": self.args.api_key,
                    "content-type": "application/json",
                    "anthropic-version": "2023-06-01",
                    "x-st-session-id": scenario.session_id,
                    "x-st-request-id": client_request_id,
                }

                req_started = time.perf_counter()
                try:
                    resp = await client.post(
                        f"{self.args.base_url}/v1/messages",
                        params={"beta": "true"},
                        headers=headers,
                        json=payload,
                        timeout=self.args.request_timeout,
                    )
                    request_latency_ms = int((time.perf_counter() - req_started) * 1000)
                except Exception as exc:
                    request_latency_ms = int((time.perf_counter() - req_started) * 1000)
                    final_status_code = 0
                    final_request_id = client_request_id
                    final_response_request_id = ""
                    final_error_type = type(exc).__name__
                    final_error_code = ""
                    final_error_message = str(exc)[:400]
                    final_error_details = None
                    final_error_payload = None
                    self.request_results.append(
                        RequestTrace(
                            session_id=scenario.session_id,
                            prompt_group=scenario.prompt_group,
                            step=step,
                            request_id=client_request_id,
                            response_request_id="",
                            request_latency_ms=request_latency_ms,
                            status_code=0,
                            ok=False,
                            stop_reason="",
                            tool_calls=0,
                            error_type=final_error_type,
                            error_code="",
                            error_message=final_error_message,
                        )
                    )
                    break

                final_status_code = resp.status_code
                final_request_id = client_request_id
                final_response_request_id = str(resp.headers.get("X-Request-ID") or "")
                if resp.status_code >= 400:
                    (
                        final_error_type,
                        final_error_code,
                        final_error_message,
                        final_error_details,
                        final_error_payload,
                    ) = self.extract_error(resp)
                    self.request_results.append(
                        RequestTrace(
                            session_id=scenario.session_id,
                            prompt_group=scenario.prompt_group,
                            step=step,
                            request_id=client_request_id,
                            response_request_id=final_response_request_id,
                            request_latency_ms=request_latency_ms,
                            status_code=resp.status_code,
                            ok=False,
                            stop_reason="",
                            tool_calls=0,
                            error_type=final_error_type,
                            error_code=final_error_code,
                            error_message=final_error_message,
                            error_details=final_error_details,
                            error_payload=final_error_payload,
                        )
                    )
                    break

                data = resp.json()
                content = data.get("content")
                if not isinstance(content, list):
                    content = []

                stop_reason = str(data.get("stop_reason") or "")
                tool_uses = [
                    block for block in content
                    if isinstance(block, dict) and block.get("type") == "tool_use"
                ]
                tool_count = len(tool_uses)
                total_tool_calls += tool_count

                self.request_results.append(
                    RequestTrace(
                        session_id=scenario.session_id,
                        prompt_group=scenario.prompt_group,
                        step=step,
                        request_id=client_request_id,
                        response_request_id=final_response_request_id,
                        request_latency_ms=request_latency_ms,
                        status_code=resp.status_code,
                        ok=True,
                        stop_reason=stop_reason,
                        tool_calls=tool_count,
                        error_type="",
                        error_code="",
                        error_message="",
                    )
                )

                messages.append({"role": "assistant", "content": content})

                if tool_count <= 0:
                    ok = True
                    break

                tool_results: List[Dict[str, Any]] = []
                for idx, block in enumerate(tool_uses):
                    tool_id = str(block.get("id") or "").strip()
                    tool_name = str(block.get("name") or "").strip()
                    if not tool_id:
                        final_status_code = 0
                        final_error_type = "missing_tool_use_id"
                        final_error_code = ""
                        final_error_message = f"step={step} tool_index={idx} tool_name={tool_name or 'unknown'}"
                        final_error_details = None
                        final_error_payload = None
                        break

                    tool_input = block.get("input")
                    if not isinstance(tool_input, dict):
                        tool_input = {}

                    if idx < self.args.max_tools_per_step:
                        tool_output = self.execute_tool(tool_name, tool_input)
                    else:
                        tool_output = (
                            "Tool execution skipped by stress client due to per-step execution cap; "
                            f"max_tools_per_step={self.args.max_tools_per_step}"
                        )

                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": self._truncate_text(tool_output, self.args.max_tool_result_chars),
                        }
                    )

                if final_error_type:
                    break
                messages.append({"role": "user", "content": tool_results})

            else:
                final_error_type = "tool_loop_exceeded"
                final_error_code = ""
                final_error_message = f"max_steps={self.args.max_steps}"
                final_error_details = None
                final_error_payload = None

            session_latency_ms = int((time.perf_counter() - started_at) * 1000)
            trace = SessionTrace(
                session_id=scenario.session_id,
                prompt_group=scenario.prompt_group,
                prompt=scenario.prompt,
                ok=ok,
                start_wait_ms=start_wait_ms,
                session_latency_ms=session_latency_ms,
                request_count=request_count,
                tool_calls=total_tool_calls,
                final_status_code=final_status_code,
                final_request_id=final_request_id,
                final_response_request_id=final_response_request_id,
                final_error_type=final_error_type,
                final_error_code=final_error_code,
                final_error_message=final_error_message,
                final_error_details=final_error_details,
                final_error_payload=final_error_payload,
            )
            self.session_results.append(trace)
            return trace

    async def run(self) -> Dict[str, Any]:
        self.args.api_key = await self.resolve_api_key()
        scenarios = self.build_scenarios()
        sem = asyncio.Semaphore(self.args.concurrency)

        async with httpx.AsyncClient(
            timeout=self.args.request_timeout,
            limits=httpx.Limits(
                max_connections=max(self.args.concurrency * 2, 100),
                max_keepalive_connections=max(self.args.concurrency, 20),
            ),
        ) as client:
            tasks = []
            enqueued = time.perf_counter()
            for scenario in scenarios:
                tasks.append(asyncio.create_task(self.run_session(client, sem, scenario, enqueued)))
            await asyncio.gather(*tasks)

        if self.args.log_flush_wait_seconds > 0:
            await asyncio.sleep(self.args.log_flush_wait_seconds)
        self.enrich_failures_from_local_db()
        return self.build_report()

    def build_report(self) -> Dict[str, Any]:
        total_sessions = len(self.session_results)
        success_sessions = sum(1 for x in self.session_results if x.ok)
        failed_sessions = total_sessions - success_sessions
        success_rate = (success_sessions / total_sessions) if total_sessions else 0.0

        session_latency = [x.session_latency_ms for x in self.session_results]
        start_wait = [x.start_wait_ms for x in self.session_results]
        request_latency = [x.request_latency_ms for x in self.request_results]
        status_counter = Counter(str(x.status_code) for x in self.request_results)
        req_error_counter = Counter(x.error_type for x in self.request_results if not x.ok)
        sess_error_counter = Counter(x.final_error_type for x in self.session_results if not x.ok)
        group_counter = Counter(x.prompt_group for x in self.session_results)

        group_stats: Dict[str, Dict[str, Any]] = {}
        for group in sorted(group_counter):
            group_sessions = [x for x in self.session_results if x.prompt_group == group]
            group_stats[group] = {
                "sessions": len(group_sessions),
                "success_sessions": sum(1 for x in group_sessions if x.ok),
                "failed_sessions": sum(1 for x in group_sessions if not x.ok),
                "session_latency_ms_mean": round(
                    statistics.fmean([x.session_latency_ms for x in group_sessions]), 2
                ) if group_sessions else 0.0,
                "start_wait_ms_mean": round(
                    statistics.fmean([x.start_wait_ms for x in group_sessions]), 2
                ) if group_sessions else 0.0,
            }

        failure_samples = []
        failure_details = []
        for item in self.session_results:
            if item.ok:
                continue
            detail = {
                "session_id": item.session_id,
                "prompt_group": item.prompt_group,
                "final_status_code": item.final_status_code,
                "final_request_id": item.final_request_id,
                "final_response_request_id": item.final_response_request_id,
                "final_error_type": item.final_error_type,
                "final_error_code": item.final_error_code,
                "final_error_message": item.final_error_message,
                "final_error_details": item.final_error_details,
                "final_error_payload": item.final_error_payload,
                "logged_status": item.logged_status,
                "logged_account_id": item.logged_account_id,
                "logged_account_name": item.logged_account_name,
                "logged_account_status": item.logged_account_status,
                "logged_daily_used": item.logged_daily_used,
                "logged_daily_quota": item.logged_daily_quota,
                "logged_error_message": item.logged_error_message,
                "logged_error_payload": item.logged_error_payload,
                "logged_timestamp": item.logged_timestamp,
                "session_latency_ms": item.session_latency_ms,
                "start_wait_ms": item.start_wait_ms,
            }
            failure_details.append(detail)
            if len(failure_samples) < self.args.failure_sample_limit:
                failure_samples.append(detail)

        return {
            "started_at_utc": utc_now(),
            "config": {
                "base_url": self.args.base_url,
                "model": self.args.model,
                "sessions": self.args.sessions,
                "concurrency": self.args.concurrency,
                "bookmark_sessions": self.args.bookmark_sessions,
                "bookmark_prompt": self.args.bookmark_prompt,
                "ipfs_prompt": self.args.ipfs_prompt,
                "max_steps": self.args.max_steps,
                "max_tools_per_step": self.args.max_tools_per_step,
                "stream_ratio": self.args.stream_ratio,
                "request_timeout_seconds": self.args.request_timeout,
                "shuffle_prompt_groups": self.args.shuffle_prompt_groups,
                "shuffle_request_order_each_round": self.args.shuffle_request_order_each_round,
                "tool_workspace_root": str(self.workspace_root),
                "log_flush_wait_seconds": self.args.log_flush_wait_seconds,
                "run_tag": self.run_tag,
            },
            "summary": {
                "total_sessions": total_sessions,
                "success_sessions": success_sessions,
                "failed_sessions": failed_sessions,
                "success_rate": round(success_rate, 4),
                "total_http_requests": len(self.request_results),
                "total_tool_calls": sum(x.tool_calls for x in self.session_results),
            },
            "session_latency_ms": {
                "min": min(session_latency) if session_latency else 0,
                "max": max(session_latency) if session_latency else 0,
                "mean": round(statistics.fmean(session_latency), 2) if session_latency else 0,
                "p50": round(percentile(session_latency, 0.50), 2) if session_latency else 0,
                "p95": round(percentile(session_latency, 0.95), 2) if session_latency else 0,
                "p99": round(percentile(session_latency, 0.99), 2) if session_latency else 0,
            },
            "session_start_wait_ms": {
                "min": min(start_wait) if start_wait else 0,
                "max": max(start_wait) if start_wait else 0,
                "mean": round(statistics.fmean(start_wait), 2) if start_wait else 0,
                "p50": round(percentile(start_wait, 0.50), 2) if start_wait else 0,
                "p95": round(percentile(start_wait, 0.95), 2) if start_wait else 0,
                "p99": round(percentile(start_wait, 0.99), 2) if start_wait else 0,
            },
            "request_latency_ms": {
                "min": min(request_latency) if request_latency else 0,
                "max": max(request_latency) if request_latency else 0,
                "mean": round(statistics.fmean(request_latency), 2) if request_latency else 0,
                "p50": round(percentile(request_latency, 0.50), 2) if request_latency else 0,
                "p95": round(percentile(request_latency, 0.95), 2) if request_latency else 0,
                "p99": round(percentile(request_latency, 0.99), 2) if request_latency else 0,
            },
            "request_status_distribution": dict(status_counter),
            "request_error_type_distribution": dict(req_error_counter),
            "session_error_type_distribution": dict(sess_error_counter),
            "session_group_distribution": dict(group_counter),
            "group_stats": group_stats,
            "failure_samples": failure_samples,
            "failure_details": failure_details,
        }


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Claude Code dialogue tool-loop stress test")
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--model", default="claude-opus-4-6")
    parser.add_argument("--api-key", default=os.getenv("ST_API_KEY", ""))
    parser.add_argument("--admin-username", default=os.getenv("ADMIN_USERNAME", "admin"))
    parser.add_argument("--admin-password", default=os.getenv("ADMIN_PASSWORD", "admin123"))
    parser.add_argument("--local-db-path", default="data/api_service.db")

    parser.add_argument("--sessions", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--bookmark-sessions", type=int, default=10)
    parser.add_argument("--bookmark-prompt", default="@Bookmarkvault 分析项目")
    parser.add_argument("--ipfs-prompt", default="@ipfs-file-manager 分析项目")
    parser.add_argument("--shuffle-prompt-groups", action="store_true", default=False)
    parser.add_argument("--shuffle-request-order-each-round", action="store_true", default=True)
    parser.add_argument("--no-shuffle-request-order-each-round", dest="shuffle_request_order_each_round", action="store_false")

    parser.add_argument("--max-steps", type=int, default=24)
    parser.add_argument("--max-tools-per-step", type=int, default=4)
    parser.add_argument("--max-history-messages", type=int, default=24)
    parser.add_argument("--max-tool-result-chars", type=int, default=2200)
    parser.add_argument("--stream-ratio", type=float, default=0.7)
    parser.add_argument("--request-timeout", type=float, default=180.0)
    parser.add_argument("--log-flush-wait-seconds", type=float, default=5.0)
    parser.add_argument("--max-tokens", type=int, default=220)
    parser.add_argument("--seed", type=int, default=20260330)
    parser.add_argument("--session-prefix", default="tenantA-user")
    parser.add_argument("--tool-workspace-root", default="/home/ww/Project")
    parser.add_argument(
        "--system-prompt",
        default=(
            "You are a rigorous engineering copilot. "
            "Use tools when needed, collect evidence first, and keep outputs concise."
        ),
    )
    parser.add_argument("--output-dir", default="data/stress_reports")
    parser.add_argument("--failure-sample-limit", type=int, default=30)
    return parser.parse_args(argv)


async def async_main(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    runner = DialogueRunner(args)
    report = await runner.run()

    stamp = utc_now()
    report_path = output_dir / f"cc_dialogue_report_{stamp}.json"
    sessions_path = output_dir / f"cc_dialogue_sessions_{stamp}.jsonl"
    requests_path = output_dir / f"cc_dialogue_requests_{stamp}.jsonl"

    with report_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    with sessions_path.open("w", encoding="utf-8") as f:
        for item in runner.session_results:
            f.write(json.dumps(asdict(item), ensure_ascii=False) + "\n")

    with requests_path.open("w", encoding="utf-8") as f:
        for item in runner.request_results:
            f.write(json.dumps(asdict(item), ensure_ascii=False) + "\n")

    summary = report["summary"]
    print("\n=== Claude Code Dialogue Summary ===")
    print(
        f"sessions={summary['total_sessions']} "
        f"success={summary['success_sessions']} "
        f"failed={summary['failed_sessions']} "
        f"success_rate={summary['success_rate']}"
    )
    print(f"http_requests={summary['total_http_requests']} tool_calls={summary['total_tool_calls']}")
    print(f"session_latency_ms={report['session_latency_ms']}")
    print(f"session_start_wait_ms={report['session_start_wait_ms']}")
    print(f"request_latency_ms={report['request_latency_ms']}")
    print(f"request_status_distribution={report['request_status_distribution']}")
    print(f"request_error_types={report['request_error_type_distribution']}")
    print(f"session_error_types={report['session_error_type_distribution']}")
    print(f"group_distribution={report['session_group_distribution']}")
    print(f"report_file={report_path}")
    print(f"sessions_file={sessions_path}")
    print(f"requests_file={requests_path}")
    print(f"api_key_prefix={args.api_key[:7]}..." if args.api_key else "api_key_prefix=(none)")

    return 0


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
