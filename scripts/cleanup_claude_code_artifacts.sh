#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

REPORT_DIR="${REPORT_DIR:-data/stress_reports}"
KEEP_LATEST="${KEEP_LATEST:-3}"
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage:
  bash scripts/cleanup_claude_code_artifacts.sh [--dry-run] [--keep-latest N]

Behavior:
  - Only manages generated Claude Code validation artifacts.
  - Does not touch tracked historical stress_report/session_stats/stress_details files.
  - Keeps the newest N files for each generated artifact family.

Managed artifact families:
  - data/stress_reports/cc_dialogue_report_*.json
  - data/stress_reports/cc_dialogue_requests_*.jsonl
  - data/stress_reports/cc_dialogue_sessions_*.jsonl
  - data/stress_reports/real_claude_cli_smoke_report_*.json
  - data/stress_reports/claude_code_regression_gate_*.json
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --keep-latest)
      if [[ $# -lt 2 ]]; then
        echo "missing_value_for_keep_latest" >&2
        exit 2
      fi
      KEEP_LATEST="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown_argument=$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! [[ "$KEEP_LATEST" =~ ^[0-9]+$ ]]; then
  echo "invalid_keep_latest=$KEEP_LATEST" >&2
  exit 2
fi

cleanup_family() {
  local label="$1"
  local pattern="$2"

  mapfile -t files < <(find "$REPORT_DIR" -maxdepth 1 -type f -name "$pattern" | sort)
  local total="${#files[@]}"
  local keep="$KEEP_LATEST"
  local remove_count=0

  if (( total > keep )); then
    remove_count=$((total - keep))
  fi

  echo "family=${label} total=${total} keep=${keep} remove=${remove_count}"

  if (( remove_count == 0 )); then
    return 0
  fi

  for file in "${files[@]:0:remove_count}"; do
    if (( DRY_RUN == 1 )); then
      echo "would_remove=${file}"
    else
      rm -f "$file"
      echo "removed=${file}"
    fi
  done
}

cleanup_family "cc_dialogue_report" "cc_dialogue_report_*.json"
cleanup_family "cc_dialogue_requests" "cc_dialogue_requests_*.jsonl"
cleanup_family "cc_dialogue_sessions" "cc_dialogue_sessions_*.jsonl"
cleanup_family "real_cli_smoke" "real_claude_cli_smoke_report_*.json"
cleanup_family "regression_gate" "claude_code_regression_gate_*.json"
