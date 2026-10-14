# -*- coding: utf-8 -*-
"""GPT account workspace backed exclusively by ``gpt_accounts``.

Registration, Codex authorization and edits all change the same account row.
Jobs and logs are separate operation records linked by that row's ID.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from core import account_store, codex_relay_service as relay, db, phone_store, sqlite_store
from core.account_proxy import proxy_display

_REG_ACTIVE = {"pending", "running", "stopping"}
_RELAY_ACTIVE = {
    "pending", "running", "stopping", "waiting_email", "waiting_sms",
    "waiting_totp", "waiting_browser",
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


class AccountBusyError(ValueError):
    """A running operation prevents editing or deleting its account."""


def _email(value: Any) -> str:
    return str(value or "").strip().lower()


def _first(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _sort_key(row: dict) -> tuple[str, str]:
    return str(row.get("created_at") or row.get("started_at") or row.get("updated_at") or ""), str(row.get("id") or "")


def _latest(rows: list[dict]) -> dict | None:
    return max(rows, key=_sort_key) if rows else None


def _job_public(job: dict | None, kind: str) -> dict | None:
    if not job:
        return None
    job_id = job.get("id")
    if kind in {"registration", "codex_retry"}:
        return {
            "id": job_id, "kind": kind, "status": job.get("status") or "",
            "email": job.get("email") or "", "created_at": job.get("created_at") or "",
            "started_at": job.get("started_at") or "", "completed_at": job.get("completed_at") or "",
            "message": job.get("error_message") or "", "error": job.get("error_message") or "",
            "log_endpoint": f"/api/jobs/{job_id}/log",
        }
    return {
        "id": job_id, "kind": kind, "action": job.get("action") or "",
        "status": job.get("status") or "", "stage": job.get("stage") or "",
        "email": job.get("email") or "", "created_at": job.get("created_at") or "",
        "started_at": job.get("started_at") or "", "completed_at": job.get("completed_at") or "",
        "message": job.get("message") or "", "error": job.get("error") or "",
        "waiting_since": job.get("waiting_since") or "", "browser_url": job.get("browser_url") or "",
        "log_endpoint": f"/api/codex-relay/jobs/{job_id}/log",
    }


def _is_sms_job(job: dict | None) -> bool:
    if not job:
        return False
    stage = str(job.get("stage") or "").lower()
    status = str(job.get("status") or "").lower()
    message = str(job.get("message") or "").lower()
    error = str(job.get("error") or "").lower()
    return stage in {"sms", "phone"} or status == "waiting_sms" or any(
        marker in message or marker in error for marker in ("短信", "手机", "sms", "phone")
    )


def _project(account: dict, registration_jobs: list[dict], relay_jobs: list[dict]) -> dict:
    row = dict(account)
    # Internal route snapshots can contain proxy authentication credentials.
    row.pop("_codex_verified_proxy", None)
    account_id = str(row["id"])
    reg_job = _latest([job for job in registration_jobs if str(job.get("job_type") or "registration") == "registration"])
    retry_job = _latest([job for job in registration_jobs if job.get("job_type") == "codex_retry"])
    auth_job = _latest([job for job in relay_jobs if not job.get("action")])
    latest_relay = _latest(relay_jobs)
    active_relay = _latest([job for job in relay_jobs if str(job.get("status") or "").lower() in _RELAY_ACTIVE])
    maintenance = active_relay if active_relay and active_relay.get("action") else None
    codex_job = _latest([job for job in (auth_job, retry_job) if job])
    codex_kind = "codex" if codex_job is auth_job and auth_job else "codex_retry" if retry_job else ""
    codex_raw = str(row.get("codex_status") or "not_authorized").lower()
    if codex_job and str(codex_job.get("status") or "") in _RELAY_ACTIVE:
        codex_status = "authorizing"
    elif codex_raw in {"deactivated", "deleted"}:
        # A confirmed unusable account is not a retryable OAuth failure;
        # historical successful jobs must not hide its current state either.
        codex_status = "deactivated"
    elif codex_job and codex_job.get("status") == "success":
        codex_status = "authorized"
    elif codex_raw in {"authorized", "success"}:
        codex_status = "authorized"
    elif codex_raw == "failed":
        codex_status = "failed"
    else:
        codex_status = "unauthorized"
    phone_verified = bool(row.get("phone_verified_at") and row.get("phone") and row.get("sms_code_url"))
    if auth_job and auth_job.get("status") in _RELAY_ACTIVE and _is_sms_job(auth_job):
        phone_status = "verifying"
    elif auth_job and auth_job.get("status") in {"failed", "stopped", "cancelled"} and _is_sms_job(auth_job):
        phone_status = "failed"
    else:
        phone_status = "verified" if phone_verified else "unverified"
    phone_number = str(row.get("phone") or "") if phone_verified else ""
    sms_url = str(row.get("sms_code_url") or "") if phone_verified else ""
    sms_source = ""
    password = str(row.get("chatgpt_password") or "")
    provider = _first(
        row.get("email_provider"),
        "outlook" if row.get("outlook_client_id") and row.get("outlook_refresh_token") else "",
        "generic_api" if row.get("email_code_url") else "",
    )
    from core.email_provider import EMAIL_SOURCE_LABELS
    log_refs = [
        _job_public(reg_job, "registration"),
        _job_public(retry_job, "codex_retry"),
        _job_public(latest_relay, "maintenance" if (latest_relay or {}).get("action") else "codex"),
    ]
    logs = [ref for ref in log_refs if ref]
    latest_log = max(logs, key=lambda ref: str(ref.get("created_at") or ref.get("started_at") or ""), default=None)
    if reg_job and reg_job.get("status") in _REG_ACTIVE:
        active_operation, active_label = "registering", "注册任务运行中"
    elif phone_status == "verifying":
        active_operation, active_label = "verifying", "正在等待短信"
    elif codex_status == "authorizing":
        active_operation, active_label = "authorizing", "Codex 授权运行中"
    elif maintenance:
        active_operation = "maintenance"
        active_label = _MAINTENANCE_LABELS.get(str(maintenance.get("action") or ""), str(maintenance.get("message") or "账号维护任务运行中"))
    else:
        active_operation, active_label = "", ""
    plan = str(row.get("quota_plan") or row.get("current_plan_type") or row.get("plan_type") or "")
    liveness = str(row.get("liveness_status") or row.get("live_check_status") or "unknown")
    email_code_url = str(row.get("email_code_url") or "")
    row.update({
        "id": account_id, "email": _email(row.get("email")),
        "registration_status": "registered", "registration_status_raw": str((reg_job or {}).get("status") or ""),
        "codex_status": codex_status, "codex_status_raw": codex_raw,
        "phone_status": phone_status,
        "phone": f"{phone_number}----{sms_url or sms_source}" if phone_verified else "",
        "phone_number": phone_number, "sms_code_url": sms_url,
        "sms_provider": sms_source or str(row.get("sms_provider") or ""),
        "password": password, "chatgpt_password": password,
        "totp_secret": str(row.get("totp_secret") or ""),
        "email_code_url": email_code_url,
        "mailbox_password": str(row.get("mailbox_password") or ""),
        "outlook_client_id": str(row.get("outlook_client_id") or ""),
        "outlook_refresh_token": str(row.get("outlook_refresh_token") or ""),
        "has_proxy": bool(row.get("proxy")), "proxy_source": str(row.get("proxy_source") or ""),
        "proxy_display": proxy_display(row.get("proxy")),
        "has_password": bool(password), "has_totp": bool(row.get("totp_secret")),
        "has_email_code_url": bool(email_code_url), "has_phone": bool(phone_number),
        "has_sms_code_url": bool(sms_url),
        "login_method": row.get("login_method") or ("password" if password else "email_otp"),
        "email_provider": provider, "email_provider_label": EMAIL_SOURCE_LABELS.get(provider, provider or "未配置"),
        "gpt_status": liveness, "liveness_status": liveness,
        "quota_plan": plan, "plan": plan, "note": str(row.get("note") or ""),
        "created_at": row.get("created_at") or "", "updated_at": row.get("updated_at") or "",
        "registration_job_id": reg_job.get("id") if reg_job else None,
        "codex_job_id": codex_job.get("id") if codex_job else None,
        "codex_job_kind": codex_kind,
        "registration_job": _job_public(reg_job, "registration"),
        "codex_job": _job_public(codex_job, codex_kind) if codex_job else None,
        "maintenance_job": _job_public(maintenance, "maintenance"),
        "latest_log": latest_log, "logs": logs, "log_count": len(logs),
        "active_operation": active_operation, "active_operation_label": active_label,
        "phone_verified_at": row.get("phone_verified_at") or "",
        "quota_status": row.get("quota_status") or "unknown",
        "codex_authorized_at": row.get("codex_authorized_at") or "",
    })
    return row


def list_accounts(
    *, q: str = "", registration_status: str = "", codex_status: str = "",
    phone_status: str = "", gpt_status: str = "", provider: str = "",
) -> list[dict]:
    """List only persisted GPT accounts; jobs cannot create account rows."""
    with relay._LOCK, db._LOCK:
        accounts = account_store.read_gpt_accounts(db._ACCOUNTS_JSON)
        registration_jobs = db.list_jobs(limit=1_000_000)
        relay_jobs = relay.list_jobs()
    reg_by_id: dict[str, list[dict]] = {}
    relay_by_id: dict[str, list[dict]] = {}
    for job in registration_jobs:
        if job.get("account_id") is not None:
            reg_by_id.setdefault(str(job["account_id"]), []).append(job)
    for job in relay_jobs:
        if job.get("account_id") is not None:
            relay_by_id.setdefault(str(job["account_id"]), []).append(job)
    rows = [_project(row, reg_by_id.get(str(row["id"]), []), relay_by_id.get(str(row["id"]), [])) for row in accounts]
    q = _email(q)
    filters = {
        "registration_status": str(registration_status or "").strip().lower(),
        "codex_status": str(codex_status or "").strip().lower(),
        "phone_status": str(phone_status or "").strip().lower(),
        "gpt_status": str(gpt_status or "").strip().lower(),
        "email_provider": str(provider or "").strip().lower(),
    }
    if q:
        rows = [row for row in rows if q in _email(row.get("email")) or q in str(row.get("note") or "").lower()]
    for field, target in filters.items():
        if target:
            rows = [row for row in rows if str(row.get(field) or "").lower() == target]
    return sorted(rows, key=lambda row: (str(row.get("updated_at") or ""), str(row.get("email") or "")), reverse=True)


def get_account(account_id: str) -> dict | None:
    target = str(account_id or "").strip()
    return next((row for row in list_accounts() if str(row["id"]) == target), None)


def update_account(account_id: str, data: dict) -> dict:
    """Edit one row in the canonical ``gpt_accounts`` collection.

    Codex authorization jobs still use the relay job service, but account edits
    have no reason to pass through its account API.  Keep the account row and
    any newly added phone-pool row in one SQLite transaction.
    """
    if not isinstance(data, dict):
        raise ValueError("账号编辑数据必须是对象")
    allowed = {"email", "chatgpt_password", "email_code_url", "totp_secret", "note", "phone", "sms_code_url", "proxy"}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"不支持编辑字段：{', '.join(sorted(unknown))}")

    with relay._LOCK, db._LOCK:
        # Resolve the operation state before opening the write transaction. It
        # is computed from the canonical account row and job records.
        current = get_account(account_id)
        if current is None:
            raise LookupError("GPT账号不存在或已删除")
        if current.get("active_operation"):
            raise AccountBusyError("账号仍有任务运行，请先停止任务：" + current["email"])

        changes = {key: str(data.get(key) or "").strip() for key in allowed if key in data}
        if "phone" in changes or "sms_code_url" in changes:
            if "phone" not in changes or "sms_code_url" not in changes:
                raise ValueError("手机号与短信取码 URL 必须同时提交")
        if "totp_secret" in changes and not changes["totp_secret"]:
            changes.pop("totp_secret")
            changes["clear_totp"] = True

        with sqlite_store.write_transaction(db._ACCOUNTS_JSON):
            rows = account_store.read_gpt_accounts(db._ACCOUNTS_JSON)
            row = next((item for item in rows if str(item.get("id") or "") == str(account_id).strip()), None)
            if row is None:
                raise LookupError("GPT账号不存在或已删除")

            new_email = str(changes.get("email") or row.get("email") or "").strip().lower()
            if not relay._EMAIL_RE.match(new_email):
                raise ValueError("邮箱格式错误")
            if any(item is not row and str(item.get("email") or "").strip().lower() == new_email for item in rows):
                raise ValueError("邮箱已存在")
            row["email"] = new_email

            if "chatgpt_password" in changes:
                row["chatgpt_password"] = changes["chatgpt_password"]
                if changes["chatgpt_password"]:
                    row["password_changed_at"] = relay._now()
            if "email_code_url" in changes:
                code_url = changes["email_code_url"]
                if code_url and not relay._valid_url(code_url):
                    raise ValueError("邮箱取码 URL 必须是 http(s) 地址")
                row["email_code_url"] = code_url
            if "proxy" in changes:
                row["proxy"] = relay._normalize_proxy_value(changes["proxy"])
                row["proxy_source"] = "manual" if row["proxy"] else ""
                row.pop("_codex_verified_proxy", None)
                row.pop("codex_profile_id", None)
            if "note" in changes:
                row["note"] = changes["note"]
            if "totp_secret" in changes:
                row["totp_secret"] = relay._normalize_totp_secret(changes["totp_secret"])
                row["twofa_enabled_at"] = relay._now()
            elif changes.get("clear_totp"):
                row["totp_secret"] = ""
                row["twofa_enabled_at"] = ""

            phones = phone_store.read(relay._PHONES_PATH)
            phone_value = changes.get("phone")
            sms_url = changes.get("sms_code_url")
            if phone_value is not None or sms_url is not None:
                phone_value = relay._normalize_phone(phone_value or "")
                sms_url = str(sms_url or "").strip()
                if bool(phone_value) != bool(sms_url):
                    raise ValueError("手机号与短信取码 URL 必须同时填写或同时留空")
                if sms_url and not relay._valid_url(sms_url):
                    raise ValueError("短信取码 URL 必须是 http(s) 地址")
                if phone_value and not 7 <= len(phone_value.lstrip("+")) <= 15:
                    raise ValueError("手机号长度错误")
                if phone_value:
                    relay._upsert_phone_locked(phones, phone_value, sms_url)

            row["updated_at"] = relay._now()
            account_store.write_gpt_accounts(db._ACCOUNTS_JSON, rows)
            phone_store.write(relay._PHONES_PATH, phones)

        return get_account(str(account_id))


def delete_accounts(account_ids: list[str]) -> dict:
    """Permanently remove selected GPT rows by canonical ID."""
    requested = list(dict.fromkeys(str(value).strip() for value in account_ids or [] if str(value).strip()))
    if not requested:
        raise ValueError("account_ids 必须是非空数组")
    with relay._LOCK, db._LOCK:
        selected, skipped = [], []
        for account_id in requested:
            row = get_account(account_id)
            if row is None:
                skipped.append({"id": account_id, "reason": "账号不存在或已删除"})
            else:
                selected.append(row)
        if not selected:
            raise LookupError("所选 GPT 账号不存在或已删除")
        busy = [row["email"] for row in selected if row.get("active_operation")]
        if busy:
            raise AccountBusyError("以下账号仍有任务运行，请先停止任务：" + "、".join(busy[:3]))
        with sqlite_store.write_transaction(db._ACCOUNTS_JSON):
            deleted = relay.delete_accounts([row["id"] for row in selected])
        items = [{"id": row["id"], "email": row["email"], "deleted_at": datetime.now().isoformat(timespec="seconds")} for row in selected]
        message = f"已删除 {deleted} 个 GPT 账号"
        if skipped:
            message += f"，跳过 {len(skipped)} 个"
        return {"ok": True, "deleted": deleted, "deleted_count": deleted, "items": items, "skipped": skipped, "message": message}


def authorization_material(row: dict) -> dict:
    """Read authorization credentials directly from the supplied GPT row."""
    if not isinstance(row, dict):
        raise ValueError("GPT账号不存在")
    email = _email(row.get("email"))
    if not email:
        raise ValueError("GPT账号缺少邮箱")
    fields = (
        "chatgpt_password", "mailbox_password", "outlook_client_id", "outlook_refresh_token",
        "email_code_url", "totp_secret", "login_method", "email_provider", "note", "proxy", "proxy_source",
    )
    material = {"email": email, **{key: _first(row.get(key)) for key in fields}}
    material["email_provider_context"] = dict(row.get("email_provider_context") or {})
    material["codex_status"] = row.get("codex_status_raw") or row.get("codex_status") or "not_authorized"
    if row.get("phone_verified_at"):
        material["phone"] = _first(row.get("phone_number"), row.get("phone"))
        material["sms_code_url"] = _first(row.get("sms_code_url"))
    return material


def list_gpt_accounts(**filters) -> list[dict]:
    return list_accounts(**filters)
