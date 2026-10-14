# -*- coding: utf-8 -*-
"""Browser-backed ChatGPT password login for account maintenance flows."""
from __future__ import annotations

import logging
from typing import Any

import pyotp

from core.account_export import fetch_session
from core.session import BrowserSession

logger = logging.getLogger(__name__)


class PasswordLoginUnavailable(RuntimeError):
    """The current OpenAI auth branch requires email OTP instead of a password."""


def _browser_cookies(driver) -> list[dict[str, Any]]:
    """Read the complete isolated browser cookie jar.

    Roxy's Selenium adapter may expose only the current page domain through
    ``get_cookies()``.  A registration flow normally ends on ``chatgpt.com``
    while the fresh reauthentication cookies can still belong to
    ``auth.openai.com``.  Prefer CDP ``Network.getAllCookies`` so the HTTP
    session used for 2FA receives both domains; fall back to Playwright's
    context API and finally the Selenium current-domain API for older drivers.
    """
    cdp = getattr(driver, "execute_cdp_cmd", None)
    if callable(cdp):
        try:
            result = cdp("Network.getAllCookies", {})
            rows = result.get("cookies") if isinstance(result, dict) else None
            if isinstance(rows, list) and rows:
                return [row for row in rows if isinstance(row, dict)]
        except Exception:
            pass
    try:
        context = getattr(driver, "context", None)
        if context is not None:
            rows = context.cookies()
            if isinstance(rows, list) and rows:
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


def _login_password_in_browser(
    driver,
    email: str,
    password: str,
    *,
    totp_provider=None,
    require_totp: bool = False,
) -> dict:
    """Fresh login in the caller's browser; do not change its route or own it.

    Registration must not depend on a second browser implementation or an HTTP
    cookie transfer after it already established a working browser session.
    Strict mode still requires an actual password submission and TOTP challenge.
    """
    email = str(email or "").strip()
    password = str(password or "").strip()
    if not email:
        raise ValueError("email 不能为空")
    if not password:
        raise ValueError("password 不能为空")
    if require_totp and not callable(totp_provider):
        raise ValueError("严格登录验证需要 TOTP 提供器")

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

    clear_roxy_browser_auth_state(driver)
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
    password_state = _submit_login_password(driver, password, timeout=25, email=email)
    if password_state == "email_otp":
        raise PasswordLoginUnavailable("OpenAI 当前直接要求邮箱验证码，未提供密码登录页")
    if require_totp and password_state != "submitted":
        raise RuntimeError("无法证明密码已在全新登录会话中提交")

    totp_submissions = 0

    def submit_totp():
        nonlocal totp_submissions
        code = totp_provider()
        if str(code or "").strip():
            totp_submissions += 1
        return code

    if password_state != "next" or require_totp:
        challenge_state = _post_password_challenge(
            driver, submit_totp if require_totp else totp_provider, timeout=30,
        )
        if challenge_state == "email_otp":
            raise PasswordLoginUnavailable("密码提交后 OpenAI 仍要求邮箱验证码")
    if require_totp and not totp_submissions:
        raise RuntimeError("全新登录会话未出现并完成 TOTP challenge")
    browser_info = _fetch_chatgpt_session(driver, timeout=60, auto_jump_wait=8)

    if not str(browser_info.get("accessToken") or ""):
        raise RuntimeError("浏览器密码登录完成，但未拿到 ChatGPT accessToken")
    return browser_info


def authenticate_chatgpt_with_password(
    email: str,
    password: str,
    *,
    proxy: str | None = None,
    totp_provider=None,
    require_totp: bool = False,
) -> tuple[BrowserSession, dict]:
    """Log in through stock Chrome and return a verified HTTP session.

    The function returns only after ``/api/auth/session`` exposes an access
    token in both the browser and the transferred ``BrowserSession``.  A direct
    email-OTP branch raises :class:`PasswordLoginUnavailable`; the caller must
    stop instead of using an expired temporary mailbox as a fallback.  With
    ``require_totp``, both password submission and a completed TOTP challenge
    must be observed in this fresh browser session.
    """
    email = str(email or "").strip()
    password = str(password or "").strip()
    if not email:
        raise ValueError("email 不能为空")
    if not password:
        raise ValueError("password 不能为空")
    if require_totp and not callable(totp_provider):
        raise ValueError("严格登录验证需要 TOTP 提供器")

    from core.chrome_cdp_driver import build_chrome_cdp_driver

    driver, opened = build_chrome_cdp_driver(
        proxy=proxy,
        background=True,
        keep_open=False,
    )
    try:
        logger.info("[查活][密码] 使用本机 Chrome 登录：%s", email)
        _login_password_in_browser(
            driver, email, password, totp_provider=totp_provider,
            require_totp=require_totp,
        )
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


def verify_password_totp_login(
    email: str,
    password: str,
    totp_secret: str,
    *,
    proxy: str | None = None,
    driver=None,
) -> dict:
    """Prove a fresh password + TOTP login, using the registration browser if given.

    A supplied browser stays on its existing, verified route and is not closed.
    Without one, the maintenance path still creates its isolated Chrome session.
    Both paths reject passwordless/old sessions and require the matching email.
    """
    secret = str(totp_secret or "").strip()
    if not secret:
        raise ValueError("无法验证密码 + TOTP 登录：缺少 TOTP secret")
    session = None
    if driver is None:
        session, info = authenticate_chatgpt_with_password(
            email, password, proxy=proxy,
            totp_provider=lambda: pyotp.TOTP(secret).now(), require_totp=True,
        )
    else:
        info = _login_password_in_browser(
            driver, email, password,
            totp_provider=lambda: pyotp.TOTP(secret).now(), require_totp=True,
        )
    try:
        user = info.get("user") or {}
        actual_email = str(user.get("email") or "").strip()
        if not actual_email:
            raise RuntimeError("新登录会话未返回账号邮箱，无法确认登录账号")
        if actual_email.casefold() != str(email).strip().casefold():
            raise RuntimeError("新登录会话与注册邮箱不一致")
        if not str(info.get("accessToken") or ""):
            raise RuntimeError("新登录会话未返回 ChatGPT accessToken")
        return info
    finally:
        if session is not None:
            try:
                session.session.close()
            except Exception:
                pass


__all__ = [
    "PasswordLoginUnavailable",
    "authenticate_chatgpt_with_password",
    "verify_password_totp_login",
]
