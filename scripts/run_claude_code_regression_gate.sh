#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-/home/ww/Project/.venv/bin/python}"
OUTPUT_DIR="${OUTPUT_DIR:-data/stress_reports}"
RUN_TAG="${RUN_TAG:-$(date -u +%Y%m%dT%H%M%SZ)}"
REPORT_JSON="${REPORT_JSON:-${OUTPUT_DIR}/claude_code_regression_gate_${RUN_TAG}.json}"
UNIT_LOG="${UNIT_LOG:-/tmp/claude_code_regression_pytest_${RUN_TAG}.log}"
SMOKE_LOG="${SMOKE_LOG:-/tmp/claude_code_regression_smoke_${RUN_TAG}.log}"
AUTO_CLEANUP="${AUTO_CLEANUP:-0}"
AUTO_CLEANUP_KEEP_LATEST="${AUTO_CLEANUP_KEEP_LATEST:-3}"
AUTO_CLEANUP_DRY_RUN="${AUTO_CLEANUP_DRY_RUN:-0}"
CLEANUP_LOG="${CLEANUP_LOG:-/tmp/claude_code_regression_cleanup_${RUN_TAG}.log}"
SKIP_REAL_CLI_SMOKE="${SKIP_REAL_CLI_SMOKE:-0}"

UNIT_EXIT_CODE=0
SMOKE_EXIT_CODE=0
SMOKE_STATUS="not_run"
CLEANUP_EXIT_CODE=0
CLEANUP_STATUS="not_run"
GATE_STATUS="FAIL"
FAIL_STAGE=""
FAIL_REASON=""
SMOKE_REPORT_FILE=""

mkdir -p "$OUTPUT_DIR"
rm -f "$UNIT_LOG" "$SMOKE_LOG" "$CLEANUP_LOG"

write_report() {
  REPORT_PATH="$REPORT_JSON" \
  RUN_TAG_VALUE="$RUN_TAG" \
  GATE_STATUS_VALUE="$GATE_STATUS" \
  FAIL_STAGE_VALUE="$FAIL_STAGE" \
  FAIL_REASON_VALUE="$FAIL_REASON" \
  UNIT_EXIT_CODE_VALUE="$UNIT_EXIT_CODE" \
  SMOKE_EXIT_CODE_VALUE="$SMOKE_EXIT_CODE" \
  SMOKE_STATUS_VALUE="$SMOKE_STATUS" \
  CLEANUP_EXIT_CODE_VALUE="$CLEANUP_EXIT_CODE" \
  CLEANUP_STATUS_VALUE="$CLEANUP_STATUS" \
  UNIT_LOG_VALUE="$UNIT_LOG" \
  SMOKE_LOG_VALUE="$SMOKE_LOG" \
  CLEANUP_LOG_VALUE="$CLEANUP_LOG" \
  SMOKE_REPORT_FILE_VALUE="$SMOKE_REPORT_FILE" \
  AUTO_CLEANUP_VALUE="$AUTO_CLEANUP" \
  AUTO_CLEANUP_KEEP_LATEST_VALUE="$AUTO_CLEANUP_KEEP_LATEST" \
  AUTO_CLEANUP_DRY_RUN_VALUE="$AUTO_CLEANUP_DRY_RUN" \
  "$PYTHON_BIN" - <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

report_path = Path(os.environ["REPORT_PATH"])

report = {
    "started_at_utc": os.environ["RUN_TAG_VALUE"],
    "generated_at_utc": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
    "status": os.environ["GATE_STATUS_VALUE"],
    "fail_stage": os.environ["FAIL_STAGE_VALUE"],
    "fail_reason": os.environ["FAIL_REASON_VALUE"],
    "steps": {
        "pytest": {
            "exit_code": int(os.environ["UNIT_EXIT_CODE_VALUE"]),
            "log_file": os.environ["UNIT_LOG_VALUE"],
        },
        "real_cli_smoke": {
            "status": os.environ["SMOKE_STATUS_VALUE"],
            "exit_code": int(os.environ["SMOKE_EXIT_CODE_VALUE"]),
            "log_file": os.environ["SMOKE_LOG_VALUE"],
            "report_file": os.environ["SMOKE_REPORT_FILE_VALUE"],
        },
        "cleanup": {
            "status": os.environ["CLEANUP_STATUS_VALUE"],
            "exit_code": int(os.environ["CLEANUP_EXIT_CODE_VALUE"]),
            "log_file": os.environ["CLEANUP_LOG_VALUE"],
            "enabled": os.environ["AUTO_CLEANUP_VALUE"] == "1",
            "keep_latest": int(os.environ["AUTO_CLEANUP_KEEP_LATEST_VALUE"]),
            "dry_run": os.environ["AUTO_CLEANUP_DRY_RUN_VALUE"] == "1",
        },
    },
}

report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
PY
}

run_cleanup_step() {
  if [[ "$AUTO_CLEANUP" != "1" ]]; then
    CLEANUP_STATUS="not_run"
    CLEANUP_EXIT_CODE=0
    return 0
  fi

  if ! [[ "$AUTO_CLEANUP_KEEP_LATEST" =~ ^[0-9]+$ ]] || (( AUTO_CLEANUP_KEEP_LATEST < 1 )); then
    CLEANUP_STATUS="failed"
    CLEANUP_EXIT_CODE=2
    FAIL_STAGE="cleanup"
    FAIL_REASON="invalid_auto_cleanup_keep_latest"
    return 2
  fi

  local -a cleanup_cmd=(
    bash
    "$ROOT_DIR/scripts/cleanup_claude_code_artifacts.sh"
    --keep-latest
    "$AUTO_CLEANUP_KEEP_LATEST"
  )
  if [[ "$AUTO_CLEANUP_DRY_RUN" == "1" ]]; then
    cleanup_cmd+=(--dry-run)
  fi

  set +e
  "${cleanup_cmd[@]}" >"$CLEANUP_LOG" 2>&1
  CLEANUP_EXIT_CODE=$?
  set -e

  if [[ "$CLEANUP_EXIT_CODE" -ne 0 ]]; then
    CLEANUP_STATUS="failed"
    FAIL_STAGE="cleanup"
    FAIL_REASON="cleanup_failed"
    return "$CLEANUP_EXIT_CODE"
  fi

  if [[ "$AUTO_CLEANUP_DRY_RUN" == "1" ]]; then
    CLEANUP_STATUS="dry_run"
  else
    CLEANUP_STATUS="passed"
  fi
  return 0
}

finalize_success() {
  GATE_STATUS="PASS"
  FAIL_STAGE=""
  FAIL_REASON=""

  if ! run_cleanup_step; then
    GATE_STATUS="FAIL"
    write_report
    echo "gate_status=FAIL"
    echo "fail_stage=$FAIL_STAGE"
    echo "fail_reason=$FAIL_REASON"
    echo "unit_log=$UNIT_LOG"
    echo "smoke_log=$SMOKE_LOG"
    echo "cleanup_log=$CLEANUP_LOG"
    echo "report_file=$REPORT_JSON"
    cat "$CLEANUP_LOG"
    exit "$CLEANUP_EXIT_CODE"
  fi

  write_report
}

echo "=== Claude Code Regression Gate ==="
echo "run_tag=$RUN_TAG"

set +e
"$PYTHON_BIN" -m pytest -q \
  tests/test_history_budget.py \
  tests/test_protocol_bridge.py \
  tests/test_gateway_runtime_routing.py \
  tests/test_capability_matrix.py \
  tests/test_capability_matrix_routes.py \
  >"$UNIT_LOG" 2>&1
UNIT_EXIT_CODE=$?
set -e

if [[ "$UNIT_EXIT_CODE" -ne 0 ]]; then
  GATE_STATUS="FAIL"
  FAIL_STAGE="pytest"
  FAIL_REASON="pytest_failed"
  write_report
  echo "gate_status=FAIL"
  echo "fail_stage=pytest"
  echo "fail_reason=pytest_failed"
  echo "unit_log=$UNIT_LOG"
  echo "report_file=$REPORT_JSON"
  cat "$UNIT_LOG"
  exit "$UNIT_EXIT_CODE"
fi

echo "pytest_status=PASS"
echo "unit_log=$UNIT_LOG"

if [[ "$SKIP_REAL_CLI_SMOKE" == "1" ]]; then
  SMOKE_STATUS="skipped"
  SMOKE_EXIT_CODE=0
  finalize_success
  echo "real_cli_smoke=SKIPPED"
  if [[ "$AUTO_CLEANUP" == "1" ]]; then
    echo "cleanup_status=$CLEANUP_STATUS"
    echo "cleanup_log=$CLEANUP_LOG"
  fi
  echo "gate_status=PASS"
  echo "report_file=$REPORT_JSON"
  exit 0
fi

set +e
bash "$ROOT_DIR/scripts/run_local_real_claude_cli_smoke.sh" >"$SMOKE_LOG" 2>&1
SMOKE_EXIT_CODE=$?
set -e

SMOKE_REPORT_FILE="$(
  awk -F= '/^report_file=/{value=$2} END{print value}' "$SMOKE_LOG"
)"

if [[ "$SMOKE_EXIT_CODE" -ne 0 ]]; then
  SMOKE_STATUS="failed"
  GATE_STATUS="FAIL"
  FAIL_STAGE="real_cli_smoke"
  FAIL_REASON="real_cli_smoke_failed"
  write_report
  echo "gate_status=FAIL"
  echo "fail_stage=real_cli_smoke"
  echo "fail_reason=real_cli_smoke_failed"
  echo "unit_log=$UNIT_LOG"
  echo "smoke_log=$SMOKE_LOG"
  echo "smoke_report_file=${SMOKE_REPORT_FILE:-<missing>}"
  echo "report_file=$REPORT_JSON"
  cat "$SMOKE_LOG"
  exit "$SMOKE_EXIT_CODE"
fi

SMOKE_STATUS="passed"
finalize_success

echo "real_cli_smoke=PASS"
echo "smoke_log=$SMOKE_LOG"
echo "smoke_report_file=${SMOKE_REPORT_FILE:-<missing>}"
if [[ "$AUTO_CLEANUP" == "1" ]]; then
  echo "cleanup_status=$CLEANUP_STATUS"
  echo "cleanup_log=$CLEANUP_LOG"
fi
echo "gate_status=PASS"
echo "report_file=$REPORT_JSON"
