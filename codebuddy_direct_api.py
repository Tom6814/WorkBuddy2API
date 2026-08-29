#!/usr/bin/env python3
"""
CodeBuddy Direct API Client — 完全脱离客户端，从服务器直接调用
================================================================
- 使用本地 auth 文件提取的 token
- 自动刷新 token（/v2/plugin/auth/token/refresh）
- 支持 OpenAI 兼容的 /console/as/chat/completions 端点
- 支持 SSE 流式响应
- 支持思考深度调节（reasoning_effort）
- 内置反封号保护措施（限速、延迟、UA轮换、指数退避）
- 零依赖 WorkBuddy 客户端

使用方式:
  # 交互式聊天
  python3 codebuddy_direct_api.py

  # 单次问答（高思考深度）
  python3 codebuddy_direct_api.py --prompt "用 Python 写一个快速排序" --thinking high

  # 指定模型
  python3 codebuddy_direct_api.py --model "glm-5.1" -p "你好"

  # 检查 token
  python3 codebuddy_direct_api.py --check-token

  # 列出可用模型
  python3 codebuddy_direct_api.py --list-models

  # 输出 raw token
  python3 codebuddy_direct_api.py --show-token

  # 安全模式（更长的请求间隔）
  python3 codebuddy_direct_api.py --safe-mode

环境变量:
  CODEBUDDY_AUTH_TOKEN  - 直接设置 Bearer token（优先级最高）
  CODEBUDDY_API_BASE    - API 基础 URL（默认 https://copilot.tencent.com）
"""

from __future__ import annotations

import os
import sys
import json
import time
import base64
import random
import uuid
import argparse
import http.client
import ssl
import re
import threading
from typing import Optional
from urllib.parse import urlparse

# ── Constants ──────────────────────────────────────────────────────────────
DEFAULT_API_BASE = "https://copilot.tencent.com"
DEFAULT_AUTH_FILE = os.path.expanduser(
    "~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info"
)
CHAT_COMPLETIONS_PATH = "/v2/chat/completions"
IMAGE_GENERATIONS_PATH = "/v2/images/generations"
IMAGE_EDITS_PATH = "/v2/images/edits"
TOKEN_REFRESH_PATH = "/v2/plugin/auth/token/refresh"

AUTH_FILE_PATHS = [
    DEFAULT_AUTH_FILE,
    os.path.expanduser("~/.workbuddy/auth/workbuddy-desktop.info"),
    os.path.expanduser("~/.config/workbuddy/auth/workbuddy-desktop.info"),
]

# ── Thinking Level Model Map ───────────────────────────────────────────────
# 哪些模型支持 reasoning_effort 参数（经 /v2/chat/completions 实测验证）
THINKING_CAPABLE_MODELS = {
    # DeepSeek 系列
    "deepseek-v3", "deepseek-v3-0324", "deepseek-v3-1",
    "deepseek-r1", "deepseek-r1-0528", "deepseek-v4-flash",
    "deepseek-v4-pro", "deepseek-v3-2-volc",
    # GLM 系列
    "glm-5.1", "glm-5.0", "glm-5.2",
    # Kimi 系列（实测支持 reasoning_effort，默认开启思考）
    "kimi-k2.5", "kimi-k2.6", "kimi-k2.7", "kimi-k3-1",
    # HY 系列
    "hy3-preview", "hy3-preview-agent",
    "hy4-preview",
    # Hunyuan
    "hunyuan-2.0-thinking",
}

# reasoning_effort 可用值映射
# high → 深度思考（默认）, medium → 平衡, low → 快速, off → 关闭
THINKING_LEVELS = {
    "off": "none",       # 关闭思考（仅对支持关闭的模型有效）
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "max",        # 最大思考深度（如 deepseek-r1）
}
DEFAULT_THINKING = "max"

# ── Safety / Anti-Ban Config ───────────────────────────────────────────────
# 最小请求间隔（秒），模拟真实用户操作节奏
MIN_REQUEST_INTERVAL = 1.5
# 安全模式下更长的间隔
SAFE_MODE_INTERVAL = 6.0
# 最大重试次数
MAX_RETRIES = 3
# 指数退避基数（秒）
BACKOFF_BASE = 2.0
# 最大退避时间（秒）
MAX_BACKOFF = 30.0
# 默认不发送 User-Agent（匹配真实客户端 Node.js 行为）
# 仅在必要时（如 /v3/config）使用固定 UA
CONFIG_UA = "WorkBuddy/0.0.0"


# ── Rate Limiter ───────────────────────────────────────────────────────────
class RateLimiter:
    """请求速率限制器，防止触发反爬/封号机制。"""

    def __init__(self, min_interval: float = MIN_REQUEST_INTERVAL):
        self.min_interval = min_interval
        self._last_request_time = 0.0
        self._lock = threading.Lock()
        self._request_count = 0
        self._window_start = time.time()

    def wait(self) -> float:
        """等待直到可以发送下一个请求，返回等待的秒数。"""
        with self._lock:
            now = time.time()
            elapsed = now - self._last_request_time
            if elapsed < self.min_interval:
                sleep_time = self.min_interval - elapsed
            else:
                sleep_time = 0

            # 加入微小随机抖动（±0.3s），模拟人类操作节奏
            jitter = random.uniform(-0.3, 0.3)
            total_sleep = max(0, sleep_time + jitter)
            if total_sleep > 0:
                time.sleep(total_sleep)

            self._last_request_time = time.time()
            self._request_count += 1
            return total_sleep

    def requests_in_window(self) -> int:
        """返回当前时间窗口内的请求数。"""
        return self._request_count


# ── JWT Helpers ────────────────────────────────────────────────────────────
def decode_jwt_payload(token: str) -> dict | None:
    """解码 JWT payload（不验证签名），返回 claims 字典。"""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload_json = base64.urlsafe_b64decode(payload_b64)
        return json.loads(payload_json)
    except Exception:
        return None


def jwt_is_expired(token: str, margin_seconds: int = 300) -> bool:
    """检查 JWT 是否已过期（或接近过期）。"""
    claims = decode_jwt_payload(token)
    if not claims or "exp" not in claims:
        return True
    return time.time() + margin_seconds >= claims["exp"]


def jwt_get_user_id(token: str) -> str | None:
    claims = decode_jwt_payload(token)
    return claims.get("sub") if claims else None


def jwt_get_domain(token: str) -> str | None:
    claims = decode_jwt_payload(token)
    iss = claims.get("iss", "") if claims else ""
    try:
        return urlparse(iss).hostname
    except Exception:
        return None


# ── Token Management ───────────────────────────────────────────────────────
def extract_token_from_auth_file(filepath: str) -> dict | None:
    """从 auth 文件中提取 token 信息。"""
    try:
        with open(filepath, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

    auth = data.get("auth", {})
    account = data.get("account", {})

    if not auth.get("accessToken"):
        return None

    return {
        "access_token": auth["accessToken"],
        "refresh_token": auth.get("refreshToken", ""),
        "token_type": auth.get("tokenType", "Bearer"),
        "expires_at": auth.get("expiresAt", 0) / 1000,
        "refresh_expires_at": auth.get("refreshExpiresAt", 0) / 1000,
        "domain": auth.get("domain", ""),
        "user_id": account.get("uid", ""),
        "nickname": account.get("nickname", ""),
        "enterprise_id": account.get("enterpriseId", ""),
        "account_type": account.get("type", "personal"),
    }


def find_and_load_token(auth_file_override: str | None = None) -> dict:
    """按优先级查找并加载 token：环境变量 > 指定文件 > 默认路径。"""
    env_token = os.environ.get("CODEBUDDY_AUTH_TOKEN")
    if env_token:
        user_id = jwt_get_user_id(env_token) or ""
        domain = jwt_get_domain(env_token) or "www.codebuddy.cn"
        return {
            "access_token": env_token,
            "refresh_token": "",
            "token_type": "Bearer",
            "expires_at": 0,
            "refresh_expires_at": 0,
            "domain": domain,
            "user_id": user_id,
            "nickname": "",
            "enterprise_id": "",
            "account_type": "personal",
        }

    if auth_file_override:
        result = extract_token_from_auth_file(auth_file_override)
        if result:
            return result
        print(f"[!] 指定的 auth 文件无效: {auth_file_override}", file=sys.stderr)

    for path in AUTH_FILE_PATHS:
        result = extract_token_from_auth_file(path)
        if result:
            return result

    raise RuntimeError(
        "无法获取 token。请:\n"
        "  1. 设置环境变量 CODEBUDDY_AUTH_TOKEN\n"
        "  2. 或确保 WorkBuddy 已登录（auth 文件路径: {})\n".format(
            " | ".join(AUTH_FILE_PATHS)
        )
    )


# ── HTTP Client ────────────────────────────────────────────────────────────
class ApiClient:
    """轻量 HTTP 客户端，内置反封号保护措施。"""

    def __init__(self, base_url: str, token_info: dict, safe_mode: bool = False):
        self.base_url = base_url.rstrip("/")
        self.token_info = token_info
        self._conn: http.client.HTTPSConnection | None = None
        interval = SAFE_MODE_INTERVAL if safe_mode else MIN_REQUEST_INTERVAL
        self.rate_limiter = RateLimiter(min_interval=interval)
        # 会话级 32 位 hex ID（无破折号），匹配真实客户端 UUID 格式
        self._session_id = uuid.uuid4().hex
        # 会话级 trace ID（OpenTelemetry 格式，32 位 hex）
        self._trace_id = uuid.uuid4().hex[:32]
        # 当前 conversationId（从 SSE 响应中提取，回传到后续请求）
        self._conversation_id: str | None = None

    @property
    def hostname(self) -> str:
        return urlparse(self.base_url).hostname or ""

    def _get_connection(self) -> http.client.HTTPSConnection:
        if self._conn is None:
            ctx = ssl.create_default_context()
            # 使用 TLS 1.2+，禁用旧版本以减少指纹
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            self._conn = http.client.HTTPSConnection(
                self.hostname, 443, context=ctx, timeout=90
            )
        return self._conn

    def _reset_connection(self):
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _build_headers(self, extra: dict | None = None, stream: bool = False, model: str = "") -> dict:
        """构建与真实客户端一致的请求头。

        对齐 WorkBuddy Electron 客户端通过 undici/axios 发出的实际 HTTP 请求：
        - X-Product: SaaS（部署类型）
        - X-Request-ID: 会话级 UUID（无破折号），来自 X-Trace-ID
        - X-Trace-ID: 会话级 OpenTelemetry trace ID
        - X-Model-ID: 当前使用的模型 ID
        - 不发送 User-Agent（匹配 Node.js HTTP 客户端默认行为）
        """
        token = self.token_info["access_token"]
        headers = {
            "Authorization": f"Bearer {token}",
            "X-User-Id": self.token_info.get("user_id", ""),
            "X-Product": "SaaS",
            "X-Request-ID": self._session_id,
            "X-Trace-ID": self._trace_id,
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }
        if model:
            headers["X-Model-ID"] = model
        if self.token_info.get("enterprise_id"):
            headers["X-Enterprise-Id"] = self.token_info["enterprise_id"]
            headers["X-Tenant-Id"] = self.token_info["enterprise_id"]
        if self.token_info.get("domain"):
            headers["X-Domain"] = self.token_info["domain"]
        if extra:
            headers.update(extra)
        return headers

    def refresh_token(self) -> bool:
        """刷新 access token（不受频率限制）。"""
        refresh_token = self.token_info.get("refresh_token")
        if not refresh_token:
            print("[!] 没有 refresh_token，无法刷新", file=sys.stderr)
            return False

        print("[*] 正在刷新 token...", file=sys.stderr)

        try:
            conn = self._get_connection()
            headers = {
                "Authorization": f"Bearer {self.token_info['access_token']}",
                "X-Refresh-Token": refresh_token,
                "X-User-Id": self.token_info.get("user_id", ""),
                "X-Auth-Refresh-Source": "plugin",
                "X-Request-ID": self._session_id,
                "X-Product": "SaaS",
            }
            if self.token_info.get("domain"):
                headers["X-Domain"] = self.token_info["domain"]

            conn.request("POST", TOKEN_REFRESH_PATH, body="{}", headers=headers)
            resp = conn.getresponse()
            body = resp.read().decode("utf-8")
            self._reset_connection()

            if resp.status != 200:
                print(f"[!] Token 刷新失败: {resp.status} {body[:500]}", file=sys.stderr)
                return False

            data = json.loads(body)
            auth_data = data.get("data", data)

            new_access = auth_data.get("accessToken") or auth_data.get("access_token")
            new_refresh = auth_data.get("refreshToken") or auth_data.get("refresh_token")

            if new_access:
                self.token_info["access_token"] = new_access
                expires_in = auth_data.get("expiresIn", 0) or 0
                self.token_info["expires_at"] = expires_in / 1000 + time.time() if isinstance(expires_in, (int, float)) else 0
            if new_refresh:
                self.token_info["refresh_token"] = new_refresh
                refresh_expires_in = auth_data.get("refreshExpiresIn", 0) or 0
                self.token_info["refresh_expires_at"] = refresh_expires_in / 1000 + time.time() if isinstance(refresh_expires_in, (int, float)) else 0

            print(f"[✓] Token 刷新成功", file=sys.stderr)
            return True

        except Exception as e:
            self._reset_connection()
            print(f"[!] Token 刷新异常: {e}", file=sys.stderr)
            return False

    def ensure_valid_token(self):
        """确保 token 有效，过期则自动刷新。"""
        if not jwt_is_expired(self.token_info["access_token"]):
            return True

        print("[!] Access token 已过期，尝试刷新...", file=sys.stderr)
        if self.refresh_token():
            return True

        raise RuntimeError("Token 已过期且刷新失败。请重新登录 WorkBuddy。")

    def chat_completion(
        self,
        messages: list[dict],
        model: str = "deepseek-v3",
        temperature: float = 0.7,
        max_tokens: int = 8192,
        stream: bool = True,
        thinking_level: str | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> dict | None:
        """发送聊天请求，返回 {"content": str, "reasoning_content": str, "tool_calls": list} 或 None。

        Args:
            thinking_level: 思考深度 - "off"|"low"|"medium"|"high"|"max"。
                            None 表示不设置（使用模型默认值）。
            tools: OpenAI 标准 tools 列表 [{"type": "function", "function": {...}}]。
            tool_choice: "auto"|"none"|"required"|{"type": "function", "function": {"name": "..."}}。
        """
        self.ensure_valid_token()

        # 构建请求体（对齐真实客户端字段顺序）
        body: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }

        # 回传 conversationId（真实客户端行为）
        if self._conversation_id:
            body["conversationId"] = self._conversation_id

        # 思考深度参数
        # 默认对所有支持思考的模型开启最高深度思考
        if model in THINKING_CAPABLE_MODELS:
            if thinking_level is None:
                thinking_level = DEFAULT_THINKING
            effort = THINKING_LEVELS.get(thinking_level, THINKING_LEVELS[DEFAULT_THINKING])
            body["reasoning_effort"] = effort if effort != "none" else "none"

        # 工具调用参数（OpenAI 标准格式，上游支持）
        if tools:
            body["tools"] = tools
        if tool_choice is not None:
            body["tool_choice"] = tool_choice

        headers = self._build_headers(stream=stream, model=model)

        # 频率限制：等待安全间隔
        self.rate_limiter.wait()

        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                conn = self._get_connection()
                conn.request(
                    "POST",
                    CHAT_COMPLETIONS_PATH,
                    body=json.dumps(body),
                    headers=headers,
                )
                resp = conn.getresponse()

                # 401 → 自动刷新 token 后重试
                if resp.status == 401 and attempt < MAX_RETRIES - 1:
                    error_body = resp.read(2048).decode("utf-8", errors="replace")
                    self._reset_connection()
                    print(f"[!] 收到 401，尝试刷新 token 后重试...", file=sys.stderr)
                    if self.refresh_token():
                        headers = self._build_headers(stream=stream)
                        continue
                    print(f"[!] 401 错误体: {error_body[:300]}", file=sys.stderr)
                    break

                # 429 → 指数退避
                if resp.status == 429:
                    self._reset_connection()
                    backoff = min(BACKOFF_BASE ** (attempt + 1) + random.uniform(0, 2), MAX_BACKOFF)
                    print(f"[!] 收到 429 (频率限制)，退避 {backoff:.1f}s...", file=sys.stderr)
                    time.sleep(backoff)
                    continue

                if resp.status != 200:
                    error_body = resp.read(2048).decode("utf-8", errors="replace")
                    self._reset_connection()

                    # 500+ → 指数退避重试
                    if resp.status >= 500 and attempt < MAX_RETRIES - 1:
                        backoff = min(BACKOFF_BASE ** (attempt + 1) + random.uniform(0, 1), MAX_BACKOFF)
                        print(f"[!] 服务端错误 {resp.status}，{backoff:.1f}s 后重试 ({attempt + 1}/{MAX_RETRIES})...", file=sys.stderr)
                        time.sleep(backoff)
                        continue

                    print(f"[!] API 错误 {resp.status}: {error_body[:500]}", file=sys.stderr)
                    last_error = f"{resp.status}: {error_body[:200]}"
                    return None

                if stream:
                    return self._parse_sse_stream(resp)
                else:
                    data = json.loads(resp.read().decode("utf-8"))
                    self._reset_connection()
                    choices = data.get("choices", [])
                    if choices:
                        message = choices[0].get("message", {})
                        return {
                            "content": message.get("content", ""),
                            "reasoning_content": message.get("reasoning_content", ""),
                            "tool_calls": message.get("tool_calls"),
                            "finish_reason": choices[0].get("finish_reason", "stop"),
                        }
                    return None

            except (http.client.HTTPException, ConnectionError, OSError, TimeoutError) as e:
                self._reset_connection()
                last_error = str(e)
                if attempt < MAX_RETRIES - 1:
                    backoff = min(BACKOFF_BASE ** (attempt + 1) + random.uniform(0, 1), MAX_BACKOFF)
                    print(f"[!] 连接错误，{backoff:.1f}s 后重试 ({attempt + 1}/{MAX_RETRIES}): {e}", file=sys.stderr)
                    time.sleep(backoff)
                    continue
                print(f"[!] 请求失败: {e}", file=sys.stderr)
                return None

        if last_error:
            print(f"[!] 所有重试均失败。最后错误: {last_error}", file=sys.stderr)
        return None

    def image_generation(
        self,
        prompt: str,
        model: str = "hunyuan-image-v3.0",
        size: str = "1024x1024",
        n: int = 1,
        quality: str | None = None,
        style: str | None = None,
        background: str | None = None,
        footnote: str | None = None,
        revise: bool | None = None,
    ) -> list[dict] | None:
        """文生图（POST /v2/images/generations）。

        返回 [{url} | {b64_json}] 列表，或 None（失败）。
        对齐 WorkBuddy ImageService.buildBaseRequestBody：
          - hunyuan-* 模型: n/footnote/revise（返回 url）
          - 其他模型 (openai 分支): response_format=b64_json, n/quality/style/background
        """
        self.ensure_valid_token()

        body: dict = {
            "model": model,
            "prompt": prompt,
            "size": size,
        }

        if model.startswith("hunyuan-"):
            body["n"] = n or 1
            if footnote:
                body["footnote"] = footnote
            if revise is not None:
                body["revise"] = {"value": revise}
        else:
            body["response_format"] = "b64_json"
            body["n"] = n
            if quality:
                body["quality"] = quality
            if style:
                body["style"] = style
            if background:
                body["background"] = background

        headers = self._build_headers(stream=False, model=model)
        headers["Accept"] = "application/json"

        self.rate_limiter.wait()

        try:
            conn = self._get_connection()
            conn.request(
                "POST",
                IMAGE_GENERATIONS_PATH,
                body=json.dumps(body),
                headers=headers,
            )
            resp = conn.getresponse()
            data = json.loads(resp.read().decode("utf-8"))
            self._reset_connection()

            if resp.status == 401:
                print(f"[!] 生图 401，尝试刷新 token", file=sys.stderr)
                if self.refresh_token():
                    return self.image_generation(
                        prompt, model, size, n, quality, style, background, footnote, revise
                    )
                return None

            if resp.status != 200:
                print(f"[!] 生图失败 {resp.status}: {json.dumps(data)[:300]}", file=sys.stderr)
                return None

            if data.get("code") != 0:
                print(f"[!] 生图 API 错误: {data.get('msg')} (code: {data.get('code')})", file=sys.stderr)
                return None

            items = data.get("data", {}).get("data", [])
            return [item for item in items if item.get("url") or item.get("b64_json")]
        except (http.client.HTTPException, ConnectionError, OSError, TimeoutError) as e:
            self._reset_connection()
            print(f"[!] 生图请求失败: {e}", file=sys.stderr)
            return None

    def image_edit(
        self,
        prompt: str,
        image: str | list[str],
        model: str = "hunyuan-image-v2.0-general-edit",
        size: str = "1024x1024",
        n: int = 1,
        input_fidelity: str | None = None,
        footnote: str | None = None,
        revise: bool | None = None,
    ) -> list[dict] | None:
        """图生图编辑（POST /v2/images/edits）。

        image: 图片路径/data URL/base64/URL（参考 WorkBuddy processImageInput）。
        返回 [{url} | {b64_json}] 列表，或 None（失败）。
        """
        self.ensure_valid_token()

        # 归一化输入图片为 data URL（对齐 WorkBuddy processImageInput）
        image_input = self._normalize_image_input(image)

        body: dict = {
            "model": model,
            "prompt": prompt,
            "size": size,
            "image": image_input,
        }
        if input_fidelity:
            body["input_fidelity"] = input_fidelity

        if model.startswith("hunyuan-"):
            body["n"] = n or 1
            if footnote:
                body["footnote"] = footnote
            if revise is not None:
                body["revise"] = {"value": revise}
        else:
            body["response_format"] = "b64_json"
            body["n"] = n

        headers = self._build_headers(stream=False, model=model)
        headers["Accept"] = "application/json"

        self.rate_limiter.wait()

        try:
            conn = self._get_connection()
            conn.request(
                "POST",
                IMAGE_EDITS_PATH,
                body=json.dumps(body),
                headers=headers,
            )
            resp = conn.getresponse()
            data = json.loads(resp.read().decode("utf-8"))
            self._reset_connection()

            if resp.status == 401:
                print(f"[!] 图生图 401，尝试刷新 token", file=sys.stderr)
                if self.refresh_token():
                    return self.image_edit(prompt, image, model, size, n, input_fidelity, footnote, revise)
                return None

            if resp.status != 200:
                print(f"[!] 图生图失败 {resp.status}: {json.dumps(data)[:300]}", file=sys.stderr)
                return None

            if data.get("code") != 0:
                print(f"[!] 图生图 API 错误: {data.get('msg')} (code: {data.get('code')})", file=sys.stderr)
                return None

            items = data.get("data", {}).get("data", [])
            return [item for item in items if item.get("url") or item.get("b64_json")]
        except (http.client.HTTPException, ConnectionError, OSError, TimeoutError) as e:
            self._reset_connection()
            print(f"[!] 图生图请求失败: {e}", file=sys.stderr)
            return None

    def _normalize_image_input(self, image: str | list[str]) -> list[str]:
        """将输入图片归一化为 data URL 列表（对齐 WorkBuddy processImageInput）。"""
        if isinstance(image, str):
            images = [image]
        else:
            images = list(image)

        results = []
        for img in images:
            img = img.strip()
            if img.startswith("data:"):
                results.append(img)
            elif img.startswith("http://") or img.startswith("https://"):
                results.append(f"data:image/png;base64,{self._fetch_remote_image(img)}")
            elif self._is_likely_base64(img):
                results.append(f"data:{self._detect_mime(img)};base64,{img}")
            else:
                # 本地文件路径
                try:
                    with open(img, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode()
                    results.append(f"data:{self._detect_mime(img)};base64,{b64}")
                except OSError:
                    # 无法读取则原样传递
                    results.append(img)
        return results

    def _fetch_remote_image(self, url: str) -> str:
        """下载远程图片并返回 base64。"""
        try:
            parsed = urlparse(url)
            ctx = ssl.create_default_context()
            conn = http.client.HTTPSConnection(parsed.hostname, 443, timeout=15, context=ctx)
            conn.request("GET", parsed.path + (f"?{parsed.query}" if parsed.query else ""))
            resp = conn.getresponse()
            data = resp.read()
            conn.close()
            return base64.b64encode(data).decode()
        except Exception as e:
            print(f"[!] 下载图片失败: {e}", file=sys.stderr)
            return base64.b64encode(b"").decode()

    def _is_likely_base64(self, s: str) -> bool:
        """判断字符串是否为原始 base64 图片数据（对齐 WorkBuddy）。"""
        if "/" in s or "\\" in s:
            return False
        if len(s) < 100:
            return False
        return bool(re.match(r"^[A-Za-z0-9+/]+=*$", s))

    def _detect_mime(self, s: str) -> str:
        """根据内容检测 MIME 类型（对齐 WorkBuddy detectMimeTypeFromBase64）。"""
        try:
            raw = base64.b64decode(s[:32])
            if len(raw) >= 4 and raw[0] == 0x89 and raw[1] == 0x50 and raw[2] == 0x4E and raw[3] == 0x47:
                return "image/png"
            if len(raw) >= 3 and raw[0] == 0xFF and raw[1] == 0xD8 and raw[2] == 0xFF:
                return "image/jpeg"
            if len(raw) >= 4 and raw[0] == 0x47 and raw[1] == 0x49 and raw[2] == 0x46 and raw[3] == 0x38:
                return "image/gif"
            if len(raw) >= 12 and raw[0] == 0x52 and raw[1] == 0x49 and raw[2] == 0x46 and raw[3] == 0x46 and raw[8] == 0x57 and raw[9] == 0x45 and raw[10] == 0x42 and raw[11] == 0x50:
                return "image/webp"
            if len(raw) >= 2 and raw[0] == 0x42 and raw[1] == 0x4D:
                return "image/bmp"
        except Exception:
            pass
        return "image/png"

    def _parse_sse_stream(self, resp: http.client.HTTPResponse) -> dict | None:
        """解析 SSE 流式响应，返回 {"content": str, "reasoning_content": str, "tool_calls": list, "finish_reason": str}。

        收集 reasoning_content、content 和 tool_calls（流式增量拼接），分别返回。

        提取 conversationId 并存储，供后续请求回传。
        """
        full_text = ""
        thinking_text = ""
        finish_reason = "stop"
        chunk_count = 0
        current_event = ""  # 当前 SSE event 类型

        # 流式 tool_calls 增量累积（按 index 聚合）
        tool_calls_acc: dict[int, dict] = {}

        try:
            buffer = b""
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buffer += chunk

                while b"\n" in buffer:
                    line_end = buffer.index(b"\n")
                    line = buffer[:line_end].decode("utf-8", errors="replace").strip()
                    buffer = buffer[line_end + 1:]

                    # SSE comment — skip
                    if not line or line.startswith(":"):
                        continue

                    # SSE event type line
                    if line.startswith("event:"):
                        current_event = line[6:].strip()
                        continue

                    if line.startswith("data:"):
                        data_str = line[6:].strip()

                        # conversationId 事件
                        if current_event == "conversationId":
                            if data_str.startswith("conv-"):
                                self._conversation_id = data_str
                            current_event = ""
                            continue

                        # 流结束
                        if data_str == "[DONE]":
                            if thinking_text and not full_text:
                                full_text = thinking_text
                                print(thinking_text)
                            print()
                            break

                        try:
                            data = json.loads(data_str)
                            choices = data.get("choices", [])
                            if choices:
                                delta = choices[0].get("delta", {})
                                content = delta.get("content", "")
                                reasoning = delta.get("reasoning_content", "")
                                tool_calls = delta.get("tool_calls")

                                # 累积 tool_calls（流式增量拼接）
                                if tool_calls:
                                    for tc in tool_calls:
                                        idx = tc.get("index", 0)
                                        if idx not in tool_calls_acc:
                                            tool_calls_acc[idx] = {
                                                "index": idx,
                                                "id": None,
                                                "type": "function",
                                                "function": {"name": None, "arguments": ""},
                                            }
                                        acc = tool_calls_acc[idx]
                                        # id 和 type 通常只在第一个 chunk 出现
                                        if tc.get("id"):
                                            acc["id"] = tc["id"]
                                        if tc.get("type"):
                                            acc["type"] = tc["type"]
                                        # function name 在第一个 chunk
                                        if tc.get("function", {}).get("name"):
                                            acc["function"]["name"] = tc["function"]["name"]
                                        # arguments 逐步拼接
                                        if tc.get("function", {}).get("arguments"):
                                            acc["function"]["arguments"] += tc["function"]["arguments"]

                                if reasoning:
                                    thinking_text += reasoning

                                if content:
                                    chunk_count += 1
                                    if chunk_count == 1:
                                        if thinking_text and len(thinking_text) > 20:
                                            print(f"\n  [思考: {thinking_text[:80]}...]\n", file=sys.stderr)
                                        print()
                                    sys.stdout.write(content)
                                    sys.stdout.flush()
                                    full_text += content

                                finish_reason = choices[0].get("finish_reason", finish_reason)
                        except json.JSONDecodeError:
                            pass

                        current_event = ""

        finally:
            self._reset_connection()

        if not full_text and thinking_text:
            print(thinking_text)
            full_text = thinking_text

        if not full_text and not thinking_text and not tool_calls_acc:
            return None

        result = {
            "content": full_text,
            "reasoning_content": thinking_text,
            "finish_reason": finish_reason,
        }
        if tool_calls_acc:
            result["tool_calls"] = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
        return result


# ── Interactive Chat ───────────────────────────────────────────────────────
def interactive_chat(client: ApiClient, model: str, thinking_level: str | None):
    """交互式聊天模式。"""
    print(f"\n{'='*60}")
    print(f"  CodeBuddy Direct API — 交互模式")
    print(f"  Model:   {model}")
    print(f"  Thinking: {thinking_level or 'default'}")
    print(f"  User:    {client.token_info.get('nickname') or client.token_info['user_id'][:16] + '...'}")
    print(f"  API:     {client.base_url}{CHAT_COMPLETIONS_PATH}")
    print(f"  输入 /quit 退出, /clear 清除上下文, /think <level> 切换思考深度")
    print(f"{'='*60}\n")

    conversation = []
    current_thinking = thinking_level

    while True:
        try:
            prompt = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见!")
            break

        if not prompt:
            continue
        if prompt.lower() in ("/quit", "/exit", "/q"):
            print("再见!")
            break
        if prompt.lower() == "/clear":
            conversation = []
            client._conversation_id = None
            print("[✓] 上下文已清除\n")
            continue
        if prompt.lower().startswith("/think "):
            level = prompt.split(" ", 1)[1].strip()
            if level in THINKING_LEVELS:
                current_thinking = level
                print(f"[✓] 思考深度已切换为: {level}\n")
            else:
                print(f"[!] 无效的思考深度。可用: {', '.join(THINKING_LEVELS.keys())}\n")
            continue

        conversation.append({"role": "user", "content": prompt})

        result = client.chat_completion(
            messages=conversation,
            model=model,
            stream=True,
            thinking_level=current_thinking,
        )

        if result:
            content = result.get("content", "") if isinstance(result, dict) else result
            conversation.append({"role": "assistant", "content": content})
        else:
            print("\n[!] 未收到回复")
            conversation.pop()

        print()


# ── Model Registry ─────────────────────────────────────────────────────────
# 当前项目支持的所有模型（单一数据源，/v1/models 与 CLI 共用）
# 已在 /v2/chat/completions 实测验证可用
KNOWN_CHAT_MODELS = {
    # DeepSeek 系列
    "deepseek-v3", "deepseek-v3-0324", "deepseek-v3-1",
    "deepseek-v3-0324-lkeap", "deepseek-v3-1-lkeap",
    "deepseek-r1", "deepseek-r1-0528", "deepseek-r1-0528-lkeap",
    "deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v3-2-volc",
    # Kimi 系列
    "kimi-k2.5", "kimi-k2.6", "kimi-k2.7", "kimi-k3-1",
    # Hunyuan 系列
    "hunyuan-chat", "hunyuan-2.0-instruct", "hunyuan-2.0-thinking",
    # MiniMax 系列
    "minimax-m2.7",
    # GLM 系列
    "glm-4.7", "glm-5.0", "glm-5.1", "glm-5.2", "glm-5.0-turbo", "glm-5v-turbo",
    # HY 系列
    # hy3-preview 免费版；hy3-preview-agent 收费版（x0.04 credits，功能相同）
    "hy3", "hy3-preview", "hy3-preview-agent",
    "hy4-preview",
}

# 生图模型（已在 /v2/images/* 实测验证可用）
KNOWN_IMAGE_MODELS = {
    "hunyuan-image-v3.0",                # 文生图
    "hunyuan-image-v3.0-art",            # 文生图（艺术风格）
    "hunyuan-image-v2.0-general-edit",   # 图生图
}

# 全部支持模型
ALL_SUPPORTED_MODELS = KNOWN_CHAT_MODELS | KNOWN_IMAGE_MODELS

# 免费模型（调用不消耗 credits/积分；依据内置 product 配置的 credits 字段判定）
FREE_MODELS = {
    "hy3-preview",   # Hy3 preview 免费版（credits 为空）
    "hy4-preview",   # Hy4 preview 免费体验版（体验额度内 0.00，超额后按 0.04 credits/次）
    # hy3-preview-agent 为收费版（x0.04 credits），不在本集合中
}


# ── Model Listing ──────────────────────────────────────────────────────────
def list_models(client: ApiClient):
    """获取并显示可用模型列表。"""

    known_working = KNOWN_CHAT_MODELS

    headers = client._build_headers()
    headers.update({
        "X-Product": "SaaS",
        "X-Client-Version": "0.0.0",
        "User-Agent": CONFIG_UA,
    })

    ctx = ssl.create_default_context()
    conn = http.client.HTTPSConnection(client.hostname, 443, context=ctx, timeout=15)
    conn.request("GET", "/v3/config", headers=headers)
    resp = conn.getresponse()
    body = resp.read().decode("utf-8")
    conn.close()

    if resp.status != 200:
        print(f"[!] 获取模型列表失败: {resp.status}", file=sys.stderr)
        return

    data = json.loads(body)
    models = data.get("data", data).get("models", [])

    print(f"\n{'='*80}")
    print(f"  CodeBuddy 可用模型列表 (共 {len(models)} 个)")
    print(f"{'='*80}")
    print(f"  {'模型 ID':<30s} {'名称':<25s} {'已验证':<8s} {'思考'}")
    print(f"  {'-'*30} {'-'*25} {'-'*8} {'-'*6}")

    for m in models:
        mid = m.get("id", "")
        name = m.get("name", "")[:23]
        tested = "✓" if mid in known_working else "?"
        thinking = "✓" if mid in THINKING_CAPABLE_MODELS else "-"
        print(f"  {mid:<30s} {name:<25s} {tested:<8s} {thinking}")

    print(f"\n  已验证可用: {len(known_working)} 个")
    print(f"  支持思考深度调节: {len(THINKING_CAPABLE_MODELS)} 个")
    print(f"  其他模型可通过 --model <id> 尝试调用\n")


# ── Token Display ──────────────────────────────────────────────────────────
def show_token(token_info: dict):
    """显示完整 auth token 信息。"""
    print(f"\n{'='*60}")
    print(f"  CodeBuddy Auth Token")
    print(f"{'='*60}")
    print(f"\n  Access Token:")
    print(f"  {token_info['access_token']}")
    print(f"\n  Refresh Token:")
    print(f"  {token_info.get('refresh_token', 'N/A')}")
    print(f"\n  User ID:   {token_info.get('user_id', 'N/A')}")
    print(f"  Nickname:  {token_info.get('nickname', 'N/A')}")
    print(f"  Domain:    {token_info.get('domain', 'N/A')}")
    print(f"  Type:      {token_info.get('account_type', 'N/A')}")
    print(f"\n  --- JWT Claims ---")
    claims = decode_jwt_payload(token_info["access_token"])
    if claims:
        for key in ("iss", "sub", "azp", "typ", "exp", "iat", "scope"):
            val = claims.get(key, "N/A")
            if key in ("exp", "iat") and isinstance(val, (int, float)):
                val = f"{val} ({time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(val))})"
            print(f"  {key}: {val}")
    print()


# ── Main ───────────────────────────────────────────────────────────────────
def parse_args():
    parser = argparse.ArgumentParser(
        description="CodeBuddy Direct API Client — 完全脱离客户端的服务器直连调用",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s                                          # 交互式聊天
  %(prog)s --prompt "Hello" --thinking high          # 单次问答（高思考深度）
  %(prog)s -m "glm-5.1" -p "你好"                    # 指定模型
  %(prog)s --model "minimax-m2.7" -p "write a poem"  # MiniMax 模型
  %(prog)s --check-token                             # 仅检查 token 状态
  %(prog)s --show-token                              # 显示完整 token
  %(prog)s --list-models                             # 列出可用模型
  %(prog)s --refresh-token                           # 强制刷新 token
  %(prog)s --safe-mode                               # 安全模式（更保守的请求频率）
        """,
    )
    parser.add_argument(
        "-p", "--prompt", type=str, help="单次提问内容（不指定则进入交互模式）"
    )
    parser.add_argument(
        "-m", "--model", type=str, default="deepseek-v3",
        help="模型名称 (默认: deepseek-v3)",
    )
    parser.add_argument(
        "-t", "--temperature", type=float, default=0.7, help="温度 (默认: 0.7)"
    )
    parser.add_argument(
        "--max-tokens", type=int, default=8192, help="最大输出 token (默认: 8192)"
    )
    parser.add_argument(
        "--no-stream", action="store_true", help="禁用流式输出"
    )
    parser.add_argument(
        "--thinking", type=str, default=DEFAULT_THINKING,
        choices=list(THINKING_LEVELS.keys()),
        help=f"思考深度 (默认: {DEFAULT_THINKING}). 选项: {', '.join(THINKING_LEVELS.keys())}",
    )
    parser.add_argument(
        "--auth-file", type=str, help="指定 auth 文件路径"
    )
    parser.add_argument(
        "--api-base", type=str, default=DEFAULT_API_BASE, help="API 基础 URL"
    )
    parser.add_argument(
        "--check-token", action="store_true", help="仅检查 token 状态并退出"
    )
    parser.add_argument(
        "--show-token", action="store_true", help="显示完整 token 信息并退出"
    )
    parser.add_argument(
        "--list-models", action="store_true", help="列出可用模型并退出"
    )
    parser.add_argument(
        "--refresh-token", action="store_true", help="强制刷新 token（并显示新 token）"
    )
    parser.add_argument(
        "--safe-mode", action="store_true",
        help="安全模式：更长的请求间隔、更保守的重试策略"
    )
    parser.add_argument(
        "-s", "--system", type=str, help="System prompt"
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # 加载 token
    print("[*] 正在加载认证信息...", file=sys.stderr)
    try:
        token_info = find_and_load_token(args.auth_file)
    except RuntimeError as e:
        print(f"\n[✗] {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[✓] Token 已加载 (uid={token_info['user_id'][:12]}...)", file=sys.stderr)

    # 解码 token 信息
    claims = decode_jwt_payload(token_info["access_token"])
    if claims:
        exp_ts = claims.get("exp", 0)
        if exp_ts:
            remaining = max(0, exp_ts - time.time())
            days = remaining / 86400
            print(f"[*] Token 有效期: {days:.0f} 天 ({time.strftime('%Y-%m-%d', time.localtime(exp_ts))})", file=sys.stderr)

    if token_info.get("refresh_token"):
        refresh_claims = decode_jwt_payload(token_info["refresh_token"])
        if refresh_claims:
            exp_ts = refresh_claims.get("exp", 0)
            if exp_ts:
                days = max(0, exp_ts - time.time()) / 86400
                print(f"[*] Refresh Token 有效期: {days:.0f} 天 ({time.strftime('%Y-%m-%d', time.localtime(exp_ts))})", file=sys.stderr)

    if token_info.get("nickname"):
        print(f"[*] 用户: {token_info['nickname']}", file=sys.stderr)

    # 处理信息展示类参数
    if args.check_token:
        expired = jwt_is_expired(token_info["access_token"])
        print(f"\n{'[✓] Token 有效' if not expired else '[!] Token 已过期或即将过期'}")
        print(f"  Domain:  {token_info.get('domain', 'N/A')}")
        print(f"  User ID: {token_info.get('user_id', 'N/A')}")
        print(f"  Type:    {token_info.get('account_type', 'N/A')}")
        return

    if args.show_token:
        show_token(token_info)
        return

    # 创建客户端
    client = ApiClient(args.api_base, token_info, safe_mode=args.safe_mode)

    if args.safe_mode:
        print("[*] 安全模式已启用 (请求间隔: {}s)".format(SAFE_MODE_INTERVAL), file=sys.stderr)

    # 列出模型
    if args.list_models:
        list_models(client)
        return

    # 强制刷新 token
    if args.refresh_token:
        if client.refresh_token():
            show_token(token_info)
        else:
            print("\n[✗] Token 刷新失败")
            sys.exit(1)
        return

    # 单次问答模式
    if args.prompt:
        messages = []
        if args.system:
            messages.append({"role": "system", "content": args.system})
        messages.append({"role": "user", "content": args.prompt})

        result = client.chat_completion(
            messages=messages,
            model=args.model,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            stream=not args.no_stream,
            thinking_level=args.thinking,
        )

        if result and args.no_stream:
            content = result.get("content", "") if isinstance(result, dict) else result
            print(content)

        if result is None:
            sys.exit(1)
        return

    # 交互模式
    interactive_chat(client, args.model, args.thinking)


if __name__ == "__main__":
    main()
