# -*- coding: utf-8 -*-
"""Application use case for one ChatGPT registration.

This module owns driver dispatch and shared registration checkpoints.  CLI and
Web workers call :func:`run_registration`; browser integrations are resolved
through the lazy driver registry.
"""
from __future__ import annotations

import logging
import re
import time

from config import twofa as _twofa_cfg
from core.session import BrowserSession
from core.account_export import (
    follow_oauth_callback,
    fetch_session,
)
from core.humanize import delay as human_delay
from core.profile_utils import generate_random_birthday
from registration.application.models import RegistrationRequest, RegistrationResult
from registration.drivers.registry import get_driver

logger = logging.getLogger(__name__)

def _diagnostic_text(value: object, limit: int = 240) -> str:
    """Return the complete registration error for local auditing."""
    return str(value or "-")

_FINALIZE_SESSION_MAX_ATTEMPTS = 5
_FINALIZE_SESSION_BACKOFF_BASE = 2.0


def require_password_2fa_driver(driver_mode: str | None = None) -> None:
    """Reject drivers that cannot leave a temporary mailbox with password + TOTP."""
    if not _twofa_cfg.ENABLE_2FA:
        return

    from config import roxybrowser as _roxy_cfg
    from registration.drivers.registry import normalize_driver_name

    configured = driver_mode if driver_mode is not None else getattr(_roxy_cfg, "REGISTRATION_DRIVER", "protocol")
    driver = normalize_driver_name(configured)
    if driver not in {"roxy", "chrome_cdp"}:
        raise RuntimeError(
            f"启用 2FA 时，{driver} 注册驱动不能保证完成密码和 TOTP 设置；"
            "请切换到 roxy 或 chrome_cdp，再领取临时邮箱。"
        )


def _finalize_registration_session(
    session: BrowserSession,
    continue_url: str,
    email: str,
    callback_referer: str = "https://auth.openai.com/about-you",
) -> tuple[dict, str]:
    """
    完成 OAuth 回调并拉取 accessToken。

    create_account 返回只代表创建接口通过，真正可用必须等 chatgpt.com
    写入登录态 cookie 且 /api/auth/session 返回 accessToken。
    """
    if not continue_url:
        raise RuntimeError("create_account 响应缺少 continue_url，无法完成 OAuth 回调")

    last_exc: Exception | None = None
    for attempt in range(1, _FINALIZE_SESSION_MAX_ATTEMPTS + 1):
        try:
            logger.info(
                f"[登录态] 完成 OAuth 回调并拉取 Token：{email} "
                f"(尝试 {attempt}/{_FINALIZE_SESSION_MAX_ATTEMPTS})"
            )
            follow_oauth_callback(session, continue_url, referer=callback_referer)
            human_delay("post_auth")
            session_info = fetch_session(session)
            access_token = session_info.get("accessToken")
            if not access_token:
                raise RuntimeError("session 响应缺少 accessToken")
            logger.info(f"[登录态] 已拿到 accessToken：{email}")
            return session_info, access_token
        except Exception as exc:
            last_exc = exc
            if attempt >= _FINALIZE_SESSION_MAX_ATTEMPTS:
                break
            backoff = _FINALIZE_SESSION_BACKOFF_BASE ** (attempt - 1)
            logger.warning(
                f"[登录态] 回调或拉取 Token 失败：{email}，"
                f"{type(exc).__name__}: {_diagnostic_text(exc)}，{backoff:.1f}s 后重试"
            )
            time.sleep(backoff)

    raise RuntimeError(
        f"OAuth 回调/拉取 Token 重试耗尽：{email}，"
        f"最后错误：{type(last_exc).__name__ if last_exc else 'Unknown'}: {last_exc}"
    ) from last_exc

def run_registration(
    email: str,
    name: str,
    birthday: str | None = None,
    proxy: str = None,
    otp_code: str = None,
    batch_dir=None,
) -> RegistrationResult:
    """Dispatch one registration request to the selected detachable driver."""
    request = RegistrationRequest(
        email=email,
        name=name,
        birthday=birthday or generate_random_birthday(),
        proxy=proxy,
        otp_code=otp_code,
        batch_dir=batch_dir,
    )
    from config import roxybrowser as _roxy_cfg

    driver_mode = str(getattr(_roxy_cfg, "REGISTRATION_DRIVER", "protocol") or "protocol").strip().lower()
    require_password_2fa_driver(driver_mode)
    try:
        driver = get_driver(driver_mode)
    except ValueError as exc:
        # Preserve the historic public API exception type for CLI/Web callers.
        raise RuntimeError(str(exc)) from exc
    return driver.run(request)
