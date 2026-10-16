# -*- coding: utf-8 -*-
"""Register ChatGPT accounts through a stock local Chrome attached via CDP."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from config import twofa as _twofa_cfg
from core.account_export import save_account_data, setup_2fa
from core.chatgpt_browser_login import verify_password_totp_login
from core.chrome_cdp_driver import build_chrome_cdp_driver
from core.email_provider import resolve_email_source, wait_for_otp
from registration.drivers.roxy.implementation import (
    RegistrationPasswordSubmissionError,
    _diagnostic_text,
)
from core.humanize import delay as human_delay
from registration.drivers.shared.selenium_steps import (
    _check_manual_stop,
    _clear_otp_inputs,
    _click_continue,
    _click_continue_with_password_if_present,
    _click_resend_email_otp,
    _complete_profile_page,
    _fetch_chatgpt_session,
    _fill_password_page_if_present,
    _has_access_token,
    _is_email_verification_page,
    _maybe_accept,
    _submit_email_and_wait_next,
    _type_otp,
    _wait_after_email_otp_submit,
)

logger = logging.getLogger(__name__)


def _setup_2fa_after_registration(
    driver,
    email: str,
    proxy: str,
) -> str:
    """Reuse the Chrome login state to re-authenticate and enable TOTP.

    All reauthentication, OTP validation, TOTP enrollment, and activation
    requests stay inside the same browser profile.  A second HTTP client can
    lose browser-only cookies or be challenged as a different device, so it is
    intentionally not used here.
    """
    from core.browser_2fa_session import BrowserBacked2FASession

    session = BrowserBacked2FASession(driver)
    return setup_2fa(session, email, otp_provider=wait_for_otp)


def run_chrome_cdp_registration(
    email: str,
    name: str,
    birthday: str,
    proxy: str = None,
    otp_code: str = None,
    batch_dir: Path | None = None,
) -> dict:
    """Run one registration with an isolated local Chrome profile."""
    driver = None
    opened = None
    create_acknowledged = False
    openai_password: str | None = None
    require_2fa = bool(_twofa_cfg.ENABLE_2FA)
    # WebUI may reload configuration during a task; keep this requirement
    # fixed until the account has passed login verification and been saved.
    try:
        driver, opened = build_chrome_cdp_driver(proxy=proxy, background=False)
        logger.info("[Chrome注册] 开始：%s，profile=%s", email, opened.profile_id)

        otp_after_ts = time.time()
        logger.info("[Chrome注册] 打开登录页：https://chatgpt.com/auth/login")
        driver.get("https://chatgpt.com/auth/login")
        human_delay("navigate")
        _maybe_accept(driver)
        _check_manual_stop()

        next_state = _submit_email_and_wait_next(driver, email, attempts=3)
        _check_manual_stop()
        if next_state == "logged_in":
            openai_password = None
            create_acknowledged = True
            needs_email_otp = False
        elif next_state == "password":
            openai_password = _fill_password_page_if_present(
                driver, email, timeout=25, allow_passwordless_signup=not require_2fa,
            )
            if openai_password or _has_access_token(driver):
                create_acknowledged = True
            needs_email_otp = _is_email_verification_page(driver)
        else:
            if require_2fa:
                switch = _click_continue_with_password_if_present(driver)
                if not switch.get("ok"):
                    raise RuntimeError(
                        "已启用 2FA，但仍未设置密码；邮箱验证码页没有打开密码注册入口；"
                        f"已停止邮箱 OTP 单独登录的注册: {switch.get('reason')}"
                    )
                openai_password = _fill_password_page_if_present(
                    driver, email, timeout=25, allow_passwordless_signup=False,
                )
                if openai_password or _has_access_token(driver):
                    create_acknowledged = True
                needs_email_otp = _is_email_verification_page(driver)
            else:
                openai_password = None
                needs_email_otp = True
        if require_2fa and not openai_password:
            raise RuntimeError("已启用 2FA，但注册流程未设置密码；已停止邮箱 OTP 单独登录的注册")
        _check_manual_stop()

        current_otp = otp_code
        max_otp_attempts = 3
        for otp_attempt in (range(1, max_otp_attempts + 1) if needs_email_otp else ()):
            if current_otp is None:
                logger.info(
                    "[Chrome注册][OTP] 等待验证码：%s（第 %s/%s 次）",
                    email,
                    otp_attempt,
                    max_otp_attempts,
                )
                try:
                    current_otp = wait_for_otp(email, after_ts=otp_after_ts)
                except Exception as exc:
                    if otp_attempt >= max_otp_attempts:
                        raise
                    logger.warning(
                        "[Chrome注册][OTP] 未收到验证码，重新发送后继续等待（下一轮 %s/%s）：%s: %s",
                        otp_attempt + 1,
                        max_otp_attempts,
                        type(exc).__name__,
                        _diagnostic_text(exc, 180),
                    )
                    otp_after_ts = time.time()
                    _click_resend_email_otp(driver, timeout=25)
                    human_delay("api")
                    current_otp = None
                    continue

            logger.info("[Chrome注册][OTP] 已收到邮箱验证码")
            _clear_otp_inputs(driver)
            _type_otp(driver, current_otp)
            human_delay("otp_input")
            try:
                _click_continue(driver)
            except Exception as exc:
                logger.info(
                    "[Chrome注册][OTP] 未找到显式提交按钮，继续等待页面状态：%s",
                    _diagnostic_text(exc, 120),
                )

            outcome = _wait_after_email_otp_submit(driver, timeout=10)
            if outcome == "accepted":
                create_acknowledged = True
                break
            if otp_attempt >= max_otp_attempts:
                raise RuntimeError("邮箱验证码连续错误/过期，已达到最大重试次数")
            otp_after_ts = time.time()
            _click_resend_email_otp(driver, timeout=25)
            human_delay("api")
            current_otp = None

        profile_submitted = _complete_profile_page(driver, name, birthday, timeout=60)
        if profile_submitted:
            create_acknowledged = True
            human_delay("post_auth")

        session_info = _fetch_chatgpt_session(driver, timeout=120)
        access_token = str(session_info.get("accessToken") or "")
        if not access_token:
            raise RuntimeError("本机 Chrome 注册完成，但未拿到 ChatGPT accessToken")
        create_acknowledged = True
        logger.info("[Chrome注册] 已拿到 accessToken：%s", email)

        totp_secret = None

        email_source = resolve_email_source(email)
        opened_raw = opened.raw if isinstance(getattr(opened, "raw", None), dict) else {}
        effective_proxy = str(opened_raw.get("proxy") or "") or None
        codex_result = {
            "status": "not_authorized",
            "ok": False,
            "message": "GPT 注册完成，等待在 GPT账号 页面手动发起 Codex 授权",
        }
        account_fields = {
            "user_id": (session_info.get("user") or {}).get("id"),
            "user_name": (session_info.get("user") or {}).get("name"),
            "plan_type": (session_info.get("account") or {}).get("planType"),
            "expires_at": session_info.get("expires"),
            "registration_driver": "chrome_cdp",
            "registration_profile_id": opened.profile_id,
            "codex_status": codex_result["status"],
        }
        if require_2fa:
            logger.info("[Chrome注册] 开始自动设置 2FA：%s", email)
            try:
                totp_secret = _setup_2fa_after_registration(
                    driver,
                    email,
                    effective_proxy or "",
                )
                if not totp_secret:
                    raise RuntimeError("2FA 激活后未返回 TOTP secret")
                logger.info("[Chrome注册] 2FA 设置完成：%s", email)
            except Exception as exc:
                logger.error("[Chrome注册] 2FA 设置失败：%s: %s", type(exc).__name__, _diagnostic_text(exc))
                raise RuntimeError(
                    f"Chrome 注册已创建密码，但 2FA 设置失败：{type(exc).__name__}: {_diagnostic_text(exc, 180)}"
                ) from exc

            # The verification helper starts a separate Chrome profile and
            # requires password submission, TOTP challenge, and accessToken.
            from registration.drivers.roxy.implementation import _clear_chatgpt_auth_state

            logger.info("[Chrome注册] 清理注册浏览器登录态：%s", email)
            _clear_chatgpt_auth_state(driver)
            logger.info("[Chrome注册] 开始密码 + TOTP 重登录验证：%s", email)
            try:
                verified_info = verify_password_totp_login(
                    email, openai_password, totp_secret, proxy=effective_proxy,
                )
                access_token = verified_info["accessToken"]
            except Exception as exc:
                logger.error("[Chrome注册] 密码 + TOTP 重登录验证失败：%s", type(exc).__name__)
                raise RuntimeError(
                    "Chrome 注册已创建密码和 2FA，但独立密码 + TOTP 登录验证失败："
                    f"{type(exc).__name__}"
                ) from exc
            logger.info("[Chrome注册] 密码 + TOTP 重登录验证成功：%s", email)

        account_id = save_account_data(
            email=email,
            access_token=access_token,
            totp_secret=totp_secret,
            email_source=email_source,
            chatgpt_password=openai_password,
            login_method="password" if openai_password else "email_otp",
            proxy=effective_proxy,
            batch_dir=batch_dir,
            **account_fields,
            archive=True,
        verified_registration=True,
            enqueue_plan=True,
        )
        return {
            "success": True,
            "email": email,
            "account_id": account_id,
            "access_token": access_token,
            "totp_secret": totp_secret,
            "codex": codex_result,
            "error": None,
        }
    except Exception as exc:
        logger.error("[Chrome注册] 失败：%s: %s", type(exc).__name__, _diagnostic_text(exc))
        try:
            from core.email_provider import release_email

            release_email(
                email,
                status=("disabled" if isinstance(exc, RegistrationPasswordSubmissionError)
                        else "failed" if create_acknowledged else "available"),
                note=f"Chrome注册失败: {_diagnostic_text(exc, 180)}",
            )
        except Exception:
            pass
        return {
            "success": False,
            "email": email,
            "error": f"{type(exc).__name__}: {_diagnostic_text(exc, 300)}",
        }
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
