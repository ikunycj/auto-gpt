#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ChatGPT backend-api 指纹环境请求测试脚本。

用途：
    先创建项目统一 BrowserSession 指纹环境（curl_cffi TLS 指纹 + UA/语言/时区画像 + oai-did cookie），
    再用该环境测试 ChatGPT backend-api 的 Bearer token 请求。

用法：
    # 方式1：直接参数传 token
    python3 scripts/diagnostics/chatgpt_curl_cffi.py --token '<JWT>' --verbose

    # 方式2：从文件读取 token（默认取第一条非空行；允许有 Bearer 前缀）
    python3 scripts/diagnostics/chatgpt_curl_cffi.py --token-file 注册成功的token.txt

    # 方式3：测试 subscriptions，需要传 account_id
    python3 scripts/diagnostics/chatgpt_curl_cffi.py --token '<JWT>' --endpoint subscriptions --account-id '<account_id>'

    # 代理：不传则沿用项目 pick_proxy()；传空字符串禁用代理；传具体地址使用指定代理
    python3 scripts/diagnostics/chatgpt_curl_cffi.py --token '<JWT>' --proxy ''

说明：
    - 本脚本不会把 token 落盘。
    - 默认 endpoint=accounts-check。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote

# 让独立诊断脚本能 import 到项目根的 core / config
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from core.session import BrowserSession  # noqa: E402

logger = logging.getLogger("chatgpt_curl_cffi_test")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("core").setLevel(logging.DEBUG if verbose else logging.INFO)


def _mask(s: str, left: int = 14, right: int = 8) -> str:
    if not s:
        return ""
    if len(s) <= left + right:
        return "***"
    return f"{s[:left]}...{s[-right:]}"


def _normalize_token(token: str) -> str:
    token = (token or "").strip().strip('"').strip("'")
    if token.lower().startswith("authorization:"):
        token = token.split(":", 1)[1].strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def _read_token_file(path: str, index: int = 0) -> str:
    p = Path(path).expanduser().resolve()
    lines = []
    for raw in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # 兼容常见格式：纯 token / email----token / email:token / JSON 行
        token = ""
        if line.startswith("{"):
            try:
                obj = json.loads(line)
                token = obj.get("token") or obj.get("access_token") or obj.get("bearer") or ""
            except Exception:
                token = ""
        if not token:
            if "----" in line:
                token = line.rsplit("----", 1)[-1]
            elif "\t" in line:
                token = line.rsplit("\t", 1)[-1]
            else:
                token = line
        token = _normalize_token(token)
        if token:
            lines.append(token)
    if not lines:
        raise RuntimeError(f"token 文件没有可用内容: {p}")
    if index < 0 or index >= len(lines):
        raise RuntimeError(f"--token-index 越界：{index}，文件内可用 token 数={len(lines)}")
    return lines[index]


def _decode_jwt_payload_unverified(token: str) -> dict:
    """仅用于本地解析 account_id/plan/email；不校验签名。"""
    try:
        import base64

        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return {}


def _extract_account_id(token: str) -> Optional[str]:
    payload = _decode_jwt_payload_unverified(token)
    auth = payload.get("https://api.openai.com/auth") or {}
    return auth.get("chatgpt_account_id")


def _build_url(endpoint: str, token: str, account_id: Optional[str], timezone_offset_min: str) -> tuple[str, str, str]:
    if endpoint == "accounts-check":
        path = "/backend-api/accounts/check/v4-2023-04-27"
        url = f"https://chatgpt.com{path}?timezone_offset_min={quote(str(timezone_offset_min))}"
        return url, path, path
    if endpoint == "subscriptions":
        account_id = account_id or _extract_account_id(token)
        if not account_id:
            raise RuntimeError("endpoint=subscriptions 需要 --account-id，或 token payload 内包含 chatgpt_account_id")
        path = "/backend-api/subscriptions"
        url = f"https://chatgpt.com{path}?account_id={quote(account_id)}"
        return url, path, path
    raise RuntimeError(f"未知 endpoint: {endpoint}")


def _build_headers(env: BrowserSession, token: str, target_path: str, target_route: str) -> Dict[str, str]:
    headers = env._get_common_headers()  # 复用项目统一指纹画像头
    headers.update(
        {
            "accept": "*/*",
            "authorization": f"Bearer {token}",
            "oai-device-id": env.device_id,
            "oai-language": env.navigator_language(),
            "referer": "https://chatgpt.com/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "x-openai-target-path": target_path,
            "x-openai-target-route": target_route,
        }
    )
    return headers


def _try_json(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None


def _print_profile(env: BrowserSession) -> None:
    p = env.browser_profile or {}
    logger.info("[指纹] proxy=%s", env.proxy if env.proxy else "<direct>")
    logger.info("[指纹] device_id=%s", env.device_id)
    logger.info("[指纹] ua=%s", p.get("user_agent"))
    logger.info("[指纹] accept_language=%s", p.get("accept_language"))
    logger.info("[指纹] navigator_language=%s", p.get("navigator_language"))
    logger.info("[指纹] timezone=%s offset=%s", p.get("timezone_iana"), p.get("timezone_offset_minutes"))
    logger.info("[指纹] screen=%sx%s dpr=%s", p.get("screen_width"), p.get("screen_height"), p.get("device_pixel_ratio"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="ChatGPT backend-api curl_cffi 指纹环境请求测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--token", default=None, help="Bearer JWT；可带或不带 Bearer 前缀")
    parser.add_argument("--token-file", default=None, help="从文件读取 token，默认第一条非空行")
    parser.add_argument("--token-index", type=int, default=0, help="读取 token 文件时使用第几个 token，默认 0")
    parser.add_argument(
        "--endpoint",
        choices=["accounts-check", "subscriptions"],
        default="accounts-check",
        help="要测试的接口，默认 accounts-check",
    )
    parser.add_argument("--account-id", default=None, help="subscriptions 接口 account_id；不传则尝试从 JWT payload 提取")
    parser.add_argument("--timezone-offset-min", default="-", help="accounts-check 参数，默认 -")
    parser.add_argument("--proxy", default=None, help="代理；不传随机抽项目代理池；传空字符串禁用代理")
    parser.add_argument("--verbose", action="store_true", help="显示 DEBUG 日志")
    args = parser.parse_args()

    _setup_logging(args.verbose)

    token = _normalize_token(args.token or "")
    if not token and args.token_file:
        token = _read_token_file(args.token_file, args.token_index)
    if not token:
        logger.error("缺少 token：请传 --token 或 --token-file")
        return 2

    logger.info("=" * 70)
    logger.info("[测试] 创建 BrowserSession 指纹环境")
    logger.info("=" * 70)
    env = BrowserSession(proxy=args.proxy)
    _print_profile(env)

    payload = _decode_jwt_payload_unverified(token)
    auth = payload.get("https://api.openai.com/auth") or {}
    profile = payload.get("https://api.openai.com/profile") or {}
    if payload:
        logger.info("[Token] sub=%s", payload.get("sub"))
        logger.info("[Token] email=%s", profile.get("email"))
        logger.info("[Token] account_id=%s", auth.get("chatgpt_account_id"))
        logger.info("[Token] plan=%s", auth.get("chatgpt_plan_type"))
        logger.info("[Token] exp=%s", payload.get("exp"))
    logger.info("[Token] masked=%s", _mask(token))

    try:
        url, target_path, target_route = _build_url(
            args.endpoint,
            token,
            args.account_id,
            args.timezone_offset_min,
        )
    except Exception as exc:
        logger.error("构建 URL 失败：%s", exc)
        return 2

    headers = _build_headers(env, token, target_path, target_route)

    logger.info("-" * 70)
    logger.info("[请求] GET %s", url)
    if args.verbose:
        safe_headers = dict(headers)
        safe_headers["authorization"] = f"Bearer {_mask(token)}"
        logger.debug("[请求头] %s", json.dumps(safe_headers, ensure_ascii=False, indent=2))

    started = time.time()
    try:
        resp = env.session.get(url, headers=headers, allow_redirects=False)
    except Exception as exc:
        logger.exception("[请求] 异常：%s: %s", type(exc).__name__, exc)
        return 1
    elapsed = time.time() - started

    logger.info("-" * 70)
    logger.info("[响应] HTTP %s %s，耗时 %.2fs", resp.status_code, getattr(resp, "reason", ""), elapsed)
    logger.info("[响应] content-type=%s", resp.headers.get("content-type"))

    if args.verbose:
        logger.debug("[响应头]")
        for k, v in resp.headers.items():
            if k.lower() == "set-cookie":
                v = "<redacted>"
            logger.debug("  %s: %s", k, v)

    print("-" * 70)
    text = resp.text or ""
    data = _try_json(text)
    if data is not None:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(text[:4000])
        if len(text) > 4000:
            print(f"\n... 已截断，原始长度 {len(text)} 字符")

    if 200 <= resp.status_code < 300:
        logger.info("✅ [PASS] 请求成功")
        return 0
    logger.error("❌ [FAIL] 请求失败 status=%s", resp.status_code)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
