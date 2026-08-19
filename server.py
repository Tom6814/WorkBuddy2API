#!/usr/bin/env python3
# 版本: 1.3.1 (统一模型注册表 + 生图 + README 拆分)
"""
CodeBuddy API Server — OpenAI 兼容的 REST API 后端
===================================================
- POST /v1/chat/completions  — OpenAI 兼容的聊天端点（支持 SSE 流式）
- POST /v1/images/generations — OpenAI 兼容的文生图
- POST /v1/images/edits       — OpenAI 兼容的图生图编辑
- GET  /v1/models             — 列出可用模型
- GET  /health                — 健康检查

部署到 Zeabur / Railway / 任意 Docker 平台。
只需设置两个环境变量即可运行。

环境变量:
  CODEBUDDY_AUTH_TOKEN  (必填)  CodeBuddy Bearer Token
  API_KEY               (必填)  调用本 API 所需的密钥
  DEFAULT_MODEL         (可选)  默认模型，默认 deepseek-v3
  DEFAULT_THINKING      (可选)  默认思考深度，默认 high
  PORT                  (可选)  监听端口，默认 8000
  MAX_TOKENS_DEFAULT    (可选)  默认 max_tokens，默认 8192

调用方式:
  curl http://localhost:8000/v1/chat/completions \
    -H "Authorization: Bearer $API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"model":"deepseek-v3","messages":[{"role":"user","content":"Hello"}]}'
"""

from __future__ import annotations

import os
import sys
import json
import time
import uuid
import asyncio
import traceback
from typing import Optional

# 确保当前目录在 path 中，可以导入 codebuddy_direct_api
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from codebuddy_direct_api import (
    find_and_load_token,
    ApiClient,
    CHAT_COMPLETIONS_PATH,
    THINKING_LEVELS,
    THINKING_CAPABLE_MODELS,
    DEFAULT_THINKING,
    KNOWN_CHAT_MODELS,
    KNOWN_IMAGE_MODELS,
    ALL_SUPPORTED_MODELS,
)

# ── FastAPI imports ────────────────────────────────────────────────────────
try:
    from fastapi import FastAPI, Request, HTTPException
    from fastapi.responses import StreamingResponse, JSONResponse
    from fastapi.middleware.cors import CORSMiddleware
except ImportError:
    print("请安装依赖: pip install fastapi uvicorn", file=sys.stderr)
    sys.exit(1)

# ── Config ─────────────────────────────────────────────────────────────────
API_KEY = os.environ.get("API_KEY", "")
CODEBUDDY_AUTH_TOKEN = os.environ.get("CODEBUDDY_AUTH_TOKEN", "")
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "deepseek-v3")
DEFAULT_THINKING_ENV = os.environ.get("DEFAULT_THINKING", DEFAULT_THINKING)
MAX_TOKENS_DEFAULT = int(os.environ.get("MAX_TOKENS_DEFAULT", "8192"))
PORT = int(os.environ.get("PORT", "8000"))

if not CODEBUDDY_AUTH_TOKEN:
    print("[!] 未设置 CODEBUDDY_AUTH_TOKEN 环境变量", file=sys.stderr)
    print("[!] 服务器将启动但无法处理请求", file=sys.stderr)

if not API_KEY:
    print("[!] 未设置 API_KEY 环境变量，API 将无鉴权保护", file=sys.stderr)
    print("[!] 请设置 API_KEY 后重启", file=sys.stderr)


def _get_client() -> ApiClient | None:
    """延迟初始化 ApiClient（每次请求复用单例）。"""
    if not CODEBUDDY_AUTH_TOKEN:
        return None
    return _client_singleton


# ── Singleton client ───────────────────────────────────────────────────────
_client_singleton: ApiClient | None = None


def init_client():
    global _client_singleton
    if not CODEBUDDY_AUTH_TOKEN:
        print("[!] CODEBUDDY_AUTH_TOKEN 未设置，跳过客户端初始化", file=sys.stderr)
        return
    try:
        token_info = find_and_load_token()
        _client_singleton = ApiClient(
            "https://copilot.tencent.com",
            token_info,
            safe_mode=False,
        )
        print(f"[✓] CodeBuddy client 初始化成功", file=sys.stderr)
    except Exception as e:
        print(f"[!] CodeBuddy client 初始化失败: {e}", file=sys.stderr)
        print(f"[!] 请检查 CODEBUDDY_AUTH_TOKEN 是否正确设置", file=sys.stderr)


from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时初始化客户端。即使失败也不影响服务启动。"""
    init_client()
    yield


# ── FastAPI App ────────────────────────────────────────────────────────────
app = FastAPI(
    title="CodeBuddy API Server",
    description="OpenAI-compatible API wrapper for CodeBuddy",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Auth ───────────────────────────────────────────────────────────────────
def verify_api_key(request: Request):
    """验证请求的 API Key。"""
    if not API_KEY:
        return  # 未设置 API_KEY 则不校验
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    provided_key = auth_header[7:]
    if provided_key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")


# ── Helpers ────────────────────────────────────────────────────────────────
def build_openai_chunk(
    chunk_id: str,
    model: str,
    content: str | None = None,
    reasoning_content: str | None = None,
    tool_calls: list | None = None,
    role: str | None = None,
    finish_reason: str | None = None,
    created: int | None = None,
) -> dict:
    """构建 OpenAI 兼容的 SSE chunk。"""
    delta = {}
    if role:
        delta["role"] = role
    if content:
        delta["content"] = content
    if reasoning_content:
        delta["reasoning_content"] = reasoning_content
    if tool_calls:
        delta["tool_calls"] = tool_calls
    return {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created or int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason,
        }],
    }


def build_openai_response(
    resp_id: str,
    model: str,
    content: str,
    reasoning_content: str = "",
    tool_calls: list | None = None,
    finish_reason: str = "stop",
    created: int | None = None,
) -> dict:
    """构建 OpenAI 兼容的非流式响应。"""
    message = {"role": "assistant", "content": content}
    if reasoning_content:
        message["reasoning_content"] = reasoning_content
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": resp_id,
        "object": "chat.completion",
        "created": created or int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def stream_chat_completion(client: ApiClient, body: dict, model: str) -> StreamingResponse:
    """流式处理聊天请求，返回 SSE 流。

    内部始终使用流式调用上游 API（因为上游不支持非流式），
    但将完整的响应流式分块返回给客户端。
    """
    messages = body.get("messages", [])
    temperature = body.get("temperature", 0.7)
    max_tokens = body.get("max_tokens", MAX_TOKENS_DEFAULT)
    thinking_level = body.get("reasoning_effort") or body.get("thinking_level") or "max"
    tools = body.get("tools")
    tool_choice = body.get("tool_choice")
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    async def generate():
        try:
            # 发送 role chunk（OpenAI 协议要求）
            yield f"data: {json.dumps(build_openai_chunk(chunk_id, model, role='assistant', created=created))}\n\n"

            # 内部始终流式调用上游
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: client.chat_completion(
                    messages=messages,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=True,  # 上游只支持流式
                    thinking_level=thinking_level,
                    tools=tools,
                    tool_choice=tool_choice,
                ),
            )

            if result is None:
                yield f"data: {json.dumps({'error': 'upstream API returned no content'})}\n\n"
                yield "data: [DONE]\n\n"
                return

            full_text = result.get("content", "") if isinstance(result, dict) else result
            reasoning_text = result.get("reasoning_content", "") if isinstance(result, dict) else ""
            tool_calls = result.get("tool_calls") if isinstance(result, dict) else None
            finish_reason = result.get("finish_reason", "stop") if isinstance(result, dict) else "stop"

            # 先发送思考内容（reasoning_content）的 delta chunk
            if reasoning_text:
                chunk = build_openai_chunk(chunk_id, model, reasoning_content=reasoning_text, created=created)
                yield f"data: {json.dumps(chunk)}\n\n"
                await asyncio.sleep(0.02)

            # 发送 tool_calls 的 delta chunk
            if tool_calls:
                chunk = build_openai_chunk(chunk_id, model, tool_calls=tool_calls, created=created)
                yield f"data: {json.dumps(chunk)}\n\n"
                await asyncio.sleep(0.02)

            # 将完整文本分块发送，模拟流式效果
            import re
            tokens = re.split(r'(\s+)', full_text)
            for token in tokens:
                if token:
                    chunk = build_openai_chunk(chunk_id, model, content=token, created=created)
                    yield f"data: {json.dumps(chunk)}\n\n"
                    await asyncio.sleep(0.02)

            finish_chunk = build_openai_chunk(
                chunk_id, model,
                finish_reason=finish_reason, created=created,
            )
            yield f"data: {json.dumps(finish_chunk)}\n\n"
            yield "data: [DONE]\n\n"

        except Exception as e:
            traceback.print_exc()
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Routes ─────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    """健康检查端点。"""
    client = _get_client()
    status = "ok" if client else "degraded"
    return {
        "status": status,
        "codebuddy_configured": bool(CODEBUDDY_AUTH_TOKEN),
        "api_key_configured": bool(API_KEY),
    }


@app.get("/v1/models")
async def list_models(request: Request):
    """列出当前项目支持的所有模型（OpenAI 兼容）。

    模型清单来自 codebuddy_direct_api 中的单一数据源
    （KNOWN_CHAT_MODELS / KNOWN_IMAGE_MODELS），无需额外请求上游。
    """
    verify_api_key(request)

    data = []
    for m in sorted(ALL_SUPPORTED_MODELS):
        entry = {
            "id": m,
            "object": "model",
            "created": 0,
            "owned_by": "codebuddy",
        }
        if m in KNOWN_IMAGE_MODELS:
            entry["capabilities"] = {"image": True, "chat": False}
        elif m in THINKING_CAPABLE_MODELS:
            entry["capabilities"] = {"image": False, "chat": True, "reasoning": True}
        else:
            entry["capabilities"] = {"image": False, "chat": True, "reasoning": False}
        data.append(entry)

    return {
        "object": "list",
        "data": data,
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI 兼容的聊天补全端点。

    支持流式 (stream: true) 和非流式两种模式。

    额外参数（CodeBuddy 专属）:
      - reasoning_effort: "low"|"medium"|"high"|"max"  思考深度
    """
    verify_api_key(request)

    client = _get_client()
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="CodeBuddy token not configured. Set CODEBUDDY_AUTH_TOKEN env var.",
        )

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # 验证必填字段
    messages = body.get("messages")
    if not messages or not isinstance(messages, list):
        raise HTTPException(status_code=400, detail="messages is required and must be an array")

    model = body.get("model", DEFAULT_MODEL)
    stream = body.get("stream", True)
    temperature = float(body.get("temperature", 0.7))
    max_tokens = int(body.get("max_tokens", MAX_TOKENS_DEFAULT))
    thinking_level = body.get("reasoning_effort") or body.get("thinking_level")
    tools = body.get("tools")
    tool_choice = body.get("tool_choice")

    # 限制 max_tokens 防止滥用
    if max_tokens > 32768:
        max_tokens = 32768

    # 限制温度范围
    temperature = max(0.0, min(2.0, temperature))

    if stream:
        return await stream_chat_completion(client, body, model)

    # 非流式模式 — 内部始终流式调用（上游 API 不支持非流式）
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: client.chat_completion(
                messages=messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,  # 上游只支持流式
                thinking_level=thinking_level,
                tools=tools,
                tool_choice=tool_choice,
            ),
        )
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=502, detail=f"Upstream API error: {e}")

    if result is None:
        raise HTTPException(status_code=502, detail="Upstream API returned empty response")

    content = result.get("content", "") if isinstance(result, dict) else result
    reasoning = result.get("reasoning_content", "") if isinstance(result, dict) else ""
    tool_calls = result.get("tool_calls") if isinstance(result, dict) else None
    finish_reason = result.get("finish_reason", "stop") if isinstance(result, dict) else "stop"

    resp_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    return JSONResponse(build_openai_response(resp_id, model, content,
        reasoning_content=reasoning, tool_calls=tool_calls, finish_reason=finish_reason))


# ── Image Generation ───────────────────────────────────────────────────────
DEFAULT_IMAGE_MODEL = "hunyuan-image-v3.0"
DEFAULT_IMAGE_EDIT_MODEL = "hunyuan-image-v2.0-general-edit"


@app.post("/v1/images/generations")
async def images_generations(request: Request):
    """OpenAI 兼容的文生图端点。

    Body: {"prompt", "model", "n", "size", "quality", "style",
           "background", "footnote", "revise", "response_format"}
    响应: {"created", "data": [{"url"} | {"b64_json"}]}
    """
    verify_api_key(request)

    client = _get_client()
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="CodeBuddy token not configured. Set CODEBUDDY_AUTH_TOKEN env var.",
        )

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    prompt = body.get("prompt")
    if not prompt or not isinstance(prompt, str):
        raise HTTPException(status_code=400, detail="prompt is required and must be a string")

    model = body.get("model", DEFAULT_IMAGE_MODEL)
    n = int(body.get("n", 1))
    size = body.get("size", "1024x1024")
    response_format = body.get("response_format", "url")

    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: client.image_generation(
                prompt=prompt,
                model=model,
                size=size,
                n=n,
                quality=body.get("quality"),
                style=body.get("style"),
                background=body.get("background"),
                footnote=body.get("footnote"),
                revise=body.get("revise"),
            ),
        )
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=502, detail=f"Upstream API error: {e}")

    if not result:
        raise HTTPException(status_code=502, detail="Upstream API returned empty response")

    data = []
    for item in result:
        entry = {}
        if item.get("b64_json"):
            entry["b64_json"] = item["b64_json"]
        if item.get("url"):
            entry["url"] = item["url"]
        if item.get("revised_prompt"):
            entry["revised_prompt"] = item["revised_prompt"]
        data.append(entry)

    return JSONResponse({
        "created": int(time.time()),
        "data": data,
    })


@app.post("/v1/images/edits")
async def images_edits(request: Request):
    """OpenAI 兼容的图生图编辑端点。

    Body: {"prompt", "image": str|list, "model", "n", "size", "input_fidelity"}
      image 支持: data URL / http(s) URL / raw base64 / 本地文件路径
    响应: {"created", "data": [{"url"} | {"b64_json"}]}
    """
    verify_api_key(request)

    client = _get_client()
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="CodeBuddy token not configured. Set CODEBUDDY_AUTH_TOKEN env var.",
        )

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    prompt = body.get("prompt")
    if not prompt or not isinstance(prompt, str):
        raise HTTPException(status_code=400, detail="prompt is required and must be a string")

    image = body.get("image")
    if not image:
        raise HTTPException(status_code=400, detail="image is required")

    model = body.get("model", DEFAULT_IMAGE_EDIT_MODEL)
    n = int(body.get("n", 1))
    size = body.get("size", "1024x1024")

    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: client.image_edit(
                prompt=prompt,
                image=image,
                model=model,
                size=size,
                n=n,
                input_fidelity=body.get("input_fidelity"),
                footnote=body.get("footnote"),
                revise=body.get("revise"),
            ),
        )
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=502, detail=f"Upstream API error: {e}")

    if not result:
        raise HTTPException(status_code=502, detail="Upstream API returned empty response")

    data = []
    for item in result:
        entry = {}
        if item.get("b64_json"):
            entry["b64_json"] = item["b64_json"]
        if item.get("url"):
            entry["url"] = item["url"]
        data.append(entry)

    return JSONResponse({
        "created": int(time.time()),
        "data": data,
    })


# ── Main ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    print(f"[*] Starting CodeBuddy API Server on port {PORT}", file=sys.stderr)
    print(f"[*] Default model: {DEFAULT_MODEL}", file=sys.stderr)
    print(f"[*] API Key configured: {bool(API_KEY)}", file=sys.stderr)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
