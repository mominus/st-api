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
import pathlib
import re
import subprocess
import sys
from typing import List, Optional


ROOT = pathlib.Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"
CHANGELOG_FILE = ROOT / "CHANGELOG.md"
APP_INIT_FILE = ROOT / "app" / "__init__.py"

SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


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


def parse_args() -> argparse.Namespace:
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        validate_version(args.version)
        if (args.tag or args.push) and not args.commit:
            raise RuntimeError("--tag/--push requires --commit to ensure tag points to release commit")

        current_version = read_current_version()
        if current_version == args.version:
            raise RuntimeError(f"Version is already {args.version}")

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
