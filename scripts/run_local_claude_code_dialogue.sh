#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

SERVER_LOG="${SERVER_LOG:-/tmp/st_api_stress_server.log}"
PYTHON_BIN="${PYTHON_BIN:-/home/ww/Project/.venv/bin/python}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-18080}"
BASE_URL="${BASE_URL:-http://${HOST}:${PORT}}"
ARGS=("$@")

extract_arg_value() {
  local arg_name="$1"
  shift
  local args=("$@")
  local i current
  for ((i = 0; i < ${#args[@]}; i++)); do
    current="${args[$i]}"
    if [[ "$current" == "$arg_name" ]]; then
      if (( i + 1 < ${#args[@]} )); then
        echo "${args[$((i + 1))]}"
        return 0
      fi
      break
    fi
    if [[ "$current" == "$arg_name="* ]]; then
      echo "${current#*=}"
      return 0
    fi
  done
  return 1
}

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "python_not_found=$PYTHON_BIN" >&2
  exit 2
fi

rm -f "$SERVER_LOG"

CLIENT_CONCURRENCY="$(extract_arg_value --concurrency "${ARGS[@]}" || true)"
if [[ -z "$CLIENT_CONCURRENCY" ]]; then
  CLIENT_CONCURRENCY="50"
fi

REQUEST_TIMEOUT_ARG="$(extract_arg_value --request-timeout "${ARGS[@]}" || true)"
if [[ -z "$REQUEST_TIMEOUT_ARG" ]]; then
  REQUEST_TIMEOUT_ARG="180"
fi

if [[ -z "${MAX_CONCURRENT_REQUESTS:-}" ]]; then
  export MAX_CONCURRENT_REQUESTS="$CLIENT_CONCURRENCY"
fi

if [[ -z "${REQUEST_QUEUE_TIMEOUT_SECONDS:-}" ]]; then
  export REQUEST_QUEUE_TIMEOUT_SECONDS="$(
    awk -v rt="$REQUEST_TIMEOUT_ARG" 'BEGIN { v = rt + 0; if (v < 30) v = 30; printf "%.0f", v }'
  )"
fi

echo "server_limits max_concurrent=${MAX_CONCURRENT_REQUESTS} queue_timeout=${REQUEST_QUEUE_TIMEOUT_SECONDS} client_concurrency=${CLIENT_CONCURRENCY}"

PORT="$PORT" DEBUG=false UVICORN_ACCESS_LOG=false "$PYTHON_BIN" run.py >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

cleanup() {
  kill "$SERVER_PID" >/dev/null 2>&1 || true
  wait "$SERVER_PID" >/dev/null 2>&1 || true
}
trap cleanup EXIT

HEALTH_OK=0
for i in $(seq 1 80); do
  if curl --max-time 1 -sS "${BASE_URL}/health" >/dev/null 2>&1; then
    HEALTH_OK=1
    echo "health=ok attempt=$i"
    break
  fi
  sleep 0.25
done

if [[ "$HEALTH_OK" -ne 1 ]]; then
  echo "health=failed"
  echo "server_log=$SERVER_LOG"
  exit 3
fi

ST_API_KEY_VALUE="${ST_API_KEY:-}"
if [[ -z "$ST_API_KEY_VALUE" && -f /tmp/st_api_cli_test_key.txt ]]; then
  ST_API_KEY_VALUE="$(cat /tmp/st_api_cli_test_key.txt)"
fi
export ST_API_KEY="$ST_API_KEY_VALUE"

"$PYTHON_BIN" scripts/stress_claude_code_dialogue.py --base-url "$BASE_URL" "${ARGS[@]}"

echo "server_log=$SERVER_LOG"
