# -*- coding: utf-8 -*-
"""Browser-backed ChatGPT password login for account maintenance flows."""
from __future__ import annotations

import logging
from typing import Any

from core.account_export import fetch_session
from core.session import BrowserSession

logger = logging.getLogger(__name__)


class PasswordLoginUnavailable(RuntimeError):
    """The current OpenAI auth branch requires email OTP instead of a password."""


def _browser_cookies(driver) -> list[dict[str, Any]]:
    try:
        context = getattr(driver, "context", None)
        if context is not None:
            rows = context.cookies()
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    except Exception:
        pass
    try:
        rows = driver.get_cookies()
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    except Exception:
        pass
    return []


def _session_from_browser(driver, proxy: str | None) -> BrowserSession:
    # Chrome with proxy=None is direct; BrowserSession(proxy=None) would draw
    # from the proxy pool, so preserve the browser's direct route explicitly.
    session = BrowserSession(proxy=proxy if proxy is not None else "")
    rows = _browser_cookies(driver)
    if not rows:
        try:
            session.session.close()
        except Exception:
            pass
        raise RuntimeError("浏览器密码登录成功，但无法读取登录 Cookie")
    for item in rows:
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "")
        if not name:
            continue
        domain = str(item.get("domain") or "").strip() or None
        path = str(item.get("path") or "/") or "/"
        kwargs: dict[str, Any] = {"path": path}
        if domain:
            kwargs["domain"] = domain
        session.session.cookies.set(name, value, **kwargs)
        if name == "oai-did" and value:
            session.device_id = value
    return session


def authenticate_chatgpt_with_password(
    email: str,
    password: str,
    *,
    proxy: str | None = None,
    totp_provider=None,
) -> tuple[BrowserSession, dict]:
    """Log in through stock Chrome and return a verified HTTP session.

    The function returns only after ``/api/auth/session`` exposes an access
    token in both the browser and the transferred ``BrowserSession``.  A direct
    email-OTP branch raises :class:`PasswordLoginUnavailable`, allowing the
    caller to use its existing OTP implementation without treating the stored
    password as verified.
    """
    email = str(email or "").strip()
    password = str(password or "").strip()
    if not email:
        raise ValueError("email 不能为空")
    if not password:
        raise ValueError("password 不能为空")

    from core.chrome_cdp_driver import build_chrome_cdp_driver
    from core.roxy_codex_oauth import (
        _post_password_challenge,
        _submit_email_step,
        _submit_login_password,
        _type_email_address,
        clear_roxy_browser_auth_state,
    )
    from registration.drivers.roxy.implementation import (
        _fetch_chatgpt_session,
        _maybe_accept,
        _safe_get,
    )

    driver, opened = build_chrome_cdp_driver(
        proxy=proxy,
        background=True,
        keep_open=False,
    )
    try:
        clear_roxy_browser_auth_state(driver)
        logger.info("[查活][密码] 使用本机 Chrome 登录：%s", email)
        _safe_get(
            driver,
            "https://chatgpt.com/auth/login",
            timeout=45,
            attempts=2,
            accept_hosts=("chatgpt.com", "auth.openai.com"),
        )
        _maybe_accept(driver)
        _type_email_address(driver, email, timeout=20)
        _submit_email_step(driver, email)
        password_state = _submit_login_password(driver, password, timeout=25)
        if password_state == "email_otp":
            raise PasswordLoginUnavailable("OpenAI 当前直接要求邮箱验证码，未提供密码登录页")
        if password_state != "next":
            challenge_state = _post_password_challenge(driver, totp_provider, timeout=30)
            if challenge_state == "email_otp":
                raise PasswordLoginUnavailable("密码提交后 OpenAI 仍要求邮箱验证码")
        browser_info = _fetch_chatgpt_session(driver, timeout=60, auto_jump_wait=8)

        if not str(browser_info.get("accessToken") or ""):
            raise RuntimeError("浏览器密码登录完成，但未拿到 ChatGPT accessToken")
        opened_raw = opened.raw if isinstance(getattr(opened, "raw", None), dict) else {}
        actual_proxy = str(opened_raw.get("proxy") or "")
        session = _session_from_browser(driver, actual_proxy)
        try:
            session_info = fetch_session(session)
            if not str(session_info.get("accessToken") or ""):
                raise RuntimeError("浏览器 Cookie 已转移，但 HTTP 会话未拿到 ChatGPT accessToken")
        except Exception:
            try:
                session.session.close()
            except Exception:
                pass
            raise
        logger.info("[查活][密码] 密码登录成功并验证 ChatGPT session：%s", email)
        return session, session_info
    finally:
        try:
            driver.quit()
        except Exception:
            pass


__all__ = [
    "PasswordLoginUnavailable",
    "authenticate_chatgpt_with_password",
]
