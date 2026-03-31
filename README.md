---
title: API Gateway
emoji: 🚀
colorFrom: blue
colorTo: purple
sdk: docker
pinned: false
---

# API Gateway

A unified API gateway service with OpenAI, Anthropic, and Gemini compatible endpoints.

## Features

- Multi-format API support (OpenAI, Anthropic, Gemini)
- Streaming responses (SSE)
- API key management
- Usage tracking and quotas
- Admin dashboard

## Endpoints

- `POST /v1/chat/completions` - OpenAI compatible
- `POST /v1/messages` - Anthropic compatible
- `POST /v1beta/models/{model}:generateContent` - Gemini compatible
- `GET /v1/key/info` - Query models, status, usage, and available tokens for the current API key
- `GET /health` - Health check

## Session Isolation (Important)

When multiple end users share one API key, clients should pass a stable session identity.  
Otherwise the gateway falls back to request-level isolation (safe but no cross-request memory).

Supported ways:

- Header: `X-ST-Session-ID: <tenant_or_user_session_id>` (recommended)
- Header: `X-Session-ID: <tenant_or_user_session_id>`
- OpenAI body: `user`
- Any protocol body: `metadata.user_id` (or `metadata.user`)

Examples:

```bash
curl -X POST "https://api.example.com/v1/chat/completions" \
  -H "Authorization: Bearer sk-xxx" \
  -H "Content-Type: application/json" \
  -H "X-ST-Session-ID: tenantA:user42" \
  -d '{
    "model":"claude-opus-4-6",
    "messages":[{"role":"user","content":"hello"}]
  }'
```

```bash
curl -X POST "https://api.example.com/v1/messages" \
  -H "x-api-key: sk-xxx" \
  -H "Content-Type: application/json" \
  -d '{
    "model":"claude-opus-4-6",
    "messages":[{"role":"user","content":"hello"}],
    "metadata":{"user_id":"tenantA:user42"}
  }'
```

## API Key Model Info

Use `GET /v1/key/info` to query the current key's available models and each model's current status, unavailable reasons, used tokens, and available tokens.

Supported auth methods:

- `Authorization: Bearer sk-xxx`
- `x-api-key: sk-xxx`
- Browser query: `/v1/key/info?key=sk-xxx`

Example request:

```bash
curl "http://127.0.0.1:8000/v1/key/info?key=sk-xxx"
```

Example response:

```json
{
  "models": [
    {
      "model": "gpt-5.4",
      "status": "active",
      "unavailable_reasons": [],
      "current_usage": "1.2K tokens",
      "available_tokens": "1.5M tokens"
    },
    {
      "model": "claude-opus-4-1",
      "status": "exhausted",
      "unavailable_reasons": ["accounts_exhausted"],
      "current_usage": "210 tokens",
      "available_tokens": "0 tokens"
    }
  ]
}
```

## Environment Quick Start

Start from [`.env.example`](./.env.example). It is organized in the same order you usually configure the service:

1. Minimal required values
2. Common production settings
3. PostgreSQL / 47-connection starter profile
4. Gateway concurrency and queueing
5. Upstream HTTP pool, retry, and account failover
6. Async persistence and log shedding
7. Tool Use
8. Legacy compatibility vars

Change these first:

- `BACKEND_API_URL`
- `DATABASE_URL`
- `JWT_SECRET_KEY`
- `ENCRYPTION_KEY`
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`

Common optional production vars:

- `ADMIN_PATH`
- `PROXY_SHARED_SECRET`
- `CORS_ORIGINS`
- `UVICORN_WORKERS`
- `LOG_LEVEL`
- `LOG_FILE`

Notes:

- Values in `.env.example` are production-oriented starter values, not necessarily the code defaults.
- For new deployments, prefer the newer vars and avoid the legacy compatibility vars at the bottom of the file.
- For tool-heavy Claude Code workloads, focus tuning on request concurrency, HTTP pool sizing, DB pool sizing, and per-account inflight protection.

## PostgreSQL Profile (2c/4g + 47 Connections)

SQLite is still supported. For PostgreSQL, start with the profile already grouped in [`.env.example`](./.env.example).

Recommended start values for `2c/4g` app + managed PostgreSQL (`connection_limit=47`):

```env
DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@HOST:25060/defaultdb?ssl=require
UVICORN_WORKERS=2
MAX_CONCURRENT_REQUESTS=60
MAX_CONCURRENT_STREAMS=40
MAX_CONCURRENT_DB_OPS=10
REQUEST_QUEUE_TIMEOUT_SECONDS=8
HTTP_MAX_CONNECTIONS=160
HTTP_MAX_CONNECTIONS_PER_HOST=40
HTTP_MAX_KEEPALIVE=40
DB_POOL_SIZE=8
DB_MAX_OVERFLOW=2
DB_POOL_TIMEOUT=5
DB_POOL_RECYCLE=1800
DB_CONNECTION_LIMIT=47
DB_CONNECTION_RESERVE=6
APP_INSTANCE_COUNT=1
ACCOUNT_MAX_INFLIGHT_REQUESTS_PER_ACCOUNT=8
BACKGROUND_LOG_WORKERS=2
ASYNC_USAGE_WORKERS=2
```

Optional PostgreSQL timeout settings:

```env
POSTGRES_CONNECT_TIMEOUT_SECONDS=8
POSTGRES_COMMAND_TIMEOUT_SECONDS=30
POSTGRES_STATEMENT_TIMEOUT_MS=30000
POSTGRES_LOCK_TIMEOUT_MS=5000
POSTGRES_APPLICATION_NAME=st-api
```

Pool budget quick check:

```bash
python scripts/check_pg_pool_budget.py
```

## Release Management

This project now includes built-in changelog + version release workflow:

- `VERSION`: single source of release version.
- `CHANGELOG.md`: records each release change.
- `scripts/release.py`: release helper.

### Prepare a release entry

```bash
python scripts/release.py --version 1.0.1 \
  --note "变更说明1" \
  --note "变更说明2"
```

### One-command publish to Hugging Face

```bash
python scripts/release.py --version 1.0.1 \
  --note "变更说明1" \
  --note "变更说明2" \
  --commit --tag --push --remote origin --branch main
```

### Download specific version

```bash
git clone --branch v1.0.1 https://huggingface.co/spaces/<username>/st-api
```

List released versions:

```bash
git ls-remote --tags https://huggingface.co/spaces/<username>/st-api
```

Or with `huggingface_hub`:

```python
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id="<username>/st-api",
    repo_type="space",
    revision="v1.0.1",
)
```

## License

MIT
