# 🚀 CodeBuddy API Server

Wrap WorkBuddy's internal API into an **OpenAI-compatible REST API** — deploy in one line, call from anywhere.

> 中文版: [README.md](README.md)

---

## What is this?

A featherweight proxy that turns WorkBuddy's internal API into a standard **OpenAI-compatible REST API**. Every model you can use in WorkBuddy — DeepSeek, Kimi, GLM, Hunyuan, MiniMax and more — becomes available through the familiar OpenAI interface. Any tool that speaks OpenAI API (Claude Code, custom clients, scripts…) can now drive WorkBuddy models directly.

## Highlights

- ✨ **Fully OpenAI-compatible** — `/v1/chat/completions`, `/v1/models`, tools/tool_calls passthrough
- 🧠 **Reasoning content** — `reasoning_content` (thinking process) exposed
- 🚀 **Max thinking by default** — deep reasoning even with zero extra params
- ⚡ **Streaming by default** — SSE, low latency
- 🖼️ **Image generation** — text-to-image `/v1/images/generations`, image editing `/v1/images/edits`
- 🔄 **Auto token refresh** — no manual intervention
- 🛡️ **Anti-ban protection** — rate limiting, jitter, UA rotation, exponential backoff
- 🐳 **One-line deploy** — Docker / Zeabur / Railway ready

## Quick Start

### 1. Get the CodeBuddy Token

After logging into WorkBuddy, the token is stored locally:

```bash
cat ~/Library/Application\ Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
```

### 2. Run locally

```bash
pip install fastapi uvicorn
export CODEBUDDY_AUTH_TOKEN="your-token"
export API_KEY="your-own-key"
python server.py
```

### 3. Deploy to Zeabur / Railway / any Docker platform

Only two env vars are required:

| Variable | Required | Description |
|----------|----------|-------------|
| `CODEBUDDY_AUTH_TOKEN` | ✅ | WorkBuddy Bearer Token |
| `API_KEY` | ✅ | Key for calling this API |
| `DEFAULT_MODEL` | ❌ | Default model, default `deepseek-v3` |
| `DEFAULT_THINKING` | ❌ | Default thinking level, default `max` |
| `PORT` | ❌ | Listen port, default `8000` |

## Usage Examples

**Chat (streaming, max thinking by default):**

```bash
curl https://your-domain/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "kimi-k3-1",
    "messages": [{"role": "user", "content": "Write a quicksort in Python"}]
  }'
```

**Custom thinking level:**

```bash
-d '{"model": "deepseek-v3", "messages": [...], "reasoning_effort": "high"}'
```

`reasoning_effort`: `low` | `medium` | `high` | `max` | `off`

**Tool calling:**

```bash
-d '{
  "model": "glm-5.2",
  "messages": [{"role": "user", "content": "What is the weather today?"}],
  "tools": [{"type": "function", "function": {"name": "get_weather", "description": "Get weather", "parameters": {...}}}]
}'
```

**Text-to-image:**

```bash
curl https://your-domain/v1/images/generations \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "a cute orange cat, cartoon style", "model": "hunyuan-image-v3.0"}'
```

**Image editing:**

```bash
curl https://your-domain/v1/images/edits \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "make it blue", "image": "data:image/png;base64,..."}'
```

`image` accepts: data URL / http(s) URL / raw base64 / local file path.

## Available Models

```
# DeepSeek
deepseek-v3        deepseek-v3-0324        deepseek-v3-1
deepseek-v3-0324-lkeap  deepseek-v3-1-lkeap
deepseek-r1        deepseek-r1-0528        deepseek-r1-0528-lkeap
deepseek-v4-flash  deepseek-v4-pro         deepseek-v3-2-volc

# Kimi
kimi-k2.5          kimi-k2.6               kimi-k2.7
kimi-k3-1

# Hunyuan
hunyuan-chat       hunyuan-2.0-instruct    hunyuan-2.0-thinking

# MiniMax
minimax-m2.7

# GLM
glm-4.7            glm-5.0                 glm-5.1
glm-5.2            glm-5.0-turbo           glm-5v-turbo

# HY
hy3                hy3-preview             hy3-preview-agent

# Image
hunyuan-image-v3.0                        (text-to-image)
hunyuan-image-v2.0-general-edit           (image-to-image)
```

> You can also get the full model list anytime via `GET /v1/models`.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check |
| `GET` | `/v1/models` | List all supported models |
| `POST` | `/v1/chat/completions` | Chat completion (SSE streaming) |
| `POST` | `/v1/images/generations` | Text-to-image |
| `POST` | `/v1/images/edits` | Image editing |

## Project Structure

```
codebuddy-api-server/
├── server.py                 # OpenAI-compatible REST API server
├── codebuddy_direct_api.py   # Direct CodeBuddy client (token/SSE/image)
├── Dockerfile                # Python 3.11 container
└── README.md
```

---

*Happy coding!* 🎉
