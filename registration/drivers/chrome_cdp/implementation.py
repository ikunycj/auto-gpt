# -*- coding: utf-8 -*-
"""Register ChatGPT accounts through a stock local Chrome attached via CDP."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from config import twofa as _twofa_cfg
from core.account_export import save_account_data
from core.chrome_cdp_driver import build_chrome_cdp_driver
from core.email_provider import resolve_email_source, wait_for_otp
from core.humanize import delay as human_delay
from registration.drivers.shared.selenium_steps import (
    _check_manual_stop,
    _clear_otp_inputs,
    _click_continue,
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
            openai_password = _fill_password_page_if_present(driver, email, timeout=25)
            if openai_password or _has_access_token(driver):
                create_acknowledged = True
            needs_email_otp = _is_email_verification_page(driver)
        else:
            openai_password = None
            needs_email_otp = True
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
                        str(exc)[:180],
                    )
                    otp_after_ts = time.time()
                    _click_resend_email_otp(driver, timeout=25)
                    human_delay("api")
                    current_otp = None
                    continue

            logger.info("[Chrome注册][OTP] 收到验证码：%s", current_otp)
            _clear_otp_inputs(driver)
            _type_otp(driver, current_otp)
            human_delay("otp_input")
            try:
                _click_continue(driver)
            except Exception as exc:
                logger.info(
                    "[Chrome注册][OTP] 未找到显式提交按钮，继续等待页面状态：%s",
                    str(exc)[:120],
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

        if _twofa_cfg.ENABLE_2FA:
            logger.warning("[Chrome注册] 当前本机 Chrome 注册路径暂不执行 2FA 设置，已跳过")
        totp_secret = None

        email_source = resolve_email_source(email)
        opened_raw = opened.raw if isinstance(getattr(opened, "raw", None), dict) else {}
        effective_proxy = str(opened_raw.get("proxy") or "") or None
        codex_result = {
            "status": "not_authorized",
            "ok": False,
            "message": "GPT 注册完成，等待在 GPT账号 页面手动发起 Codex 授权",
        }
        account_extra = {
            "user": session_info.get("user"),
            "account": session_info.get("account"),
            "expires": session_info.get("expires"),
            "chrome_cdp": {
                "profile_id": opened.profile_id,
                "open_result": opened_raw,
            },
            "registration_password": openai_password,
            "login_method": "password" if openai_password else "email_otp",
            "codex": codex_result,
        }
        account_id = save_account_data(
            email=email,
            access_token=access_token,
            totp_secret=totp_secret,
            email_source=email_source,
            proxy_used=effective_proxy,
            batch_dir=batch_dir,
            extra=account_extra,
            archive=False,
            enqueue_plan=True,
        )
        if openai_password:
            logger.info(
                "[Chrome注册][检查点] 账号及登录密码已即时写入：%s，账号ID=%s",
                email,
                account_id,
            )
        else:
            logger.info(
                "[Chrome注册][检查点] 账号已即时写入（OTP 登录，未经过密码创建页）：%s，账号ID=%s",
                email,
                account_id,
            )

        account_id = save_account_data(
            email=email,
            access_token=access_token,
            totp_secret=totp_secret,
            email_source=email_source,
            proxy_used=effective_proxy,
            batch_dir=batch_dir,
            extra=account_extra,
            archive=True,
            enqueue_plan=False,
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
        logger.error("[Chrome注册] 失败：%s: %s", type(exc).__name__, exc)
        logger.debug("[Chrome注册] 失败详情", exc_info=True)
        try:
            from core.email_provider import release_email

            release_email(
                email,
                status="failed" if create_acknowledged else "available",
                note=f"Chrome注册失败: {str(exc)[:180]}",
            )
        except Exception:
            pass
        return {
            "success": False,
            "email": email,
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
