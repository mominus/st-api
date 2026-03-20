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

## Environment Variables

| Variable | Description |
|----------|-------------|
| `JWT_SECRET_KEY` | JWT signing key |
| `ENCRYPTION_KEY` | Fernet encryption key |
| `ADMIN_USERNAME` | Admin username |
| `ADMIN_PASSWORD` | Admin password |
| `ADMIN_PATH` | Hidden admin panel path |

## License

MIT
