#!/usr/bin/env python3
"""
WorkBuddy Token 刷新工具
========================
使用 refresh_token 调用刷新端点，获取新的 access_token。

当 access_token 过期时，无需重新登录 WorkBuddy 客户端，
只需用 refresh_token 调用 /v2/plugin/auth/token/refresh 即可获取新 token。

使用方式:
  # 从本地 auth 文件读取 refresh_token 并刷新
  python3 refresh_token.py

  # 直接指定 refresh_token
  python3 refresh_token.py --refresh-token "eyJhbGci..."

  # 刷新后直接输出新的 access_token
  python3 refresh_token.py --format raw

  # 刷新并更新本地 auth 文件（原地写回）
  python3 refresh_token.py --update-auth-file

  # 指定 API 域名（默认从 auth 文件读取）
  python3 refresh_token.py --domain www.codebuddy.cn
"""

from __future__ import annotations

import os
import sys
import json
import time
import http.client
import ssl
import argparse
from datetime import datetime
from urllib.parse import urlparse

TOKEN_REFRESH_PATH = "/v2/plugin/auth/token/refresh"
DEFAULT_DOMAIN = "www.codebuddy.cn"
DEFAULT_AUTH_FILE = os.path.expanduser(
    "~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info"
)


def load_refresh_token_from_auth_file(filepath: str) -> dict | None:
    """从 auth 文件中读取 refresh_token 及相关上下文。"""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

    auth = data.get("auth", {})
    account = data.get("account", {})

    refresh_token = auth.get("refreshToken")
    if not refresh_token:
        return None

    return {
        "refresh_token": refresh_token,
        "access_token": auth.get("accessToken", ""),
        "domain": auth.get("domain", DEFAULT_DOMAIN),
        "user_id": account.get("uid", ""),
        "filepath": filepath,
        "raw_data": data,
    }


def refresh_access_token(
    refresh_token: str,
    current_access_token: str = "",
    domain: str = DEFAULT_DOMAIN,
    user_id: str = "",
) -> dict | None:
    """调用刷新端点，返回新的 token 信息。"""
    hostname = domain
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    conn = http.client.HTTPSConnection(hostname, 443, context=ctx, timeout=30)

    headers = {
        "Authorization": f"Bearer {current_access_token}" if current_access_token else "",
        "X-Refresh-Token": refresh_token,
        "X-User-Id": user_id,
        "X-Auth-Refresh-Source": "plugin",
        "Content-Type": "application/json",
    }
    if domain:
        headers["X-Domain"] = domain
    headers = {k: v for k, v in headers.items() if v}

    try:
        conn.request("POST", TOKEN_REFRESH_PATH, body="{}", headers=headers)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()

        if resp.status != 200:
            print(f"[!] 刷新失败: HTTP {resp.status}", file=sys.stderr)
            print(f"    响应: {body[:500]}", file=sys.stderr)
            return None

        data = json.loads(body)
        auth_data = data.get("data", data)

        new_access = auth_data.get("accessToken") or auth_data.get("access_token")
        new_refresh = auth_data.get("refreshToken") or auth_data.get("refresh_token")

        if not new_access:
            print(f"[!] 响应中未包含 accessToken: {json.dumps(auth_data)[:300]}", file=sys.stderr)
            return None

        return {
            "access_token": new_access,
            "refresh_token": new_refresh or refresh_token,
            "expires_in": auth_data.get("expiresIn", 0),
            "refresh_expires_in": auth_data.get("refreshExpiresIn", 0),
            "domain": domain,
        }

    except Exception as e:
        print(f"[!] 刷新请求异常: {e}", file=sys.stderr)
        return None


def update_auth_file(filepath: str, raw_data: dict, new_tokens: dict) -> bool:
    """将新 token 写回 auth 文件。"""
    now_ms = int(time.time() * 1000)
    auth = raw_data.get("auth", {})

    auth["accessToken"] = new_tokens["access_token"]
    if new_tokens.get("refresh_token"):
        auth["refreshToken"] = new_tokens["refresh_token"]
    auth["lastRefreshTime"] = now_ms
    if new_tokens.get("expires_in"):
        auth["expiresIn"] = new_tokens["expires_in"]
        auth["expiresAt"] = now_ms + new_tokens["expires_in"]
    if new_tokens.get("refresh_expires_in"):
        auth["refreshExpiresIn"] = new_tokens["refresh_expires_in"]
        auth["refreshExpiresAt"] = now_ms + new_tokens["refresh_expires_in"]

    raw_data["auth"] = auth

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(raw_data, f, indent=2, ensure_ascii=False)
        return True
    except OSError as e:
        print(f"[!] 写回 auth 文件失败: {e}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(
        description="使用 refresh_token 刷新 WorkBuddy access_token",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--refresh-token", type=str, default=None,
        help="直接指定 refresh_token（不指定则从 auth 文件读取）",
    )
    parser.add_argument(
        "--access-token", type=str, default="",
        help="当前的 access_token（刷新端点需要，不指定则从 auth 文件读取）",
    )
    parser.add_argument(
        "--auth-file", type=str, default=None,
        help="auth 文件路径（默认自动查找）",
    )
    parser.add_argument(
        "--domain", type=str, default=None,
        help="API 域名（默认从 auth 文件读取或 www.codebuddy.cn）",
    )
    parser.add_argument(
        "--format", choices=["raw", "json", "info"], default="info",
        help="输出格式: raw=纯token | json=JSON | info=详情",
    )
    parser.add_argument(
        "--update-auth-file", action="store_true",
        help="刷新成功后将新 token 写回 auth 文件",
    )
    args = parser.parse_args()

    file_data = None

    if args.refresh_token:
        refresh_token = args.refresh_token
        current_access = args.access_token
        domain = args.domain or DEFAULT_DOMAIN
        user_id = ""
    else:
        filepath = args.auth_file or DEFAULT_AUTH_FILE
        if not os.path.isfile(filepath):
            print(f"[✗] auth 文件不存在: {filepath}", file=sys.stderr)
            sys.exit(1)

        file_data = load_refresh_token_from_auth_file(filepath)
        if not file_data:
            print(f"[✗] 无法从 auth 文件提取 refresh_token: {filepath}", file=sys.stderr)
            sys.exit(1)

        refresh_token = file_data["refresh_token"]
        current_access = args.access_token or file_data["access_token"]
        domain = args.domain or file_data["domain"]
        user_id = file_data["user_id"]
        print(f"[*] 从 auth 文件加载: {filepath}", file=sys.stderr)

    print(f"[*] 正在刷新 token (domain={domain})...", file=sys.stderr)
    result = refresh_access_token(refresh_token, current_access, domain, user_id)

    if not result:
        print("[✗] Token 刷新失败", file=sys.stderr)
        sys.exit(1)

    print(f"[✓] 刷新成功！", file=sys.stderr)

    if args.update_auth_file and file_data:
        if update_auth_file(file_data["filepath"], file_data["raw_data"], result):
            print(f"[✓] Auth 文件已更新: {file_data['filepath']}", file=sys.stderr)
        else:
            print("[!] Auth 文件更新失败，但新 token 已输出", file=sys.stderr)

    if args.format == "raw":
        print(result["access_token"])
    elif args.format == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"\n  新 Access Token:")
        print(f"    {result['access_token'][:50]}...")
        print(f"    有效期: {result.get('expires_in', 0) / 1000 / 86400:.0f} 天")
        if result.get("refresh_token") != refresh_token:
            print(f"\n  新 Refresh Token (已轮换):")
            print(f"    {result['refresh_token'][:50]}...")
        print()


if __name__ == "__main__":
    main()
