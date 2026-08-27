# -*- coding: utf-8 -*-
"""
邮箱来源调度层。

EMAIL_SOURCE 支持单个或多个来源：
    "outlook"
    "cloudflare_domain"   # 自有域名 + QQ IMAP
    "cloudflare"          # Cloudflare Worker 临时邮箱
    "generic_api"
    "gptmail"
    "mailnest"
    "cloudmail"
    "outlook,generic_api,mailnest,cloudmail"          # 按顺序兜底
    ["outlook", "generic_api", "mailnest", "cloudmail"]  # 也兼容列表写法
"""
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterable

logger = logging.getLogger(__name__)

_VALID_SOURCES = ("outlook", "generic_api", "cloudflare_domain", "cloudflare", "gptmail", "mailnest", "cloudmail")

EMAIL_SOURCE_LABELS = {
    "outlook": "Outlook",
    "generic_api": "通用接码 API",
    "cloudflare_domain": "Cloudflare 域名邮箱",
    "cloudflare": "Cloudflare Worker",
    "gptmail": "GPTMail",
    "mailnest": "MailNest",
    "cloudmail": "CloudMail",
}

_SOURCE_KINDS = {
    "outlook": "pool",
    "generic_api": "pool",
    "cloudflare_domain": "generated",
    "cloudflare": "generated",
    "gptmail": "generated",
    "mailnest": "generated",
    "cloudmail": "generated",
}

_TASK_EMAIL_SOURCES: ContextVar[tuple[str, ...] | None] = ContextVar(
    "registration_email_sources",
    default=None,
)


def parse_email_sources(value=None) -> list[str]:
    """把 EMAIL_SOURCE 解析为有序来源列表，去重并过滤空值。"""
    if value is None:
        from config import email as _email_cfg
        value = _email_cfg.EMAIL_SOURCE
    if isinstance(value, str):
        raw = value.replace(";", ",").replace("|", ",").split(",")
    elif isinstance(value, Iterable):
        raw = list(value)
    else:
        raw = [value]

    out: list[str] = []
    for item in raw:
        s = str(item or "").strip().strip('"\'')
        if not s:
            continue
        if s not in _VALID_SOURCES:
            logger.warning(f"[EmailProvider] 未知邮箱来源 {s!r}，已忽略")
            continue
        if s not in out:
            out.append(s)
    return out or ["outlook"]


def validate_email_sources(value) -> list[str]:
    """Validate an explicit, task-scoped source selection without fallback."""
    if isinstance(value, str):
        raw = value.replace(";", ",").replace("|", ",").split(",")
    elif isinstance(value, Iterable):
        raw = list(value)
    else:
        raise ValueError("邮箱渠道必须是数组或逗号分隔的文本")

    normalized: list[str] = []
    unknown: list[str] = []
    for item in raw:
        source = str(item or "").strip().strip('"\'')
        if not source:
            continue
        if source not in _VALID_SOURCES:
            unknown.append(source)
            continue
        if source not in normalized:
            normalized.append(source)
    if unknown:
        raise ValueError(f"不支持的邮箱渠道：{', '.join(unknown)}")
    if not normalized:
        raise ValueError("请至少选择一个邮箱渠道")
    return normalized


@contextmanager
def bind_email_sources(value):
    """Bind one registration task's ordered email sources to this context."""
    if value is None or (isinstance(value, str) and not value.strip()):
        sources: tuple[str, ...] = ()
    else:
        sources = tuple(validate_email_sources(value))
    token = _TASK_EMAIL_SOURCES.set(sources)
    try:
        yield list(sources)
    finally:
        _TASK_EMAIL_SOURCES.reset(token)


def automatic_email_enabled() -> bool:
    """Return whether the current task should fetch email OTP automatically."""
    task_sources = _TASK_EMAIL_SOURCES.get()
    if task_sources is not None:
        return bool(task_sources)
    try:
        from config import email as email_config

        return bool(getattr(email_config, "USE_EMAIL_SERVICE", True))
    except Exception:
        return True


def _text_config(config, name: str, default: str = "") -> str:
    return str(getattr(config, name, default) or default).strip()


def email_source_statuses(value=None) -> list[dict]:
    """Return safe, local readiness checks for the selected email sources.

    This deliberately performs no provider network request and never returns a
    credential value.  Dynamic providers are checked for required settings;
    pool providers are checked for currently available SQLite rows.
    """
    from config import email as email_config
    from core import db

    sources = parse_email_sources(value)
    statuses: list[dict] = []
    for priority, source in enumerate(sources, start=1):
        missing: list[str] = []
        available: int | None = None
        total: int | None = None
        configured = True

        if source == "outlook":
            pool = db.outlook_pool_summary()
            available = int(pool.get("available", 0) or 0)
            total = int(pool.get("total", 0) or 0)
            configured = total > 0
            if available < 1:
                missing.append("可用 Outlook 邮箱素材")
        elif source == "generic_api":
            pool = db.generic_api_email_pool_summary()
            available = int(pool.get("available", 0) or 0)
            total = int(pool.get("total", 0) or 0)
            configured = total > 0
            if available < 1:
                missing.append("可用的邮箱 + HTTP 接码地址")
        elif source == "cloudflare_domain":
            required = (
                ("EMAIL_DOMAIN", "转发域名"),
                ("QQ_EMAIL", "QQ 邮箱地址"),
                ("QQ_IMAP_PASSWORD", "QQ 邮箱 IMAP 授权码"),
            )
            missing.extend(label for key, label in required if not _text_config(email_config, key))
            configured = not missing
        elif source == "cloudflare":
            if not _text_config(email_config, "CLOUDFLARE_API_BASE"):
                missing.append("Cloudflare API 地址")
            auth_mode = _text_config(email_config, "CLOUDFLARE_AUTH_MODE", "none").lower()
            accounts_path = _text_config(email_config, "CLOUDFLARE_PATH_ACCOUNTS", "/api/new_address").lower()
            needs_key = auth_mode in {"x-admin-auth", "bearer", "x-api-key", "query-key"} or accounts_path.rstrip("/").endswith("/admin/new_address")
            if needs_key and not _text_config(email_config, "CLOUDFLARE_API_KEY"):
                missing.append("Cloudflare API Key")
            configured = not missing
        elif source == "gptmail":
            if not _text_config(email_config, "GPTMAIL_API_KEY"):
                missing.append("GPTMail API Key")
            configured = not missing
        elif source == "mailnest":
            if not _text_config(email_config, "MAIL_NEST_API_KEY"):
                missing.append("MailNest API Key")
            if not _text_config(email_config, "MAIL_NEST_PROJECT_CODE"):
                missing.append("MailNest 项目代码")
            configured = not missing
        elif source == "cloudmail":
            if not _text_config(email_config, "CLOUDMAIL_API_BASE"):
                missing.append("CloudMail API 地址")
            if not _text_config(email_config, "CLOUDMAIL_AUTH_TOKEN"):
                missing.append("CloudMail Token")
            configured = not missing

        ready = not missing
        if ready and available is not None:
            message = f"可用 {available} 个，共 {total} 个"
        elif ready:
            message = "配置完整，任务启动时自动领取邮箱并收取 OTP"
        elif source in {"outlook", "generic_api"}:
            message = "请导入 " + "、".join(missing)
        else:
            message = "请填写 " + "、".join(missing)
        statuses.append({
            "id": source,
            "label": EMAIL_SOURCE_LABELS[source],
            "priority": priority,
            "kind": _SOURCE_KINDS[source],
            "requires_import": source in {"outlook", "generic_api"},
            "configured": configured,
            "ready": ready,
            "available": available,
            "total": total,
            "missing": missing,
            "message": message,
        })
    return statuses


def registration_email_status(*, include_all: bool = True) -> dict:
    """Return the complete safe runtime status consumed by the WebUI."""
    from config import email as email_config
    from config import register as register_config

    automatic = bool(getattr(email_config, "USE_EMAIL_SERVICE", True))
    sources = parse_email_sources(getattr(email_config, "EMAIL_SOURCE", None))
    if include_all:
        all_channels = email_source_statuses(_VALID_SOURCES)
        status_by_id = {item["id"]: item for item in all_channels}
        channels = []
        for priority, source in enumerate(sources, start=1):
            item = dict(status_by_id[source])
            item["priority"] = priority
            item["enabled"] = True
            channels.append(item)
        for item in all_channels:
            item["enabled"] = item["id"] in sources
            item["priority"] = sources.index(item["id"]) + 1 if item["enabled"] else None
    else:
        channels = email_source_statuses(sources)
        for item in channels:
            item["enabled"] = True
        all_channels = channels
    usable_sources = [item["id"] for item in channels if item["ready"]]
    manual_email = _text_config(register_config, "REGISTER_EMAIL")
    manual_configured = bool(manual_email and "@" in manual_email)
    return {
        "automatic": automatic,
        "sources": sources,
        "channels": channels,
        "all_channels": all_channels,
        "usable_sources": usable_sources,
        "ready_sources": [item["id"] for item in all_channels if item["ready"]],
        "ready": bool(usable_sources) if automatic else manual_configured,
        "manual_configured": manual_configured,
    }


def _pick_from_source(source: str) -> str:
    if source == "gptmail":
        from core.gptmail_client import pick_account
        return pick_account().email
    if source == "cloudflare":
        from core.cf_temp_mail_client import pick_account
        return pick_account().email
    if source == "cloudflare_domain":
        from core.qqmail_client import pick_domain_email
        return pick_domain_email()
    if source == "generic_api":
        from core.generic_api_mail_client import pick_account
        return pick_account().email
    if source == "mailnest":
        from core.mailnest_client import pick_account
        return pick_account().email
    if source == "cloudmail":
        from core.cloudmail_client import pick_account
        return pick_account().email
    from core.outlook_client import pick_account
    return pick_account().email


def acquire_email(value=None) -> str:
    """根据 EMAIL_SOURCE 领取一个用于注册的邮箱地址；多个来源时按顺序兜底。"""
    sources = parse_email_sources(value)
    last_exc: Exception | None = None
    for source in sources:
        try:
            email = _pick_from_source(source)
            logger.info(f"[EmailProvider] 使用邮箱来源: {source}, email={email}")
            return email
        except Exception as exc:
            last_exc = exc
            logger.warning(f"[EmailProvider] 来源 {source} 领取邮箱失败: {type(exc).__name__}: {exc}")
            continue
    raise RuntimeError(f"所有邮箱来源均领取失败: {sources}; last={last_exc}")


def resolve_email_source(email: str) -> str:
    """根据邮箱在各池中的归属判断实际来源。"""
    from core.gptmail_client import get_account_context as get_gptmail_context
    if get_gptmail_context(email):
        return "gptmail"
    from core.cf_temp_mail_client import get_account_context as get_cf_context
    if get_cf_context(email):
        return "cloudflare"
    from core.mailnest_client import get_account_context as get_mailnest_context
    if get_mailnest_context(email):
        return "mailnest"
    from core.cloudmail_client import get_account_context as get_cloudmail_context
    if get_cloudmail_context(email):
        return "cloudmail"

    from core import db
    if db.get_generic_api_email_by_email(email):
        return "generic_api"
    if db.get_outlook_by_email(email):
        return "outlook"
    registered = db.get_account_by_email(email)
    registered_source = str((registered or {}).get("email_source") or "").strip().lower()
    if registered_source in _VALID_SOURCES:
        return registered_source
    if db._find_domain_email(db._load_domain_pool(), email):  # 内部轻量查询，仅本项目使用
        return "cloudflare_domain"
    # 兜底：如果域名匹配 EMAIL_DOMAIN，则按域名邮箱处理
    try:
        from config import email as _email_cfg
        domain = (_email_cfg.EMAIL_DOMAIN or "").lower().strip()
        if domain and domain != "-" and email.lower().endswith("@" + domain):
            return "cloudflare_domain"
    except Exception:
        pass
    return parse_email_sources()[0]


def snapshot_email_context(email: str, source: str | None = None) -> dict:
    """Return the minimum provider state needed to fetch later OTP messages.

    Most providers can reopen a mailbox from the email address plus global
    configuration. Cloudflare Worker mailboxes are the exception: their JWT is
    issued per address, so it must travel with the registered account.
    """
    resolved = str(source or resolve_email_source(email) or "").strip().lower()
    if resolved == "cloudflare":
        from core.cf_temp_mail_client import get_account_context

        account = get_account_context(email)
        if account:
            return {
                "jwt": str(account.jwt or ""),
                "domain": str(account.domain or ""),
                "created_at": float(account.created_at or 0),
            }
    if resolved == "mailnest":
        from core.mailnest_client import get_account_context

        account = get_account_context(email)
        if account and account.project_code:
            return {"project_code": str(account.project_code)}
    if resolved == "cloudmail":
        from core.cloudmail_client import get_account_context

        account = get_account_context(email)
        if account and account.domain:
            return {"domain": str(account.domain)}
    return {}


def _restore_email_context(email: str, source: str, context: dict | None) -> None:
    """Restore process-local state for providers with per-mailbox credentials."""
    if source != "cloudflare" or not isinstance(context, dict):
        return
    jwt = str(context.get("jwt") or "").strip()
    if not jwt:
        return
    from core import cf_temp_mail_client as cloudflare_mail

    cloudflare_mail._CONTEXT_CACHE[cloudflare_mail._cache_key(email)] = cloudflare_mail.CFTempMailAccount(
        email=email,
        jwt=jwt,
        domain=str(context.get("domain") or ""),
        created_at=float(context.get("created_at") or 0),
    )


def wait_for_otp(
    email: str,
    after_ts: float,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
    source: str | None = None,
    context: dict | None = None,
) -> str:
    """等待并返回该邮箱最新的 ChatGPT OTP（6 位数字字符串）。

    USE_EMAIL_SERVICE=False 时走手动验证码通道（WebUI 提交 / CLI 输入），
    不再强制要求 Outlook clientId/refreshToken。
    """
    explicit_source = bool(str(source or "").strip())
    use_service = automatic_email_enabled()

    if not use_service and not explicit_source:
        from core.manual_otp import wait_for_manual_otp
        from config import email as _email_cfg
        timeout = int(max_wait if max_wait is not None else (getattr(_email_cfg, "OTP_MAX_WAIT", 180) or 180))
        job_id = None
        try:
            from registration.application import job_service as svc
            job_id = getattr(svc._THREAD_CTX, "job_id", None)
        except Exception:
            job_id = None
        return wait_for_manual_otp(email, timeout=timeout, job_id=job_id)

    extra_kwargs = {}
    if max_wait is not None:
        extra_kwargs["max_wait"] = max_wait
    if poll_interval is not None:
        extra_kwargs["poll_interval"] = poll_interval
    if settle_seconds is not None:
        extra_kwargs["settle_seconds"] = settle_seconds

    source = str(source or resolve_email_source(email) or "").strip().lower()
    if source not in _VALID_SOURCES:
        raise RuntimeError(f"未知邮箱来源，无法自动取码: {source or '空'}")
    _restore_email_context(email, source, context)
    if source == "gptmail":
        from core.gptmail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    if source == "cloudflare":
        from core.cf_temp_mail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    if source == "cloudflare_domain":
        from core.qqmail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    if source == "generic_api":
        from core.generic_api_mail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    if source == "mailnest":
        from core.mailnest_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    if source == "cloudmail":
        from core.cloudmail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    from core.outlook_client import fetch_latest_otp
    return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)


def release_email(email: str, status: str = "available", note: str | None = None) -> str:
    """按邮箱实际来源回收状态，返回来源名。"""
    source = resolve_email_source(email)
    if source == "gptmail":
        from core.gptmail_client import release_account
        release_account(email, status=status, note=note)
    elif source == "cloudflare":
        from core.cf_temp_mail_client import release_account
        release_account(email, status=status, note=note)
    elif source == "cloudflare_domain":
        from core.qqmail_client import release_domain_email
        release_domain_email(email, status=status, note=note)
    elif source == "generic_api":
        from core.generic_api_mail_client import release_account
        release_account(email, status=status, note=note)
    elif source == "mailnest":
        from core.mailnest_client import release_account
        release_account(email, status=status, note=note)
    elif source == "cloudmail":
        from core.cloudmail_client import release_account
        release_account(email, status=status, note=note)
    else:
        from core.outlook_client import release_account
        release_account(email, status=status, note=note)
    return source


def release_email_if_unconsumed(email: str, note: str | None = None) -> bool:
    """回收仍停留在 used 的任务领取，且绝不覆盖已注册/已判废状态。"""
    if not (email or "").strip():
        return False

    source = resolve_email_source(email)
    from core import db

    if source == "outlook":
        changed = db.release_unconsumed_outlook(email, note=note)
    elif source == "generic_api":
        changed = db.release_unconsumed_generic_api_email(email, note=note)
    elif source == "cloudflare_domain":
        changed = db.release_unconsumed_domain_email(email, note=note)
    else:
        # 临时邮箱不重新进入本地池，只清理进程上下文；已有本地账号时保留上下文。
        if db.get_account_by_email(email) is not None:
            return False
        release_email(email, status="available", note=note)
        changed = True

    if changed:
        logger.info("[EmailProvider] 已回收未消耗邮箱: source=%s, email=%s", source, email)
    return changed
