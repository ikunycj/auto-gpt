# -*- coding: utf-8 -*-
"""已注册账号查活：重新邮箱 OTP 登录，成功拿到最新 ChatGPT accessToken 即视为正常。"""
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from core.session import BrowserSession
from core.chatgpt_auth import get_providers, get_csrf_token, signin_openai
from core.openai_auth import (
    follow_authorize,
    send_email_otp,
    validate_email_otp,
    EmailOtpInvalidError,
    AccountUnusableError,
    detect_account_unusable_text,
)
from core.account_export import follow_oauth_callback, fetch_session
from core.email_provider import wait_for_otp
from core import sqlite_store

logger = logging.getLogger(__name__)
_LIVENESS_LOG_KEY_ROOT = Path(__file__).resolve().parent.parent / "注册日志"
# Historical private name retained for callers that customize isolated storage.
_LOG_DIR = _LIVENESS_LOG_KEY_ROOT
_RUNNING: set[str] = set()
_RUNNING_LOCK = threading.Lock()

# 查活网络预检失败（403/429/代理/超时等）多为出口 IP 被 CF 标记或代理池抖动，
# 视为可换新 IP 重试；账号本身问题（废号/邮箱错误等）不重试。
_RETRYABLE_NETWORK_HINTS = (
    "403", "429", "502", "503", "504",
    "proxy", "socks", "timeout", "timed out",
    "connection", "closed", "reset",
)


def _is_retryable_network_error(exc: BaseException) -> bool:
    if isinstance(exc, AccountUnusableError):
        return False
    text = str(exc or "").lower()
    return any(h in text for h in _RETRYABLE_NETWORK_HINTS)


def _network_preflight_with_retry(email: str, proxy: str | None, max_attempts: int = 4) -> tuple[BrowserSession, str]:
    """Providers → CSRF → Signin 网络预检；失败换新 IP 重试（每轮新会话新代理）。"""
    session: BrowserSession | None = None
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        if session is not None:
            try:
                session.session.close()
            except Exception:
                pass
        session = BrowserSession(proxy=proxy if proxy else None)
        logger.info(
            "[查活] 会话创建完成：proxy=%s device_id=%s（网络预检第 %s/%s 次）",
            session.proxy or "配置随机/直连", session.device_id, attempt, max_attempts,
        )
        try:
            get_providers(session)
            csrf = get_csrf_token(session)
            authorize_url = signin_openai(session, csrf, email)
            return session, authorize_url
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts or not _is_retryable_network_error(exc):
                raise
            logger.warning(
                "[查活] 网络预检失败（%s/%s），换新 IP 重试：%s",
                attempt, max_attempts, str(exc)[:200],
            )
            time.sleep(2)
    raise RuntimeError(f"网络预检多次失败：{last_exc}")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def log_path(email: str) -> Path:
    safe = str(email or "").replace("/", "_").replace("\\", "_").replace(":", "_")
    return _LOG_DIR / f"live-check-{safe}.log"


def is_checking(email: str) -> bool:
    key = str(email or "").strip().lower()
    with _RUNNING_LOCK:
        return key in _RUNNING


def _validate_with_retry(session: BrowserSession, email: str, otp_after_ts: float, max_otp_attempts: int = 3, otp_provider=None) -> dict:
    current_otp = None
    last_exc: Exception | None = None
    for attempt in range(1, max_otp_attempts + 1):
        try:
            if current_otp is None:
                logger.info("[查活] 等待登录 OTP：%s（第 %s/%s 次）", email, attempt, max_otp_attempts)
                current_otp = (otp_provider(email, after_ts=otp_after_ts) if callable(otp_provider) else wait_for_otp(email, after_ts=otp_after_ts))
            result = validate_email_otp(session, current_otp, sentinel_header=None, so_header=None)
            return result
        except EmailOtpInvalidError as exc:
            last_exc = exc
            if attempt >= max_otp_attempts:
                break
            logger.warning("[查活] OTP 无效/过期，重新发送后再取：%s", str(exc)[:180])
            send_email_otp(session)
            # 以“重新发送请求完成后”为新基准，避免刚刚失败的上一封旧码再次被 after 容忍窗口命中。
            otp_after_ts = time.time()
            current_otp = None
            time.sleep(1)
        except Exception as exc:
            # 提交 OTP 后的网络抖动（连接断开/超时/代理波动）：同一会话重发验证码再验证一次。
            if attempt >= max_otp_attempts or not _is_retryable_network_error(exc):
                raise
            last_exc = exc
            logger.warning("[查活] OTP 验证网络抖动，重新发送后再取（%s/%s）：%s", attempt, max_otp_attempts, str(exc)[:180])
            try:
                send_email_otp(session)
            except Exception:
                raise
            otp_after_ts = time.time()
            current_otp = None
            time.sleep(1)
    raise last_exc if last_exc else RuntimeError("OTP 验证失败")


def authenticate_account_session(
    email: str,
    proxy: str | None = None,
    otp_provider=None,
    *,
    login_password: str | None = None,
    totp_provider=None,
):
    """Authenticate an existing account and return its live BrowserSession.

    This is intentionally separate from the liveness result so maintenance actions
    such as enabling TOTP can continue using the authenticated session.
    """
    email = str(email or "").strip()
    if not email:
        raise ValueError("email 不能为空")
    if str(login_password or "").strip():
        from core.chatgpt_browser_login import (
            PasswordLoginUnavailable,
            authenticate_chatgpt_with_password,
        )

        logger.info("[查活] 检测到已确认的 ChatGPT 密码，优先使用密码登录")
        try:
            session, session_info = authenticate_chatgpt_with_password(
                email,
                str(login_password),
                proxy=proxy,
                totp_provider=totp_provider,
            )
            setattr(session, "authentication_method", "password")
            return session, session_info
        except PasswordLoginUnavailable as exc:
            logger.info("[查活] 当前页面不提供密码登录，回退邮箱 OTP：%s", str(exc)[:180])

    logger.info("[查活] 使用邮箱 OTP 登录")
    session, authorize_url = _network_preflight_with_retry(email, proxy)
    try:
        otp_after_ts = time.time()
        final_url = follow_authorize(session, authorize_url)
        dead_code = detect_account_unusable_text(final_url)
        if dead_code:
            raise AccountUnusableError(dead_code)
        validate_result = _validate_with_retry(session, email, otp_after_ts, otp_provider=otp_provider)
        page = validate_result.get("page") if isinstance(validate_result, dict) else {}
        page = page if isinstance(page, dict) else {}
        page_type = str(page.get("type") or "")
        continue_url = (
            validate_result.get("continue_url")
            or validate_result.get("external_url")
            or validate_result.get("url")
            or page.get("continue_url")
            or page.get("external_url")
            or page.get("url")
        )
        if not continue_url:
            raise RuntimeError(f"OTP 登录成功但没有 OAuth continue_url: {validate_result}")
        if "about-you" in str(continue_url) or page_type in {"about_you", "about-you"}:
            raise RuntimeError("该邮箱登录后进入资料页，疑似不是完整已注册账号")
        follow_oauth_callback(session, str(continue_url), referer="https://auth.openai.com/email-verification")
        session_info = fetch_session(session)
        setattr(session, "authentication_method", "email_otp")
        return session, session_info
    except Exception:
        try:
            session.session.close()
        except Exception:
            pass
        raise


def check_account_liveness(
    email: str,
    proxy: str | None = None,
    *,
    clear_log: bool = True,
    otp_provider=None,
    login_password: str | None = None,
    totp_provider=None,
) -> dict:
    """
    重新登录账号并刷新最新 accessToken。

    返回：
      {
        ok: bool,
        status: live/deactivated/failed,
        access_token: str?,
        session: dict?,
        checked_at: ISO,
        error: str?
      }
    """
    email = str(email or "").strip()
    if not email:
        raise ValueError("email 不能为空")

    checked_at = _now()
    key = email.lower()
    path = log_path(email)
    if clear_log:
        sqlite_store.write_file(path, b"", category="liveness_logs", mode=0o600, mirror=False)

    fh: sqlite_store.SQLiteFileHandler | None = None
    root_logger = logging.getLogger()
    thread_name = threading.current_thread().name
    with _RUNNING_LOCK:
        _RUNNING.add(key)
    try:
        fh = sqlite_store.SQLiteFileHandler(path, category="liveness_logs", mirror=False)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        fh.addFilter(lambda record: record.threadName == thread_name)
        root_logger.addHandler(fh)

        logger.info("[查活] 日志文件：%s", path)
        logger.info("[查活] 开始重新登录：%s", email)
        logger.info("[查活] 流程：有密码先走浏览器密码登录；无密码/页面仅 OTP 时走邮箱 OTP；最后校验 Session/AT")
        session, session_info = authenticate_account_session(
            email,
            proxy=proxy,
            otp_provider=otp_provider,
            login_password=login_password,
            totp_provider=totp_provider,
        )
        access_token = str(session_info.get("accessToken") or "")
        if not access_token:
            raise RuntimeError("重新登录后未拿到 accessToken")

        user = session_info.get("user") or {}
        account = session_info.get("account") or {}
        logger.info("[查活] 正常：%s user_id=%s plan=%s", email, user.get("id"), account.get("planType"))
        return {
            "ok": True,
            "status": "live",
            "checked_at": checked_at,
            "access_token": access_token,
            "session": session_info,
            "device_id": session.device_id,
            "proxy_used": session.proxy or None,
            "auth_method": getattr(session, "authentication_method", "email_otp"),
        }
    except AccountUnusableError as exc:
        code = getattr(exc, "error_code", "") or detect_account_unusable_text(str(exc)) or "account_deactivated"
        logger.warning("[查活] 已废号：%s %s", email, code)
        return {"ok": False, "status": "deactivated", "checked_at": checked_at, "error": code}
    except Exception as exc:
        code = detect_account_unusable_text(str(exc))
        if code:
            logger.warning("[查活] 已废号：%s %s", email, code)
            return {"ok": False, "status": "deactivated", "checked_at": checked_at, "error": code}
        logger.warning("[查活] 失败：%s %s: %s", email, type(exc).__name__, str(exc)[:260])
        return {"ok": False, "status": "failed", "checked_at": checked_at, "error": f"{type(exc).__name__}: {str(exc)[:500]}"}
    finally:
        try:
            try:
                if "session" in locals() and session is not None:
                    session.session.close()
            except Exception:
                pass
            logger.info("[查活] 结束：%s", email)
            if fh is not None:
                root_logger.removeHandler(fh)
                fh.close()
        finally:
            with _RUNNING_LOCK:
                _RUNNING.discard(key)
