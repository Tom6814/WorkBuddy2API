#!/usr/bin/env python3
"""
WorkBuddy 每日签到自动化（Linux 服务器 / Docker 部署）
=============================================================
基于 WorkBuddy 客户端逆向得到的签到接口，无需客户端即可每天自动签到。

接口来源（workbuddy-auth-product-coordinator.js）：
  POST {endpoint}/v2/billing/meter/checkin-activity-status  查询签到状态
  POST {endpoint}/v2/billing/meter/daily-checkin            执行每日签到
  POST {endpoint}/v2/plugin/auth/token/refresh              刷新 access_token

安全性：
  - 签到接口幂等（已签到返回 10001 "今天已签到"，不会重复扣积分）
  - 只操作用户自己的账号，等价于手动点击客户端里的"签到"按钮

运行模式:
  守护模式（常驻，每天自动签到）:  python3 workbuddy_checkin.py --daemon
  单次签到（cron/systemd 用）:      python3 workbuddy_checkin.py --sign
  查询状态:                          python3 workbuddy_checkin.py --check

Token 来源（按优先级）:
  1. ~/.workbuddy_checkin_token.json  （自动续期后的持久化 token）
  2. 环境变量 CODEBUDDY_AUTH_TOKEN    （Linux 服务器推荐）
  3. 本地 auth 文件                    （macOS/Windows 客户端登录自动维护）
  续期: 设置 CODEBUDDY_REFRESH_TOKEN 后，access_token 过期时自动刷新，
        并把新 token 持久化到本地文件，实现长期无人值守。

部署（Linux 服务器）:
  方式 A - Docker 常驻（推荐）:
    docker build -f Dockerfile.checkin -t wb-checkin .
    docker run -d --name wb-checkin --restart unless-stopped \
      -e CODEBUDDY_AUTH_TOKEN=你的access_token \
      -e CODEBUDDY_REFRESH_TOKEN=你的refresh_token \
      -v wb-checkin-data:/root \
      wb-checkin
  方式 B - systemd / cron 每日调用:
    # 每日 09:00 执行一次
    0 9 * * * cd /opt/wb-checkin && CODEBUDDY_AUTH_TOKEN=xxx CODEBUDDY_REFRESH_TOKEN=yyy python3 workbuddy_checkin.py --sign
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import ssl
import sys
import time
from datetime import datetime, timedelta

# 确保可导入同目录的 codebuddy_direct_api（复用 token 加载/刷新逻辑）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from codebuddy_direct_api import (
    find_and_load_token,
    ApiClient,
    jwt_is_expired,
)

ENDPOINT = "https://copilot.tencent.com"
CHECKIN_STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
DAILY_CHECKIN_PATH = "/v2/billing/meter/daily-checkin"

# token 持久化文件（自动续期后写入，默认 ~/.workbuddy_checkin_token.json）
TOKEN_FILE = os.environ.get(
    "WORKBUDDY_CHECKIN_TOKEN_FILE",
    os.path.expanduser("~/.workbuddy_checkin_token.json"),
)


# ── Token 加载 / 续期 ─────────────────────────────────────────────────────
def load_persisted_token() -> dict | None:
    """读取持久化的 token 文件。"""
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("access_token"):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return None


def save_persisted_token(token_info: dict):
    """把 token 持久化到本地文件（仅本用户可读）。"""
    try:
        d = os.path.dirname(TOKEN_FILE)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump(token_info, f, ensure_ascii=False)
        os.chmod(TOKEN_FILE, 0o600)
    except OSError as e:
        print(f"[!] 写入 token 文件失败: {e}", file=sys.stderr)


def resolve_token_info() -> dict:
    """按优先级加载 token：持久化文件 > 环境变量 > 本地 auth 文件。"""
    # 1. 持久化文件（已自动续期的 token 优先）
    persisted = load_persisted_token()
    if persisted:
        return persisted

    # 2. 环境变量（Linux 服务器推荐）
    env_token = os.environ.get("CODEBUDDY_AUTH_TOKEN", "")
    if env_token:
        refresh = os.environ.get("CODEBUDDY_REFRESH_TOKEN", "")
        # 复用 find_and_load_token 的 env 分支逻辑构造 token_info
        from codebuddy_direct_api import jwt_get_user_id, jwt_get_domain
        return {
            "access_token": env_token,
            "refresh_token": refresh,
            "token_type": "Bearer",
            "expires_at": 0,
            "refresh_expires_at": 0,
            "domain": jwt_get_domain(env_token) or "www.codebuddy.cn",
            "user_id": jwt_get_user_id(env_token) or "",
            "nickname": "",
            "enterprise_id": "",
            "account_type": "personal",
        }

    # 3. 本地 auth 文件（macOS/Windows 客户端登录自动维护）
    return find_and_load_token()


def ensure_token_valid(token_info: dict) -> bool:
    """确保 access_token 有效，过期则用 refresh_token 自动续期。"""
    if not jwt_is_expired(token_info.get("access_token", "")):
        return True
    if not token_info.get("refresh_token"):
        print("[✗] access_token 已过期且未配置 CODEBUDDY_REFRESH_TOKEN，无法续期", file=sys.stderr)
        return False
    print("[*] access_token 即将过期，正在刷新...", file=sys.stderr)
    client = ApiClient(ENDPOINT, token_info)
    if client.refresh_token():
        save_persisted_token(token_info)
        print("[✓] token 续期成功并已持久化", file=sys.stderr)
        return True
    print("[✗] token 续期失败，请检查 refresh_token 是否有效", file=sys.stderr)
    return False


# ── HTTP 调用 ─────────────────────────────────────────────────────────────
def build_headers(token_info: dict) -> dict:
    """构建与客户端一致的请求头（对齐 buildHeaders(session)）。"""
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token_info['access_token']}",
        "Content-Type": "application/json",
        "X-User-Id": token_info.get("user_id", ""),
    }
    if token_info.get("enterprise_id"):
        headers["X-Enterprise-Id"] = token_info["enterprise_id"]
        headers["X-Tenant-Id"] = token_info["enterprise_id"]
    if token_info.get("domain"):
        headers["X-Domain"] = token_info["domain"]
    return headers


def _request(method: str, path: str, headers: dict, body: str = "{}") -> tuple[int, dict]:
    ctx = ssl.create_default_context()
    conn = http.client.HTTPSConnection("copilot.tencent.com", 443, context=ctx, timeout=15)
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        return resp.status, json.loads(raw)
    finally:
        conn.close()


# ── 签到业务 ──────────────────────────────────────────────────────────────
def get_checkin_status(token_info: dict) -> tuple[dict | None, str | None]:
    """查询签到状态，返回 (data, error)。"""
    status, data = _request("POST", CHECKIN_STATUS_PATH, build_headers(token_info))
    if status != 200 or data.get("code") != 0:
        return None, f"查询失败: HTTP {status}, code={data.get('code')}, msg={data.get('msg')}"
    return data.get("data") or {}, None


def do_checkin(token_info: dict) -> dict:
    """执行签到。幂等：已签到返回 code=10001。"""
    status, data = _request("POST", DAILY_CHECKIN_PATH, build_headers(token_info))
    return {
        "http_status": status,
        "code": data.get("code"),
        "msg": data.get("msg", ""),
        "data": data.get("data") or {},
    }


def do_sign(token_info: dict) -> int:
    """完整签到流程：确保 token 有效 → 查状态 → 未签则签到。返回退出码。"""
    if not ensure_token_valid(token_info):
        return 1

    data, err = get_checkin_status(token_info)
    if err:
        print(f"[✗] {err}", file=sys.stderr)
        return 1

    print(f"[{datetime.now():%Y-%m-%d %H:%M}] 活动: {data.get('theme_name', '')} | "
          f"连续 {data.get('streak_days', 0)} 天 | 累计 {data.get('total_credits', 0)} 积分", flush=True)

    if data.get("today_checked_in"):
        print("[•] 今天已签到，跳过", flush=True)
        return 0

    print("[→] 执行签到...", flush=True)
    result = do_checkin(token_info)
    if result["code"] == 0:
        credit = result.get("data", {}).get("today_credit", result.get("data", {}).get("credit", ""))
        print(f"[✓] 签到成功！今日获得 {credit} 积分", flush=True)
        return 0
    if result["code"] == 10001:
        print("[•] 今天已签到，跳过", flush=True)
        return 0
    print(f"[✗] 签到失败: {result['msg']} (code={result['code']})", file=sys.stderr)
    return 1


def do_night_owl(token_info: dict) -> int:
    """夜猫子任务：夜间 23:00-8:00 用 GLM-5.2 新建对话并成功使用（每天 1 次，累计 3 天）。

    任务完成判定为服务端统计"用 GLM-5.2 成功发起一次对话"。这里用最小化
    请求（stream、max_tokens=8）完成一次真实对话。
    """
    if not ensure_token_valid(token_info):
        return 1
    print("[→] 执行夜猫子任务（GLM-5.2 对话）...", flush=True)
    try:
        # 新建 client 实例 → 不带 conversationId → 等价于"新建对话"
        client = ApiClient(ENDPOINT, token_info)
        result = client.chat_completion(
            messages=[{"role": "user", "content": "你好，请简单回应"}],
            model="glm-5.2",
            max_tokens=8,
            stream=True,
            thinking_level=None,
        )
        if result:
            print(f"[✓] GLM-5.2 对话成功（content_len={len(result.get('content', ''))}）", flush=True)
            return 0
        print("[✗] GLM-5.2 对话未返回内容", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"[✗] 夜猫子任务异常: {e}", file=sys.stderr)
        return 1


def run_daemon(token_info: dict, hour: int, minute: int, owl_hour: int = 23, owl_minute: int = 0):
    """守护模式：常驻运行，每日自动执行签到 + 夜猫子任务。"""
    print(f"[*] 守护模式启动：每天 {hour:02d}:{minute:02d} 签到，{owl_hour:02d}:{owl_minute:02d} 夜猫子任务，Ctrl+C 停止", flush=True)
    last_sign_day: str | None = None
    last_owl_day: str | None = None
    while True:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if (now.hour, now.minute) == (hour, minute) and last_sign_day != today:
            print(f"[{now:%Y-%m-%d %H:%M}] 到达签到时间", flush=True)
            do_sign(token_info)
            last_sign_day = today
            time.sleep(30)
            continue
        if (now.hour, now.minute) == (owl_hour, owl_minute) and last_owl_day != today:
            print(f"[{now:%Y-%m-%d %H:%M}] 到达夜猫子时间", flush=True)
            do_night_owl(token_info)
            last_owl_day = today
            time.sleep(30)
            continue
        time.sleep(20)


def main():
    parser = argparse.ArgumentParser(
        description="WorkBuddy 每日签到自动化（Linux 服务器 / Docker）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--daemon", action="store_true", help="守护模式：常驻运行，每天自动签到（默认）")
    parser.add_argument("--time", type=str, default="09:00", help="守护模式签到时间 HH:MM（默认 09:00）")
    parser.add_argument("--owl-time", type=str, default="23:00", help="守护模式夜猫子任务时间 HH:MM（默认 23:00）")
    parser.add_argument("--sign", action="store_true", help="单次签到（cron/systemd 用）")
    parser.add_argument("--night-owl", action="store_true", help="手动触发一次夜猫子任务（GLM-5.2 对话）")
    parser.add_argument("--check", action="store_true", help="仅查询签到状态")
    parser.add_argument("--auth-file", type=str, help="指定本地 auth 文件路径（仅本机模式）")
    args = parser.parse_args()

    # 加载 token
    try:
        token_info = resolve_token_info()
    except RuntimeError as e:
        print(f"[✗] 无法获取 token: {e}", file=sys.stderr)
        print("[*] Linux 服务器请设置环境变量 CODEBUDDY_AUTH_TOKEN（可选 CODEBUDDY_REFRESH_TOKEN 用于自动续期）", file=sys.stderr)
        sys.exit(1)

    if args.check:
        if not ensure_token_valid(token_info):
            sys.exit(1)
        data, err = get_checkin_status(token_info)
        if err:
            print(f"[✗] {err}", file=sys.stderr)
            sys.exit(1)
        print(f"活动      : {data.get('theme_name', '')}（{data.get('activity_name', '')}）")
        print(f"状态      : {'已签到' if data.get('today_checked_in') else '未签到'}")
        print(f"连续签到  : {data.get('streak_days', 0)} 天")
        print(f"每日积分  : {data.get('daily_credit', 0)}")
        print(f"累计积分  : {data.get('total_credits', 0)}")
        print(f"活动时间  : {data.get('start_time', '')} ~ {data.get('end_time', '')}")
        sys.exit(0)

    if args.sign:
        sys.exit(do_sign(token_info))

    if args.night_owl:
        sys.exit(do_night_owl(token_info))

    # 默认/守护模式
    try:
        hh, mm = (int(x) for x in args.time.split(":"))
        if not (0 <= hh < 24 and 0 <= mm < 60):
            raise ValueError
        ohh, omm = (int(x) for x in args.owl_time.split(":"))
        if not (0 <= ohh < 24 and 0 <= omm < 60):
            raise ValueError
    except ValueError:
        print(f"[✗] 无效的时间参数: {args.time} / {args.owl_time}，应为 HH:MM", file=sys.stderr)
        sys.exit(1)

    run_daemon(token_info, hh, mm, ohh, omm)


if __name__ == "__main__":
    main()
