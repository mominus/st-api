#!/usr/bin/env python3
"""Run concurrent real Claude CLI sessions against a local st-api endpoint."""

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
    sorted_values = sorted(values)
    idx = int((len(sorted_values) - 1) * p)
    return float(sorted_values[idx])


def truncate_text(text: str, max_chars: int = 2000) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... [truncated {len(text) - max_chars} chars]"


@dataclass
class SessionScenario:
    session_name: str
    prompt_group: str
    prompt: str
    launch_delay_ms: int = 0


@dataclass
class SessionResult:
    session_name: str
    prompt_group: str
    prompt: str
    ok: bool
    launch_delay_ms: int
    semaphore_wait_ms: int
    start_wait_ms: int
    session_latency_ms: int
    exit_code: int
    error_type: str
    error_message: str
    stdout_preview: str
    stderr_preview: str
    stdout_bytes: int
    stderr_bytes: int
    debug_log_path: str
    cli_session_id: str


class RealClaudeCLIStressRunner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.results: List[SessionResult] = []

    def build_scenarios(self) -> List[SessionScenario]:
        scenarios: List[SessionScenario] = []
        launch_batch_size = max(0, int(self.args.launch_batch_size))
        launch_batch_interval_ms = max(0, int(self.args.launch_batch_interval_ms))

        for index in range(self.args.sessions):
            if index < self.args.bookmark_sessions:
                prompt_group = "bookmark"
                prompt = self.args.bookmark_prompt
            else:
                prompt_group = "ipfs"
                prompt = self.args.ipfs_prompt

            launch_delay_ms = 0
            if launch_batch_size > 0 and launch_batch_interval_ms > 0:
                launch_delay_ms = (index // launch_batch_size) * launch_batch_interval_ms

            scenarios.append(
                SessionScenario(
                    session_name=f"{self.args.session_prefix}-{index:04d}",
                    prompt_group=prompt_group,
                    prompt=prompt,
                    launch_delay_ms=launch_delay_ms,
                )
            )
        return scenarios

    async def run_session(
        self,
        sem: asyncio.Semaphore,
        scenario: SessionScenario,
        stamp: str,
        debug_dir: Path,
    ) -> SessionResult:
        if scenario.launch_delay_ms > 0:
            await asyncio.sleep(scenario.launch_delay_ms / 1000.0)

        ready_for_launch_at = time.perf_counter()
        async with sem:
            started_at = time.perf_counter()
            semaphore_wait_ms = int((started_at - ready_for_launch_at) * 1000)
            start_wait_ms = scenario.launch_delay_ms + semaphore_wait_ms
            cli_session_id = str(uuid.uuid4())
            debug_log_path = (
                debug_dir / f"{stamp}_{scenario.session_name}.log"
                if self.args.debug_file_mode == "all"
                else None
            )

            command = [
                self.args.claude_bin,
                "-p",
                scenario.prompt,
                "--bare",
                "--model",
                self.args.model,
                "--output-format",
                "json",
                "--setting-sources",
                "local",
                "--permission-mode",
                "bypassPermissions",
                "--no-session-persistence",
                "--session-id",
                cli_session_id,
            ]
            if debug_log_path is not None:
                command.extend(["--debug-file", str(debug_log_path)])

            env = os.environ.copy()
            env["ANTHROPIC_BASE_URL"] = self.args.base_url
            env["ANTHROPIC_API_KEY"] = self.args.api_key

            stdout_text = ""
            stderr_text = ""
            exit_code = 0
            error_type = ""
            error_message = ""
            ok = False

            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=self.args.cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self.args.timeout_seconds,
                )
                stdout_text = stdout_bytes.decode("utf-8", errors="replace")
                stderr_text = stderr_bytes.decode("utf-8", errors="replace")
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
                proc.kill()
                stdout_bytes, stderr_bytes = await proc.communicate()
                stdout_text = stdout_bytes.decode("utf-8", errors="replace")
                stderr_text = stderr_bytes.decode("utf-8", errors="replace")
            except Exception as exc:
                exit_code = -1
                error_type = type(exc).__name__
                error_message = str(exc)
                try:
                    proc.kill()
                    await proc.communicate()
                except Exception:
                    pass

            if not ok and not error_message:
                combined = stderr_text.strip() or stdout_text.strip()
                error_message = truncate_text(combined, 800)

            result = SessionResult(
                session_name=scenario.session_name,
                prompt_group=scenario.prompt_group,
                prompt=scenario.prompt,
                ok=ok,
                launch_delay_ms=scenario.launch_delay_ms,
                semaphore_wait_ms=semaphore_wait_ms,
                start_wait_ms=start_wait_ms,
                session_latency_ms=int((time.perf_counter() - started_at) * 1000),
                exit_code=exit_code,
                error_type=error_type,
                error_message=truncate_text(error_message, 800),
                stdout_preview=truncate_text(stdout_text, 1200),
                stderr_preview=truncate_text(stderr_text, 1200),
                stdout_bytes=len(stdout_text.encode("utf-8", errors="replace")),
                stderr_bytes=len(stderr_text.encode("utf-8", errors="replace")),
                debug_log_path=str(debug_log_path or ""),
                cli_session_id=cli_session_id,
            )
            self.results.append(result)
            return result

    async def run(self) -> Dict[str, Any]:
        sem = asyncio.Semaphore(self.args.concurrency)
        scenarios = self.build_scenarios()
        stamp = utc_now()
        debug_dir = Path(self.args.output_dir) / f"claude_cli_debug_{stamp}"
        if self.args.debug_file_mode == "all":
            debug_dir.mkdir(parents=True, exist_ok=True)

        tasks = [
            asyncio.create_task(
                self.run_session(sem, scenario, stamp, debug_dir)
            )
            for scenario in scenarios
        ]
        await asyncio.gather(*tasks)
        return self.build_report(stamp=stamp, debug_dir=debug_dir)

    def build_report(self, *, stamp: str, debug_dir: Path) -> Dict[str, Any]:
        total_sessions = len(self.results)
        success_sessions = sum(1 for item in self.results if item.ok)
        failed_sessions = total_sessions - success_sessions
        success_rate = (success_sessions / total_sessions) if total_sessions else 0.0

        session_latency = [item.session_latency_ms for item in self.results]
        start_wait = [item.start_wait_ms for item in self.results]
        exit_code_counter = Counter(str(item.exit_code) for item in self.results)
        error_type_counter = Counter(item.error_type for item in self.results if not item.ok)
        group_counter = Counter(item.prompt_group for item in self.results)

        failure_samples = []
        for item in self.results:
            if item.ok:
                continue
            failure_samples.append(
                {
                    "session_name": item.session_name,
                    "prompt_group": item.prompt_group,
                    "exit_code": item.exit_code,
                    "error_type": item.error_type,
                    "error_message": item.error_message,
                    "session_latency_ms": item.session_latency_ms,
                    "start_wait_ms": item.start_wait_ms,
                    "debug_log_path": item.debug_log_path,
                }
            )
            if len(failure_samples) >= self.args.failure_sample_limit:
                break

        group_stats: Dict[str, Dict[str, Any]] = {}
        for group in sorted(group_counter):
            group_items = [item for item in self.results if item.prompt_group == group]
            group_stats[group] = {
                "sessions": len(group_items),
                "success_sessions": sum(1 for item in group_items if item.ok),
                "failed_sessions": sum(1 for item in group_items if not item.ok),
                "session_latency_ms_mean": round(
                    statistics.fmean(item.session_latency_ms for item in group_items),
                    2,
                ) if group_items else 0.0,
                "start_wait_ms_mean": round(
                    statistics.fmean(item.start_wait_ms for item in group_items),
                    2,
                ) if group_items else 0.0,
            }

        return {
            "started_at_utc": stamp,
            "config": {
                "base_url": self.args.base_url,
                "cwd": self.args.cwd,
                "model": self.args.model,
                "sessions": self.args.sessions,
                "concurrency": self.args.concurrency,
                "bookmark_sessions": self.args.bookmark_sessions,
                "bookmark_prompt": self.args.bookmark_prompt,
                "ipfs_prompt": self.args.ipfs_prompt,
                "timeout_seconds": self.args.timeout_seconds,
                "claude_bin": self.args.claude_bin,
                "debug_dir": str(debug_dir),
                "debug_file_mode": self.args.debug_file_mode,
                "launch_batch_size": self.args.launch_batch_size,
                "launch_batch_interval_ms": self.args.launch_batch_interval_ms,
            },
            "summary": {
                "total_sessions": total_sessions,
                "success_sessions": success_sessions,
                "failed_sessions": failed_sessions,
                "success_rate": round(success_rate, 4),
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
            "exit_code_distribution": dict(exit_code_counter),
            "error_type_distribution": dict(error_type_counter),
            "session_group_distribution": dict(group_counter),
            "group_stats": group_stats,
            "failure_samples": failure_samples,
        }


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stress real Claude CLI sessions against a local st-api gateway."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--model", default="claude-opus-4-6")
    parser.add_argument("--cwd", default="/home/ww/Project")
    parser.add_argument("--claude-bin", default="claude")
    parser.add_argument("--sessions", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--bookmark-sessions", type=int, default=10)
    parser.add_argument("--bookmark-prompt", default="@Bookmarkvault 分析项目")
    parser.add_argument("--ipfs-prompt", default="@ipfs-file-manager 分析项目")
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument("--session-prefix", default="real-claude-cli")
    parser.add_argument("--output-dir", default="data/stress_reports")
    parser.add_argument("--failure-sample-limit", type=int, default=30)
    parser.add_argument(
        "--debug-file-mode",
        choices=("all", "none"),
        default="all",
    )
    parser.add_argument("--launch-batch-size", type=int, default=0)
    parser.add_argument("--launch-batch-interval-ms", type=int, default=0)
    return parser.parse_args(argv)


async def async_main(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    runner = RealClaudeCLIStressRunner(args)
    report = await runner.run()
    stamp = report["started_at_utc"]

    report_path = output_dir / f"real_claude_cli_report_{stamp}.json"
    sessions_path = output_dir / f"real_claude_cli_sessions_{stamp}.jsonl"

    with report_path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)

    with sessions_path.open("w", encoding="utf-8") as handle:
        for item in runner.results:
            handle.write(json.dumps(asdict(item), ensure_ascii=False) + "\n")

    print("\n=== Real Claude CLI Stress Summary ===")
    print(
        f"sessions={report['summary']['total_sessions']} "
        f"success={report['summary']['success_sessions']} "
        f"failed={report['summary']['failed_sessions']} "
        f"success_rate={report['summary']['success_rate']}"
    )
    print(f"session_latency_ms={report['session_latency_ms']}")
    print(f"session_start_wait_ms={report['session_start_wait_ms']}")
    print(f"exit_code_distribution={report['exit_code_distribution']}")
    print(f"error_type_distribution={report['error_type_distribution']}")
    print(f"group_distribution={report['session_group_distribution']}")
    print(f"report_file={report_path}")
    print(f"sessions_file={sessions_path}")
    return 0


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
