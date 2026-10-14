# -*- coding: utf-8 -*-
"""
注册后处理模块：
    1. 拉取 /api/auth/session，从中抽取 accessToken / user 信息
    2. 设置 2FA（TOTP），返回 secret
    3. 把账号信息（邮箱 + accessToken + TOTP secret）写入 SQLite 批次归档

整体复用注册阶段的 BrowserSession（同一 cookie jar / 同一 IP / 同一 UA），
避免再起新会话被风控关联或缺失登录态。
"""
import json
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
import threading
from urllib.parse import urlencode, urlsplit

import pyotp

from core.session import BrowserSession
from core.humanize import delay as human_delay
from core import sqlite_store

logger = logging.getLogger(__name__)


def _check_2fa_response(response, *, stage: str, route: str) -> None:
    """Log an allowlisted HTTP summary and stop at the first failed 2FA step.

    Redirect/callback URLs and response bodies may contain authorization codes,
    so no URL, header value, or exception from the HTTP library is logged here.
    """
    status = getattr(response, "status_code", None)
    status = status if type(status) is int else "unknown"
    try:
        host = (urlsplit(str(getattr(response, "url", "") or "")).hostname or "").lower()
    except (TypeError, ValueError):
        host = ""
    if host not in ("chatgpt.com", "auth.openai.com"):
        host = "unknown"
    headers = getattr(response, "headers", None) or {}
    def _header(name: str) -> str:
        try:
            value = headers.get(name)
            if value is None:
                value = headers.get(name.title())
            if value is None:
                value = next((v for k, v in headers.items() if str(k).lower() == name), "")
            return str(value or "")
        except Exception:
            return ""
    content_type = _header("content-type").split(";", 1)[0].strip().lower()
    if content_type not in ("application/json", "text/html", "text/plain", "application/problem+json"):
        content_type = "other" if content_type else "unknown"
    cloudflare_challenge = "chlray" in _header("server-timing").lower()
    level = logging.WARNING if type(status) is int and status >= 400 else logging.INFO
    logger.log(
        level,
        "[2FA] HTTP stage=%s host=%s route=%s status=%s mime=%s chlray=%s",
        stage, host, route, status, content_type, cloudflare_challenge,
    )
    if type(status) is int and status >= 400:
        raise RuntimeError(f"2FA {stage} HTTP {status}")

# Stable logical prefix for batch archive keys; content lives in SQLite.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_BATCH_ARCHIVE_KEY_ROOT = _PROJECT_ROOT / "accounts"
_BATCH_ARCHIVE_LOCK = threading.RLock()


def _account_material_line(email: str, row: dict | None = None) -> str:
    """优先输出 Outlook 原始素材；没有素材时退回邮箱地址。"""
    if row:
        return row.get("original_email_line") or row.get("email") or email
    return email


def _account_copy_line(material_line: str, access_token: str, totp_secret: str | None = None) -> str:
    """生成包含 token 的整行归档，方便从批次汇总文件里复制。"""
    return f"{material_line}----{access_token}----{totp_secret}" if totp_secret else f"{material_line}----{access_token}"


def create_batch_archive_dir(count: int, workers: int = 1) -> Path:
    """Return a logical SQLite archive prefix for one registration batch."""
    day = datetime.now().strftime("%Y%m%d")
    base_name = f"{day}-{count}个" if workers <= 1 else f"{day}-{count}个-{workers}线程"
    # Archive content stays in SQLite, addressed by a logical prefix.
    folder = _BATCH_ARCHIVE_KEY_ROOT / f"{base_name}-{uuid.uuid4().hex[:8]}"
    for filename, content in (
        ("注册成功的邮箱.txt", ""),
        ("注册成功的token.txt", ""),
        ("注册成功整行.txt", ""),
        ("注册成功账号.json", "[]\n"),
    ):
        sqlite_store.write_file(
            folder / filename,
            content,
            category="batch_archives",
        )
    return folder


def _append_line(path: Path, line: str) -> None:
    sqlite_store.append_file(
        path,
        line + "\n",
        category="batch_archives",
        encoding="utf-8",
    )


def _append_batch_archive(
    *,
    row_id: str,
    email: str,
    access_token: str,
    totp_secret: str | None,
    email_source: str | None,
    proxy: str | None,
    batch_dir: Path | None,
) -> Path:
    """把注册成功账号追加到本次批次目录的 TXT/JSON 文件中。"""
    from core import db

    folder = batch_dir or create_batch_archive_dir(count=1)
    row = db.get_account(row_id) or {}
    material_line = _account_material_line(email, row)
    copy_line = _account_copy_line(material_line, access_token, totp_secret)
    archive = {
        "id": row_id,
        "email": email,
        "email_source": email_source,
        "proxy": proxy,
        "access_token": access_token,
        "totp_secret": totp_secret,
        "material_line": material_line,
        "copy_line": copy_line,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "row": row,
    }

    with _BATCH_ARCHIVE_LOCK:
        _append_line(folder / "注册成功的邮箱.txt", material_line)
        _append_line(folder / "注册成功的token.txt", access_token)
        _append_line(folder / "注册成功整行.txt", copy_line)

        json_path = folder / "注册成功账号.json"
        sqlite_store.append_json_item(
            json_path,
            archive,
            collection=f"batch_archives:{json_path.resolve()}",
            category="batch_archives",
        )
    return folder


def follow_oauth_callback(session: BrowserSession, continue_url: str, referer: str = "https://auth.openai.com/about-you") -> str:
    """
    步骤12.5: 跟随 create_account 返回的 continue_url，完成 OAuth 回调。

    create_account 成功后返回的 continue_url 一般指向
        https://auth.openai.com/authorize/continue?...
    它会再 302 到
        https://chatgpt.com/api/auth/callback/openai?code=...&state=...
    回调请求会让 chatgpt.com 设置 `__Secure-next-auth.session-token` cookie，
    之后 /api/auth/session 才能返回 accessToken。

    Returns:
        重定向链最终落点 URL（一般是 chatgpt.com 站内地址）
    """
    if not continue_url:
        raise ValueError("continue_url 为空，无法完成 OAuth 回调")

    # continue_url 通常是 auth.openai.com/authorize/continue；
    # OTP 后 external_url 分支也可能直接给 chatgpt.com 回调地址。
    # 按目标域名选择导航头，避免 auth step 正确但请求头语义不一致。
    if str(continue_url).startswith("https://chatgpt.com"):
        headers = session.get_chatgpt_navigate_headers(referer=referer)
    else:
        headers = session.get_auth_navigate_headers(referer=referer)

    logger.info(f"[OAuth回调] 跟随 continue_url 完成 OAuth 回调...")
    resp = session.get(continue_url, headers=headers, allow_redirects=True)
    logger.info("[OAuth回调] 完成, status=%s", getattr(resp, "status_code", "unknown"))
    return resp.url


def fetch_session(session: BrowserSession, *, diagnostic_2fa_stage: str | None = None) -> dict:
    """
    GET https://chatgpt.com/api/auth/session
    注册成功后立刻调用，拿到 accessToken / user / account / expires。

    Returns:
        完整 session JSON，包含字段:
            - accessToken: str (Bearer token, 用于 backend-api 调用)
            - user: {id, name, email, idp, iat, mfa}
            - account: {id, planType, structure, ...}
            - expires: ISO 时间字符串
    """
    url = "https://chatgpt.com/api/auth/session"
    headers = session.get_nextauth_headers(referer="https://chatgpt.com/")

    logger.info("[Session] 拉取 ChatGPT session 信息...")
    resp = session.get(url, headers=headers)
    if diagnostic_2fa_stage:
        _check_2fa_response(resp, stage=diagnostic_2fa_stage, route="session")
    else:
        resp.raise_for_status()
    data = resp.json()

    if not data.get("accessToken"):
        logger.error("[Session] 响应中没有 accessToken, status=%s", getattr(resp, "status_code", "unknown"))
        raise RuntimeError("未拿到 accessToken，登录态可能未建立")

    user = data.get("user") or {}
    account = data.get("account") or {}
    logger.info(
        f"[Session] 成功，user_id={user.get('id')}, email={user.get('email')}, "
        f"plan={account.get('planType')}, mfa={user.get('mfa')}"
    )
    return data


def _trigger_reauth(session: BrowserSession, email: str) -> str:
    """
    步骤2-3: 发起密码重认证，返回 OpenAI authorize URL。
    重定向链会自动触发邮箱发送一份新的 OTP（用于 2FA 重认证）。
    """
    # 重新拿一次 csrf（旧的可能已过期）
    csrf_url = "https://chatgpt.com/api/auth/csrf"
    csrf_resp = session.get(csrf_url, headers=session.get_nextauth_headers(referer="https://chatgpt.com/"))
    _check_2fa_response(csrf_resp, stage="reauth_csrf", route="csrf")
    csrf_token = csrf_resp.json()["csrfToken"]
    logger.info("[2FA] 已获取重认证 CSRF")

    # POST /api/auth/signin/openai 带 reauth 参数
    query = {
        "connection": "password",
        "login_hint": email,
        "reauth": "password",
        "max_age": "0",
        "ext-oai-did": session.device_id,
    }
    signin_url = "https://chatgpt.com/api/auth/signin/openai?" + urlencode(query)

    headers = session.get_nextauth_headers(referer="https://chatgpt.com/")
    headers["content-type"] = "application/x-www-form-urlencoded"
    headers["origin"] = "https://chatgpt.com"

    body = urlencode({
        "callbackUrl": "https://chatgpt.com/?action=enable&factor=totp",
        "csrfToken": csrf_token,
        "json": "true",
    })

    logger.info("[2FA] 发起重认证 signin/openai...")
    resp = session.post(signin_url, headers=headers, data=body)
    _check_2fa_response(resp, stage="reauth_signin", route="signin/openai")
    auth_url = resp.json().get("url")
    if not auth_url:
        logger.error("[2FA] reauth 响应缺少 authorize URL, status=%s", getattr(resp, "status_code", "unknown"))
        raise RuntimeError("未拿到 reauth authorize URL")
    return auth_url


def _follow_reauth(session: BrowserSession, auth_url: str) -> None:
    """
    步骤3: 跟随 authorize URL 触发邮箱 OTP 发送。
    auth.openai.com 会重定向到 /email-verification 页面，期间发送 OTP 邮件。
    """
    headers = session.get_auth_navigate_headers(referer="https://chatgpt.com/")
    logger.info("[2FA] 跟随 authorize URL，触发 OTP 发送...")
    resp = session.get(auth_url, headers=headers, allow_redirects=True)
    _check_2fa_response(resp, stage="reauth_authorize", route="authorize")
    logger.info("[2FA] 重认证页面已打开")


def _validate_reauth_otp(session: BrowserSession, code: str) -> str:
    """
    步骤4: 提交邮箱 OTP 验证。
    返回 continue_url（带 code 参数的 callback URL，用于跳回 chatgpt.com）。
    """
    url = "https://auth.openai.com/api/accounts/email-otp/validate"
    headers = session.get_auth_headers(referer="https://auth.openai.com/email-verification")
    body = json.dumps({"code": code})

    logger.info("[2FA] 提交重认证 OTP")
    resp = session.post(url, headers=headers, data=body)
    _check_2fa_response(resp, stage="reauth_otp", route="email-otp/validate")
    data = resp.json()
    continue_url = data.get("continue_url")
    if not continue_url:
        logger.error("[2FA] OTP 验证响应缺少 continue_url, status=%s", getattr(resp, "status_code", "unknown"))
        raise RuntimeError("OTP 验证响应缺少 continue_url")
    return continue_url


def _exchange_new_token(session: BrowserSession, continue_url: str) -> str:
    """
    步骤5: 跟随 continue_url 完成回调，再次拉 /api/auth/session 拿到新 accessToken
    （此时 token 内嵌的 pwd_auth_time 是新鲜的，2FA enroll 才会接受）。
    """
    headers = session.get_auth_navigate_headers(referer="https://auth.openai.com/email-verification")
    logger.info("[2FA] 跟随 continue_url，刷新 session-token cookie...")
    callback_resp = session.get(continue_url, headers=headers, allow_redirects=True)
    _check_2fa_response(callback_resp, stage="session_callback", route="authorize/continue")

    # 拿新的 accessToken
    new_session = fetch_session(session, diagnostic_2fa_stage="session_refresh")
    new_token = new_session["accessToken"]
    logger.info("[2FA] 已刷新带新鲜 pwd_auth_time 的登录态")
    return new_token


def _enroll_totp(session: BrowserSession, access_token: str) -> tuple[str, str]:
    """
    步骤6: 注册 TOTP，返回 (secret, session_id)
    """
    url = "https://chatgpt.com/backend-api/accounts/mfa/enroll"
    headers = session.get_chatgpt_headers(referer="https://chatgpt.com/")
    headers["authorization"] = f"Bearer {access_token}"
    headers["oai-device-id"] = session.device_id
    headers["oai-language"] = session.navigator_language()

    body = json.dumps({"factor_type": "totp"})

    logger.info("[2FA] 注册 TOTP...")
    resp = session.post(url, headers=headers, data=body)
    _check_2fa_response(resp, stage="totp_enroll", route="mfa/enroll")
    if resp.status_code != 200:
        raise RuntimeError("2FA totp_enroll 未返回 HTTP 200")
    data = resp.json()
    secret = data.get("secret")
    session_id = data.get("session_id")
    missing_fields = [
        field for field, value in (("secret", secret), ("session_id", session_id))
        if not isinstance(value, str) or not value.strip()
    ]
    if missing_fields:
        logger.error("[2FA] enroll 响应字段缺失或格式错误: %s", ", ".join(missing_fields))
        raise RuntimeError(f"enroll 响应字段缺失或格式错误: {', '.join(missing_fields)}")
    secret = secret.strip()
    session_id = session_id.strip()
    try:
        # Fail before activation if the server returned a secret that cannot
        # produce a TOTP code. Never include the secret in diagnostics.
        pyotp.TOTP(secret).now()
    except Exception:
        raise RuntimeError("enroll 返回的 TOTP secret 格式无效") from None
    logger.info("[2FA] TOTP enrollment 信息已获取")
    return secret, session_id


def _activate_totp(
    session: BrowserSession,
    access_token: str,
    secret: str,
    session_id: str,
) -> bool:
    """
    步骤7: 用 secret 生成 6 位 TOTP 码，激活 2FA。
    """
    url = "https://chatgpt.com/backend-api/accounts/mfa/user/activate_enrollment"
    headers = session.get_chatgpt_headers(referer="https://chatgpt.com/")
    headers["authorization"] = f"Bearer {access_token}"
    headers["oai-device-id"] = session.device_id
    headers["oai-language"] = session.navigator_language()

    totp_code = pyotp.TOTP(secret).now()
    body = json.dumps({
        "code": totp_code,
        "factor_type": "totp",
        "session_id": session_id,
    })

    logger.info("[2FA] 激活 enrollment")
    resp = session.post(url, headers=headers, data=body)
    _check_2fa_response(resp, stage="totp_activate", route="mfa/activate_enrollment")
    if resp.status_code != 200:
        raise RuntimeError("2FA totp_activate 未返回 HTTP 200")
    data = resp.json()
    if not isinstance(data, dict) or data.get("success") is not True:
        logger.error("[2FA] activate 未返回 success=true, status=%s", getattr(resp, "status_code", "unknown"))
        raise RuntimeError("激活未明确返回 success=true")
    return True


def setup_2fa(session: BrowserSession, email: str, otp_code: str | None = None, otp_provider=None) -> str:
    """
    完整的 2FA 设置流程。
    会触发一份新的邮箱验证码，并复用当前注册任务的自动或手动收码策略。

    Args:
        session: 已完成注册的会话
        email: 账号邮箱（用作 login_hint）
        otp_code: 邮箱验证码（None 则按上述策略获取）

    Returns:
        TOTP secret（Base32 字符串），可直接用于 pyotp.TOTP() 生成 6 位动态码
    """
    logger.info("=" * 60)
    logger.info("开始设置 2FA")
    logger.info("=" * 60)

    # 阶段一：重认证
    reauth_otp_after_ts = time.time()
    auth_url = _trigger_reauth(session, email)
    human_delay("api")
    _follow_reauth(session, auth_url)
    human_delay("navigate")

    if otp_code is None:
        if not callable(otp_provider):
            from core.email_provider import wait_for_otp

            otp_provider = wait_for_otp
        logger.info("[2FA] 使用当前任务邮箱取码策略等待重认证 OTP...")
        otp_code = otp_provider(email, after_ts=reauth_otp_after_ts)

    human_delay("otp_input")
    continue_url = _validate_reauth_otp(session, otp_code)
    human_delay("api")
    new_token = _exchange_new_token(session, continue_url)
    human_delay("api")

    # 阶段二：enroll + activate
    secret, session_id = _enroll_totp(session, new_token)
    human_delay("form")
    _activate_totp(session, new_token, secret, session_id)

    logger.info("=" * 60)
    logger.info("✅ 2FA 设置完成")
    logger.info("=" * 60)
    return secret


def save_account_data(
    email: str,
    access_token: str,
    totp_secret: str | None = None,
    email_source: str | None = None,
    proxy: str | None = None,
    chatgpt_password: str | None = None,
    login_method: str | None = None,
    user_id: str | None = None,
    user_name: str | None = None,
    plan_type: str | None = None,
    expires_at: str | None = None,
    device_id: str | None = None,
    email_provider_context: dict | None = None,
    registration_driver: str | None = None,
    registration_profile_id: str | None = None,
    browser_session_id: str | None = None,
    browser_proxy_country_code: str | None = None,
    sentinel_sid: str | None = None,
    browser_profile: dict | None = None,
    codex_status: str | None = None,
    codex_error: str | None = None,
    batch_dir: Path | None = None,
    archive: bool = True,
    enqueue_plan: bool = True,
    verified_registration: bool = False,
) -> str:
    """
    将账号信息保存到 SQLite，并把本次批次归档写入 SQLite 文件集合。
    返回新插入/更新的 row id。
    """
    if verified_registration:
        from config import twofa as _twofa_cfg

        if not archive:
            raise ValueError("已验证注册账号必须一次写入并归档")
        if _twofa_cfg.ENABLE_2FA and not (
            login_method == "password"
            and isinstance(chatgpt_password, str) and chatgpt_password.strip()
            and isinstance(totp_secret, str) and totp_secret.strip()
        ):
            raise ValueError("启用 2FA 的注册账号必须含密码和 TOTP secret，并通过独立登录验证")
    from core.db import insert_account
    if verified_registration:
        from core.account_proxy import registration_proxy
        proxy = registration_proxy(proxy)
    source = str(email_source or "").strip()
    if source and email_provider_context is None:
        try:
            from core.email_provider import snapshot_email_context

            provider_context = snapshot_email_context(email, source=source)
            if provider_context:
                email_provider_context = provider_context
        except Exception as exc:
            logger.debug("[Save] 邮箱渠道上下文快照失败（不影响账号保存）: %s", exc)
    row_id = insert_account(
        email=email,
        access_token=access_token,
        totp_secret=totp_secret,
        chatgpt_password=chatgpt_password,
        login_method=login_method,
        user_id=user_id,
        user_name=user_name,
        plan_type=plan_type,
        expires_at=expires_at,
        device_id=device_id,
        proxy=proxy,
        email_source=email_source,
        email_provider_context=email_provider_context,
        registration_driver=registration_driver,
        registration_profile_id=registration_profile_id,
        browser_session_id=browser_session_id,
        browser_proxy_country_code=browser_proxy_country_code,
        sentinel_sid=sentinel_sid,
        browser_profile=browser_profile,
        codex_status=codex_status,
        codex_error=codex_error,
    )
    logger.info(f"[Save] 账号已写入 DB, id={row_id}, email={email}")
    if archive:
        batch_folder = _append_batch_archive(
            row_id=row_id,
            email=email,
            access_token=access_token,
            totp_secret=totp_secret,
            email_source=email_source,
            proxy=proxy,
            batch_dir=batch_dir,
        )
        logger.info(f"[Save] 批次归档目录: {batch_folder}")
    # session 中的 account.planType 不能说明 Plus 试用资格。账号落库后只负责
    # 入队，由专用线程池异步查询并回写，避免占用注册工作线程。
    try:
        if enqueue_plan:
            from core.plan_check_service import enqueue_account_plan_check

            queued = enqueue_account_plan_check(
                account_id=row_id,
                email=email,
                access_token=access_token,
                trigger="registration_auto",
                proxy=proxy or "",
            )
            if queued.get("accepted"):
                logger.info(f"[Plan] 注册后自动查询已入队: id={row_id}, email={email}")
            elif queued.get("busy"):
                logger.info(f"[Plan] 账号已有套餐查询，注册流程不重复入队: id={row_id}, email={email}")
            else:
                logger.warning(f"[Plan] 注册后自动查询入队失败（不影响注册结果）: {email}, {queued.get('error')}")
    except Exception as exc:
        logger.warning(
            f"[Plan] 注册后自动查询入队异常（不影响注册结果）: "
            f"{email}, {type(exc).__name__}: {str(exc)[:180]}"
        )
    return row_id
