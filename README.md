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
- `GET /health` - Health check

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
