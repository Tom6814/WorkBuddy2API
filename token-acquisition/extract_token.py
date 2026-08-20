#!/usr/bin/env python3
"""
WorkBuddy Auth Token 提取工具
==============================
从本地明文 auth 文件中提取 access_token / refresh_token。

WorkBuddy 桌面端登录后，认证信息会以明文 JSON 写入：
  ~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info

本脚本只读取该文件（不做任何逆向 / 解密 / 反编译），属于正常的配置文件读取。

使用方式:
  # 输出 access_token（纯文本，适合管道）
  python3 extract_token.py

  # 输出为 shell export 命令
  python3 extract_token.py --format export

  # 输出完整 JSON（含 refresh_token、用户信息、过期时间）
  python3 extract_token.py --format json

  # 直接写入 server.py 所需的 tokens.json
  python3 extract_token.py --write-tokens-json

  # 指定 auth 文件路径
  python3 extract_token.py --auth-file /path/to/workbuddy-desktop.info
"""

from __future__ import annotations

import os
import sys
import json
import time
import base64
import argparse
from datetime import datetime

DEFAULT_AUTH_FILE = os.path.expanduser(
    "~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info"
)

FALLBACK_PATHS = [
    DEFAULT_AUTH_FILE,
    os.path.expanduser("~/.workbuddy/auth/workbuddy-desktop.info"),
    os.path.expanduser("~/.config/workbuddy/auth/workbuddy-desktop.info"),
]


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


def parse_auth_file(filepath: str) -> dict | None:
    """解析 auth 文件，返回结构化 token 信息。"""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"[!] 读取 auth 文件失败: {e}", file=sys.stderr)
        return None

    auth = data.get("auth", {})
    account = data.get("account", {})

    access_token = auth.get("accessToken")
    if not access_token:
        print("[!] auth 文件中未找到 auth.accessToken", file=sys.stderr)
        return None

    return {
        "access_token": access_token,
        "refresh_token": auth.get("refreshToken", ""),
        "token_type": auth.get("tokenType", "Bearer"),
        "domain": auth.get("domain", ""),
        "expires_at_ms": auth.get("expiresAt", 0),
        "refresh_expires_at_ms": auth.get("refreshExpiresAt", 0),
        "user_id": account.get("uid", ""),
        "nickname": account.get("nickname", ""),
        "uin": account.get("uin", ""),
        "phone": account.get("phoneNumber", ""),
        "account_type": account.get("type", "personal"),
    }


def find_auth_file(override: str | None = None) -> str | None:
    """按优先级查找 auth 文件。"""
    candidates = [override] if override else FALLBACK_PATHS
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def format_timestamp(ms: int) -> str:
    """毫秒时间戳 → 可读日期。"""
    if not ms:
        return "N/A"
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def get_token_expiry_info(token: str) -> dict:
    """从 JWT 中提取过期时间信息。"""
    claims = decode_jwt_payload(token)
    if not claims:
        return {"valid": False}
    exp = claims.get("exp", 0)
    remaining = max(0, exp - time.time()) if exp else 0
    return {
        "valid": True,
        "exp": exp,
        "exp_str": datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M:%S") if exp else "N/A",
        "remaining_days": remaining / 86400,
        "remaining_hours": remaining / 3600,
        "issuer": claims.get("iss", ""),
        "subject": claims.get("sub", ""),
    }


def write_tokens_json(info: dict, output_path: str = "../tokens.json"):
    """写入 server.py 所需的 tokens.json 格式。"""
    tokens_data = {
        "tokens": [
            {
                "name": info.get("nickname") or info.get("user_id", "default"),
                "token_info": {
                    "access_token": info["access_token"],
                    "refresh_token": info.get("refresh_token", ""),
                    "domain": info.get("domain", ""),
                    "user_id": info.get("user_id", ""),
                },
            }
        ],
        "updated_at": datetime.now().isoformat(),
    }
    abs_path = os.path.abspath(output_path)
    with open(abs_path, "w", encoding="utf-8") as f:
        json.dump(tokens_data, f, indent=2, ensure_ascii=False)
    print(f"[✓] 已写入 {abs_path}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="从本地 auth 文件提取 WorkBuddy access_token",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--auth-file", type=str, default=None,
        help="指定 auth 文件路径（默认自动查找）",
    )
    parser.add_argument(
        "--format", choices=["raw", "json", "export", "info"], default="raw",
        help="输出格式: raw=纯token | json=完整JSON | export=shell命令 | info=详情",
    )
    parser.add_argument(
        "--write-tokens-json", metavar="PATH", nargs="?", const="../tokens.json",
        help="直接写入 server.py 所需的 tokens.json（默认 ../tokens.json）",
    )
    args = parser.parse_args()

    filepath = find_auth_file(args.auth_file)
    if not filepath:
        print("[✗] 未找到 auth 文件。请确保 WorkBuddy 已登录，或用 --auth-file 指定路径。", file=sys.stderr)
        print(f"    已尝试: {' | '.join(FALLBACK_PATHS)}", file=sys.stderr)
        sys.exit(1)

    print(f"[*] Auth 文件: {filepath}", file=sys.stderr)
    info = parse_auth_file(filepath)
    if not info:
        sys.exit(1)

    if args.write_tokens_json:
        write_tokens_json(info, args.write_tokens_json)
        return

    access_exp = get_token_expiry_info(info["access_token"])

    if args.format == "raw":
        print(info["access_token"])

    elif args.format == "export":
        print(f'export CODEBUDDY_AUTH_TOKEN="{info["access_token"]}"')

    elif args.format == "json":
        output = {
            "access_token": info["access_token"],
            "refresh_token": info["refresh_token"],
            "token_type": info["token_type"],
            "domain": info["domain"],
            "user_id": info["user_id"],
            "nickname": info["nickname"],
            "expires_at": format_timestamp(info["expires_at_ms"]),
            "refresh_expires_at": format_timestamp(info["refresh_expires_at_ms"]),
            "access_token_expiry": {
                "expires_at": access_exp.get("exp_str", "N/A"),
                "remaining_days": round(access_exp.get("remaining_days", 0), 1),
            } if access_exp["valid"] else "unable to decode",
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))

    elif args.format == "info":
        print(f"\n{'='*55}")
        print(f"  WorkBuddy Auth Token 详情")
        print(f"{'='*55}")
        print(f"\n  用户:     {info.get('nickname', 'N/A')}")
        print(f"  UID:      {info.get('user_id', 'N/A')}")
        print(f"  手机号:   {info.get('phone', 'N/A')}")
        print(f"  域名:     {info.get('domain', 'N/A')}")
        print(f"  账号类型: {info.get('account_type', 'N/A')}")

        if access_exp["valid"]:
            print(f"\n  Access Token:")
            print(f"    过期时间:   {access_exp['exp_str']}")
            print(f"    剩余天数:   {access_exp['remaining_days']:.1f} 天")
            print(f"    前缀:       {info['access_token'][:30]}...")
        else:
            print(f"\n  Access Token: (无法解码 JWT)")

        if info.get("refresh_token"):
            refresh_exp = get_token_expiry_info(info["refresh_token"])
            if refresh_exp["valid"]:
                print(f"\n  Refresh Token:")
                print(f"    过期时间:   {refresh_exp['exp_str']}")
                print(f"    剩余天数:   {refresh_exp['remaining_days']:.1f} 天")
                print(f"    前缀:       {info['refresh_token'][:30]}...")

        print(f"\n  文件路径: {filepath}")
        print(f"  文件大小: {os.path.getsize(filepath)} bytes")
        print()


if __name__ == "__main__":
    main()
