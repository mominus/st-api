#!/usr/bin/env python3
"""
Release helper for st-api.

Capabilities:
- Update VERSION and app/__init__.py version
- Prepend a release entry into CHANGELOG.md
- Optionally create git tag
- Optionally push branch and tags to remote (Hugging Face remote works)
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Optional


ROOT = pathlib.Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"
CHANGELOG_FILE = ROOT / "CHANGELOG.md"
APP_INIT_FILE = ROOT / "app" / "__init__.py"
CLAUDE_CODE_GATE_SCRIPT = ROOT / "scripts" / "run_claude_code_regression_gate.sh"
CLAUDE_CODE_GATE_REPORT_DIR = ROOT / "data" / "stress_reports"

SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


@dataclass(frozen=True)
class ClaudeCodeGateOptions:
    enabled: bool = False
    python_bin: Optional[str] = None
    skip_real_cli_smoke: bool = False
    auto_cleanup: bool = False
    auto_cleanup_keep_latest: int = 3
    auto_cleanup_dry_run: bool = False


@dataclass(frozen=True)
class ClaudeCodeGateResult:
    run_tag: str
    report_path: pathlib.Path
    exit_code: int


def run_git(args: List[str], check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=check,
        text=True,
        capture_output=True,
    )
    return (result.stdout or "").strip()


def validate_version(version: str) -> None:
    if not SEMVER_PATTERN.match(version):
        raise ValueError("Version must be SemVer format: x.y.z")


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def build_claude_code_gate_run_tag(now: Optional[dt.datetime] = None) -> str:
    timestamp = (now or utc_now()).strftime("%Y%m%dT%H%M%SZ")
    return f"release_{timestamp}"


def build_claude_code_gate_report_path(run_tag: str) -> pathlib.Path:
    return CLAUDE_CODE_GATE_REPORT_DIR / f"claude_code_regression_gate_{run_tag}.json"


def build_claude_code_gate_env(
    options: ClaudeCodeGateOptions,
    *,
    run_tag: str,
    report_path: pathlib.Path,
) -> dict[str, str]:
    env = os.environ.copy()
    env["RUN_TAG"] = run_tag
    env["REPORT_JSON"] = str(report_path)
    if options.python_bin:
        env["PYTHON_BIN"] = options.python_bin
    if options.skip_real_cli_smoke:
        env["SKIP_REAL_CLI_SMOKE"] = "1"
    if options.auto_cleanup:
        env["AUTO_CLEANUP"] = "1"
        env["AUTO_CLEANUP_KEEP_LATEST"] = str(options.auto_cleanup_keep_latest)
        if options.auto_cleanup_dry_run:
            env["AUTO_CLEANUP_DRY_RUN"] = "1"
    return env


def validate_claude_code_gate_options(options: ClaudeCodeGateOptions) -> None:
    if not options.enabled:
        return
    if options.auto_cleanup_keep_latest < 1:
        raise ValueError("Claude Code gate auto-cleanup keep-latest must be >= 1")


def run_claude_code_gate(
    options: ClaudeCodeGateOptions,
    *,
    runner=subprocess.run,
    now_factory=utc_now,
) -> ClaudeCodeGateResult:
    validate_claude_code_gate_options(options)
    if not options.enabled:
        raise ValueError("Claude Code gate is not enabled")

    run_tag = build_claude_code_gate_run_tag(now_factory())
    report_path = build_claude_code_gate_report_path(run_tag)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    env = build_claude_code_gate_env(options, run_tag=run_tag, report_path=report_path)

    result = runner(
        ["bash", str(CLAUDE_CODE_GATE_SCRIPT)],
        cwd=ROOT,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Claude Code regression gate failed before release publish "
            f"(report: {report_path})"
        )
    return ClaudeCodeGateResult(
        run_tag=run_tag,
        report_path=report_path,
        exit_code=result.returncode,
    )


def read_current_version() -> str:
    if VERSION_FILE.exists():
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    init_text = APP_INIT_FILE.read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', init_text)
    if match:
        return match.group(1)
    raise RuntimeError("Cannot determine current version")


def update_version_files(new_version: str) -> None:
    VERSION_FILE.write_text(f"{new_version}\n", encoding="utf-8")

    init_text = APP_INIT_FILE.read_text(encoding="utf-8")
    updated = re.sub(
        r'(__version__\s*=\s*")([^"]+)(")',
        rf"\g<1>{new_version}\g<3>",
        init_text,
        count=1,
    )
    if updated == init_text:
        raise RuntimeError("Failed to update app/__init__.py version")
    APP_INIT_FILE.write_text(updated, encoding="utf-8")


def get_latest_tag() -> Optional[str]:
    try:
        tag = run_git(["describe", "--tags", "--abbrev=0"])
        return tag if tag else None
    except subprocess.CalledProcessError:
        return None


def get_commit_subjects_since(tag: Optional[str]) -> List[str]:
    revision_range = f"{tag}..HEAD" if tag else "HEAD"
    output = run_git(["log", "--pretty=format:%s", revision_range], check=False)
    if not output:
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def build_release_entry(
    version: str,
    release_date: str,
    notes: List[str],
    commit_subjects: List[str],
) -> str:
    lines: List[str] = [f"## v{version} - {release_date}", ""]

    if notes:
        for note in notes:
            lines.append(f"- {note}")
    elif commit_subjects:
        for subject in commit_subjects:
            lines.append(f"- {subject}")
    else:
        lines.append("- No user-facing changes recorded.")

    lines.append("")
    return "\n".join(lines)


def prepend_changelog(entry: str, version: str) -> None:
    header = "# Changelog\n\nAll notable changes to this project will be documented in this file.\n\n"
    if CHANGELOG_FILE.exists():
        existing = CHANGELOG_FILE.read_text(encoding="utf-8")
    else:
        existing = header

    if f"## v{version} " in existing:
        raise RuntimeError(f"CHANGELOG already contains v{version}")

    if not existing.startswith("# Changelog"):
        existing = f"{header}{existing.lstrip()}"

    marker = "\n\n"
    first_block_end = existing.find(marker)
    if first_block_end == -1:
        updated = f"{existing.rstrip()}\n\n{entry}"
    else:
        insert_at = existing.find(marker, first_block_end + len(marker))
        if insert_at == -1:
            # Keep header + single paragraph intact, append entry afterwards
            updated = f"{existing.rstrip()}\n\n{entry}"
        else:
            updated = f"{existing[:insert_at+2]}{entry}{existing[insert_at+2:]}"

    CHANGELOG_FILE.write_text(updated, encoding="utf-8")


def create_tag(version: str) -> None:
    tag_name = f"v{version}"
    run_git(["tag", "-a", tag_name, "-m", f"Release {tag_name}"])


def push_release(remote: str, branch: str) -> None:
    run_git(["push", remote, branch])
    run_git(["push", remote, "--tags"])


def commit_release(version: str, message: Optional[str] = None) -> None:
    commit_message = message or f"release: v{version}"
    run_git(
        [
            "add",
            str(VERSION_FILE.relative_to(ROOT)),
            str(APP_INIT_FILE.relative_to(ROOT)),
            str(CHANGELOG_FILE.relative_to(ROOT)),
        ]
    )
    run_git(["commit", "-m", commit_message])


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare and publish a release.")
    parser.add_argument("--version", required=True, help="Release version, SemVer format (x.y.z)")
    parser.add_argument(
        "--note",
        action="append",
        default=[],
        help="Release note bullet item; can be used multiple times",
    )
    parser.add_argument(
        "--date",
        default=dt.date.today().isoformat(),
        help="Release date in YYYY-MM-DD (default: today)",
    )
    parser.add_argument("--commit", action="store_true", help="Create release commit after updating files")
    parser.add_argument("--commit-message", default=None, help="Custom release commit message")
    parser.add_argument("--tag", action="store_true", help="Create annotated git tag (v<version>)")
    parser.add_argument("--push", action="store_true", help="Push current branch and tags")
    parser.add_argument("--remote", default="origin", help="Git remote name for push")
    parser.add_argument("--branch", default="main", help="Git branch name for push")
    parser.add_argument(
        "--run-claude-code-gate",
        action="store_true",
        help="Run the Claude Code regression gate before mutating release files",
    )
    parser.add_argument(
        "--gate-python-bin",
        default=None,
        help="Override PYTHON_BIN for the Claude Code regression gate",
    )
    parser.add_argument(
        "--gate-skip-real-cli-smoke",
        action="store_true",
        help="Run only the pytest portion of the Claude Code gate",
    )
    parser.add_argument(
        "--gate-auto-cleanup",
        action="store_true",
        help="Prune older generated Claude Code artifacts after a successful gate run",
    )
    parser.add_argument(
        "--gate-auto-cleanup-keep-latest",
        type=int,
        default=3,
        help="Retention count passed to the Claude Code gate cleanup step",
    )
    parser.add_argument(
        "--gate-auto-cleanup-dry-run",
        action="store_true",
        help="Preview Claude Code artifact cleanup without deleting files",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    try:
        validate_version(args.version)
        if (args.tag or args.push) and not args.commit:
            raise RuntimeError("--tag/--push requires --commit to ensure tag points to release commit")
        if (
            args.gate_python_bin
            or args.gate_skip_real_cli_smoke
            or args.gate_auto_cleanup
            or args.gate_auto_cleanup_keep_latest != 3
            or args.gate_auto_cleanup_dry_run
        ) and not args.run_claude_code_gate:
            raise RuntimeError(
                "--gate-* options require --run-claude-code-gate"
            )

        current_version = read_current_version()
        if current_version == args.version:
            raise RuntimeError(f"Version is already {args.version}")

        gate_options = ClaudeCodeGateOptions(
            enabled=args.run_claude_code_gate,
            python_bin=args.gate_python_bin,
            skip_real_cli_smoke=args.gate_skip_real_cli_smoke,
            auto_cleanup=args.gate_auto_cleanup,
            auto_cleanup_keep_latest=args.gate_auto_cleanup_keep_latest,
            auto_cleanup_dry_run=args.gate_auto_cleanup_dry_run,
        )
        validate_claude_code_gate_options(gate_options)
        gate_result = None
        if gate_options.enabled:
            gate_result = run_claude_code_gate(gate_options)

        latest_tag = get_latest_tag()
        commit_subjects = get_commit_subjects_since(latest_tag)
        entry = build_release_entry(
            version=args.version,
            release_date=args.date,
            notes=args.note,
            commit_subjects=commit_subjects,
        )

        update_version_files(args.version)
        prepend_changelog(entry, args.version)

        if args.commit:
            commit_release(args.version, args.commit_message)

        if args.tag:
            create_tag(args.version)

        if args.push:
            push_release(args.remote, args.branch)

        print(f"Release prepared: v{args.version}")
        print("Updated files:")
        print(f"- {VERSION_FILE.relative_to(ROOT)}")
        print(f"- {APP_INIT_FILE.relative_to(ROOT)}")
        print(f"- {CHANGELOG_FILE.relative_to(ROOT)}")
        if gate_result:
            print(f"Claude Code gate report: {gate_result.report_path.relative_to(ROOT)}")
        if args.commit:
            print(f"Release commit created: {args.commit_message or f'release: v{args.version}'}")
        if args.tag:
            print(f"Tag created: v{args.version}")
        if args.push:
            print(f"Pushed branch '{args.branch}' and tags to remote '{args.remote}'")
        return 0

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
