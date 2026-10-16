"""Attachment-compatible pure HTTP registration flow.

This keeps the attachment project's request ordering while adapting the
result to this repository's driver-neutral account storage and email lifecycle.
It deliberately does not copy the attachment project's file exports,
automatic Codex OAuth, or best-effort 2FA persistence.
"""
from __future__ import annotations

import logging
import time

from config import twofa as _twofa_cfg
from core.account_export import save_account_data
from core.chatgpt_auth import get_csrf_token, get_providers, signin_openai
from core.email_provider import (
    automatic_email_enabled,
    resolve_email_source,
    snapshot_email_context,
    wait_for_otp,
)
from core.flow_trigger import trigger_flow
from core.humanize import delay as human_delay
from core.openai_auth import (
    build_sentinel_header,
    create_account,
    follow_authorize,
    request_sentinel_token,
    validate_email_otp,
)
from core.session import BrowserSession
from registration.application.use_case import (
    _finalize_registration_session,
    _diagnostic_text,
    require_password_2fa_driver,
)

logger = logging.getLogger(__name__)


def _wait_for_attachment_otp(email: str, otp_code: str | None, after_ts: float) -> str:
    if otp_code:
        return str(otp_code).strip()
    if automatic_email_enabled():
        logger.info("[附件协议][OTP] 等待验证码：%s", email)
    else:
        logger.info("[附件协议][OTP] 等待手动提交验证码：%s", email)
    return wait_for_otp(email, after_ts=after_ts)


def _build_attachment_session(proxy: str | None) -> BrowserSession:
    """Disable current-project geo probing to match the attachment session."""
    try:
        return BrowserSession(proxy=proxy, detect_exit_geo=False)
    except TypeError:
        # Keeps small test doubles and third-party-compatible session adapters
        # usable if they only implement the original one-argument constructor.
        return BrowserSession(proxy=proxy)


def run_attachment_protocol_registration(
    email: str,
    name: str,
    birthday: str | None = None,
    proxy: str | None = None,
    otp_code: str | None = None,
    batch_dir=None,
) -> dict:
    """Run the attachment project's OTP protocol sequence.

    The attachment flow creates the ``authorize_continue`` Sentinel token
    before waiting for the mailbox OTP, then unconditionally continues through
    ``oauth_create_account`` and ``create_account`` after OTP validation.
    """
    require_password_2fa_driver("protocol")
    if not birthday:
        from core.profile_utils import generate_random_birthday

        birthday = generate_random_birthday()

    session = _build_attachment_session(proxy)
    create_acknowledged = False

    try:
        logger.info(
            "[附件协议] 开始：%s，代理=%s",
            email,
            "已配置" if session.proxy else "无",
        )

        get_providers(session)
        human_delay("api")
        csrf_token = get_csrf_token(session)
        human_delay("api")
        authorize_url = signin_openai(session, csrf_token, email)
        human_delay("api")

        otp_after_ts = time.time()
        follow_authorize(session, authorize_url)
        human_delay("navigate")

        # Match the attachment ordering: the challenge is created before the
        # mailbox wait, rather than immediately before the validate request.
        sentinel_response = request_sentinel_token(session, "authorize_continue")
        sentinel_header, _ = build_sentinel_header(
            session, sentinel_response, "authorize_continue"
        )
        human_delay("challenge")

        code = _wait_for_attachment_otp(email, otp_code, otp_after_ts)
        validate_email_otp(session, code, sentinel_header)
        human_delay("api")

        sentinel_response = request_sentinel_token(session, "oauth_create_account")
        sentinel_header, so_header = build_sentinel_header(
            session, sentinel_response, "oauth_create_account"
        )
        human_delay("challenge")
        human_delay("form")

        create_result = create_account(
            session, name, birthday, sentinel_header, so_header
        )
        create_acknowledged = True
        continue_url = create_result.get("continue_url")
        if not continue_url:
            raise RuntimeError("create_account 响应缺少 continue_url，无法继续")

        session_info, access_token = _finalize_registration_session(
            session, continue_url, email
        )
        human_delay("post_auth")

        # Protocol mode remains OTP-only in this application.  The guard above
        # rejects ENABLE_2FA before a mailbox is consumed.
        if _twofa_cfg.ENABLE_2FA:
            raise RuntimeError("protocol 不支持当前项目要求的密码 + TOTP 独立登录验证")

        email_source = resolve_email_source(email)
        provider_context = snapshot_email_context(email, source=email_source)
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
            "device_id": session.device_id,
            "sentinel_sid": getattr(session, "sentinel_sid", None),
            "browser_profile": getattr(session, "browser_profile", None),
            "registration_driver": "protocol",
            "email_provider_context": provider_context,
            "codex_status": codex_result["status"],
        }
        account_id = save_account_data(
            email=email,
            access_token=access_token,
            totp_secret=None,
            email_source=email_source,
            proxy=session.proxy or None,
            login_method="email_otp",
            batch_dir=batch_dir,
            **account_fields,
            archive=True,
            verified_registration=True,
            enqueue_plan=True,
        )

        flow_result = {"status": "skipped", "ok": False, "message": "未触发"}
        try:
            flow_result = trigger_flow(access_token)
        except Exception as exc:
            flow_result = {
                "status": "failed",
                "ok": False,
                "message": f"{type(exc).__name__}: {exc}",
            }

        return {
            "success": True,
            "email": email,
            "account_id": account_id,
            "access_token": access_token,
            "totp_secret": None,
            "flow": flow_result,
            "codex": codex_result,
            "error": None,
        }
    except Exception as exc:
        logger.error(
            "[附件协议][失败] %s: %s: %s",
            email,
            type(exc).__name__,
            _diagnostic_text(exc),
        )
        try:
            from core.email_provider import release_email
            from core.openai_auth import AccountUnusableError

            if email:
                if isinstance(exc, AccountUnusableError):
                    release_email(
                        email,
                        status="failed",
                        note=f"账号已废弃，邮箱不可用: {str(exc)[:180]}",
                    )
                elif create_acknowledged:
                    release_email(
                        email,
                        status="failed",
                        note=f"创建接口已通过但后续失败，已废弃: {str(exc)[:180]}",
                    )
                else:
                    release_email(
                        email,
                        status="available",
                        note=f"上次失败: {str(exc)[:180]}",
                    )
        except Exception:
            logger.debug("[附件协议] 邮箱状态回收失败", exc_info=True)
        return {"success": False, "email": email, "error": str(exc)}


__all__ = ["run_attachment_protocol_registration"]
