from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
RELEASE_SCRIPT = ROOT / "scripts" / "release.py"
SPEC = importlib.util.spec_from_file_location("st_api_release_helper", RELEASE_SCRIPT)
release = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = release
SPEC.loader.exec_module(release)


def test_build_claude_code_gate_env_sets_expected_overrides(monkeypatch):
    monkeypatch.setenv("EXISTING_ENV", "1")
    options = release.ClaudeCodeGateOptions(
        enabled=True,
        python_bin="/tmp/python",
        skip_real_cli_smoke=True,
        auto_cleanup=True,
        auto_cleanup_keep_latest=5,
        auto_cleanup_dry_run=True,
    )
    report_path = release.build_claude_code_gate_report_path("release_20260420T000000Z")

    env = release.build_claude_code_gate_env(
        options,
        run_tag="release_20260420T000000Z",
        report_path=report_path,
    )

    assert env["EXISTING_ENV"] == "1"
    assert env["RUN_TAG"] == "release_20260420T000000Z"
    assert env["REPORT_JSON"] == str(report_path)
    assert env["PYTHON_BIN"] == "/tmp/python"
    assert env["SKIP_REAL_CLI_SMOKE"] == "1"
    assert env["AUTO_CLEANUP"] == "1"
    assert env["AUTO_CLEANUP_KEEP_LATEST"] == "5"
    assert env["AUTO_CLEANUP_DRY_RUN"] == "1"


def test_run_claude_code_gate_uses_release_scoped_report_path():
    calls = {}
    now = dt.datetime(2026, 4, 20, 0, 0, 0, tzinfo=dt.timezone.utc)

    def fake_runner(cmd, cwd, env, check):
        calls["cmd"] = cmd
        calls["cwd"] = cwd
        calls["env"] = env
        calls["check"] = check
        return SimpleNamespace(returncode=0)

    result = release.run_claude_code_gate(
        release.ClaudeCodeGateOptions(enabled=True, skip_real_cli_smoke=True),
        runner=fake_runner,
        now_factory=lambda: now,
    )

    expected_run_tag = "release_20260420T000000Z"
    expected_report = (
        release.ROOT
        / "data"
        / "stress_reports"
        / "claude_code_regression_gate_release_20260420T000000Z.json"
    )
    assert calls["cmd"] == ["bash", str(release.CLAUDE_CODE_GATE_SCRIPT)]
    assert calls["cwd"] == release.ROOT
    assert calls["check"] is False
    assert calls["env"]["RUN_TAG"] == expected_run_tag
    assert calls["env"]["REPORT_JSON"] == str(expected_report)
    assert calls["env"]["SKIP_REAL_CLI_SMOKE"] == "1"
    assert result.run_tag == expected_run_tag
    assert result.report_path == expected_report
    assert result.exit_code == 0


def test_main_runs_gate_before_writing_release_files(monkeypatch, capsys):
    calls = []

    monkeypatch.setattr(release, "validate_version", lambda version: calls.append(("validate", version)))
    monkeypatch.setattr(release, "read_current_version", lambda: "1.0.0")
    monkeypatch.setattr(
        release,
        "run_claude_code_gate",
        lambda options: calls.append(("gate", options.enabled)) or SimpleNamespace(
            report_path=release.ROOT / "data" / "stress_reports" / "gate.json"
        ),
    )
    monkeypatch.setattr(release, "get_latest_tag", lambda: calls.append(("latest_tag", None)) or None)
    monkeypatch.setattr(
        release,
        "get_commit_subjects_since",
        lambda tag: calls.append(("commits", tag)) or ["feat: change"],
    )
    monkeypatch.setattr(
        release,
        "build_release_entry",
        lambda **kwargs: calls.append(("entry", kwargs["version"])) or "entry",
    )
    monkeypatch.setattr(
        release,
        "update_version_files",
        lambda version: calls.append(("update_files", version)),
    )
    monkeypatch.setattr(release, "prepend_changelog", lambda entry, version: calls.append(("changelog", version)))

    exit_code = release.main(["--version", "1.0.1", "--run-claude-code-gate"])

    assert exit_code == 0
    assert calls == [
        ("validate", "1.0.1"),
        ("gate", True),
        ("latest_tag", None),
        ("commits", None),
        ("entry", "1.0.1"),
        ("update_files", "1.0.1"),
        ("changelog", "1.0.1"),
    ]
    stdout = capsys.readouterr().out
    assert "Claude Code gate report: data/stress_reports/gate.json" in stdout


def test_main_stops_before_mutating_files_when_gate_fails(monkeypatch):
    calls = []

    monkeypatch.setattr(release, "validate_version", lambda version: calls.append(("validate", version)))
    monkeypatch.setattr(release, "read_current_version", lambda: "1.0.0")
    monkeypatch.setattr(
        release,
        "run_claude_code_gate",
        lambda options: (_ for _ in ()).throw(RuntimeError("gate failed")),
    )
    monkeypatch.setattr(
        release,
        "update_version_files",
        lambda version: calls.append(("update_files", version)),
    )

    exit_code = release.main(["--version", "1.0.1", "--run-claude-code-gate"])

    assert exit_code == 1
    assert calls == [("validate", "1.0.1")]
