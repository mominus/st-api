#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

SERVER_LOG="${SERVER_LOG:-/tmp/st_api_real_cli_smoke_server.log}"
PYTHON_BIN="${PYTHON_BIN:-/home/ww/Project/.venv/bin/python}"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-18080}"
BASE_URL="${BASE_URL:-http://${HOST}:${PORT}}"
MODEL="${MODEL:-claude-opus-4-6}"
PROMPT="${PROMPT:-@BookmarkVault 仅用最少工具，告诉我这个项目主要使用什么前端框架和构建工具。回答控制在3行内，不要继续扩展。}"
CWD="${CWD:-/home/ww/Project}"
CLAUDE_TIMEOUT_SECONDS="${CLAUDE_TIMEOUT_SECONDS:-180}"
ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin123}"
OUTPUT_DIR="${OUTPUT_DIR:-data/stress_reports}"
RUN_TAG="${RUN_TAG:-$(date -u +%Y%m%dT%H%M%SZ)}"
TMP_DIR="$(mktemp -d /tmp/st_api_real_cli_smoke.XXXXXX)"
RESULT_JSON="${RESULT_JSON:-/tmp/st_api_real_cli_smoke_result.json}"
REPORT_JSON="${REPORT_JSON:-${OUTPUT_DIR}/real_claude_cli_smoke_report_${RUN_TAG}.json}"
SUMMARY_JSON="${TMP_DIR}/summary.json"
EXPECTED_SUBSTRINGS="${EXPECTED_SUBSTRINGS:-Vue,Vite}"

TEMP_KEY_ID=""
TEMP_API_KEY=""
AUTH_TOKEN=""
SERVER_PID=""
GATE_STATUS="FAIL"
FAIL_STAGE="init"
FAIL_REASON=""
TEMP_KEY_CREATED="false"

cleanup_resources() {
  if [[ -n "$TEMP_KEY_ID" && -n "$AUTH_TOKEN" ]]; then
    curl --noproxy '*' --max-time 5 -sS -X DELETE \
      "${BASE_URL}/api/admin/keys/${TEMP_KEY_ID}" \
      -H "Authorization: Bearer ${AUTH_TOKEN}" >/dev/null 2>&1 || true
  fi

  if [[ -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi

  rm -rf "$TMP_DIR"
}

write_report() {
  local exit_code="$1"
  REPORT_PATH="$REPORT_JSON" \
  SUMMARY_PATH="$SUMMARY_JSON" \
  RESULT_PATH="$RESULT_JSON" \
  SERVER_LOG_PATH="$SERVER_LOG" \
  GATE_STATUS_VALUE="$GATE_STATUS" \
  FAIL_STAGE_VALUE="$FAIL_STAGE" \
  FAIL_REASON_VALUE="$FAIL_REASON" \
  EXIT_CODE_VALUE="$exit_code" \
  BASE_URL_VALUE="$BASE_URL" \
  MODEL_VALUE="$MODEL" \
  PROMPT_VALUE="$PROMPT" \
  CWD_VALUE="$CWD" \
  EXPECTED_SUBSTRINGS_VALUE="$EXPECTED_SUBSTRINGS" \
  TEMP_KEY_CREATED_VALUE="$TEMP_KEY_CREATED" \
  RUN_TAG_VALUE="$RUN_TAG" \
  "$PYTHON_BIN" - <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

report_path = Path(os.environ["REPORT_PATH"])
summary_path = Path(os.environ["SUMMARY_PATH"])
summary = None
if summary_path.exists():
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

report = {
    "started_at_utc": os.environ["RUN_TAG_VALUE"],
    "generated_at_utc": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
    "status": os.environ["GATE_STATUS_VALUE"],
    "exit_code": int(os.environ["EXIT_CODE_VALUE"]),
    "fail_stage": os.environ["FAIL_STAGE_VALUE"],
    "fail_reason": os.environ["FAIL_REASON_VALUE"],
    "config": {
        "base_url": os.environ["BASE_URL_VALUE"],
        "model": os.environ["MODEL_VALUE"],
        "prompt": os.environ["PROMPT_VALUE"],
        "cwd": os.environ["CWD_VALUE"],
        "expected_substrings": [
            item.strip() for item in os.environ["EXPECTED_SUBSTRINGS_VALUE"].split(",")
            if item.strip()
        ],
        "temp_key_created": os.environ["TEMP_KEY_CREATED_VALUE"].lower() == "true",
    },
    "artifacts": {
        "result_json": os.environ["RESULT_PATH"],
        "server_log": os.environ["SERVER_LOG_PATH"],
    },
    "summary": summary,
}

report_path.parent.mkdir(parents=True, exist_ok=True)
report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
PY
}

finalize() {
  local exit_code=$?
  trap - EXIT

  if [[ "$exit_code" -eq 0 && "$GATE_STATUS" != "PASS" ]]; then
    GATE_STATUS="FAIL"
    FAIL_STAGE="${FAIL_STAGE:-finalize}"
    FAIL_REASON="${FAIL_REASON:-completed_without_pass_marker}"
    exit_code=1
  fi

  write_report "$exit_code" || true

  if [[ "$exit_code" -ne 0 ]]; then
    echo "=== Real Claude CLI Smoke ==="
    echo "gate_status=FAIL"
    echo "fail_stage=${FAIL_STAGE:-unknown}"
    echo "fail_reason=${FAIL_REASON:-exit_code_${exit_code}}"
    echo "report_file=$REPORT_JSON"
    echo "result_json=$RESULT_JSON"
    echo "server_log=$SERVER_LOG"
  fi

  cleanup_resources
  exit "$exit_code"
}
trap finalize EXIT

mkdir -p "$OUTPUT_DIR"

if [[ ! -x "$PYTHON_BIN" ]]; then
  FAIL_STAGE="bootstrap"
  FAIL_REASON="python_not_found"
  echo "python_not_found=$PYTHON_BIN" >&2
  exit 2
fi

if ! command -v "$CLAUDE_BIN" >/dev/null 2>&1; then
  FAIL_STAGE="bootstrap"
  FAIL_REASON="claude_not_found"
  echo "claude_not_found=$CLAUDE_BIN" >&2
  exit 2
fi

rm -f "$SERVER_LOG"
rm -f "$RESULT_JSON"

PORT="$PORT" DEBUG=false UVICORN_ACCESS_LOG=false "$PYTHON_BIN" run.py >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

HEALTH_OK=0
for i in $(seq 1 80); do
  if curl --noproxy '*' --max-time 1 -sS "${BASE_URL}/health" >/dev/null 2>&1; then
    HEALTH_OK=1
    echo "health=ok attempt=$i"
    break
  fi
  sleep 0.25
done

if [[ "$HEALTH_OK" -ne 1 ]]; then
  FAIL_STAGE="startup"
  FAIL_REASON="health_check_failed"
  echo "health=failed"
  echo "server_log=$SERVER_LOG"
  exit 3
fi

API_KEY="${ST_API_KEY:-}"
if [[ -z "$API_KEY" ]]; then
  FAIL_STAGE="admin_login"
  LOGIN_RESPONSE="$(
    curl --noproxy '*' --max-time 5 -sS -X POST \
      "${BASE_URL}/api/admin/auth/login" \
      -H "Content-Type: application/json" \
      -d "{\"username\":\"${ADMIN_USERNAME}\",\"password\":\"${ADMIN_PASSWORD}\"}"
  )"

  AUTH_TOKEN="$(
    printf '%s' "$LOGIN_RESPONSE" | "$PYTHON_BIN" -c \
      'import json, sys; data = json.load(sys.stdin); print(data.get("token", ""))'
  )"

  if [[ -z "$AUTH_TOKEN" ]]; then
    FAIL_REASON="admin_login_failed"
    echo "admin_login_failed"
    printf '%s\n' "$LOGIN_RESPONSE"
    exit 4
  fi

  FAIL_STAGE="create_temp_key"
  CREATE_KEY_RESPONSE="$(
    curl --noproxy '*' --max-time 5 -sS -X POST \
      "${BASE_URL}/api/admin/keys" \
      -H "Authorization: Bearer ${AUTH_TOKEN}" \
      -H "Content-Type: application/json" \
      -d "{\"name\":\"real-cli-smoke\",\"model_groups\":[\"${MODEL}\"],\"request_quota\":null,\"token_quota\":null,\"cost_limit\":null,\"expires_at\":null}"
  )"

  TEMP_KEY_ID="$(
    printf '%s' "$CREATE_KEY_RESPONSE" | "$PYTHON_BIN" -c \
      'import json, sys; data = json.load(sys.stdin); print(((data.get("key_info") or {}).get("id", "")))'
  )"
  TEMP_API_KEY="$(
    printf '%s' "$CREATE_KEY_RESPONSE" | "$PYTHON_BIN" -c \
      'import json, sys; data = json.load(sys.stdin); print(data.get("key", ""))'
  )"

  if [[ -z "$TEMP_KEY_ID" || -z "$TEMP_API_KEY" ]]; then
    FAIL_REASON="temp_key_create_failed"
    echo "temp_key_create_failed"
    printf '%s\n' "$CREATE_KEY_RESPONSE"
    exit 5
  fi

  API_KEY="$TEMP_API_KEY"
  TEMP_KEY_CREATED="true"
fi

FAIL_STAGE="claude_cli"
set +e
(
  cd "$CWD" && \
  env ANTHROPIC_BASE_URL="$BASE_URL" ANTHROPIC_API_KEY="$API_KEY" \
    NO_PROXY="${HOST},127.0.0.1,localhost" no_proxy="${HOST},127.0.0.1,localhost" \
    timeout "${CLAUDE_TIMEOUT_SECONDS}s" \
    "$CLAUDE_BIN" -p "$PROMPT" \
      --model "$MODEL" \
      --output-format json \
      --setting-sources local \
      --permission-mode bypassPermissions \
      --no-session-persistence \
      --verbose
) >"$RESULT_JSON"
CLAUDE_RC=$?
set -e

if [[ "$CLAUDE_RC" -ne 0 ]]; then
  FAIL_REASON="claude_cli_failed"
  echo "claude_cli_failed exit_code=$CLAUDE_RC"
  cat "$RESULT_JSON"
  echo "server_log=$SERVER_LOG"
  exit "$CLAUDE_RC"
fi

FAIL_STAGE="validate_result"
set +e
PARSE_OUTPUT="$("$PYTHON_BIN" - "$RESULT_JSON" "$EXPECTED_SUBSTRINGS" "$SUMMARY_JSON" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
expected_items = [item.strip() for item in sys.argv[2].split(",") if item.strip()]
summary_path = pathlib.Path(sys.argv[3])
payload = json.loads(path.read_text(encoding="utf-8"))

if not isinstance(payload, list):
    raise SystemExit("invalid_claude_output_shape")

result_event = next((item for item in reversed(payload) if item.get("type") == "result"), None)
assistant_event = next((item for item in reversed(payload) if item.get("type") == "assistant"), None)

if not result_event or result_event.get("subtype") != "success":
    raise SystemExit("missing_success_result")

result_text = str(result_event.get("result") or "").strip()
if not result_text:
    raise SystemExit("empty_result_text")

stop_reason = str(result_event.get("stop_reason") or "")
if stop_reason != "end_turn":
    raise SystemExit(f"unexpected_stop_reason:{stop_reason or '<missing>'}")

assistant_message = (assistant_event or {}).get("message") or {}
assistant_id = str(assistant_message.get("id") or "")
usage = result_event.get("usage") or {}
input_tokens = usage.get("input_tokens")
output_tokens = usage.get("output_tokens")

normalized_result = result_text.casefold()
missing_expected = [
    item for item in expected_items
    if item.casefold() not in normalized_result
]
if missing_expected:
    raise SystemExit(
        "missing_expected_substrings:" + ",".join(missing_expected)
    )

preview = result_text.replace("\n", " ").strip()
if len(preview) > 240:
    preview = preview[:240] + "..."

summary = {
    "assistant_message_id": assistant_id or "",
    "stop_reason": stop_reason,
    "input_tokens": input_tokens,
    "output_tokens": output_tokens,
    "result_preview": preview,
    "expected_substrings": expected_items,
}
summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

print(f"assistant_message_id={assistant_id or '<missing>'}")
print(f"cli_stop_reason={stop_reason or '<missing>'}")
print(f"input_tokens={input_tokens}")
print(f"output_tokens={output_tokens}")
print(f"expected_substrings={','.join(expected_items)}")
print(f"result_preview={preview}")
PY
)"
PARSE_RC=$?
set -e

if [[ "$PARSE_RC" -ne 0 ]]; then
  FAIL_REASON="result_validation_failed"
  printf '%s\n' "$PARSE_OUTPUT"
  exit "$PARSE_RC"
fi

GATE_STATUS="PASS"
FAIL_STAGE=""
FAIL_REASON=""

echo "=== Real Claude CLI Smoke ==="
echo "gate_status=PASS"
printf '%s\n' "$PARSE_OUTPUT"
echo "report_file=$REPORT_JSON"
echo "result_json=$RESULT_JSON"
echo "server_log=$SERVER_LOG"
