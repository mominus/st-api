#!/usr/bin/env python3
"""Concurrent real Claude CLI stress against BlogDemo.

Runs a configurable mix of two scenarios on the local st-api gateway:

* ``analyze`` group  - tool-calling style: ``@BlogDemo/ 深入分析该项目``.
* ``plan`` group     - plan mode style:    ``@BlogDemo/ 完善项目并增加更多功能``.

For every session we capture:

* first-token latency (time from process start to first model output event)
* first-event latency (time to the first non-system stream event)
* total session wall time
* exit code, error type / message, stop reason, token usage, tool-use count
* per-session debug log path

Per-scenario permission mode is configurable. The defaults are
``acceptEdits`` for analyze (auto-approves edit/write) and ``plan`` for the
plan-mode group (Claude generates a plan without executing writes), matching
the requested behaviour: stay inside BlogDemo and auto-approve edit/write.

Output: aggregated JSON report + per-session JSONL under ``--output-dir``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    sv = sorted(values)
    idx = int((len(sv) - 1) * p)
    return float(sv[idx])


def truncate_text(text: str, max_chars: int = 2000) -> str:
    if not text:
        return text
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... [truncated {len(text) - max_chars} chars]"


def stats_dict(values: List[int]) -> Dict[str, Any]:
    if not values:
        return {
            "count": 0,
            "min": 0,
            "max": 0,
            "mean": 0,
            "p50": 0,
            "p95": 0,
            "p99": 0,
        }
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "mean": round(statistics.fmean(values), 2),
        "p50": round(percentile(values, 0.50), 2),
        "p95": round(percentile(values, 0.95), 2),
        "p99": round(percentile(values, 0.99), 2),
    }


@dataclass
class Scenario:
    group: str
    prompt: str
    permission_mode: str


@dataclass
class SessionResult:
    session_name: str
    group: str
    prompt: str
    permission_mode: str
    cli_session_id: str
    ok: bool
    exit_code: int
    error_type: str
    error_message: str
    semaphore_wait_ms: int
    first_event_ms: int
    first_token_ms: int
    session_total_ms: int
    assistant_message_count: int
    tool_use_count: int
    user_event_count: int
    stream_event_count: int
    stop_reason: str
    result_subtype: str
    input_tokens: int
    output_tokens: int
    result_preview: str
    stderr_preview: str
    stdout_bytes: int
    stderr_bytes: int
    debug_log_path: str


class StressRunner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.results: List[SessionResult] = []

    def build_scenarios(self) -> List[Scenario]:
        analyze = Scenario(
            group="analyze",
            prompt=self.args.analyze_prompt,
            permission_mode=self.args.analyze_permission_mode,
        )
        plan = Scenario(
            group="plan",
            prompt=self.args.plan_prompt,
            permission_mode=self.args.plan_permission_mode,
        )
        scenarios: List[Scenario] = []
        i = j = 0
        while i < self.args.analyze_sessions or j < self.args.plan_sessions:
            if i < self.args.analyze_sessions:
                scenarios.append(analyze)
                i += 1
            if j < self.args.plan_sessions:
                scenarios.append(plan)
                j += 1
        return scenarios

    async def run_session(
        self,
        sem: asyncio.Semaphore,
        idx: int,
        scenario: Scenario,
        stamp: str,
        debug_dir: Path,
    ) -> SessionResult:
        ready_at = time.perf_counter()
        async with sem:
            started_at = time.perf_counter()
            sem_wait_ms = int((started_at - ready_at) * 1000)
            cli_session_id = str(uuid.uuid4())
            session_name = f"{self.args.session_prefix}-{idx:04d}"
            debug_log_path = debug_dir / f"{stamp}_{session_name}.log"

            command = [
                self.args.claude_bin,
                "-p",
                scenario.prompt,
                "--model",
                self.args.model,
                "--output-format",
                "stream-json",
                "--verbose",
                "--include-partial-messages",
                "--setting-sources",
                "local",
                "--permission-mode",
                scenario.permission_mode,
                "--no-session-persistence",
                "--session-id",
                cli_session_id,
                "--debug-file",
                str(debug_log_path),
            ]

            env = os.environ.copy()
            env["ANTHROPIC_BASE_URL"] = self.args.base_url
            env["ANTHROPIC_API_KEY"] = self.args.api_key
            env["NO_PROXY"] = "127.0.0.1,localhost,::1"
            env["no_proxy"] = "127.0.0.1,localhost,::1"

            ok = False
            exit_code = 0
            error_type = ""
            error_message = ""
            first_event_ms = -1
            first_token_ms = -1
            stop_reason = ""
            result_subtype = ""
            input_tokens = 0
            output_tokens = 0
            result_preview = ""
            assistant_count = 0
            tool_use_count = 0
            user_event_count = 0
            stream_event_count = 0
            stdout_bytes = 0
            stderr_bytes = 0
            stderr_text = ""
            tail_buffer: List[str] = []

            try:
                proc = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=self.args.cwd,
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except Exception as exc:
                return SessionResult(
                    session_name=session_name,
                    group=scenario.group,
                    prompt=scenario.prompt,
                    permission_mode=scenario.permission_mode,
                    cli_session_id=cli_session_id,
                    ok=False,
                    exit_code=-1,
                    error_type=type(exc).__name__,
                    error_message=truncate_text(str(exc), 800),
                    semaphore_wait_ms=sem_wait_ms,
                    first_event_ms=-1,
                    first_token_ms=-1,
                    session_total_ms=int((time.perf_counter() - started_at) * 1000),
                    assistant_message_count=0,
                    tool_use_count=0,
                    user_event_count=0,
                    stream_event_count=0,
                    stop_reason="",
                    result_subtype="",
                    input_tokens=0,
                    output_tokens=0,
                    result_preview="",
                    stderr_preview="",
                    stdout_bytes=0,
                    stderr_bytes=0,
                    debug_log_path=str(debug_log_path),
                )

            def _process_line(line_text: str) -> None:
                nonlocal first_event_ms, first_token_ms
                nonlocal assistant_count, tool_use_count, user_event_count
                nonlocal stream_event_count, stop_reason, result_subtype
                nonlocal input_tokens, output_tokens, result_preview

                if not line_text:
                    return
                try:
                    event = json.loads(line_text)
                except Exception:
                    return
                etype = str(event.get("type") or "")
                now_ms = int((time.perf_counter() - started_at) * 1000)
                if first_event_ms < 0 and etype and etype != "system":
                    first_event_ms = now_ms
                if etype == "stream_event":
                    stream_event_count += 1
                    ev = event.get("event") or {}
                    ev_type = str(ev.get("type") or "")
                    if ev_type == "content_block_delta":
                        delta = ev.get("delta") or {}
                        d_type = str(delta.get("type") or "")
                        text_chunk = delta.get("text") or delta.get("partial_json") or ""
                        if first_token_ms < 0 and (d_type or text_chunk):
                            first_token_ms = now_ms
                elif etype == "assistant":
                    assistant_count += 1
                    if first_token_ms < 0:
                        first_token_ms = now_ms
                    msg = event.get("message") or {}
                    for block in msg.get("content", []) or []:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            tool_use_count += 1
                elif etype == "user":
                    user_event_count += 1
                elif etype == "result":
                    stop_reason = str(event.get("stop_reason") or "")
                    result_subtype = str(event.get("subtype") or "")
                    usage = event.get("usage") or {}
                    try:
                        input_tokens = int(usage.get("input_tokens") or 0)
                        output_tokens = int(usage.get("output_tokens") or 0)
                    except Exception:
                        pass
                    rt = str(event.get("result") or "")
                    if rt:
                        result_preview = truncate_text(
                            rt.replace("\n", " ").strip(),
                            320,
                        )

            async def read_stdout() -> None:
                # Chunked read avoids asyncio.StreamReader.readline()'s default
                # 64KB per-line limit -- stream-json events with large content
                # blocks frequently exceed it (observed >120KB).
                nonlocal stdout_bytes
                buffer = bytearray()
                while True:
                    try:
                        chunk = await proc.stdout.read(65536)
                    except Exception:
                        break
                    if not chunk:
                        if buffer:
                            text = buffer.decode("utf-8", errors="replace").strip()
                            if text:
                                tail_buffer.append(text)
                                if len(tail_buffer) > 30:
                                    del tail_buffer[: len(tail_buffer) - 30]
                                _process_line(text)
                        break
                    stdout_bytes += len(chunk)
                    buffer.extend(chunk)
                    while True:
                        nl = buffer.find(b"\n")
                        if nl < 0:
                            break
                        line_bytes = bytes(buffer[:nl])
                        del buffer[: nl + 1]
                        text = line_bytes.decode("utf-8", errors="replace").strip()
                        if not text:
                            continue
                        tail_buffer.append(text)
                        if len(tail_buffer) > 30:
                            del tail_buffer[: len(tail_buffer) - 30]
                        _process_line(text)

            async def read_stderr() -> None:
                nonlocal stderr_text, stderr_bytes
                data = await proc.stderr.read()
                stderr_bytes = len(data)
                stderr_text = data.decode("utf-8", errors="replace")

            stdout_task = asyncio.create_task(read_stdout())
            stderr_task = asyncio.create_task(read_stderr())

            try:
                await asyncio.wait_for(
                    proc.wait(),
                    timeout=self.args.timeout_seconds,
                )
                exit_code = int(proc.returncode or 0)
                if exit_code == 0:
                    ok = True
                else:
                    error_type = "nonzero_exit"
                    error_message = f"Claude CLI exited with code {exit_code}"
            except asyncio.TimeoutError:
                exit_code = 124
                error_type = "timeout"
                error_message = f"Claude CLI timed out after {self.args.timeout_seconds}s"
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    await proc.wait()
                except Exception:
                    pass
            except Exception as exc:
                exit_code = -1
                error_type = type(exc).__name__
                error_message = str(exc)
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass

            try:
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            except Exception:
                pass

            session_total_ms = int((time.perf_counter() - started_at) * 1000)

            if not ok and not error_message:
                fallback = stderr_text.strip() or "".join(tail_buffer)[-800:]
                error_message = truncate_text(fallback, 800)

            result = SessionResult(
                session_name=session_name,
                group=scenario.group,
                prompt=scenario.prompt,
                permission_mode=scenario.permission_mode,
                cli_session_id=cli_session_id,
                ok=ok,
                exit_code=exit_code,
                error_type=error_type,
                error_message=truncate_text(error_message, 800),
                semaphore_wait_ms=sem_wait_ms,
                first_event_ms=first_event_ms,
                first_token_ms=first_token_ms,
                session_total_ms=session_total_ms,
                assistant_message_count=assistant_count,
                tool_use_count=tool_use_count,
                user_event_count=user_event_count,
                stream_event_count=stream_event_count,
                stop_reason=stop_reason,
                result_subtype=result_subtype,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                result_preview=result_preview,
                stderr_preview=truncate_text(stderr_text, 800),
                stdout_bytes=stdout_bytes,
                stderr_bytes=stderr_bytes,
                debug_log_path=str(debug_log_path),
            )
            self.results.append(result)
            return result

    async def run(self) -> Dict[str, Any]:
        sem = asyncio.Semaphore(self.args.concurrency)
        scenarios = self.build_scenarios()
        stamp = utc_now()
        debug_dir = Path(self.args.output_dir) / f"blogdemo_cli_debug_{stamp}"
        debug_dir.mkdir(parents=True, exist_ok=True)

        tasks = [
            asyncio.create_task(self.run_session(sem, idx, sc, stamp, debug_dir))
            for idx, sc in enumerate(scenarios)
        ]
        await asyncio.gather(*tasks)
        return self.build_report(stamp=stamp, debug_dir=debug_dir)

    def build_report(self, *, stamp: str, debug_dir: Path) -> Dict[str, Any]:
        total = len(self.results)
        success = sum(1 for r in self.results if r.ok)
        failed = total - success
        success_rate = success / total if total else 0.0

        all_first_token = [r.first_token_ms for r in self.results if r.first_token_ms >= 0]
        all_first_event = [r.first_event_ms for r in self.results if r.first_event_ms >= 0]
        all_total = [r.session_total_ms for r in self.results]

        groups = sorted({r.group for r in self.results})
        group_stats: Dict[str, Any] = {}
        for g in groups:
            items = [r for r in self.results if r.group == g]
            ft = [r.first_token_ms for r in items if r.first_token_ms >= 0]
            fe = [r.first_event_ms for r in items if r.first_event_ms >= 0]
            tot = [r.session_total_ms for r in items]
            group_stats[g] = {
                "sessions": len(items),
                "success": sum(1 for r in items if r.ok),
                "failed": sum(1 for r in items if not r.ok),
                "first_token_ms": stats_dict(ft),
                "first_event_ms": stats_dict(fe),
                "session_total_ms": stats_dict(tot),
                "tool_use_total": sum(r.tool_use_count for r in items),
                "tool_use_mean": round(
                    statistics.fmean(r.tool_use_count for r in items),
                    2,
                ) if items else 0,
                "input_tokens_total": sum(r.input_tokens for r in items),
                "output_tokens_total": sum(r.output_tokens for r in items),
            }

        exit_codes = Counter(str(r.exit_code) for r in self.results)
        error_types = Counter(r.error_type for r in self.results if not r.ok)
        stop_reasons = Counter(r.stop_reason for r in self.results)
        result_subtypes = Counter(r.result_subtype for r in self.results)

        failures: List[Dict[str, Any]] = []
        for r in self.results:
            if r.ok:
                continue
            failures.append(
                {
                    "session_name": r.session_name,
                    "group": r.group,
                    "permission_mode": r.permission_mode,
                    "exit_code": r.exit_code,
                    "error_type": r.error_type,
                    "error_message": r.error_message,
                    "session_total_ms": r.session_total_ms,
                    "first_token_ms": r.first_token_ms,
                    "first_event_ms": r.first_event_ms,
                    "stderr_preview": r.stderr_preview,
                    "debug_log_path": r.debug_log_path,
                }
            )
            if len(failures) >= self.args.failure_sample_limit:
                break

        return {
            "started_at_utc": stamp,
            "config": {
                "base_url": self.args.base_url,
                "cwd": self.args.cwd,
                "model": self.args.model,
                "concurrency": self.args.concurrency,
                "analyze_sessions": self.args.analyze_sessions,
                "plan_sessions": self.args.plan_sessions,
                "analyze_prompt": self.args.analyze_prompt,
                "plan_prompt": self.args.plan_prompt,
                "analyze_permission_mode": self.args.analyze_permission_mode,
                "plan_permission_mode": self.args.plan_permission_mode,
                "timeout_seconds": self.args.timeout_seconds,
                "claude_bin": self.args.claude_bin,
                "debug_dir": str(debug_dir),
            },
            "summary": {
                "total_sessions": total,
                "success_sessions": success,
                "failed_sessions": failed,
                "success_rate": round(success_rate, 4),
            },
            "first_token_ms_overall": stats_dict(all_first_token),
            "first_event_ms_overall": stats_dict(all_first_event),
            "session_total_ms_overall": stats_dict(all_total),
            "exit_code_distribution": dict(exit_codes),
            "error_type_distribution": dict(error_types),
            "stop_reason_distribution": dict(stop_reasons),
            "result_subtype_distribution": dict(result_subtypes),
            "group_stats": group_stats,
            "failure_samples": failures,
        }


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Concurrent real Claude CLI stress against BlogDemo.",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--model", default="claude-opus-4-6")
    parser.add_argument("--cwd", default="/home/ww/Project")
    parser.add_argument("--claude-bin", default="claude")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--analyze-sessions", type=int, default=5)
    parser.add_argument("--plan-sessions", type=int, default=5)
    parser.add_argument(
        "--analyze-prompt",
        default="@BlogDemo/ 深入分析该项目",
    )
    parser.add_argument(
        "--plan-prompt",
        default="@BlogDemo/ 完善项目并增加更多功能",
    )
    parser.add_argument(
        "--analyze-permission-mode",
        default="acceptEdits",
        choices=("acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan"),
    )
    parser.add_argument(
        "--plan-permission-mode",
        default="plan",
        choices=("acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan"),
    )
    parser.add_argument("--timeout-seconds", type=float, default=420.0)
    parser.add_argument("--session-prefix", default="blogdemo-cli")
    parser.add_argument("--output-dir", default="data/stress_reports")
    parser.add_argument("--failure-sample-limit", type=int, default=30)
    return parser.parse_args(argv)


async def async_main(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    runner = StressRunner(args)
    report = await runner.run()
    stamp = report["started_at_utc"]

    report_path = output_dir / f"blogdemo_cli_stress_report_{stamp}.json"
    sessions_path = output_dir / f"blogdemo_cli_stress_sessions_{stamp}.jsonl"

    with report_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with sessions_path.open("w", encoding="utf-8") as f:
        for r in runner.results:
            f.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")

    print("=== BlogDemo Concurrent Claude CLI Stress ===")
    print(f"summary={json.dumps(report['summary'], ensure_ascii=False)}")
    print(f"first_token_ms_overall={json.dumps(report['first_token_ms_overall'], ensure_ascii=False)}")
    print(f"first_event_ms_overall={json.dumps(report['first_event_ms_overall'], ensure_ascii=False)}")
    print(f"session_total_ms_overall={json.dumps(report['session_total_ms_overall'], ensure_ascii=False)}")
    print(f"exit_code_distribution={json.dumps(report['exit_code_distribution'], ensure_ascii=False)}")
    print(f"error_type_distribution={json.dumps(report['error_type_distribution'], ensure_ascii=False)}")
    print(f"stop_reason_distribution={json.dumps(report['stop_reason_distribution'], ensure_ascii=False)}")
    print(f"result_subtype_distribution={json.dumps(report['result_subtype_distribution'], ensure_ascii=False)}")
    print("group_stats=" + json.dumps(report["group_stats"], ensure_ascii=False, indent=2))
    print(f"report_file={report_path}")
    print(f"sessions_file={sessions_path}")
    return 0


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
