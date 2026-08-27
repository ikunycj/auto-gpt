# -*- coding: utf-8 -*-
"""Unified GPT account projection.

The registration workflow and the Codex relay predate one another and keep
their records in separate SQLite collections.  This module deliberately keeps
those stores independent while exposing one read model for the WebUI.  Email
is the only common identity available in legacy data, so the normalized email
is retained in every row together with both source IDs.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from core import codex_relay_service as relay
from core import db
from core import sqlite_store


_REG_ACTIVE = {"pending", "running", "stopping"}
_RELAY_ACTIVE = {
    "pending",
    "running",
    "stopping",
    "waiting_email",
    "waiting_sms",
    "waiting_totp",
    "waiting_browser",
}
_MAINTENANCE_LABELS = {
    "enable_2fa": "正在开启 2FA",
    "check_liveness": "正在验活账号",
    "check_email_liveness": "正在验活邮箱",
    "check_gpt_liveness": "正在验活 GPT",
    "check_quota": "正在查询限额",
    "check_sub2_status": "正在查询 sub2 状态",
    "refresh_sub2": "正在刷新 sub2 状态",
}
_DELETIONS_KEY = Path(__file__).resolve().parent.parent / "gpt_account_deletions.json"
_DELETIONS_COLLECTION = "gpt_account_deletions"


class AccountBusyError(ValueError):
    """Raised when a running account operation prevents soft deletion."""


def _deletion_rows() -> list[dict]:
    rows = sqlite_store.read_json(
        _DELETIONS_KEY,
        [],
        collection=_DELETIONS_COLLECTION,
    )
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _deleted_emails() -> set[str]:
    return {_email(row.get("email")) for row in _deletion_rows() if _email(row.get("email"))}


def restore_deleted_accounts(emails: list[str] | tuple[str, ...] | set[str]) -> dict:
    """Restore soft-deleted rows when the user explicitly imports them again.

    Soft deletion is intentionally retained as a separate collection so source
    accounts and their logs remain recoverable.  Re-importing the same email is
    an explicit restore signal; it should therefore remove only matching
    deletion markers and leave every other marker untouched.
    """
    targets = {_email(value) for value in (emails or ()) if _email(value)}
    if not targets:
        return {"restored": 0, "emails": []}

    restored: list[str] = []

    def update(current: Any) -> list[dict]:
        existing = [row for row in current if isinstance(row, dict)] if isinstance(current, list) else []
        kept: list[dict] = []
        for row in existing:
            email = _email(row.get("email"))
            if email in targets:
                restored.append(email)
            else:
                kept.append(row)
        return kept

    sqlite_store.update_json(
        _DELETIONS_KEY,
        update,
        default=[],
        collection=_DELETIONS_COLLECTION,
        mode=0o600,
        mirror=False,
    )
    restored_emails = sorted(set(restored))
    return {"restored": len(restored_emails), "emails": restored_emails}


def _email(value: Any) -> str:
    return str(value or "").strip().lower()


def _stamp(value: Any) -> str:
    return str(value or "").strip()


def _extra(registered: dict | None) -> dict:
    """Decode the optional registration archive metadata safely."""
    raw = (registered or {}).get("extra_json")
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _first(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _registered_material(registered: dict | None) -> dict:
    """Extract ChatGPT/mailbox credentials from both old and new DB shapes."""
    registered = registered or {}
    extra = _extra(registered)
    # Registration drivers keep the generated OpenAI password in extra_json;
    # older imported rows may instead expose it directly as password.
    chatgpt_password = _first(
        registered.get("chatgpt_password"),
        registered.get("openai_password"),
        extra.get("registration_password"),
        extra.get("openai_password"),
    )
    client_id = _first(registered.get("outlook_client_id"), registered.get("client_id"))
    refresh_token = _first(registered.get("outlook_refresh_token"), registered.get("refresh_token"))
    mailbox_password = _first(
        registered.get("mailbox_password"),
        registered.get("email_password"),
        # The compact registered row historically stored the Outlook password
        # in ``password`` when client/refresh fields are present.
        registered.get("password") if client_id and refresh_token else "",
    )
    return {
        "chatgpt_password": chatgpt_password,
        "mailbox_password": mailbox_password,
        "outlook_client_id": client_id,
        "outlook_refresh_token": refresh_token,
        "email_code_url": _first(registered.get("email_code_url")),
        "totp_secret": _first(registered.get("totp_secret"), extra.get("totp_secret")),
        "login_method": _first(registered.get("login_method"), extra.get("login_method")),
        "email_provider": _first(registered.get("email_source"), extra.get("email_provider")),
        "email_provider_context": extra.get("email_provider_context") if isinstance(extra.get("email_provider_context"), dict) else {},
    }


def _sort_key(row: dict) -> tuple[str, int, str]:
    """Sort mixed UUID/int jobs without assuming timestamps are present."""
    stamp = _stamp(row.get("created_at") or row.get("started_at") or row.get("updated_at"))
    try:
        numeric = int(row.get("id") or 0)
    except (TypeError, ValueError):
        numeric = 0
    return stamp, numeric, str(row.get("id") or "")


def _latest(rows: list[dict]) -> dict | None:
    return max(rows, key=_sort_key) if rows else None


def _registration_status(reg_account: dict | None, job: dict | None, relay_account: dict | None) -> str:
    if reg_account:
        return "registered"
    if job and str(job.get("status") or "") in _REG_ACTIVE:
        return "registering"
    if job and job.get("account_id") is not None and str(job.get("status") or "") in {"success", "failed", "stopped", "cancelled"}:
        # Registration saves the local account before a possible Codex
        # failure, so a terminal job carrying account_id still means GPT
        # registration completed.
        return "registered"
    if job and str(job.get("status") or "") == "success":
        return "registered"
    # A relay record with a password or a completed OAuth/liveness check is an
    # existing GPT account even when it predates the registered_accounts table.
    if relay_account and (
        relay_account.get("chatgpt_password")
        or str(relay_account.get("codex_status") or "") == "authorized"
        or str(relay_account.get("liveness_status") or "") == "alive"
    ):
        return "registered"
    if job and str(job.get("status") or "") == "failed":
        return "failed"
    # A user stop/cancellation is not evidence that account registration
    # failed. Keep it eligible for another registration attempt.
    if job and str(job.get("status") or "") in {"stopped", "cancelled"}:
        return "unregistered"
    return "unregistered"


def _codex_status(account: dict | None, registered: dict | None, job: dict | None) -> str:
    if job:
        status = str(job.get("status") or "")
        if status in _RELAY_ACTIVE:
            return "authorizing"
        if status == "success":
            return "authorized"
        if status in {"failed", "stopped", "cancelled"}:
            return "failed"
    raw = str((account or {}).get("codex_status") or "").strip().lower()
    if raw in {"authorized", "success"}:
        return "authorized"
    if raw in {"failed", "deactivated", "deleted"}:
        return "failed"
    raw_registered = str((registered or {}).get("codex_status") or "").strip().lower()
    if raw_registered in {"authorized", "success"}:
        return "authorized"
    if raw_registered in {"authorizing", "pending", "running", "retrying"}:
        return "authorizing"
    if raw_registered == "failed":
        return "failed"
    return "unauthorized"


def _is_sms_job(job: dict | None) -> bool:
    if not job:
        return False
    stage = str(job.get("stage") or "").lower()
    status = str(job.get("status") or "").lower()
    message = str(job.get("message") or "").lower()
    error = str(job.get("error") or "").lower()
    return (
        stage in {"sms", "phone"}
        or status == "waiting_sms"
        or "短信" in message
        or "手机" in message
        or "sms" in error
        or "phone" in error
    )


def _phone_status(account: dict | None, job: dict | None) -> str:
    account = account or {}
    if job and str(job.get("status") or "") in _RELAY_ACTIVE and (
        str(job.get("status") or "") == "waiting_sms"
        or str(job.get("stage") or "").lower() in {"sms", "phone"}
    ):
        return "verifying"
    if job and str(job.get("status") or "") in {"failed", "stopped", "cancelled"} and _is_sms_job(job):
        return "failed"
    if account.get("phone_verified_at") and (
        (account.get("phone") and account.get("sms_code_url"))
        or (account.get("last_sms_phone") and account.get("last_sms_provider"))
    ):
        return "verified"
    return "unverified"


def _job_public(job: dict | None, kind: str) -> dict | None:
    if not job:
        return None
    if kind in {"registration", "codex_retry"}:
        job_id = job.get("id")
        return {
            "id": job_id,
            "kind": kind,
            "status": job.get("status") or "",
            "email": job.get("email") or "",
            "created_at": job.get("created_at") or "",
            "started_at": job.get("started_at") or "",
            "completed_at": job.get("completed_at") or "",
            "message": job.get("error_message") or "",
            "error": job.get("error_message") or "",
            "log_endpoint": f"/api/jobs/{job_id}/log",
        }
    job_id = job.get("id")
    return {
        "id": job_id,
        "kind": kind,
        "action": job.get("action") or "",
        "status": job.get("status") or "",
        "stage": job.get("stage") or "",
        "email": job.get("email") or "",
        "created_at": job.get("created_at") or "",
        "started_at": job.get("started_at") or "",
        "completed_at": job.get("completed_at") or "",
        "message": job.get("message") or "",
        "error": job.get("error") or "",
        "waiting_since": job.get("waiting_since") or "",
        "browser_url": job.get("browser_url") or "",
        "log_endpoint": f"/api/codex-relay/jobs/{job_id}/log",
    }


def _log_ref(*jobs: tuple[dict | None, str]) -> dict | None:
    candidates = []
    for job, kind in jobs:
        if job:
            candidates.append((str(job.get("created_at") or job.get("started_at") or ""), _job_public(job, kind)))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _source_times(relay_account: dict | None, registered: dict | None) -> tuple[str, str]:
    values_created = [_stamp((relay_account or {}).get("created_at")), _stamp((registered or {}).get("created_at"))]
    values_updated = [
        _stamp((relay_account or {}).get("updated_at")),
        _stamp((registered or {}).get("updated_at")),
    ]
    created = min((x for x in values_created if x), default="")
    updated = max((x for x in values_updated if x), default=created)
    return created, updated


def _build_row(
    email: str,
    relay_account: dict | None,
    registered: dict | None,
    reg_job: dict | None,
    relay_job: dict | None,
    retry_job: dict | None = None,
    latest_relay_job: dict | None = None,
    active_relay_job: dict | None = None,
) -> dict:
    relay_account = relay_account or {}
    registered = registered or {}
    registered_material = _registered_material(registered)
    registration_status = _registration_status(registered or None, reg_job, relay_account or None)
    if retry_job and registration_status == "unregistered":
        registration_status = "registered"
    codex_source_job = _latest([job for job in (relay_job, retry_job) if job])
    codex_source_kind = (
        "codex" if codex_source_job is relay_job and relay_job else
        "codex_retry" if codex_source_job is retry_job and retry_job else
        ""
    )
    codex_status = _codex_status(relay_account or None, registered or None, codex_source_job)
    phone_status = _phone_status(relay_account or None, relay_job)
    verified_phone = phone_status == "verified"
    phone_number = str(relay_account.get("phone") or relay_account.get("last_sms_phone") or "") if verified_phone else ""
    sms_url = str(relay_account.get("sms_code_url") or "") if verified_phone else ""
    sms_source = str(relay_account.get("last_sms_provider") or "") if verified_phone else ""
    created_at, updated_at = _source_times(relay_account, registered)
    job_stamps = [
        _stamp(job.get("created_at")) for job in (reg_job, relay_job, retry_job, latest_relay_job)
        if job and _stamp(job.get("created_at"))
    ]
    updated_stamps = [
        _stamp(job.get("completed_at") or job.get("started_at") or job.get("created_at"))
        for job in (reg_job, relay_job, retry_job, latest_relay_job)
        if job and _stamp(job.get("completed_at") or job.get("started_at") or job.get("created_at"))
    ]
    if not created_at and job_stamps:
        created_at = min(job_stamps)
    if updated_stamps:
        updated_at = max([value for value in (_stamp(updated_at), *updated_stamps) if value], default=created_at)
    relay_id = str(relay_account.get("id") or "")
    registered_id = registered.get("id")
    password = _first(relay_account.get("chatgpt_password"), registered_material.get("chatgpt_password"))
    # Preserve the legacy value for imported registered rows that have no
    # distinct OpenAI password.  It is still useful for display and can be
    # replaced from the account editor once a real login password is known.
    if not password and not registered_material.get("mailbox_password"):
        password = _first(registered.get("password"))
    totp = _first(relay_account.get("totp_secret"), registered_material.get("totp_secret"))
    login_method = _first(
        relay_account.get("login_method"),
        registered_material.get("login_method"),
        "password" if password else "email_otp",
    )
    mailbox_password = _first(relay_account.get("mailbox_password"), registered_material.get("mailbox_password"))
    outlook_client_id = _first(relay_account.get("outlook_client_id"), registered_material.get("outlook_client_id"))
    outlook_refresh_token = _first(relay_account.get("outlook_refresh_token"), registered_material.get("outlook_refresh_token"))
    email_provider = _first(
        relay_account.get("email_provider"),
        registered_material.get("email_provider"),
        registered.get("email_source"),
        "outlook" if outlook_client_id and outlook_refresh_token else "",
        "generic_api" if relay_account.get("email_code_url") or registered_material.get("email_code_url") else "",
    )
    try:
        from core.email_provider import EMAIL_SOURCE_LABELS

        email_provider_label = _first(
            relay_account.get("email_provider_label"),
            EMAIL_SOURCE_LABELS.get(email_provider),
        )
    except Exception:
        email_provider_label = _first(relay_account.get("email_provider_label"), email_provider)
    gpt_status = str(relay_account.get("liveness_status") or registered.get("live_check_status") or "unknown")
    plan = str(relay_account.get("quota_plan") or registered.get("current_plan_type") or registered.get("plan_type") or "")
    note = str(relay_account.get("note") or registered.get("note") or "")
    latest_relay_ref = latest_relay_job or relay_job
    latest_relay_kind = "maintenance" if (latest_relay_ref or {}).get("action") else "codex"
    maintenance_job = active_relay_job if (active_relay_job or {}).get("action") else None
    maintenance_action = str((maintenance_job or {}).get("action") or "")
    if registration_status == "registering":
        active_operation = "registering"
        active_operation_label = "注册任务运行中"
    elif phone_status == "verifying":
        active_operation = "verifying"
        active_operation_label = "正在等待短信"
    elif codex_status == "authorizing":
        active_operation = "authorizing"
        active_operation_label = "Codex 授权运行中"
    elif maintenance_job:
        active_operation = "maintenance"
        active_operation_label = _MAINTENANCE_LABELS.get(
            maintenance_action,
            str(maintenance_job.get("message") or "账号维护任务运行中"),
        )
    else:
        active_operation = ""
        active_operation_label = ""
    row = {
        "id": relay_id or (f"registered:{registered_id}" if registered_id is not None else email),
        "email": email,
        "relay_account_id": relay_id,
        "registered_account_id": registered_id,
        "password": password,
        "chatgpt_password": password,
        "totp_secret": totp,
        "email_code_url": _first(relay_account.get("email_code_url"), registered_material.get("email_code_url")),
        "mailbox_password": mailbox_password,
        "outlook_client_id": outlook_client_id,
        "outlook_refresh_token": outlook_refresh_token,
        "registration_status": registration_status,
        "registration_status_raw": str(reg_job.get("status") or "") if reg_job else "",
        "codex_status": codex_status,
        "codex_status_raw": str(relay_account.get("codex_status") or registered.get("codex_status") or ""),
        "phone_status": phone_status,
        "phone": f"{phone_number}----{sms_url or sms_source}" if verified_phone else "",
        "phone_number": phone_number,
        "sms_code_url": sms_url,
        "sms_provider": sms_source or str(relay_account.get("sms_provider") or ""),
        "gpt_status": gpt_status,
        "liveness_status": gpt_status,
        "quota_plan": plan,
        "plan": plan,
        "note": note,
        "created_at": created_at,
        "updated_at": updated_at,
        "registration_job_id": reg_job.get("id") if reg_job else None,
        "codex_job_id": codex_source_job.get("id") if codex_source_job else None,
        "codex_job_kind": codex_source_kind,
        "registration_job": _job_public(reg_job, "registration"),
        "codex_job": _job_public(codex_source_job, codex_source_kind) if codex_source_job else None,
        "maintenance_job": _job_public(maintenance_job, "maintenance") if maintenance_job else None,
        "latest_log": _log_ref(
            (reg_job, "registration"),
            (retry_job, "codex_retry"),
            (latest_relay_ref, latest_relay_kind),
        ),
        "logs": [
            ref for ref in (
                _job_public(reg_job, "registration"),
                _job_public(retry_job, "codex_retry"),
                _job_public(latest_relay_ref, latest_relay_kind),
            ) if ref
        ],
        "log_count": int(bool(reg_job)) + int(bool(retry_job)) + int(bool(latest_relay_ref)),
        "active_operation": active_operation,
        "active_operation_label": active_operation_label,
        "email_provider": email_provider,
        "email_provider_label": email_provider_label,
        "has_password": bool(password),
        "has_totp": bool(totp),
        "login_method": login_method,
        "phone_verified_at": relay_account.get("phone_verified_at") or "",
        "quota_status": relay_account.get("quota_status") or "unknown",
        "codex_authorized_at": relay_account.get("codex_authorized_at") or "",
    }
    return row


def list_accounts(
    *,
    q: str = "",
    registration_status: str = "",
    codex_status: str = "",
    phone_status: str = "",
    gpt_status: str = "",
    provider: str = "",
) -> list[dict]:
    """Return the canonical GPT account read model, filtered before paging."""
    relay_rows = relay.list_accounts()
    relay_by_email: dict[str, dict] = {}
    for row in relay_rows:
        key = _email(row.get("email"))
        if key and key not in relay_by_email:
            relay_by_email[key] = row
    try:
        registered_rows = db.list_accounts(limit=1_000_000, archived="all")
    except Exception:
        registered_rows = []
    registered_by_email: dict[str, dict] = {}
    for row in registered_rows:
        key = _email(row.get("email"))
        if key and key not in registered_by_email:
            registered_by_email[key] = row
    try:
        registration_jobs = db.list_jobs(limit=1_000_000)
    except Exception:
        registration_jobs = []
    try:
        generic_pool = db.list_generic_api_email_pool(limit=1_000_000)
    except Exception:
        generic_pool = []
    try:
        outlook_pool = db.list_outlook_pool(limit=1_000_000)
    except Exception:
        outlook_pool = []
    generic_by_email = {_email(row.get("email")): row for row in generic_pool if _email(row.get("email"))}
    outlook_by_email = {_email(row.get("email")): row for row in outlook_pool if _email(row.get("email"))}
    relay_jobs = relay.list_jobs()

    reg_by_email: dict[str, list[dict]] = {}
    retry_by_email: dict[str, list[dict]] = {}
    all_registration_by_email: dict[str, list[dict]] = {}
    for job in registration_jobs:
        key = _email(job.get("email"))
        if not key:
            continue
        all_registration_by_email.setdefault(key, []).append(job)
        job_type = str(job.get("job_type") or "registration")
        if job_type == "registration":
            reg_by_email.setdefault(key, []).append(job)
        elif job_type == "codex_retry":
            retry_by_email.setdefault(key, []).append(job)
    codex_by_key: dict[str, list[dict]] = {}
    all_relay_by_key: dict[str, list[dict]] = {}
    for job in relay_jobs:
        key = _email(job.get("email"))
        if key:
            all_relay_by_key.setdefault(key, []).append(job)
            if not job.get("action"):
                codex_by_key.setdefault(key, []).append(job)

    keys = (
        set(relay_by_email)
        | set(registered_by_email)
        | set(all_registration_by_email)
        | set(all_relay_by_key)
    ) - _deleted_emails()
    rows = []
    for key in keys:
        reg_job = _latest(reg_by_email.get(key, []))
        relay_account = relay_by_email.get(key)
        relay_id = str((relay_account or {}).get("id") or "")
        relay_candidates = codex_by_key.get(key, [])
        if relay_id:
            relay_candidates = [job for job in relay_jobs if not job.get("action") and str(job.get("account_id") or "") == relay_id] or relay_candidates
        relay_job = _latest(relay_candidates)
        retry_job = _latest(retry_by_email.get(key, []))
        all_relay_jobs = all_relay_by_key.get(key, [])
        latest_relay_job = _latest(all_relay_jobs)
        active_relay_job = _latest([
            job for job in all_relay_jobs
            if str(job.get("status") or "").lower() in _RELAY_ACTIVE
        ])
        registered = registered_by_email.get(key)
        if registered:
            # The registered account table intentionally omits mailbox API
            # material from its compact projection.  Fill it from the source
            # pool so the unified row can still display/target registration.
            registered = dict(registered)
            generic = generic_by_email.get(key)
            outlook = outlook_by_email.get(key)
            if generic:
                if not registered.get("email_code_url"):
                    registered["email_code_url"] = generic.get("code_url") or ""
            if outlook:
                if not registered.get("password"):
                    registered["password"] = outlook.get("password") or ""
                if not registered.get("mailbox_password"):
                    registered["mailbox_password"] = outlook.get("password") or ""
                if not registered.get("outlook_client_id"):
                    registered["outlook_client_id"] = outlook.get("client_id") or ""
                if not registered.get("outlook_refresh_token"):
                    registered["outlook_refresh_token"] = outlook.get("refresh_token") or ""
        rows.append(_build_row(
            key,
            relay_account,
            registered,
            reg_job,
            relay_job,
            retry_job,
            latest_relay_job,
            active_relay_job,
        ))

    q = _email(q) if q else ""
    registration_status = str(registration_status or "").strip().lower()
    codex_status = str(codex_status or "").strip().lower()
    phone_status = str(phone_status or "").strip().lower()
    gpt_status = str(gpt_status or "").strip().lower()
    provider = str(provider or "").strip().lower()
    if q:
        rows = [row for row in rows if q in str(row.get("email") or "").lower() or q in str(row.get("note") or "").lower()]
    if registration_status:
        aliases = {"not_registered": "unregistered", "registration_failed": "failed", "registered": "registered", "registering": "registering"}
        target = aliases.get(registration_status, registration_status)
        rows = [row for row in rows if row.get("registration_status") == target]
    if codex_status:
        aliases = {"not_authorized": "unauthorized", "reauthorize": "unauthorized", "deactivated": "failed"}
        target = aliases.get(codex_status, codex_status)
        rows = [row for row in rows if row.get("codex_status") == target]
    if phone_status:
        aliases = {"verified": "verified", "bound": "verified", "unverified": "unverified", "unbound": "unverified"}
        rows = [row for row in rows if row.get("phone_status") == aliases.get(phone_status, phone_status)]
    if gpt_status:
        rows = [row for row in rows if str(row.get("gpt_status") or "").lower() == gpt_status]
    if provider:
        rows = [row for row in rows if str(row.get("email_provider") or "").lower() == provider]
    return sorted(rows, key=lambda row: (str(row.get("updated_at") or ""), str(row.get("email") or "")), reverse=True)


def get_account(account_id: str) -> dict | None:
    target = str(account_id or "")
    rows = list_accounts()
    return next((row for row in rows if str(row.get("id") or "") == target or str(row.get("relay_account_id") or "") == target), None)


def soft_delete_accounts(account_ids: list[str]) -> dict:
    """Hide unified GPT accounts while retaining every source record and log."""
    requested = list(dict.fromkeys(
        str(value).strip() for value in (account_ids or []) if str(value).strip()
    ))
    if not requested:
        raise ValueError("account_ids 必须是非空数组")

    rows = list_accounts()
    aliases: dict[str, dict] = {}
    for row in rows:
        values = {
            str(row.get("id") or "").strip(),
            str(row.get("relay_account_id") or "").strip(),
            str(row.get("email") or "").strip().lower(),
        }
        registered_id = row.get("registered_account_id")
        if registered_id is not None and str(registered_id).strip():
            values.add(f"registered:{registered_id}")
            values.add(str(registered_id).strip())
        for value in values:
            if value:
                aliases.setdefault(value, row)

    selected: list[dict] = []
    selected_emails: set[str] = set()
    skipped: list[dict] = []
    for account_id in requested:
        row = aliases.get(account_id) or aliases.get(account_id.lower())
        if row is None:
            skipped.append({"id": account_id, "reason": "账号不存在或已删除"})
            continue
        email = _email(row.get("email"))
        if not email or email in selected_emails:
            continue
        selected.append(row)
        selected_emails.add(email)

    if not selected:
        raise LookupError("所选 GPT 账号不存在或已删除")

    busy = [
        str(row.get("email") or "")
        for row in selected
        if row.get("active_operation")
        or row.get("registration_status") == "registering"
        or row.get("codex_status") == "authorizing"
        or row.get("phone_status") == "verifying"
    ]
    if busy:
        raise AccountBusyError("以下账号仍有任务运行，请先停止任务：" + "、".join(busy[:3]))

    deleted_at = datetime.now().isoformat(timespec="seconds")
    items = [
        {
            "id": str(row.get("id") or ""),
            "email": _email(row.get("email")),
            "relay_account_id": str(row.get("relay_account_id") or ""),
            "registered_account_id": row.get("registered_account_id"),
            "deleted_at": deleted_at,
        }
        for row in selected
    ]

    def update(current: Any) -> list[dict]:
        existing = [row for row in current if isinstance(row, dict)] if isinstance(current, list) else []
        by_email = {_email(row.get("email")): dict(row) for row in existing if _email(row.get("email"))}
        for item in items:
            by_email[item["email"]] = item
        return sorted(by_email.values(), key=lambda row: str(row.get("deleted_at") or ""))

    sqlite_store.update_json(
        _DELETIONS_KEY,
        update,
        default=[],
        collection=_DELETIONS_COLLECTION,
        mode=0o600,
        mirror=False,
    )
    deleted_count = len(items)
    message = f"已软删除 {deleted_count} 个 GPT 账号（账号数据与日志已保留）"
    if skipped:
        message += f"，跳过 {len(skipped)} 个"
    return {
        "ok": True,
        "deleted": deleted_count,
        "deleted_count": deleted_count,
        "items": items,
        "skipped": skipped,
        "message": message,
    }


def authorization_material(row: dict) -> dict:
    """Return the minimal internal material needed to create a Relay row."""
    if not isinstance(row, dict):
        raise ValueError("GPT账号不存在")
    email = _email(row.get("email"))
    if not email:
        raise ValueError("GPT账号缺少邮箱")
    registered_source = None
    registered_id = row.get("registered_account_id")
    if registered_id not in (None, ""):
        try:
            registered_source = db.get_account(int(registered_id))
        except (TypeError, ValueError):
            registered_source = None
    registered_material = _registered_material(registered_source or row)
    chatgpt_password = _first(row.get("chatgpt_password"), registered_material.get("chatgpt_password"))
    has_outlook_material = bool(
        _first(row.get("mailbox_password"), registered_material.get("mailbox_password"))
        and _first(row.get("outlook_client_id"), registered_material.get("outlook_client_id"))
        and _first(row.get("outlook_refresh_token"), registered_material.get("outlook_refresh_token"))
    )
    if not chatgpt_password and not has_outlook_material:
        chatgpt_password = _first(row.get("password"))
    material = {
        "email": email,
        "chatgpt_password": chatgpt_password,
        "mailbox_password": _first(row.get("mailbox_password"), registered_material.get("mailbox_password")),
        "outlook_client_id": _first(row.get("outlook_client_id"), registered_material.get("outlook_client_id")),
        "outlook_refresh_token": _first(row.get("outlook_refresh_token"), registered_material.get("outlook_refresh_token")),
        "email_code_url": _first(row.get("email_code_url"), registered_material.get("email_code_url")),
        "totp_secret": _first(row.get("totp_secret"), registered_material.get("totp_secret")),
        "login_method": _first(row.get("login_method"), registered_material.get("login_method")),
        "email_provider": _first(row.get("email_provider"), registered_material.get("email_provider")),
        "email_provider_context": registered_material.get("email_provider_context") or {},
        "note": _first(row.get("note")),
        "codex_status": row.get("codex_status") or "not_authorized",
    }
    # Synthetic registered rows should not copy an unverified phone candidate
    # into the Relay account.  The phone pool assigns a number only when OAuth
    # actually asks for SMS, and the projection exposes it after verification.
    if row.get("relay_account_id"):
        material["phone"] = _first(row.get("phone_number"))
        material["sms_code_url"] = _first(row.get("sms_code_url"))
    return material


def list_gpt_accounts(**filters) -> list[dict]:
    """Explicit alias used by callers that want to distinguish this read model
    from the legacy ``registered_accounts`` and ``relay_accounts`` lists.
    """
    return list_accounts(**filters)
