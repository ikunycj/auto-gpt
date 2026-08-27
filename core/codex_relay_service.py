# -*- coding: utf-8 -*-
"""Batch Codex OAuth for existing ChatGPT accounts with task-scoped verification."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
import zipfile
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

import pyotp

from core import sms_provider, sqlite_store
from config import codex as _codex_config
from core.import_parser import (
    clean_import_value,
    is_http_url,
    iter_import_splits,
    parse_account_material_line,
    split_import_line,
)

_ROOT = Path(__file__).resolve().parent.parent
# The following paths are stable SQLite source-key labels. They are retained
# for migration and test injection; production writes use mirror=False for
# repository paths, so no matching directories or JSON files are created.
_RELAY_ACCOUNTS_KEY = _ROOT / "codex_接码账号.json"
_RELAY_PHONES_KEY = _ROOT / "codex_接码手机.json"
_RELAY_JOBS_KEY = _ROOT / "codex_接码任务.json"
_RELAY_LOG_KEY_ROOT = _ROOT / "codex_接码日志"
_CODEX_CREDENTIAL_KEY_ROOT = _ROOT / "codex_accounts"
_RELAY_SUB2_SERVICES_KEY = _ROOT / "codex_sub2_services.json"

# Private aliases are kept for existing callers/tests that isolate relay
# storage by monkeypatching these names.
_ACCOUNTS_PATH = _RELAY_ACCOUNTS_KEY
_PHONES_PATH = _RELAY_PHONES_KEY
_JOBS_PATH = _RELAY_JOBS_KEY
_LOG_DIR = _RELAY_LOG_KEY_ROOT
_CREDENTIAL_DIR = _CODEX_CREDENTIAL_KEY_ROOT
_SUB2_SERVICES_PATH = _RELAY_SUB2_SERVICES_KEY
_SUB2_SYNC_MARKER = "自动导入注册机"
_SUB2_LEGACY_TRAILING_MARKERS = {"注册机自动转入"}
_SUB2_PAGE_SIZE = 100
_SUB2_MAX_PAGES = 1000
_PHONE_AVAILABLE_DEFAULT = 1
_PHONE_AVAILABLE_MAX = 3
_LOCK = threading.RLock()
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_CODE_RE = re.compile(r"^\d{4,8}$")
_SENSITIVE_RE = re.compile(
    r"(?i)(refresh_token|access_token|id_token|password|code|otp|token)=([^\s&]+)"
)
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{7,}\d)(?!\d)")
_IMPORT_FORMATS = {
    "auto",
    "generic_api",
    "chatgpt_email_url",
    "chatgpt_totp",
    "outlook",
    "combined",
}
_EMAIL_PROVIDER_LABELS = {
    "outlook": "微软邮箱",
    "generic_api": "通用 API",
    "cloudflare_domain": "Cloudflare 域名邮箱",
    "cloudflare": "Cloudflare Worker",
    "gptmail": "GPTMail",
    "mailnest": "MailNest",
    "cloudmail": "CloudMail",
}
_DYNAMIC_EMAIL_PROVIDERS = {
    "cloudflare_domain", "cloudflare", "gptmail", "mailnest", "cloudmail",
}

_SMS_PROVIDER_LABELS = {
    "grizzly": "GrizzlySMS",
    "l": "L 接码服务",
    "h": "H 接码服务",
}
_SMS_PLATFORM_ROW_PREFIX = "sms-provider:"

_stop_events: dict[str, threading.Event] = {}
_verification_events: dict[tuple[str, str], threading.Event] = {}
_verification_codes: dict[tuple[str, str], list[str]] = {}
_active_accounts: set[str] = set()
_browser_controls: dict[str, dict[str, object]] = {}
_phone_locks: dict[str, threading.Lock] = {}


class _ExecutionSlot:
    """One job's ownership of a shared batch concurrency slot."""

    def __init__(self, semaphore: threading.BoundedSemaphore):
        self._semaphore = semaphore
        self.held = False

    def acquire(self) -> None:
        if self.held:
            return
        self._semaphore.acquire()
        self.held = True

    def release(self) -> None:
        if not self.held:
            return
        self.held = False
        self._semaphore.release()


def _browser_assist_grace_seconds() -> float:
    raw = str(os.environ.get("CODEX_RELAY_BROWSER_ASSIST_GRACE_SECONDS", "300") or "300").strip()
    try:
        return max(0.1, min(300.0, float(raw)))
    except ValueError:
        return 300.0


def _show_full_urls() -> bool:
    return str(os.environ.get("CODEX_RELAY_SHOW_FULL_URLS", "")).strip().lower() in {"1", "true", "yes", "on"}


class RelayStopped(RuntimeError):
    pass


class BrowserAssistTimeout(RuntimeError):
    pass


class Sub2RemoteAccountNotFound(RuntimeError):
    pass


_TERMINAL_CODEX_STATUSES = {"deactivated", "deleted"}
_ACTIVE_JOB_STATUSES = {
    "pending", "running", "stopping", "waiting_email", "waiting_sms",
    "waiting_totp", "waiting_browser",
}


def _terminal_codex_status(*values: object) -> str:
    text = " ".join(str(value or "").lower() for value in values)
    tokens = {str(value or "").strip().lower() for value in values}
    if "deleted" in tokens or "account_deleted" in text:
        return "deactivated"
    if "deactivated" in tokens or "account_deactivated" in text or "account_banned" in text or "账号已废" in text:
        return "deactivated"
    return ""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read(path: Path) -> list[dict]:
    data = sqlite_store.read_json(path, [], collection=_collection_name(path))
    return data if isinstance(data, list) else []


def _write(path: Path, rows: list[dict]) -> None:
    sqlite_store.write_json(
        path,
        rows,
        collection=_collection_name(path),
        mode=0o600,
        # Temporary paths used by isolated tests and explicit export tooling
        # may still request a mirror; repository runtime paths remain SQLite-only.
        mirror=sqlite_store.legacy_mirror_allowed(path),
    )


def _collection_name(path: Path) -> str:
    target = Path(path).expanduser().resolve()
    mapping = {
        Path(_ACCOUNTS_PATH).expanduser().resolve(): "relay_accounts",
        Path(_PHONES_PATH).expanduser().resolve(): "relay_phones",
        Path(_JOBS_PATH).expanduser().resolve(): "relay_jobs",
        Path(_SUB2_SERVICES_PATH).expanduser().resolve(): "relay_sub2_services",
    }
    return mapping.get(target, f"relay:{target}")


def initialize_storage() -> dict:
    """Import relay compatibility documents into the shared SQLite database."""
    for path in (_ACCOUNTS_PATH, _PHONES_PATH, _JOBS_PATH, _SUB2_SERVICES_PATH):
        sqlite_store.migrate_json_file(path, default=[], collection=_collection_name(path))
    return sqlite_store.storage_info(sqlite_store.database_path(_ACCOUNTS_PATH))


def _valid_url(value: str) -> bool:
    return is_http_url(value)


def _normalize_phone(value: str) -> str:
    raw = str(value or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if raw.startswith("+"):
        return "+" + digits
    return digits


def _phone_key(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _normalize_totp_secret(value: str) -> str:
    value = str(value or "").strip().replace(" ", "")
    if not value:
        return ""
    try:
        if value.lower().startswith("otpauth://"):
            pyotp.parse_uri(value).now()
        else:
            pyotp.TOTP(value).now()
    except Exception as exc:
        raise ValueError("2FA 密钥不是有效的 Base32 或 otpauth:// URI") from exc
    return value


def _auto_import_parts(line: str) -> list[str]:
    """Choose an automatic account split without damaging URL query strings."""
    # Prefer the richer formats first.  URL/phone combinations are selected
    # by validation; plain four-field Outlook material falls back to its first
    # structural candidate, preserving tokens that contain separators.
    for count in (6, 5, 4, 3):
        candidates = list(iter_import_splits(line, count))
        if not candidates:
            continue
        if count == 6:
            match = next(
                (
                    candidate for candidate in candidates
                    if _EMAIL_RE.match(candidate[0])
                    and _valid_url(candidate[2])
                    and _valid_url(candidate[5])
                    and 7 <= len(_normalize_phone(candidate[4]).lstrip("+")) <= 15
                ),
                None,
            )
            if match:
                return list(match)
        elif count == 5:
            match = next(
                (
                    candidate for candidate in candidates
                    if _EMAIL_RE.match(candidate[0])
                    and _valid_url(candidate[4])
                    and 7 <= len(_normalize_phone(candidate[3]).lstrip("+")) <= 15
                ),
                None,
            )
            if match:
                return list(match)
        elif count == 4:
            match = next(
                (
                    candidate for candidate in candidates
                    if _EMAIL_RE.match(candidate[0])
                    and _valid_url(candidate[1])
                    and _valid_url(candidate[3])
                    and 7 <= len(_normalize_phone(candidate[2]).lstrip("+")) <= 15
                ),
                None,
            )
            if match:
                return list(match)
            # Outlook is the other four-field auto format.  Its fields are
            # opaque, so the earliest structural candidate is the safest one.
            if _EMAIL_RE.match(candidates[0][0]):
                return list(candidates[0])
        elif count == 3:
            match = next(
                (
                    candidate for candidate in candidates
                    if _EMAIL_RE.match(candidate[0]) and _valid_url(candidate[2])
                ),
                None,
            )
            if match:
                return list(match)
            if _EMAIL_RE.match(candidates[0][0]):
                return list(candidates[0])

    return split_import_line(line, max_fields=2)


def _parse_record(
    raw: str,
    line_no: int,
    format_name: str = "auto",
    *,
    require_login_material: bool = True,
) -> dict:
    format_name = str(format_name or "auto").strip().lower()
    if format_name not in _IMPORT_FORMATS:
        raise ValueError("不支持的账号格式")
    line = str(raw or "").strip()
    if not line or line.startswith("#"):
        return {}
    if line.startswith("{"):
        try:
            data = json.loads(line)
        except Exception as exc:
            raise ValueError(f"第 {line_no} 行 JSON 格式错误") from exc
        if not isinstance(data, dict):
            raise ValueError(f"第 {line_no} 行必须是 JSON 对象")
        record = {
            "email": data.get("email"),
            "chatgpt_password": data.get("chatgpt_password") or data.get("password"),
            "mailbox_password": data.get("mailbox_password") or data.get("email_password"),
            "outlook_client_id": data.get("outlook_client_id") or data.get("client_id"),
            "outlook_refresh_token": data.get("outlook_refresh_token") or data.get("refresh_token"),
            "email_code_url": data.get("email_code_url") or data.get("email_url"),
            "totp_secret": data.get("totp_secret") or data.get("totp"),
            "phone": data.get("phone"),
            "sms_code_url": data.get("sms_code_url") or data.get("sms_url"),
        }
    else:
        semantic_record = parse_account_material_line(line) if format_name == "auto" else None
        if semantic_record is not None:
            record = semantic_record
            parts = []
        elif format_name == "generic_api":
            parts = split_import_line(line, max_fields=2)
        elif format_name == "chatgpt_email_url" or format_name == "chatgpt_totp":
            parts = split_import_line(line, max_fields=3)
        elif format_name == "outlook":
            parts = split_import_line(line, max_fields=4)
        elif format_name == "combined":
            parts = split_import_line(line, max_fields=6)
        else:
            parts = _auto_import_parts(line)
        if semantic_record is not None:
            pass
        elif format_name == "generic_api":
            if len(parts) != 2:
                raise ValueError(f"第 {line_no} 行通用 API 格式应为：邮箱----邮箱取码URL")
            email, email_url = parts
            record = {"email": email, "email_code_url": email_url}
        elif format_name == "chatgpt_email_url":
            if len(parts) != 3:
                raise ValueError(f"第 {line_no} 行密码 + 邮箱取码格式应为：邮箱----ChatGPT密码----邮箱取码URL")
            email, password, email_url = parts
            record = {
                "email": email,
                "chatgpt_password": password,
                "email_code_url": email_url,
            }
        elif format_name == "chatgpt_totp":
            if len(parts) != 3:
                raise ValueError(f"第 {line_no} 行密码 + 2FA 格式应为：邮箱----ChatGPT密码----TOTP密钥")
            email, password, totp = parts
            record = {"email": email, "chatgpt_password": password, "totp_secret": totp}
        elif format_name == "outlook":
            if len(parts) != 4:
                raise ValueError(f"第 {line_no} 行微软 Outlook 格式应为：邮箱----邮箱密码----Client_ID----Refresh_Token")
            email, password, client_id, refresh_token = parts
            record = {"email": email, "mailbox_password": password, "outlook_client_id": client_id, "outlook_refresh_token": refresh_token, "email_provider": "outlook"}
        elif len(parts) == 1 and not require_login_material:
            record = {"email": parts[0]}
        elif len(parts) == 2:
            email, email_url = parts
            record = {"email": email, "email_code_url": email_url}
        elif len(parts) == 3:
            email, password, verification_source = parts
            if _valid_url(verification_source):
                record = {
                    "email": email,
                    "chatgpt_password": password,
                    "email_code_url": verification_source,
                }
            else:
                record = {
                    "email": email,
                    "chatgpt_password": password,
                    "totp_secret": verification_source,
                }
        elif len(parts) == 4:
            email, second, third, fourth = parts
            if _valid_url(second):
                record = {"email": email, "email_code_url": second, "phone": third, "sms_code_url": fourth}
            else:
                record = {
                    "email": email,
                    "mailbox_password": second,
                    "outlook_client_id": third,
                    "outlook_refresh_token": fourth,
                    "email_provider": "outlook",
                }
        elif len(parts) == 5:
            email, password, totp, phone, sms_url = parts
            record = {"email": email, "chatgpt_password": password, "totp_secret": totp, "phone": phone, "sms_code_url": sms_url}
        elif len(parts) == 6:
            email, password, email_url, totp, phone, sms_url = parts
            record = {
                "email": email, "chatgpt_password": password, "email_code_url": email_url,
                "totp_secret": totp, "phone": phone, "sms_code_url": sms_url,
            }
        else:
            raise ValueError(f"第 {line_no} 行字段数应为 2、3、4、5 或 6")

    record = {k: clean_import_value(v) for k, v in record.items()}
    email = record.get("email", "").lower()
    if not _EMAIL_RE.match(email):
        raise ValueError(f"第 {line_no} 行邮箱格式错误")
    record["email"] = email
    has_outlook = bool(record.get("outlook_client_id") and record.get("outlook_refresh_token"))
    if require_login_material and not record.get("chatgpt_password") and not record.get("email_code_url") and not has_outlook:
        raise ValueError(f"第 {line_no} 行至少需要 ChatGPT 密码、邮箱取码 URL 或微软邮箱凭证")
    if bool(record.get("outlook_client_id")) != bool(record.get("outlook_refresh_token")):
        raise ValueError(f"第 {line_no} 行微软邮箱必须同时填写 Client_ID 和 Refresh_Token")
    for key, label in (("email_code_url", "邮箱取码 URL"), ("sms_code_url", "短信取码 URL")):
        if record.get(key) and not _valid_url(record[key]):
            raise ValueError(f"第 {line_no} 行{label}必须是 http(s) 地址")
    phone = _normalize_phone(record.get("phone", ""))
    if bool(phone) != bool(record.get("sms_code_url")):
        raise ValueError(f"第 {line_no} 行手机号与短信取码 URL 必须同时填写或同时留空")
    if phone and not 7 <= len(phone.lstrip("+")) <= 15:
        raise ValueError(f"第 {line_no} 行手机号长度错误")
    record["phone"] = phone
    record["totp_secret"] = _normalize_totp_secret(record.get("totp_secret", ""))
    if has_outlook:
        record["email_provider"] = "outlook"
    return record


def _parse_phone_record(raw: str, line_no: int) -> dict:
    line = str(raw or "").strip()
    if not line or line.startswith("#"):
        return {}
    if line.startswith("{"):
        try:
            data = json.loads(line)
        except Exception as exc:
            raise ValueError(f"第 {line_no} 行 JSON 格式错误") from exc
        if not isinstance(data, dict):
            raise ValueError(f"第 {line_no} 行必须是 JSON 对象")
        phone = data.get("phone")
        code_url = data.get("sms_code_url") or data.get("sms_url") or data.get("code_url")
        available_uses = next(
            (data[key] for key in ("available_uses", "max_uses", "reuse_limit", "uses") if key in data),
            _PHONE_AVAILABLE_DEFAULT,
        )
    else:
        dash_separator = re.compile(r"(?<!-)-{3,4}(?!-)")
        parts = [x.strip() for x in (
            dash_separator.split(line) if dash_separator.search(line) else line.split("\t")
        )]
        if len(parts) not in (2, 3):
            raise ValueError(
                f"第 {line_no} 行手机号格式应为：手机号---短信取码URL[---可用次数]（兼容四个短横线）"
            )
        phone, code_url = parts[:2]
        available_uses = parts[2] if len(parts) == 3 else _PHONE_AVAILABLE_DEFAULT
    phone = _normalize_phone(phone)
    code_url = str(code_url or "").strip()
    if not 7 <= len(phone.lstrip("+")) <= 15:
        raise ValueError(f"第 {line_no} 行手机号长度错误")
    if not _valid_url(code_url):
        raise ValueError(f"第 {line_no} 行短信取码 URL 必须是 http(s) 地址")
    try:
        available_uses = int(available_uses)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"第 {line_no} 行手机号可用次数必须是 0、1、2 或 3") from exc
    if available_uses not in (0, 1, 2, 3):
        raise ValueError(f"第 {line_no} 行手机号可用次数必须是 0、1、2 或 3")
    return {"phone": phone, "sms_code_url": code_url, "available_uses": available_uses}


def _id_list(value) -> list[str]:
    if isinstance(value, list):
        return list(dict.fromkeys(str(item) for item in value if item))
    if value:
        return [str(value)]
    return []


def _active_reservation_ids(row: dict) -> list[str]:
    return _id_list(row.get("reserved_job_ids"))


def _upsert_phone_locked(phones: list[dict], phone_value: str, sms_url: str, *, now: str | None = None) -> dict:
    now = now or _now()
    key = _phone_key(phone_value)
    row = next((item for item in phones if _phone_key(item.get("phone")) == key), None)
    if row is None:
        row = {
            "id": uuid.uuid4().hex,
            "phone": phone_value,
            "sms_code_url": sms_url,
            "assigned_account_id": "",
            "assigned_account_ids": [],
            "reserved_job_ids": [],
            "available_uses": _PHONE_AVAILABLE_DEFAULT,
            "invalid": False,
            "seq": max((int(item.get("seq") or 0) for item in phones), default=0) + 1,
            "created_at": now,
            "updated_at": now,
        }
        phones.append(row)
    else:
        row["phone"] = phone_value
        row["sms_code_url"] = sms_url
        if not any(key in row for key in ("available_uses", "remaining_uses", "max_uses")):
            row["available_uses"] = _PHONE_AVAILABLE_DEFAULT
        row.setdefault("invalid", False)
        row.setdefault("assigned_account_ids", _id_list(row.get("assigned_account_id")))
        row.setdefault("reserved_job_ids", [])
        row["updated_at"] = now
    return row


def _ensure_assignments_locked(accounts: list[dict], phones: list[dict]) -> tuple[int, bool]:
    """Migrate legacy phone data without binding unverified numbers to accounts."""
    changed = False
    now = _now()
    next_account_seq = max((int(row.get("seq") or 0) for row in accounts), default=0) + 1
    for account in accounts:
        if not account.get("seq"):
            account["seq"] = next_account_seq
            next_account_seq += 1
            changed = True
    next_phone_seq = max((int(row.get("seq") or 0) for row in phones), default=0) + 1
    for phone in phones:
        legacy_assigned = len(_id_list(phone.get("assigned_account_ids") or phone.get("assigned_account_id")))
        if not legacy_assigned:
            phone_key = _phone_key(phone.get("phone"))
            legacy_assigned = sum(
                1 for account in accounts
                if account.get("phone_verified_at") and _phone_key(account.get("phone")) == phone_key
            )
        try:
            if "available_uses" in phone:
                available_uses = int(phone.get("available_uses"))
            elif "remaining_uses" in phone:
                available_uses = int(phone.get("remaining_uses"))
            elif "max_uses" in phone:
                available_uses = int(phone.get("max_uses")) - legacy_assigned
            else:
                available_uses = _PHONE_AVAILABLE_DEFAULT
        except (TypeError, ValueError):
            available_uses = _PHONE_AVAILABLE_DEFAULT
        available_uses = max(0, min(_PHONE_AVAILABLE_MAX, available_uses))
        if phone.get("available_uses") != available_uses:
            phone["available_uses"] = available_uses
            changed = True
        for legacy_key in ("max_uses", "remaining_uses", "use_count"):
            if legacy_key in phone:
                phone.pop(legacy_key, None)
                changed = True
        if "invalid" not in phone:
            phone["invalid"] = False
            changed = True
        if not phone.get("seq"):
            phone["seq"] = next_phone_seq
            next_phone_seq += 1
            changed = True
    account_by_id = {str(row.get("id") or ""): row for row in accounts if row.get("id")}

    # Combined imports and legacy pre-bindings become unbound phone-pool candidates
    # unless the account has a confirmed phone verification timestamp.
    for account in accounts:
        verified = bool(account.get("phone_verified_at"))
        phone_value = account.get("phone") or (account.get("last_sms_phone") if verified else "") or ""
        sms_url = account.get("sms_code_url") or (account.get("last_sms_code_url") if verified else "") or ""
        key = _phone_key(phone_value)
        if not key or not sms_url:
            continue
        existing_phone = next((item for item in phones if _phone_key(item.get("phone")) == key), None)
        phone_changed = (
            existing_phone is None
            or existing_phone.get("phone") != phone_value
            or existing_phone.get("sms_code_url") != sms_url
        )
        _upsert_phone_locked(phones, phone_value, sms_url, now=now)
        if phone_changed:
            changed = True
        if verified:
            if account.get("phone") != phone_value or account.get("sms_code_url") != sms_url:
                account["phone"] = phone_value
                account["sms_code_url"] = sms_url
                account["updated_at"] = now
                changed = True
        else:
            if account.get("phone") or account.get("sms_code_url"):
                account["phone"] = ""
                account["sms_code_url"] = ""
                account["updated_at"] = now
                changed = True

    owners_by_phone: dict[str, list[str]] = {}
    for account in accounts:
        if not account.get("phone_verified_at"):
            continue
        key = _phone_key(account.get("phone"))
        if key and account.get("sms_code_url"):
            owners_by_phone.setdefault(key, []).append(str(account.get("id") or ""))

    for phone in phones:
        desired_ids = [
            account_id for account_id in owners_by_phone.get(_phone_key(phone.get("phone")), [])
            if account_id in account_by_id
        ]
        current_ids = _id_list(phone.get("assigned_account_ids") or phone.get("assigned_account_id"))
        reservations = _id_list(phone.get("reserved_job_ids"))
        desired_last = desired_ids[-1] if desired_ids else ""
        if current_ids != desired_ids or str(phone.get("assigned_account_id") or "") != desired_last:
            phone["assigned_account_ids"] = desired_ids
            phone["assigned_account_id"] = desired_last
            phone["updated_at"] = now
            changed = True
        if phone.get("reserved_job_ids") != reservations:
            phone["reserved_job_ids"] = reservations
            changed = True
    return 0, changed


def _validate_unique_account_phones(accounts: list[dict]) -> None:
    # A phone may be deliberately reused for more than one verified account.
    return None


def import_accounts(text: str, format_name: str = "auto", *, include_emails: bool = False) -> dict:
    records: list[dict] = []
    errors: list[str] = []
    for line_no, raw in enumerate(str(text or "").splitlines(), 1):
        try:
            item = _parse_record(raw, line_no, format_name=format_name)
            if item:
                records.append(item)
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        raise ValueError("；".join(errors[:8]))
    if not records:
        raise ValueError("没有可导入的账号")

    with _LOCK:
        rows = _read(_ACCOUNTS_PATH)
        phones = _read(_PHONES_PATH)
        _ensure_assignments_locked(rows, phones)
        next_seq = max((int(row.get("seq") or 0) for row in rows), default=0) + 1
        by_email = {str(x.get("email") or "").lower(): x for x in rows}
        inserted = updated = 0
        for record in records:
            old = by_email.get(record["email"])
            if old:
                for key, value in record.items():
                    if value:
                        old[key] = value
                old["updated_at"] = _now()
                updated += 1
            else:
                record.update({
                    "id": uuid.uuid4().hex,
                    "seq": next_seq,
                    "created_at": _now(),
                    "updated_at": _now(),
                    "last_status": "not_started",
                })
                rows.append(record)
                by_email[record["email"]] = record
                inserted += 1
                next_seq += 1
        _validate_unique_account_phones(rows)
        assigned, _changed = _ensure_assignments_locked(rows, phones)
        _write(_ACCOUNTS_PATH, rows)
        _write(_PHONES_PATH, phones)
    result = {
        "ok": True, "inserted": inserted, "updated": updated, "total": len(records),
        "assigned": assigned,
        "unassigned_accounts": sum(1 for row in rows if not row.get("phone") or not row.get("sms_code_url")),
    }
    # The WebUI uses this transient list to clear matching soft-delete
    # markers. Keep it opt-in so the lower-level import API remains backward
    # compatible and does not expose account identifiers unnecessarily.
    if include_emails:
        result["emails"] = [record["email"] for record in records]
    return result


def _normalize_account_material(material: dict) -> tuple[str, dict]:
    """Validate and normalize structured material before touching storage."""
    if not isinstance(material, dict):
        raise ValueError("账号材料必须是对象")
    email = str(material.get("email") or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise ValueError("邮箱格式错误")
    fields = (
        "chatgpt_password", "mailbox_password", "outlook_client_id",
        "outlook_refresh_token", "email_code_url", "totp_secret",
        "phone", "sms_code_url", "note", "login_method",
    )
    incoming = {key: str(material.get(key) or "").strip() for key in fields}
    provider = str(material.get("email_provider") or "").strip().lower()
    if provider and provider not in _EMAIL_PROVIDER_LABELS:
        raise ValueError(f"不支持的邮箱取码渠道：{provider}")
    provider_context = material.get("email_provider_context")
    if not isinstance(provider_context, dict):
        provider_context = {}
    incoming["email_provider"] = provider
    incoming["email_provider_context"] = dict(provider_context)
    if incoming["email_code_url"] and not _valid_url(incoming["email_code_url"]):
        raise ValueError("邮箱取码 URL 必须是 http(s) 地址")
    if incoming["sms_code_url"] and not _valid_url(incoming["sms_code_url"]):
        raise ValueError("短信取码 URL 必须是 http(s) 地址")
    if bool(incoming["outlook_client_id"]) != bool(incoming["outlook_refresh_token"]):
        raise ValueError("微软邮箱必须同时填写 Client_ID 和 Refresh_Token")
    if incoming["totp_secret"]:
        incoming["totp_secret"] = _normalize_totp_secret(incoming["totp_secret"])
    if incoming["phone"]:
        incoming["phone"] = _normalize_phone(incoming["phone"])
        if not 7 <= len(incoming["phone"].lstrip("+")) <= 15:
            raise ValueError("手机号长度错误")
    if bool(incoming["phone"]) != bool(incoming["sms_code_url"]):
        raise ValueError("手机号与短信取码 URL 必须同时填写或同时留空")
    if not any((
        incoming["chatgpt_password"], incoming["email_code_url"],
        incoming["outlook_client_id"] and incoming["outlook_refresh_token"],
        provider in _DYNAMIC_EMAIL_PROVIDERS,
    )):
        raise ValueError("账号缺少 ChatGPT 密码或可用的邮箱取码渠道")
    return email, incoming


def validate_account_material(material: dict) -> dict:
    """Validate bridge material without creating a Relay account."""
    email, incoming = _normalize_account_material(material)
    result = {"email": email, **incoming}
    if isinstance(material, dict) and material.get("codex_status"):
        result["codex_status"] = material.get("codex_status")
    return result


def ensure_account_material(material: dict) -> dict:
    """Create or complete one Relay account from an existing GPT account.

    Registration accounts and Relay accounts intentionally live in separate
    collections for backwards compatibility.  This helper is the small,
    idempotent bridge used by the unified GPT account workspace.  It accepts a
    structured dict instead of rebuilding an import line, so URLs containing
    configured separators are preserved byte-for-byte.

    Existing Relay state (authorization status, result files, notes, and
    timestamps) is never reset.  Only missing credential/material fields are
    filled from ``material``.
    """
    email, incoming = _normalize_account_material(material)

    with _LOCK:
        rows = _read(_ACCOUNTS_PATH)
        phones = _read(_PHONES_PATH)
        existing = next((row for row in rows if _email_key(row.get("email")) == email), None)
        now = _now()
        created = False
        changed = False
        if existing is None:
            next_seq = max((int(row.get("seq") or 0) for row in rows), default=0) + 1
            raw_codex_status = str(material.get("codex_status") or "").strip().lower()
            initial_codex_status = {
                "success": "authorized",
                "authorized": "authorized",
                "failed": "failed",
                "deactivated": "deactivated",
                "deleted": "deactivated",
            }.get(raw_codex_status, "not_authorized")
            existing = {
                "id": uuid.uuid4().hex,
                "seq": next_seq,
                "email": email,
                "created_at": now,
                "updated_at": now,
                "last_status": "not_started",
                "codex_status": initial_codex_status,
            }
            rows.append(existing)
            created = True
            changed = True

        # Never overwrite an explicitly maintained Relay value with an empty
        # value (or a stale value from the registration projection).
        for key, value in incoming.items():
            if not value or existing.get(key):
                continue
            existing[key] = value
            changed = True
        if not existing.get("email_provider"):
            if existing.get("outlook_client_id") and existing.get("outlook_refresh_token"):
                existing["email_provider"] = "outlook"
                changed = True
            elif existing.get("email_code_url"):
                existing["email_provider"] = "generic_api"
                changed = True
        if changed and not created:
            existing["updated_at"] = now
        _validate_unique_account_phones(rows)
        _ensure_assignments_locked(rows, phones)
        _write(_ACCOUNTS_PATH, rows)
        _write(_PHONES_PATH, phones)
        public = _public_account(existing)
        public["created"] = created
        return public


def _email_key(value: object) -> str:
    """Normalize an email for internal Relay joins without exposing it."""
    return str(value or "").strip().lower()


def import_phones(text: str) -> dict:
    records: list[dict] = []
    errors: list[str] = []
    for line_no, raw in enumerate(str(text or "").splitlines(), 1):
        try:
            item = _parse_phone_record(raw, line_no)
            if item:
                records.append(item)
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        raise ValueError("；".join(errors[:8]))
    if not records:
        raise ValueError("没有可导入的手机号")

    with _LOCK:
        accounts = _read(_ACCOUNTS_PATH)
        phones = _read(_PHONES_PATH)
        _ensure_assignments_locked(accounts, phones)
        by_phone = {_phone_key(row.get("phone")): row for row in phones}
        next_seq = max((int(row.get("seq") or 0) for row in phones), default=0) + 1
        inserted = updated = 0
        for record in records:
            key = _phone_key(record["phone"])
            old = by_phone.get(key)
            if old:
                old["phone"] = record["phone"]
                old["sms_code_url"] = record["sms_code_url"]
                old["available_uses"] = record.get(
                    "available_uses", old.get("available_uses", _PHONE_AVAILABLE_DEFAULT)
                )
                old["invalid"] = False
                old.pop("invalid_reason", None)
                old["updated_at"] = _now()
                updated += 1
            else:
                record.update({
                    "id": uuid.uuid4().hex,
                    "assigned_account_id": "",
                    "assigned_account_ids": [],
                    "reserved_job_ids": [],
                    "available_uses": record.get("available_uses", _PHONE_AVAILABLE_DEFAULT),
                    "invalid": False,
                    "seq": next_seq,
                    "created_at": _now(),
                    "updated_at": _now(),
                })
                phones.append(record)
                by_phone[key] = record
                inserted += 1
                next_seq += 1
        assigned, _changed = _ensure_assignments_locked(accounts, phones)
        _write(_ACCOUNTS_PATH, accounts)
        _write(_PHONES_PATH, phones)
    return {
        "ok": True, "inserted": inserted, "updated": updated, "total": len(records),
        "assigned": assigned,
        "unassigned_phones": sum(1 for row in phones if not row.get("assigned_account_id")),
    }


def _public_account(row: dict) -> dict:
    email_provider = str(row.get("email_provider") or ("outlook" if row.get("outlook_refresh_token") else "generic_api" if row.get("email_code_url") else ""))
    codex_status = str(row.get("codex_status") or "not_authorized")
    if codex_status == "deleted":
        codex_status = "deactivated"
    if codex_status not in ("not_authorized", "authorized", "reauthorize", "failed", "deactivated", "deleted"):
        codex_status = "failed" if row.get("last_status") == "failed" else "not_authorized"
    chatgpt_password = str(row.get("chatgpt_password") or "")
    mailbox_password = str(row.get("mailbox_password") or "")
    return {
        "id": row.get("id"),
        "email": row.get("email"),
        "has_password": bool(chatgpt_password or mailbox_password),
        "password_kind": "ChatGPT" if chatgpt_password else "微软邮箱" if mailbox_password else "",
        "chatgpt_password": chatgpt_password,
        "mailbox_password": mailbox_password,
        "has_email_code_url": bool(row.get("email_code_url")),
        "email_code_url": row.get("email_code_url") or "",
        "has_totp": bool(row.get("totp_secret")),
        "totp_secret": row.get("totp_secret") or "",
        "outlook_client_id": row.get("outlook_client_id") or "",
        "outlook_refresh_token": row.get("outlook_refresh_token") or "",
        "email_provider": email_provider,
        "email_provider_label": _EMAIL_PROVIDER_LABELS.get(email_provider, "未配置"),
        "login_method": row.get("login_method") or ("password" if chatgpt_password else "email_otp"),
        "has_phone": bool(row.get("phone")),
        "has_sms_code_url": bool(row.get("sms_code_url")),
        "phone": row.get("phone") or "",
        "sms_provider": _url_host(row.get("sms_code_url") or ""),
        "sms_code_url": row.get("sms_code_url") or "",
        "phone_verified_at": row.get("phone_verified_at") or "",
        "last_sms_phone": row.get("last_sms_phone") or "",
        "last_sms_provider": row.get("last_sms_provider") or "",
        "last_sms_code_url": row.get("last_sms_code_url") or "",
        "codex_status": codex_status,
        "codex_authorized_at": row.get("codex_authorized_at") or "",
        "liveness_status": row.get("liveness_status") or "unknown",
        "liveness_checked_at": row.get("liveness_checked_at") or "",
        "email_liveness_status": row.get("email_liveness_status") or "unknown",
        "email_liveness_checked_at": row.get("email_liveness_checked_at") or "",
        "quota_status": row.get("quota_status") or "unknown",
        "quota_checked_at": row.get("quota_checked_at") or "",
        "quota_plan": row.get("quota_plan") or "",
        "quota_summary": row.get("quota_summary") or "",
        "quota_weekly_used_percent": row.get("quota_weekly_used_percent"),
        "quota_weekly_reset_at": row.get("quota_weekly_reset_at"),
        "quota_monthly_used_percent": row.get("quota_monthly_used_percent"),
        "quota_monthly_reset_at": row.get("quota_monthly_reset_at"),
        "twofa_enabled_at": row.get("twofa_enabled_at") or "",
        "password_changed_at": row.get("password_changed_at") or "",
        "sub2_service_id": row.get("sub2_service_id") or "",
        "sub2_account_id": row.get("sub2_account_id") or "",
        "sub2_status": row.get("sub2_status") or "",
        "sub2_synced_at": row.get("sub2_synced_at") or "",
        "sub2_deleted_at": row.get("sub2_deleted_at") or "",
        "sub2_credentials_updated_at": row.get("sub2_credentials_updated_at") or "",
        "sub2_credentials_update_status": row.get("sub2_credentials_update_status") or "",
        "sub2_refresh_at": row.get("sub2_refresh_at") or "",
        "sub2_refresh_status": row.get("sub2_refresh_status") or "",
        "sub2_refresh_error": row.get("sub2_refresh_error") or "",
        "sub2_checked_at": row.get("sub2_checked_at") or "",
        "sub2_liveness_status": row.get("sub2_liveness_status") or "unknown",
        "sub2_account_status": row.get("sub2_account_status") or "",
        "sub2_schedulable": row.get("sub2_schedulable"),
        "sub2_credentials_status": row.get("sub2_credentials_status") or "",
        "sub2_temp_unschedulable_reason": row.get("sub2_temp_unschedulable_reason") or "",
        "sub2_temp_unschedulable_until": row.get("sub2_temp_unschedulable_until"),
        "sub2_session_window_start": row.get("sub2_session_window_start"),
        "sub2_session_window_end": row.get("sub2_session_window_end"),
        "sub2_session_window_status": row.get("sub2_session_window_status") or "",
        "sub2_rate_limit_reset_at": row.get("sub2_rate_limit_reset_at"),
        "sub2_rate_limited_at": row.get("sub2_rate_limited_at"),
        "sub2_overload_until": row.get("sub2_overload_until"),
        "sub2_expires_at": row.get("sub2_expires_at"),
        "sub2_last_used_at": row.get("sub2_last_used_at") or "",
        "sub2_quota_status": row.get("sub2_quota_status") or "unknown",
        "sub2_quota_plan": row.get("sub2_quota_plan") or "",
        "sub2_quota_allowed": row.get("sub2_quota_allowed"),
        "sub2_quota_limit_reached": row.get("sub2_quota_limit_reached"),
        "sub2_quota_primary_used_percent": row.get("sub2_quota_primary_used_percent"),
        "sub2_quota_primary_reset_at": row.get("sub2_quota_primary_reset_at"),
        "sub2_quota_primary_reset_after_seconds": row.get("sub2_quota_primary_reset_after_seconds"),
        "sub2_quota_primary_window_seconds": row.get("sub2_quota_primary_window_seconds"),
        "sub2_usage_checked_at": row.get("sub2_usage_checked_at") or "",
        "sub2_five_hour_utilization": row.get("sub2_five_hour_utilization"),
        "sub2_five_hour_resets_at": row.get("sub2_five_hour_resets_at") or "",
        "sub2_five_hour_remaining_seconds": row.get("sub2_five_hour_remaining_seconds"),
        "sub2_five_hour_stats": row.get("sub2_five_hour_stats") if isinstance(row.get("sub2_five_hour_stats"), dict) else {},
        "sub2_seven_day_utilization": row.get("sub2_seven_day_utilization"),
        "sub2_seven_day_resets_at": row.get("sub2_seven_day_resets_at") or "",
        "sub2_seven_day_remaining_seconds": row.get("sub2_seven_day_remaining_seconds"),
        "sub2_seven_day_stats": row.get("sub2_seven_day_stats") if isinstance(row.get("sub2_seven_day_stats"), dict) else {},
        "sub2_status_error": row.get("sub2_status_error") or "",
        "sub2_runtime": row.get("sub2_runtime") if isinstance(row.get("sub2_runtime"), dict) else {},
        "note": row.get("note") or "",
        "last_status": row.get("last_status") or "not_started",
        "last_job_id": row.get("last_job_id"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _url_host(value: str) -> str:
    try:
        return str(urlparse(value).netloc or "")
    except Exception:
        return ""


def list_accounts(
    q: str = "",
    status: str = "",
    provider: str = "",
    codex_status: str = "",
    liveness: str = "",
    email_liveness: str = "",
    quota_status: str = "",
    phone_status: str = "",
    twofa: str = "",
) -> list[dict]:
    q = str(q or "").strip().lower()
    status = str(status or "").strip()
    provider = str(provider or "").strip().lower()
    codex_status = str(codex_status or "").strip()
    liveness = str(liveness or "").strip()
    email_liveness = str(email_liveness or "").strip()
    quota_status = str(quota_status or "").strip()
    phone_status = str(phone_status or "").strip()
    twofa = str(twofa or "").strip()
    with _LOCK:
        accounts = _read(_ACCOUNTS_PATH)
        phones = _read(_PHONES_PATH)
        active_authorization_accounts = {
            str(job.get("account_id") or "")
            for job in _read(_JOBS_PATH)
            if not job.get("action") and str(job.get("status") or "") in _ACTIVE_JOB_STATUSES
        }
        _assigned, changed = _ensure_assignments_locked(accounts, phones)
        if changed:
            _write(_ACCOUNTS_PATH, accounts)
            _write(_PHONES_PATH, phones)
        rows = []
        for account in accounts:
            public = _public_account(account)
            public["authorization_in_progress"] = str(account.get("id") or "") in active_authorization_accounts
            rows.append(public)
    if q:
        rows = [x for x in rows if q in str(x.get("email") or "").lower()]
    if status:
        rows = [x for x in rows if x.get("last_status") == status]
    if provider:
        rows = [x for x in rows if x.get("email_provider") == provider]
    if codex_status:
        rows = [x for x in rows if x.get("codex_status") == codex_status]
    if liveness:
        rows = [x for x in rows if x.get("liveness_status") == liveness]
    if email_liveness:
        rows = [x for x in rows if x.get("email_liveness_status") == email_liveness]
    if quota_status:
        rows = [x for x in rows if x.get("quota_status") == quota_status]
    if phone_status == "bound":
        rows = [x for x in rows if x.get("has_phone") and x.get("has_sms_code_url")]
    elif phone_status == "unbound":
        rows = [x for x in rows if not (x.get("has_phone") and x.get("has_sms_code_url"))]
    if twofa == "enabled":
        rows = [x for x in rows if x.get("has_totp")]
    elif twofa == "disabled":
        rows = [x for x in rows if not x.get("has_totp")]
    return sorted(rows, key=lambda x: str(x.get("updated_at") or ""), reverse=True)


def _mask_phone(value: str) -> str:
    raw = _normalize_phone(value)
    prefix = "+" if raw.startswith("+") else ""
    digits = raw.lstrip("+")
    return prefix + ("*" * max(0, len(digits) - 4)) + digits[-4:]


def _phone_public_status(*, invalid: bool, reserved: bool, assigned: bool, available_uses: int) -> str:
    """Return the single status used by the phone-pool UI and API filters.

    A bound number remains visibly bound even after its reusable balance is
    exhausted.  This preserves the relationship users need to inspect, while
    unbound numbers with no remaining balance are reported as ``used``.
    """
    if invalid:
        return "invalid"
    if reserved:
        return "reserved"
    if assigned:
        return "bound"
    if available_uses <= 0:
        return "used"
    return "available"


def _sms_platform_state() -> dict:
    """Return the configured dynamic SMS source without touching storage."""
    provider = str(getattr(_codex_config, "SMS_PROVIDER", "") or "").strip().lower()
    enabled = bool(getattr(_codex_config, "SMS_POOL_PLATFORM_ENABLED", False))
    label = _SMS_PROVIDER_LABELS.get(provider, provider or "未选择平台")
    missing: list[str] = []
    if provider not in _SMS_PROVIDER_LABELS:
        missing.append("当前接码平台")
    if provider == "grizzly":
        if not str(getattr(_codex_config, "SMS_API_BASE", "") or "").strip():
            missing.append("Grizzly API 地址")
        if not str(getattr(_codex_config, "SMS_API_KEY", "") or "").strip():
            missing.append("Grizzly API Key")
    elif provider == "l":
        if not str(getattr(_codex_config, "L_API_BASE", "") or "").strip():
            missing.append("L API 地址")
        if not str(getattr(_codex_config, "L_ADMIN_AUTH_CODE", "") or "").strip():
            missing.append("L 授权码")
    elif provider == "h":
        if not str(getattr(_codex_config, "H_API_BASE", "") or "").strip():
            missing.append("H API 地址")
        if not str(getattr(_codex_config, "H_ADMIN_AUTH_CODE", "") or "").strip():
            missing.append("H 授权码")
    return {
        "provider": provider,
        "label": label,
        "enabled": enabled,
        "ready": enabled and not missing,
        "missing": missing,
        "id": f"{_SMS_PLATFORM_ROW_PREFIX}{provider or 'unknown'}",
    }


def _sms_platform_row() -> dict | None:
    state = _sms_platform_state()
    if not state["enabled"]:
        return None
    return {
        "id": state["id"],
        "phone": "",
        "sms_code_url": "",
        "sms_provider": state["label"],
        "provider": state["provider"],
        "provider_label": state["label"],
        "label": f"{state['label']} 自动取号",
        "special": True,
        "ready": state["ready"],
        "enabled": True,
        "message": "可用，授权时动态取号" if state["ready"] else "未就绪：" + "、".join(state["missing"]),
        "status": "platform" if state["ready"] else "unavailable",
        "candidate": state["ready"],
        "available_uses": None,
        "reserved_count": 0,
        "assigned": False,
        "assigned_count": 0,
        "assigned_account_email": "",
        "assigned_account_emails": [],
        "reserved": False,
        "invalid": False,
        "import_seq": -1,
        "updated_at": "",
    }


def list_phones(q: str = "", status: str = "") -> list[dict]:
    q = str(q or "").strip().lower()
    status = str(status or "").strip().lower()
    with _LOCK:
        accounts = _read(_ACCOUNTS_PATH)
        phones = _read(_PHONES_PATH)
        _assigned, changed = _ensure_assignments_locked(accounts, phones)
        if changed:
            _write(_ACCOUNTS_PATH, accounts)
            _write(_PHONES_PATH, phones)
        emails = {str(row.get("id") or ""): str(row.get("email") or "") for row in accounts}
        rows = []
        for row in phones:
            assigned_ids = _id_list(row.get("assigned_account_ids") or row.get("assigned_account_id"))
            assigned_emails = [emails[account_id] for account_id in assigned_ids if emails.get(account_id)]
            reserved_ids = _active_reservation_ids(row)
            try:
                available_uses = int(row.get("available_uses"))
            except (TypeError, ValueError):
                available_uses = _PHONE_AVAILABLE_DEFAULT
            available_uses = max(0, min(_PHONE_AVAILABLE_MAX, available_uses))
            invalid = bool(row.get("invalid"))
            public_status = _phone_public_status(
                invalid=invalid,
                reserved=bool(reserved_ids),
                assigned=bool(assigned_ids),
                available_uses=available_uses,
            )
            rows.append({
                "id": row.get("id"),
                "phone": row.get("phone") or "",
                "sms_code_url": row.get("sms_code_url") or "",
                "sms_provider": _url_host(row.get("sms_code_url") or ""),
                "has_sms_code_url": bool(row.get("sms_code_url")),
                "assigned": bool(assigned_ids),
                "assigned_count": len(assigned_ids),
                "assigned_account_email": assigned_emails[-1] if assigned_emails else "",
                "assigned_account_emails": assigned_emails,
                "reserved": bool(reserved_ids),
                "reserved_count": len(reserved_ids),
                "available_uses": available_uses,
                "invalid": invalid,
                "invalid_reason": row.get("invalid_reason") or "",
                "candidate": not invalid and len(reserved_ids) < available_uses,
                "status": public_status,
                "import_seq": int(row.get("seq") or 0),
                "updated_at": row.get("updated_at"),
            })
    special_row = _sms_platform_row()
    if q:
        rows = [
            row for row in rows
            if q in (row.get("phone") or "").lower()
            or q in (row.get("label") or "").lower()
            or q in (row.get("provider_label") or "").lower()
            or any(q in email.lower() for email in row.get("assigned_account_emails") or [])
        ]
    if status == "bound":
        rows = [row for row in rows if row.get("assigned")]
    elif status in ("available", "unbound"):
        rows = [row for row in rows if row.get("candidate")]
    elif status == "invalid":
        rows = [row for row in rows if row.get("invalid")]
    elif status == "reserved":
        rows = [row for row in rows if row.get("reserved")]
    elif status == "used":
        rows = [row for row in rows if row.get("status") == "used"]
    elif status == "platform":
        rows = []
    if special_row and (not status or status == "platform" or (status in ("available", "unbound") and special_row.get("ready"))):
        if not q or q in special_row["label"].lower() or q in special_row["provider_label"].lower():
            rows.insert(0, special_row)
    # Candidate priority is stable import order, oldest first.
    return sorted(rows, key=lambda row: (0 if row.get("special") else 1, int(row.get("import_seq") or 0), str(row.get("updated_at") or "")))


def delete_phones(phone_ids: list[str]) -> int:
    ids = {str(value) for value in phone_ids if value}
    if any(value.startswith(_SMS_PLATFORM_ROW_PREFIX) for value in ids):
        raise ValueError("接码平台特殊来源不能删除；请到设置中关闭")
    with _LOCK:
        accounts = _read(_ACCOUNTS_PATH)
        phones = _read(_PHONES_PATH)
        _ensure_assignments_locked(accounts, phones)
        selected = [row for row in phones if row.get("id") in ids]
        if any(_id_list(row.get("assigned_account_ids") or row.get("assigned_account_id")) for row in selected):
            raise ValueError("已绑定账号的手机号不能删除；请先删除对应账号")
        if any(_id_list(row.get("reserved_job_ids")) for row in selected):
            raise ValueError("任务占用中的手机号不能删除")
        kept = [row for row in phones if row.get("id") not in ids]
        _write(_PHONES_PATH, kept)
        return len(phones) - len(kept)


def adjust_phone_available_uses(phone_ids: list[str], delta: int) -> dict:
    ids = list(dict.fromkeys(str(value) for value in phone_ids if value))
    if not ids:
        raise ValueError("请先选择手机号")
    if any(value.startswith(_SMS_PLATFORM_ROW_PREFIX) for value in ids):
        raise ValueError("接码平台特殊来源没有固定次数，请到设置中管理")
    try:
        delta = int(delta)
    except (TypeError, ValueError) as exc:
        raise ValueError("可用次数每次只能增加或减少 1") from exc
    if delta not in (-1, 1):
        raise ValueError("可用次数每次只能增加或减少 1")

    with _LOCK:
        accounts = _read(_ACCOUNTS_PATH)
        phones = _read(_PHONES_PATH)
        _ensure_assignments_locked(accounts, phones)
        by_id = {str(row.get("id") or ""): row for row in phones}
        missing = [phone_id for phone_id in ids if phone_id not in by_id]
        if missing:
            raise ValueError("包含不存在的手机号素材")
        if delta < 0 and any(_active_reservation_ids(by_id[phone_id]) for phone_id in ids):
            raise ValueError("任务占用中的手机号不能减少可用次数")

        changed = []
        for phone_id in ids:
            row = by_id[phone_id]
            current = int(row.get("available_uses") or 0)
            value = max(0, min(_PHONE_AVAILABLE_MAX, current + delta))
            if value == current:
                continue
            row["available_uses"] = value
            row["updated_at"] = _now()
            changed.append({"id": phone_id, "available_uses": value})
        _write(_ACCOUNTS_PATH, accounts)
        _write(_PHONES_PATH, phones)
    return {
        "ok": True,
        "requested": len(ids),
        "updated": len(changed),
        "unchanged": len(ids) - len(changed),
        "items": changed,
    }


def _get_account(account_id: str) -> dict | None:
    with _LOCK:
        return next((dict(x) for x in _read(_ACCOUNTS_PATH) if x.get("id") == account_id), None)


def update_account(account_id: str, data: dict) -> dict:
    """Update editable account material while preserving secrets omitted by the UI."""
    account_id = str(account_id or "").strip()
    if not account_id:
        raise ValueError("账号 ID 不能为空")
    data = data if isinstance(data, dict) else {}
    with _LOCK:
        rows = _read(_ACCOUNTS_PATH)
        phones = _read(_PHONES_PATH)
        row = next((item for item in rows if str(item.get("id") or "") == account_id), None)
        if row is None:
            raise ValueError("ChatGPT账号不存在")

        new_email = str(data.get("email") or row.get("email") or "").strip().lower()
        if not _EMAIL_RE.match(new_email):
            raise ValueError("邮箱格式错误")
        if any(item is not row and str(item.get("email") or "").lower() == new_email for item in rows):
            raise ValueError("邮箱已存在")
        row["email"] = new_email

        for field in ("chatgpt_password", "mailbox_password", "email_code_url", "note"):
            if field not in data:
                continue
            value = str(data.get(field) or "").strip()
            if field == "email_code_url" and value and not _valid_url(value):
                raise ValueError("邮箱取码 URL 必须是 http(s) 地址")
            row[field] = value
            if field in ("chatgpt_password", "mailbox_password") and value:
                row["password_changed_at"] = _now()
        if data.get("clear_password"):
            row["chatgpt_password"] = ""
            row["mailbox_password"] = ""
        if data.get("password_changed"):
            row["password_changed_at"] = _now()

        if "totp_secret" in data and str(data.get("totp_secret") or "").strip():
            row["totp_secret"] = _normalize_totp_secret(data.get("totp_secret"))
            row["twofa_enabled_at"] = _now()
        if data.get("clear_totp"):
            row["totp_secret"] = ""
            row["twofa_enabled_at"] = ""

        client_id = str(data.get("outlook_client_id") or row.get("outlook_client_id") or "").strip()
        refresh_token = str(data.get("outlook_refresh_token") or row.get("outlook_refresh_token") or "").strip()
        if "outlook_client_id" in data:
            row["outlook_client_id"] = client_id
        if "outlook_refresh_token" in data:
            row["outlook_refresh_token"] = refresh_token
        if bool(row.get("outlook_client_id")) != bool(row.get("outlook_refresh_token")):
            raise ValueError("微软邮箱必须同时填写 Client_ID 和 Refresh_Token")
        if row.get("outlook_refresh_token"):
            row["email_provider"] = "outlook"
        elif row.get("email_code_url"):
            row["email_provider"] = "generic_api"

        if "phone" in data or "sms_code_url" in data:
            phone_value = _normalize_phone(data.get("phone") or "")
            sms_url = str(data.get("sms_code_url") or "").strip()
            if bool(phone_value) != bool(sms_url):
                raise ValueError("手机号与短信取码 URL 必须同时填写或同时留空")
            if sms_url and not _valid_url(sms_url):
                raise ValueError("短信取码 URL 必须是 http(s) 地址")
            if phone_value and not 7 <= len(phone_value.lstrip("+")) <= 15:
                raise ValueError("手机号长度错误")
            if phone_value:
                _upsert_phone_locked(phones, phone_value, sms_url)

        row["updated_at"] = _now()
        _validate_unique_account_phones(rows)
        _ensure_assignments_locked(rows, phones)
        _write(_ACCOUNTS_PATH, rows)
        _write(_PHONES_PATH, phones)
        return _public_account(row)


def _update_account(account_id: str, **changes) -> None:
    with _LOCK:
        rows = _read(_ACCOUNTS_PATH)
        for row in rows:
            if row.get("id") == account_id:
                row.update(changes)
                row["updated_at"] = _now()
                break
        _write(_ACCOUNTS_PATH, rows)


def _phone_lock(phone_id: str) -> threading.Lock | None:
    phone_id = str(phone_id or "")
    if not phone_id:
        return None
    with _LOCK:
        return _phone_locks.setdefault(phone_id, threading.Lock())


def _release_phone_reservation(job_id: str, phone_id: str = "") -> None:
    with _LOCK:
        phones = _read(_PHONES_PATH)
        changed = False
        for phone in phones:
            if phone_id and str(phone.get("id") or "") != str(phone_id):
                continue
            reservations = _id_list(phone.get("reserved_job_ids"))
            deferred = _id_list(phone.get("deferred_job_ids"))
            if job_id not in reservations and job_id not in deferred:
                continue
            phone["reserved_job_ids"] = [value for value in reservations if value != job_id]
            phone["deferred_job_ids"] = [value for value in deferred if value != job_id]
            phone["updated_at"] = _now()
            changed = True
        if changed:
            _write(_PHONES_PATH, phones)


def _mark_phone_invalid(phone_id: str, reason: str = "") -> None:
    """Mark a phone/URL unusable only after a real SMS attempt failed."""
    phone_id = str(phone_id or "")
    if not phone_id:
        return
    with _LOCK:
        phones = _read(_PHONES_PATH)
        changed = False
        for phone in phones:
            if str(phone.get("id") or "") != phone_id:
                continue
            phone["invalid"] = True
            phone["invalid_reason"] = str(reason or "短信接码失败")[:240]
            phone["invalid_at"] = _now()
            phone["updated_at"] = _now()
            changed = True
            break
        if changed:
            _write(_PHONES_PATH, phones)


def _phone_capacity(row: dict) -> tuple[int, int]:
    reserved = len(_active_reservation_ids(row))
    try:
        available = int(row.get("available_uses"))
    except (TypeError, ValueError):
        available = _PHONE_AVAILABLE_DEFAULT
    return reserved, max(0, min(_PHONE_AVAILABLE_MAX, available))


def _acquire_phone_for_job(
    job_id: str,
    account: dict,
    preferred_phone_ids: list[str] | None = None,
    prefer_bound: bool = True,
) -> dict:
    """Return the slot reserved for this job, or reserve one for legacy callers."""
    preferred = {str(item) for item in (preferred_phone_ids or []) if item}
    account_id = str(account.get("id") or "")
    with _LOCK:
        accounts = _read(_ACCOUNTS_PATH)
        phones = _read(_PHONES_PATH)
        _ensure_assignments_locked(accounts, phones)
        current = next((row for row in accounts if str(row.get("id") or "") == account_id), account)
        bound_key = _phone_key(current.get("phone")) if current.get("phone_verified_at") else ""
        candidates = []
        for row in phones:
            key = _phone_key(row.get("phone"))
            if not key or not row.get("sms_code_url") or row.get("invalid"):
                continue
            reserved, available = _phone_capacity(row)
            reservations = _id_list(row.get("reserved_job_ids"))
            owns_reservation = job_id in reservations
            owns_account = bool(bound_key and key == bound_key)
            if owns_reservation or reserved < available:
                candidates.append((row, owns_reservation, owns_account))
        candidates.sort(key=lambda item: (
            0 if item[1] else 1,
            0 if (prefer_bound and item[2]) else 1,
            0 if item[0].get("id") in preferred else 1,
            int(item[0].get("seq") or 0),
        ))
        chosen = candidates[0][0] if candidates else None
        if chosen is None:
            raise sms_provider.SmsProviderError("检测到手机验证页，但没有可用手机号；请导入手机号或增加可用次数")
        reservations = _id_list(chosen.get("reserved_job_ids"))
        if job_id not in reservations:
            reservations.append(job_id)
        chosen["reserved_job_ids"] = reservations
        chosen["updated_at"] = _now()
        _write(_PHONES_PATH, phones)
        return {
            "activation_id": f"relay-{job_id}-{chosen.get('id')}",
            "phone_id": chosen.get("id") or "",
            "phone": chosen.get("phone") or "",
            "code_url": chosen.get("sms_code_url") or "",
        }


def _bind_verified_phone(
    account_id: str,
    phone_id: str,
    phone_value: str,
    sms_url: str,
    *,
    consume_use: bool = True,
) -> None:
    now = _now()
    with _LOCK:
        accounts = _read(_ACCOUNTS_PATH)
        phones = _read(_PHONES_PATH)
        account = next((row for row in accounts if str(row.get("id") or "") == account_id), None)
        if account is None:
            raise ValueError("ChatGPT账号不存在")
        _ensure_assignments_locked(accounts, phones)
        pool_row = next((row for row in phones if str(row.get("id") or "") == str(phone_id or "")), None)
        if pool_row is None:
            pool_row = _upsert_phone_locked(phones, phone_value, sms_url, now=now)
        else:
            pool_row["phone"] = phone_value
            pool_row["sms_code_url"] = sms_url
        if consume_use:
            available = int(pool_row.get("available_uses") or 0)
            if available <= 0:
                raise sms_provider.SmsProviderError("手机号可用次数不足；请先增加可用次数")
            pool_row["available_uses"] = available - 1
        for other in phones:
            if other is pool_row:
                continue
            other_ids = _id_list(other.get("assigned_account_ids") or other.get("assigned_account_id"))
            if account_id in other_ids:
                other_ids = [value for value in other_ids if value != account_id]
                other["assigned_account_ids"] = other_ids
                other["assigned_account_id"] = other_ids[-1] if other_ids else ""
                other["updated_at"] = now
        assigned_ids = _id_list(pool_row.get("assigned_account_ids") or pool_row.get("assigned_account_id"))
        if account_id not in assigned_ids:
            assigned_ids.append(account_id)
        pool_row["assigned_account_ids"] = assigned_ids
        pool_row["assigned_account_id"] = account_id
        pool_row["last_verified_at"] = now
        pool_row["updated_at"] = now
        account.update(
            phone=phone_value,
            sms_code_url=sms_url,
            phone_verified_at=now,
            last_sms_phone=phone_value,
            last_sms_code_url=sms_url,
            updated_at=now,
        )
        _write(_ACCOUNTS_PATH, accounts)
        _write(_PHONES_PATH, phones)


def _credential_path_for_account(account: dict) -> Path | None:
    """Locate a locally saved Codex credential without ever exposing its contents in list APIs."""
    candidates: list[Path] = []
    result_file = str(account.get("result_file") or "").strip()
    if result_file:
        path = Path(result_file)
        if not path.is_absolute():
            path = _ROOT / path
        candidates.append(path)
    email = str(account.get("email") or "").strip().lower()
    if email:
        files = sqlite_store.list_files(
            _CREDENTIAL_DIR,
            f"codex-{email}*.json",
            category="codex_credentials",
        )
        candidates.extend(Path(item["path"]) for item in files)
    for path in candidates:
        try:
            if (
                sqlite_store.file_exists(path, category="codex_credentials")
                and path.resolve().parent == _CREDENTIAL_DIR.resolve()
            ):
                return path
        except Exception:
            continue
    return None


def _sub2_plain_line(*values: object) -> str:
    parts = [str(value or "").strip() for value in values]
    if not all(parts) or any("----" in part or "\n" in part or "\r" in part for part in parts):
        return ""
    return "----".join(parts)


def _sub2_account_line(account: dict) -> str:
    email = str(account.get("email") or "").strip().lower()
    chatgpt_password = str(account.get("chatgpt_password") or "").strip()
    email_code_url = str(account.get("email_code_url") or "").strip()
    totp_secret = str(account.get("totp_secret") or "").strip()
    mailbox_password = str(account.get("mailbox_password") or "").strip()
    outlook_client_id = str(account.get("outlook_client_id") or "").strip()
    outlook_refresh_token = str(account.get("outlook_refresh_token") or "").strip()

    if email_code_url and not any((chatgpt_password, totp_secret, mailbox_password, outlook_client_id, outlook_refresh_token)):
        plain = _sub2_plain_line(email, email_code_url)
        if plain:
            return plain
    if chatgpt_password and email_code_url and not any((totp_secret, mailbox_password, outlook_client_id, outlook_refresh_token)):
        plain = _sub2_plain_line(email, chatgpt_password, email_code_url)
        if plain:
            return plain
    if chatgpt_password and totp_secret and not any((email_code_url, mailbox_password, outlook_client_id, outlook_refresh_token)):
        plain = _sub2_plain_line(email, chatgpt_password, totp_secret)
        if plain:
            return plain
    if mailbox_password and outlook_client_id and outlook_refresh_token and not any((chatgpt_password, email_code_url, totp_secret)):
        plain = _sub2_plain_line(email, mailbox_password, outlook_client_id, outlook_refresh_token)
        if plain:
            return plain

    material = {
        key: value
        for key, value in (
            ("email", email),
            ("chatgpt_password", chatgpt_password),
            ("email_code_url", email_code_url),
            ("totp_secret", totp_secret),
            ("mailbox_password", mailbox_password),
            ("outlook_client_id", outlook_client_id),
            ("outlook_refresh_token", outlook_refresh_token),
        )
        if value
    }
    return json.dumps(material, ensure_ascii=False, separators=(",", ":"))


def _sub2_phone_line(account: dict) -> str:
    verified = bool(account.get("phone_verified_at"))
    phone = str(account.get("phone") or (account.get("last_sms_phone") if verified else "") or "").strip()
    sms_code_url = str(account.get("sms_code_url") or (account.get("last_sms_code_url") if verified else "") or "").strip()
    if not phone or not sms_code_url:
        return ""
    plain = _sub2_plain_line(phone, sms_code_url)
    if plain:
        return plain
    return json.dumps({"phone": phone, "sms_code_url": sms_code_url}, ensure_ascii=False, separators=(",", ":"))


def _sub2_account_notes(account: dict) -> str:
    return "\n".join((_SUB2_SYNC_MARKER, _sub2_account_line(account), _sub2_phone_line(account)))


def _parse_sub2_notes(remote_item: dict) -> dict:
    notes = str(remote_item.get("notes") or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = notes.split("\n")
    if not lines or lines[0].strip() != _SUB2_SYNC_MARKER:
        raise ValueError("备注没有新格式标记")
    if len(lines) == 4 and lines[3].strip() in _SUB2_LEGACY_TRAILING_MARKERS:
        lines = lines[:3]
    # Some sub2api versions trim the final blank line. Treat a two-line
    # response as the canonical empty third (phone) line.
    if len(lines) not in (2, 3):
        raise ValueError("备注必须为两或三行；仅兼容末行“注册机自动转入”的旧格式")
    account_line = lines[1].strip()
    phone_line = lines[2].strip() if len(lines) == 3 else ""
    if not account_line:
        raise ValueError("备注第二行邮箱信息不能为空")
    account = _parse_record(account_line, 2, require_login_material=False)
    if account.get("phone") or account.get("sms_code_url"):
        raise ValueError("手机号素材只能写在备注第三行")
    phone = _parse_phone_record(phone_line, 3) if phone_line else {}
    return {
        "account": account,
        "phone": phone,
        "account_line": account_line,
        "phone_line": phone_line,
        "notes": "\n".join((_SUB2_SYNC_MARKER, account_line, phone_line)),
    }


def export_credentials(account_ids: list[str], format_name: str = "rt") -> dict:
    """Return explicitly requested credential material for copy/download endpoints.

    `rt` is a line-oriented email----refresh_token format; `sub2` is used to
    build a Sub2API backup/import file; `cpa` is the original one-file-per-account JSON.
    """
    format_name = str(format_name or "rt").strip().lower()
    if format_name not in {"rt", "sub2", "cpa"}:
        raise ValueError("导出格式仅支持 rt、sub2、cpa")
    ids = list(dict.fromkeys(str(x) for x in account_ids if x))
    if not ids:
        raise ValueError("请先选择账号")
    if len(ids) > 1000:
        raise ValueError("单次最多导出 1000 个账号")
    with _LOCK:
        rows = _read(_ACCOUNTS_PATH)
        known = {str(row.get("id") or ""): row for row in rows}
    items: list[dict] = []
    errors: list[dict] = []
    for account_id in ids:
        account = known.get(account_id)
        if not account:
            errors.append({"account_id": account_id, "error": "ChatGPT账号不存在"})
            continue
        path = _credential_path_for_account(account)
        if not path:
            errors.append({"account_id": account_id, "email": account.get("email"), "error": "未找到本地 Codex 凭证，请先完成授权"})
            continue
        try:
            data = json.loads(sqlite_store.read_text_file(path, category="codex_credentials"))
            if not isinstance(data, dict):
                raise ValueError("凭证不是 JSON 对象")
            refresh_token = str(data.get("refresh_token") or data.get("refreshToken") or "").strip()
            if not refresh_token:
                raise ValueError("凭证缺少 refresh_token")
            items.append({
                "account_id": account_id,
                "email": account.get("email"),
                "filename": path.name,
                "data": data,
                "refresh_token": refresh_token,
                "sub2_notes": _sub2_account_notes(account),
            })
        except Exception as exc:
            errors.append({"account_id": account_id, "email": account.get("email"), "error": f"{type(exc).__name__}: {exc}"})
    return {"ok": bool(items), "format": format_name, "items": items, "errors": errors, "count": len(items)}


def _sub2_service_view(row: dict) -> dict:
    return {
        "id": str(row.get("id") or ""),
        "name": str(row.get("name") or ""),
        "homepage": str(row.get("homepage") or ""),
        "api_base": str(row.get("api_base") or ""),
        "admin_key": str(row.get("admin_key") or ""),
        # Some self-hosted sub2api deployments use an internal certificate.
        # Keep verification configurable per service; legacy entries default
        # to disabled so their existing import/sync behavior keeps working.
        "verify_ssl": _sub2_verify_ssl(row),
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
    }


def list_sub2_services() -> list[dict]:
    with _LOCK:
        rows = _read(_SUB2_SERVICES_PATH)
    return [_sub2_service_view(row) for row in rows]


def save_sub2_service(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("服务配置必须是 JSON 对象")
    service_id = str(data.get("id") or "").strip()
    name = str(data.get("name") or "").strip()
    homepage = str(data.get("homepage") or "").strip().rstrip("/")
    api_base = str(data.get("api_base") or "").strip().rstrip("/")
    admin_key = str(data.get("admin_key") or "").strip()
    if not name:
        raise ValueError("请填写服务名称")
    if len(name) > 100:
        raise ValueError("服务名称不能超过 100 个字符")
    if homepage and not _valid_url(homepage):
        raise ValueError("服务官网必须是 http(s) 地址")
    if not _valid_url(api_base):
        raise ValueError("sub2api 服务地址必须是 http(s) 地址")
    parsed = urlparse(api_base)
    if parsed.query or parsed.fragment:
        raise ValueError("sub2api 服务地址不能包含查询参数或锚点")
    if not admin_key:
        raise ValueError("请填写管理员密钥")
    if len(admin_key) > 8192:
        raise ValueError("管理员密钥长度异常")

    with _LOCK:
        rows = _read(_SUB2_SERVICES_PATH)
        now = _now()
        row = next((item for item in rows if str(item.get("id") or "") == service_id), None)
        if service_id and row is None:
            raise ValueError("sub2api 服务不存在")
        if row is None:
            if len(rows) >= 100:
                raise ValueError("最多保存 100 个 sub2api 服务")
            row = {"id": uuid.uuid4().hex, "created_at": now}
            rows.append(row)
        row.update({
            "name": name,
            "homepage": homepage,
            "api_base": api_base,
            "admin_key": admin_key,
            "verify_ssl": _sub2_verify_ssl(data),
            "updated_at": now,
        })
        _write(_SUB2_SERVICES_PATH, rows)
        return _sub2_service_view(row)


def delete_sub2_service(service_id: str) -> int:
    service_id = str(service_id or "").strip()
    if not service_id:
        raise ValueError("缺少服务 ID")
    with _LOCK:
        rows = _read(_SUB2_SERVICES_PATH)
        kept = [row for row in rows if str(row.get("id") or "") != service_id]
        if len(kept) == len(rows):
            raise ValueError("sub2api 服务不存在")
        _write(_SUB2_SERVICES_PATH, kept)
    return 1


def _sub2_import_endpoint(api_base: str) -> str:
    base = str(api_base or "").strip().rstrip("/")
    suffix = "/api/v1/admin/accounts/import/codex-session"
    if base.endswith(suffix):
        return base
    if base.endswith("/api/v1/admin"):
        return base + "/accounts/import/codex-session"
    if base.endswith("/api/v1"):
        return base + "/admin/accounts/import/codex-session"
    return base + suffix


def _sub2_accounts_endpoint(api_base: str) -> str:
    base = str(api_base or "").strip().rstrip("/")
    import_suffix = "/api/v1/admin/accounts/import/codex-session"
    if base.endswith(import_suffix):
        return base[: -len("/import/codex-session")]
    if base.endswith("/api/v1/admin/accounts"):
        return base
    if base.endswith("/api/v1/admin"):
        return base + "/accounts"
    if base.endswith("/api/v1"):
        return base + "/admin/accounts"
    return base + "/api/v1/admin/accounts"


def _sub2_headers(admin_key: str, *, idempotency_key: str = "") -> dict[str, str]:
    headers = {
        "x-api-key": admin_key,
        "Accept": "application/json",
        "User-Agent": "turb-gpt-free-register/1.0",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _sub2_verify_ssl(service: dict | None) -> bool:
    """Return the per-service TLS verification setting.

    The relay historically connected to self-signed sub2api installations, so
    missing/legacy values intentionally retain the old ``verify=False``
    behavior.  New services can opt into normal certificate verification by
    setting ``verify_ssl`` to a truthy value.
    """
    if not isinstance(service, dict):
        return False
    value = service.get("verify_ssl")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _sub2_service(service_id: str) -> dict:
    with _LOCK:
        service = next(
            (row for row in _read(_SUB2_SERVICES_PATH) if str(row.get("id") or "") == str(service_id or "")),
            None,
        )
    if service is None:
        raise ValueError("请选择已保存的 sub2api 服务")
    return service


def _sub2_public_service(service: dict) -> dict:
    return {
        "id": str(service.get("id") or ""),
        "name": str(service.get("name") or ""),
        "homepage": str(service.get("homepage") or ""),
        "api_base": str(service.get("api_base") or ""),
    }


def _sub2_safe_detail(value: object, admin_key: str, *, limit: int = 500) -> str:
    detail = str(value or "未知错误")
    if admin_key:
        detail = detail.replace(admin_key, "[已隐藏]")
    return detail[:limit]


def _fetch_sub2_accounts(service: dict) -> tuple[list[dict], int]:
    import requests

    endpoint = _sub2_accounts_endpoint(service.get("api_base") or "")
    admin_key = str(service.get("admin_key") or "")
    rows: list[dict] = []
    reported_total = 0
    page = 1
    while page <= _SUB2_MAX_PAGES:
        try:
            response = requests.get(
                endpoint,
                headers=_sub2_headers(admin_key),
                params={
                    "page": page,
                    "page_size": _SUB2_PAGE_SIZE,
                    "platform": "openai",
                    "type": "oauth",
                    "sort_by": "id",
                    "sort_order": "asc",
                    "lite": 1,
                },
                timeout=30,
                verify=_sub2_verify_ssl(service),
            )
        except requests.RequestException as exc:
            detail = _sub2_safe_detail(f"{type(exc).__name__}: {exc}", admin_key)
            raise RuntimeError(f"连接 sub2api 服务失败：{detail}") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if response.status_code < 200 or response.status_code >= 300:
            detail = payload.get("error") or payload.get("message") or response.text[:500]
            raise RuntimeError(f"读取 sub2api 账号失败（HTTP {response.status_code}）：{_sub2_safe_detail(detail, admin_key)}")
        if payload.get("ok") is False or payload.get("code") not in (None, 0):
            detail = payload.get("error") or payload.get("message") or "服务返回业务失败"
            raise RuntimeError(f"读取 sub2api 账号失败：{_sub2_safe_detail(detail, admin_key)}")
        data = _sub2_response_data(payload)
        items = data.get("items")
        if not isinstance(items, list):
            raise RuntimeError("读取 sub2api 账号失败：响应缺少 data.items")
        rows.extend(item for item in items if isinstance(item, dict))
        try:
            reported_total = max(reported_total, int(data.get("total") or 0))
            pages = max(1, int(data.get("pages") or 1))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("读取 sub2api 账号失败：分页字段格式错误") from exc
        if page >= pages:
            return rows, reported_total or len(rows)
        page += 1
    raise RuntimeError(f"读取 sub2api 账号失败：分页超过安全上限 {_SUB2_MAX_PAGES}")


_SUB2_DEAD_STATUSES = {
    "deleted", "disabled", "deactivated", "banned", "revoked", "expired", "invalid", "error",
}
_SUB2_DEAD_CREDENTIAL_STATUSES = {
    "deleted", "disabled", "deactivated", "banned", "revoked", "expired", "invalid", "error",
}


def _sub2_admin_base(service: dict) -> str:
    accounts_endpoint = _sub2_accounts_endpoint(service.get("api_base") or "")
    marker = "/accounts"
    if marker not in accounts_endpoint:
        raise ValueError("sub2api 管理接口地址格式错误")
    return accounts_endpoint.rsplit(marker, 1)[0]


def _sub2_get_json(service: dict, path: str, *, timeout: float = 30) -> dict:
    """GET one sub2api admin resource and return its ``data`` object."""
    import requests

    base = _sub2_admin_base(service).rstrip("/")
    url = f"{base}/{str(path or '').lstrip('/')}"
    admin_key = str(service.get("admin_key") or "")
    try:
        response = requests.get(
            url,
            headers=_sub2_headers(admin_key),
            timeout=timeout,
            verify=_sub2_verify_ssl(service),
        )
    except requests.RequestException as exc:
        detail = _sub2_safe_detail(f"{type(exc).__name__}: {exc}", admin_key)
        raise RuntimeError(f"连接 sub2api 服务失败：{detail}") from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if response.status_code < 200 or response.status_code >= 300:
        detail = payload.get("error") or payload.get("message") or response.text[:500] or "未知错误"
        raise RuntimeError(f"sub2api 查询失败（HTTP {response.status_code}）：{_sub2_safe_detail(detail, admin_key)}")
    if payload.get("ok") is False or payload.get("code") not in (None, 0):
        detail = payload.get("error") or payload.get("message") or "服务返回业务失败"
        raise RuntimeError(f"sub2api 查询失败：{_sub2_safe_detail(detail, admin_key)}")
    data = _sub2_response_data(payload)
    if not isinstance(data, dict):
        raise RuntimeError("sub2api 查询失败：响应 data 不是对象")
    return data


def _sub2_post_json(
    service: dict,
    path: str,
    body: dict,
    *,
    timeout: float = 60,
    idempotency_key: str = "",
) -> dict:
    """POST one sub2api admin resource and return its ``data`` object."""
    import requests

    base = _sub2_admin_base(service).rstrip("/")
    url = f"{base}/{str(path or '').lstrip('/')}"
    admin_key = str(service.get("admin_key") or "")
    try:
        response = requests.post(
            url,
            headers={
                **_sub2_headers(admin_key, idempotency_key=idempotency_key),
                "Content-Type": "application/json",
            },
            json=body,
            timeout=timeout,
            verify=_sub2_verify_ssl(service),
        )
    except requests.RequestException as exc:
        detail = _sub2_safe_detail(f"{type(exc).__name__}: {exc}", admin_key)
        raise RuntimeError(f"连接 sub2api 服务失败：{detail}") from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if response.status_code < 200 or response.status_code >= 300:
        detail = payload.get("error") or payload.get("message") or response.text[:500] or "未知错误"
        raise RuntimeError(f"sub2api 更新失败（HTTP {response.status_code}）：{_sub2_safe_detail(detail, admin_key)}")
    if payload.get("ok") is False or payload.get("code") not in (None, 0):
        detail = payload.get("error") or payload.get("message") or "服务返回业务失败"
        raise RuntimeError(f"sub2api 更新失败：{_sub2_safe_detail(detail, admin_key)}")
    data = _sub2_response_data(payload)
    if not isinstance(data, dict):
        raise RuntimeError("sub2api 更新失败：响应 data 不是对象")
    return data


def _sub2_scalar(value: object):
    """Keep numeric API values numeric while avoiding bool/string surprises."""
    if value in (None, "") or isinstance(value, bool):
        return None if value in (None, "") else bool(value)
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        return text
    return int(number) if number.is_integer() else number


def _sub2_window_snapshot(value: object) -> dict:
    """Return the public, non-credential portion of a usage window."""
    raw = value if isinstance(value, dict) else {}
    result = {}
    for key in ("utilization", "resets_at", "remaining_seconds"):
        if key in raw and raw.get(key) not in (None, ""):
            result[key] = _sub2_scalar(raw.get(key))
    stats = raw.get("window_stats")
    if isinstance(stats, dict):
        safe_stats = {}
        for key in ("requests", "tokens", "cost", "standard_cost", "user_cost"):
            if key in stats and stats.get(key) not in (None, ""):
                safe_stats[key] = _sub2_scalar(stats.get(key))
        if safe_stats:
            result["window_stats"] = safe_stats
    return result


def _sub2_remote_snapshot(value: dict) -> dict:
    """Select account-list fields that describe scheduling/credential health."""
    raw = value if isinstance(value, dict) else {}
    fields = (
        "id", "status", "schedulable", "temp_unschedulable_reason", "temp_unschedulable_until",
        "session_window_start", "session_window_end", "session_window_status",
        "rate_limit_reset_at", "rate_limited_at", "overload_until", "expires_at",
        "credentials_status", "last_used_at", "updated_at",
    )
    result = {}
    for key in fields:
        if key not in raw or raw.get(key) in (None, ""):
            continue
        result[key] = _sub2_scalar(raw.get(key)) if key == "schedulable" else raw.get(key)
    return result


def _sub2_liveness_status(remote: dict) -> str:
    status = str(remote.get("status") or "").strip().lower()
    credentials = str(remote.get("credentials_status") or "").strip().lower()
    if status in _SUB2_DEAD_STATUSES or credentials in _SUB2_DEAD_CREDENTIAL_STATUSES:
        return "dead"
    if status in {"active", "enabled", "available", "ok", "ready"}:
        return "alive" if remote.get("schedulable") is not False else "blocked"
    if remote.get("schedulable") is True:
        return "alive"
    return "unknown"


def _sub2_link_for_account(account: dict, service_id: str = "") -> tuple[dict, str]:
    explicit_service_id = str(service_id or "").strip()
    service_key = explicit_service_id or str(account.get("sub2_service_id") or "").strip()
    links = account.get("sub2_links") if isinstance(account.get("sub2_links"), dict) else {}
    if not service_key and len(links) == 1:
        service_key = str(next(iter(links.keys())) or "").strip()
    if not service_key:
        # A single saved service is unambiguous.  It lets accounts created
        # before the strict three-line note contract be matched by email on
        # their first status refresh, after which the remote ID is retained.
        with _LOCK:
            services = _read(_SUB2_SERVICES_PATH)
        if len(services) == 1:
            service_key = str(services[0].get("id") or "").strip()
        elif not services:
            raise ValueError("未配置 sub2api 服务")
        else:
            raise ValueError("账号尚未关联 sub2api 服务；请先从 sub2api 同步或指定服务")
    service = _sub2_service(service_key)
    link = links.get(service_key) if isinstance(links.get(service_key), dict) else {}
    remote_id = str(
        (link.get("account_id") if isinstance(link, dict) else "")
        or (account.get("sub2_account_id") if str(account.get("sub2_service_id") or "") == service_key else "")
        or ""
    ).strip()
    return service, remote_id


def _find_sub2_remote_account(
    account: dict,
    service_id: str = "",
    *,
    remote_rows: list[dict] | None = None,
) -> tuple[dict, str, dict]:
    """Resolve a local account to exactly one remote OAuth account.

    A saved remote ID wins.  Before the first link exists, a single service
    can safely use the account email as the identity; ambiguous duplicates are
    rejected instead of updating an arbitrary remote record.
    """
    service, linked_remote_id = _sub2_link_for_account(account, service_id)
    if remote_rows is None:
        remote_rows, _total = _fetch_sub2_accounts(service)

    def is_openai_oauth(item: object) -> bool:
        return (
            isinstance(item, dict)
            and str(item.get("platform") or "").strip().lower() == "openai"
            and str(item.get("type") or "").strip().lower() == "oauth"
        )

    def ensure_writable(item: dict) -> None:
        if not is_openai_oauth(item):
            raise RuntimeError("sub2api 对应账号不是 OpenAI OAuth 账号，不能写入 Codex 凭证")
        parent_id = item.get("parent_account_id")
        if parent_id not in (None, "", 0, "0"):
            raise RuntimeError("sub2api 对应账号是影子账号，不能写入独立 Codex 凭证")

    remote_id = str(linked_remote_id or "").strip()
    if remote_id:
        remote = next(
            (item for item in remote_rows if str(item.get("id") or item.get("account_id") or "") == remote_id),
            None,
        )
        if isinstance(remote, dict):
            ensure_writable(remote)
            return service, remote_id, remote
    email = str(account.get("email") or "").strip().lower()
    matches = [
        item for item in remote_rows
        if str(item.get("name") or "").strip().lower() == email and is_openai_oauth(item)
    ]
    if not matches:
        raise Sub2RemoteAccountNotFound("sub2api 中找不到对应账号")
    if len(matches) > 1:
        raise RuntimeError("sub2api 中同一邮箱存在多个账号，无法安全确定要更新的记录")
    remote = matches[0]
    remote_id = str(remote.get("id") or remote.get("account_id") or "").strip()
    if not remote_id:
        raise RuntimeError("sub2api 对应账号缺少账号 ID")
    ensure_writable(remote)
    return service, remote_id, remote


def _delete_sub2_remote_account(service: dict, remote_id: str) -> bool:
    """Delete one remote account; return False when it is already absent."""
    import requests

    endpoint = f"{_sub2_accounts_endpoint(service.get('api_base') or '')}/{quote(str(remote_id), safe='')}"
    admin_key = str(service.get("admin_key") or "")
    try:
        response = requests.delete(
            endpoint,
            headers=_sub2_headers(admin_key, idempotency_key=f"relay-delete-{uuid.uuid4().hex}"),
            timeout=30,
            verify=_sub2_verify_ssl(service),
        )
    except requests.RequestException as exc:
        detail = _sub2_safe_detail(f"{type(exc).__name__}: {exc}", admin_key)
        raise RuntimeError(f"连接 sub2api 服务失败：{detail}") from exc
    if response.status_code == 404:
        return False
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if response.status_code < 200 or response.status_code >= 300:
        detail = payload.get("error") or payload.get("message") or response.text[:500] or "未知错误"
        raise RuntimeError(
            f"sub2api 删除失败（HTTP {response.status_code}）：{_sub2_safe_detail(detail, admin_key)}"
        )
    if payload.get("ok") is False or payload.get("code") not in (None, 0):
        detail = payload.get("error") or payload.get("message") or "服务返回业务失败"
        raise RuntimeError(f"sub2api 删除失败：{_sub2_safe_detail(detail, admin_key)}")
    return True


def _record_sub2_terminal_deletions(service: dict, updates: list[dict]) -> None:
    if not updates:
        return
    service_id = str(service.get("id") or "")
    by_local_id = {str(item.get("local_account_id") or ""): item for item in updates}
    now = _now()
    with _LOCK:
        rows = _read(_ACCOUNTS_PATH)
        changed = False
        for account in rows:
            update = by_local_id.get(str(account.get("id") or ""))
            if not update:
                continue
            remote_id = str(update.get("remote_account_id") or "")
            account.update({
                "sub2_service_id": service_id,
                "sub2_account_id": remote_id,
                "sub2_status": "deleted",
                "sub2_deleted_at": now,
                "sub2_synced_at": now,
                "updated_at": now,
            })
            links = account.get("sub2_links")
            if not isinstance(links, dict):
                links = {}
                account["sub2_links"] = links
            link = links.get(service_id) if isinstance(links.get(service_id), dict) else {}
            link.update({
                "account_id": remote_id,
                "status": "deleted",
                "deleted_at": now,
                "synced_at": now,
            })
            links[service_id] = link
            changed = True
        if changed:
            _write(_ACCOUNTS_PATH, rows)


def _delete_terminal_accounts_from_sub2(accounts: list[dict], service: dict) -> dict:
    result = {"requested": len(accounts), "deleted": 0, "already_absent": 0, "failed": 0, "errors": []}
    if not accounts:
        return result
    remote_rows, _total = _fetch_sub2_accounts(service)
    resolved_ids: set[str] = set()
    updates: list[dict] = []
    for account in accounts:
        local_id = str(account.get("id") or "")
        email = str(account.get("email") or "")
        try:
            _resolved_service, remote_id, _remote = _find_sub2_remote_account(
                account,
                str(service.get("id") or ""),
                remote_rows=remote_rows,
            )
            if remote_id in resolved_ids:
                raise RuntimeError("多个本地账号指向同一个 sub2api 账号，已停止重复删除")
            resolved_ids.add(remote_id)
            was_deleted = _delete_sub2_remote_account(service, remote_id)
            result["deleted" if was_deleted else "already_absent"] += 1
            updates.append({"local_account_id": local_id, "remote_account_id": remote_id})
        except Sub2RemoteAccountNotFound:
            _linked_service, linked_id = _sub2_link_for_account(account, str(service.get("id") or ""))
            result["already_absent"] += 1
            updates.append({"local_account_id": local_id, "remote_account_id": linked_id})
        except Exception as exc:
            result["failed"] += 1
            result["errors"].append({
                "account_id": local_id,
                "email": email,
                "error": _redact(str(exc))[:500],
            })
    _record_sub2_terminal_deletions(service, updates)
    return result


def _sub2_runtime_changes(service: dict, account: dict, remote: dict, quota: dict, usage: dict, errors: list[str]) -> dict:
    now = _now()
    remote_safe = _sub2_remote_snapshot(remote)
    quota_rate = quota.get("rate_limit") if isinstance(quota.get("rate_limit"), dict) else {}
    primary = quota_rate.get("primary_window") if isinstance(quota_rate.get("primary_window"), dict) else {}
    five_hour = _sub2_window_snapshot(usage.get("five_hour"))
    seven_day = _sub2_window_snapshot(usage.get("seven_day"))
    runtime = {
        "checked_at": now,
        "service_id": str(service.get("id") or ""),
        "account_id": str(remote_safe.get("id") or account.get("sub2_account_id") or ""),
        "account": remote_safe,
        "quota": {
            "plan_type": quota.get("plan_type") or "",
            "allowed": quota_rate.get("allowed"),
            "limit_reached": quota_rate.get("limit_reached"),
            "primary_window": {
                key: _sub2_scalar(primary.get(key))
                for key in ("used_percent", "limit_window_seconds", "reset_after_seconds", "reset_at")
                if primary.get(key) not in (None, "")
            },
            "fetched_at": quota.get("fetched_at"),
        },
        "usage": {
            "updated_at": usage.get("updated_at") or "",
            "five_hour": five_hour,
            "seven_day": seven_day,
        },
        "errors": [str(item)[:500] for item in errors[:8]],
    }
    result = {
        "sub2_checked_at": now,
        "sub2_liveness_status": _sub2_liveness_status(remote),
        "sub2_account_status": remote_safe.get("status") or "",
        "sub2_schedulable": remote_safe.get("schedulable"),
        "sub2_credentials_status": remote_safe.get("credentials_status") or "",
        "sub2_temp_unschedulable_reason": remote_safe.get("temp_unschedulable_reason") or "",
        "sub2_temp_unschedulable_until": remote_safe.get("temp_unschedulable_until"),
        "sub2_session_window_start": remote_safe.get("session_window_start"),
        "sub2_session_window_end": remote_safe.get("session_window_end"),
        "sub2_session_window_status": remote_safe.get("session_window_status") or "",
        "sub2_rate_limit_reset_at": remote_safe.get("rate_limit_reset_at"),
        "sub2_rate_limited_at": remote_safe.get("rate_limited_at"),
        "sub2_overload_until": remote_safe.get("overload_until"),
        "sub2_expires_at": remote_safe.get("expires_at"),
        "sub2_last_used_at": remote_safe.get("last_used_at") or "",
        "sub2_quota_status": "available" if quota else "error",
        "sub2_quota_plan": quota.get("plan_type") or "",
        "sub2_quota_allowed": quota_rate.get("allowed"),
        "sub2_quota_limit_reached": quota_rate.get("limit_reached"),
        "sub2_quota_primary_used_percent": _sub2_scalar(primary.get("used_percent")),
        "sub2_quota_primary_reset_at": _sub2_scalar(primary.get("reset_at")),
        "sub2_quota_primary_reset_after_seconds": _sub2_scalar(primary.get("reset_after_seconds")),
        "sub2_quota_primary_window_seconds": _sub2_scalar(primary.get("limit_window_seconds")),
        "sub2_usage_checked_at": usage.get("updated_at") or (now if usage else ""),
        "sub2_five_hour_utilization": _sub2_scalar(five_hour.get("utilization")),
        "sub2_five_hour_resets_at": five_hour.get("resets_at"),
        "sub2_five_hour_remaining_seconds": _sub2_scalar(five_hour.get("remaining_seconds")),
        "sub2_five_hour_stats": five_hour.get("window_stats") or {},
        "sub2_seven_day_utilization": _sub2_scalar(seven_day.get("utilization")),
        "sub2_seven_day_resets_at": seven_day.get("resets_at"),
        "sub2_seven_day_remaining_seconds": _sub2_scalar(seven_day.get("remaining_seconds")),
        "sub2_seven_day_stats": seven_day.get("window_stats") or {},
        "sub2_status_error": "；".join(str(item)[:500] for item in errors[:8]),
        "sub2_runtime": runtime,
    }
    return result


def sync_sub2_account_status(account_id: str, service_id: str = "") -> dict:
    """Refresh one local account from sub2api status, quota and usage APIs."""
    account = _get_account(str(account_id or ""))
    if not account:
        raise ValueError("ChatGPT账号不存在")
    service, remote_id, remote = _find_sub2_remote_account(account, service_id)
    errors: list[str] = []
    quota: dict = {}
    usage: dict = {}
    encoded_id = quote(remote_id, safe="")
    try:
        quota = _sub2_get_json(service, f"openai/accounts/{encoded_id}/quota")
    except Exception as exc:
        errors.append(str(exc))
    try:
        usage = _sub2_get_json(service, f"accounts/{encoded_id}/usage")
    except Exception as exc:
        errors.append(str(exc))
    changes = _sub2_runtime_changes(service, account, remote, quota, usage, errors)
    changes.update({
        "sub2_service_id": str(service.get("id") or ""),
        "sub2_account_id": remote_id,
        "sub2_status": str(remote.get("status") or account.get("sub2_status") or "synced"),
    })
    _update_account(str(account_id), **changes)
    return {
        # The account-list response is already a successful liveness/scheduler
        # refresh. Quota or usage can be unavailable independently on older
        # sub2api versions, so represent that as a partial result instead of
        # discarding the useful status data.
        "ok": True,
        "partial": bool(errors),
        "account_id": str(account_id),
        "email": str(account.get("email") or ""),
        "service": _sub2_public_service(service),
        "remote_account_id": remote_id,
        "errors": errors,
        "runtime": changes,
    }


def _credential_text(data: dict, *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _is_sub2_personal_access_token_mode(value: object) -> bool:
    return str(value or "").strip().lower() in {
        "personalaccesstoken",
        "personal_access_token",
        "personal-access-token",
        "pat",
        "codex_pat",
    }


def _sub2_oauth_credential_payload(
    credential: dict,
    email: str,
    existing_credentials: dict | None = None,
) -> dict:
    """Merge fresh local OAuth fields into redacted sub2api credentials.

    sub2api intentionally preserves omitted sensitive values but treats the
    non-sensitive part of ``credentials`` as a replacement.  Starting with
    the remote redacted object therefore keeps model mappings, header
    overrides and capability flags while the fresh local token replaces only
    the OAuth identity fields.
    """
    if not isinstance(credential, dict):
        raise ValueError("本地 Codex 凭证不是 JSON 对象")
    access_token = _credential_text(credential, "access_token", "accessToken")
    refresh_token = _credential_text(credential, "refresh_token", "refreshToken")
    if not access_token:
        raise ValueError("本地 Codex 凭证缺少 access_token")
    if not refresh_token:
        raise ValueError("本地 Codex 凭证缺少 refresh_token，无法刷新 sub2api 账号")
    result = dict(existing_credentials) if isinstance(existing_credentials, dict) else {}
    # A previous Codex PAT import can leave these OAuth-incompatible markers.
    # Do not turn a fresh refresh-token OAuth credential back into a PAT.
    pat_mode = _is_sub2_personal_access_token_mode(result.get("auth_mode")) or _is_sub2_personal_access_token_mode(result.get("openai_auth_mode"))
    if pat_mode:
        for key in ("auth_mode", "openai_auth_mode", "token_type", "chatgpt_account_is_fedramp"):
            result.pop(key, None)
    result.update({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "email": str(email or _credential_text(credential, "email")).strip().lower(),
    })
    optional_values = {
        "id_token": _credential_text(credential, "id_token", "idToken"),
        "expires_at": _credential_text(credential, "expires_at", "expiresAt", "expired"),
        "chatgpt_account_id": _credential_text(credential, "chatgpt_account_id", "account_id", "accountId"),
        "chatgpt_user_id": _credential_text(credential, "chatgpt_user_id", "user_id", "userId"),
        "organization_id": _credential_text(credential, "organization_id", "organizationId", "org_id", "orgId"),
        "plan_type": _credential_text(credential, "plan_type", "planType"),
    }
    for key, value in optional_values.items():
        if value:
            result[key] = value
    client_id = _credential_text(credential, "client_id", "clientId")
    if not client_id:
        from config import codex as codex_cfg
        client_id = str(getattr(codex_cfg, "CODEX_CLIENT_ID", "") or "").strip()
    if client_id:
        result["client_id"] = client_id
    return result


def _refresh_local_codex_credential(account: dict) -> tuple[dict, Path]:
    path = _credential_path_for_account(account)
    if not path:
        raise RuntimeError("未找到本地 Codex 凭证，请先完成 Codex 授权")
    try:
        existing = json.loads(sqlite_store.read_text_file(path, category="codex_credentials"))
    except Exception as exc:
        raise RuntimeError(f"无法读取 Codex 凭证：{type(exc).__name__}") from exc
    if not isinstance(existing, dict):
        raise RuntimeError("本地 Codex 凭证不是 JSON 对象")
    from core.codex_oauth import CodexTokenRefreshError, refresh_codex_credential

    try:
        refreshed = refresh_codex_credential(path, proxy="")
    except CodexTokenRefreshError as exc:
        if exc.reauthorization_required:
            _update_account(str(account.get("id") or ""), codex_status="reauthorize")
        raise
    if not isinstance(refreshed, dict):
        raise RuntimeError("刷新本地 Codex 凭证失败：响应不是 JSON 对象")
    return refreshed, path


def refresh_sub2_account(account_id: str, service_id: str = "") -> dict:
    """Apply the latest local OAuth credential to an existing sub2api account.

    ``apply-oauth-credentials`` deliberately updates credentials only; remote
    concurrency, priority, proxy, groups and rate-limit configuration remain
    owned by sub2api.  A following remote refresh proves the new RT is usable
    and renews the remote access token/cache.
    """
    account = _get_account(str(account_id or ""))
    if not account:
        raise ValueError("ChatGPT账号不存在")
    try:
        service, remote_id, remote = _find_sub2_remote_account(account, service_id)
    except Exception as exc:
        _update_account(
            str(account_id),
            sub2_credentials_update_status="failed",
            sub2_refresh_status="failed",
            sub2_refresh_error=_redact(str(exc))[:1000],
        )
        raise
    encoded_id = quote(remote_id, safe="")
    _update_account(
        str(account_id),
        sub2_credentials_update_status="running",
        sub2_refresh_status="running",
        sub2_refresh_error="",
    )
    try:
        credential, credential_path = _refresh_local_codex_credential(account)
        existing_credentials = remote.get("credentials") if isinstance(remote, dict) else None
        if not isinstance(existing_credentials, dict):
            # Older sub2 list responses may omit credentials even though the
            # detail endpoint still returns the redacted non-sensitive map.
            detail = _sub2_get_json(service, f"accounts/{encoded_id}")
            existing_credentials = detail.get("credentials") if isinstance(detail, dict) else None
        if not isinstance(existing_credentials, dict):
            raise RuntimeError("sub2api 未返回远端凭证配置，已停止更新以避免覆盖路由设置")
        remote_credentials = _sub2_oauth_credential_payload(
            credential,
            str(account.get("email") or ""),
            existing_credentials=existing_credentials,
        )
        _sub2_post_json(
            service,
            f"accounts/{encoded_id}/apply-oauth-credentials",
            {"type": "oauth", "credentials": remote_credentials},
            idempotency_key=f"relay-apply-oauth-{uuid.uuid4().hex}",
        )
    except Exception as exc:
        detail = _redact(str(exc))[:1000]
        _update_account(
            str(account_id),
            sub2_credentials_update_status="failed",
            sub2_refresh_status="failed",
            sub2_refresh_error=detail,
        )
        raise

    errors: list[str] = []
    refresh_succeeded = False
    try:
        _sub2_post_json(
            service,
            f"accounts/{encoded_id}/refresh",
            {},
            idempotency_key=f"relay-refresh-oauth-{uuid.uuid4().hex}",
        )
        refresh_succeeded = True
    except Exception as exc:
        # The credential update already succeeded.  Keep that fact, surface
        # a partial result, and let the user retry just the remote refresh.
        errors.append(str(exc))

    status_result = {}
    try:
        status_result = sync_sub2_account_status(str(account_id), str(service.get("id") or ""))
        errors.extend(str(item) for item in (status_result.get("errors") or []))
    except Exception as exc:
        errors.append(f"状态读取失败：{exc}")

    now = _now()
    changes = {
        "codex_status": "authorized",
        "gpt_access_token": str(credential.get("access_token") or ""),
        "result_file": str(credential_path),
        "sub2_service_id": str(service.get("id") or ""),
        "sub2_account_id": remote_id,
        "sub2_credentials_updated_at": now,
        "sub2_credentials_update_status": "updated",
        "sub2_refresh_at": now if refresh_succeeded else account.get("sub2_refresh_at") or "",
        "sub2_refresh_status": "refreshed" if refresh_succeeded else "failed",
        "sub2_refresh_error": "；".join(errors[:8]),
    }
    _update_account(str(account_id), **changes)
    return {
        "ok": True,
        "partial": bool(errors),
        "account_id": str(account_id),
        "email": str(account.get("email") or ""),
        "service": _sub2_public_service(service),
        "remote_account_id": remote_id,
        "remote_refreshed": refresh_succeeded,
        "errors": errors[:8],
        "status": status_result,
    }


def _sub2_link_values(service: dict, remote_item: dict, notes: str, synced_at: str) -> dict:
    return {
        "sub2_service_id": str(service.get("id") or ""),
        "sub2_account_id": str(remote_item.get("id") or remote_item.get("account_id") or ""),
        "sub2_status": str(remote_item.get("status") or "synced"),
        "sub2_synced_at": synced_at,
        "sub2_notes": notes,
    }


def _set_sub2_link(account: dict, service: dict, remote_item: dict, notes: str, synced_at: str) -> bool:
    values = _sub2_link_values(service, remote_item, notes, synced_at)
    changed = any(account.get(key) != value for key, value in values.items() if key != "sub2_synced_at")
    account.update(values)
    service_id = values["sub2_service_id"]
    links = account.get("sub2_links")
    if not isinstance(links, dict):
        links = {}
        account["sub2_links"] = links
        changed = True
    link = {key.removeprefix("sub2_"): value for key, value in values.items()}
    previous_link = links.get(service_id) if isinstance(links.get(service_id), dict) else {}
    comparable = {key: value for key, value in link.items() if key != "synced_at"}
    previous_comparable = {key: value for key, value in previous_link.items() if key != "synced_at"}
    if previous_comparable != comparable:
        changed = True
    links[service_id] = link
    return changed


def sync_accounts_from_sub2(service_id: str) -> dict:
    service = _sub2_service(service_id)
    remote_rows, remote_total = _fetch_sub2_accounts(service)
    stats = {
        "remote_total": remote_total,
        "fetched": len(remote_rows),
        "marked": 0,
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped_unmarked": 0,
        "skipped_invalid": 0,
        "skipped_non_openai": 0,
        "skipped_duplicate": 0,
        "conflicts": 0,
    }
    errors: list[dict] = []
    candidate_groups: dict[str, list[dict]] = {}
    for remote_item in remote_rows:
        remote_id = str(remote_item.get("id") or "")
        remote_name = str(remote_item.get("name") or "").strip().lower()
        if str(remote_item.get("platform") or "").strip().lower() != "openai":
            stats["skipped_non_openai"] += 1
            continue
        raw_notes = str(remote_item.get("notes") or "").replace("\r\n", "\n").replace("\r", "\n")
        raw_lines = raw_notes.split("\n")
        if not raw_lines or raw_lines[0].strip() != _SUB2_SYNC_MARKER:
            stats["skipped_unmarked"] += 1
            continue
        if len(raw_lines) in (2, 3) or (
            len(raw_lines) == 4 and raw_lines[3].strip() in _SUB2_LEGACY_TRAILING_MARKERS
        ):
            stats["marked"] += 1
        try:
            parsed = _parse_sub2_notes(remote_item)
        except ValueError as exc:
            stats["skipped_invalid"] += 1
            errors.append({"remote_account_id": remote_id, "email": remote_name, "error": str(exc)})
            continue
        note_email = str(parsed["account"].get("email") or "").strip().lower()
        if not _EMAIL_RE.match(remote_name) or remote_name != note_email:
            stats["conflicts"] += 1
            errors.append({
                "remote_account_id": remote_id,
                "email": remote_name,
                "error": "sub2api 账号名与备注第二行邮箱不一致",
            })
            continue
        candidate_groups.setdefault(remote_name, []).append({"remote": remote_item, "parsed": parsed})

    with _LOCK:
        accounts = _read(_ACCOUNTS_PATH)
        phones = _read(_PHONES_PATH)
        _ensure_assignments_locked(accounts, phones)
        by_email = {str(row.get("email") or "").strip().lower(): row for row in accounts}
        candidates: list[dict] = []
        service_key = str(service.get("id") or "")
        for email, group in candidate_groups.items():
            signatures = {
                (item["parsed"]["account_line"], item["parsed"]["phone_line"])
                for item in group
            }
            if len(signatures) > 1:
                stats["conflicts"] += len(group)
                errors.append({
                    "email": email,
                    "error": "sub2api 中同一邮箱存在不同的邮箱或手机接码信息",
                })
                continue
            if len(group) == 1:
                candidates.append(group[0])
                continue
            linked_id = ""
            local = by_email.get(email)
            if local:
                links = local.get("sub2_links") if isinstance(local.get("sub2_links"), dict) else {}
                link = links.get(service_key) if isinstance(links.get(service_key), dict) else {}
                linked_id = str(link.get("account_id") or "")
                if not linked_id and str(local.get("sub2_service_id") or "") == service_key:
                    linked_id = str(local.get("sub2_account_id") or "")
            selected = next(
                (item for item in group if str(item["remote"].get("id") or "") == linked_id),
                None,
            )
            if selected is None:
                selected = group[0]
            candidates.append(selected)
            stats["skipped_duplicate"] += len(group) - 1
        next_seq = max((int(row.get("seq") or 0) for row in accounts), default=0) + 1
        now = _now()
        for candidate in candidates:
            remote_item = candidate["remote"]
            parsed = candidate["parsed"]
            material = parsed["account"]
            phone_material = parsed["phone"]
            email = material["email"]
            account = by_email.get(email)
            if account is None:
                account = {
                    key: value for key, value in material.items()
                    if value and key not in {"phone", "sms_code_url"}
                }
                account.update({
                    "id": uuid.uuid4().hex,
                    "seq": next_seq,
                    "created_at": now,
                    "updated_at": now,
                    "last_status": "not_started",
                    "codex_status": "not_authorized",
                })
                next_seq += 1
                accounts.append(account)
                by_email[email] = account
                inserted = True
            else:
                inserted = False

            conflict = ""
            existing_links = account.get("sub2_links") if isinstance(account.get("sub2_links"), dict) else {}
            existing_link = existing_links.get(str(service.get("id") or "")) if isinstance(existing_links, dict) else None
            if not isinstance(existing_link, dict) and str(account.get("sub2_service_id") or "") == str(service.get("id") or ""):
                existing_link = {
                    "account_id": str(account.get("sub2_account_id") or ""),
                    "notes": str(account.get("sub2_notes") or ""),
                }
            remote_id = str(remote_item.get("id") or "")
            remote_notes = parsed["notes"]
            accept_remote = inserted
            if isinstance(existing_link, dict):
                linked_id = str(existing_link.get("account_id") or "")
                baseline_notes = str(existing_link.get("notes") or "")
                if linked_id and linked_id != remote_id:
                    conflict = "本地账号已关联该服务中的另一个账号 ID"
                elif baseline_notes:
                    local_notes = _sub2_account_notes(account)
                    if remote_notes == baseline_notes:
                        accept_remote = False
                    elif local_notes == baseline_notes or local_notes == remote_notes:
                        accept_remote = True
                    else:
                        conflict = "本地和远端都修改了邮箱或手机接码信息，无法自动合并"
            elif not inserted:
                if _sub2_account_notes(account) == remote_notes:
                    accept_remote = False
                else:
                    conflict = "同邮箱本地账号与远端备注第一、二行不一致"

            merge_fields = (
                "chatgpt_password", "mailbox_password", "outlook_client_id",
                "outlook_refresh_token", "email_code_url", "totp_secret",
            )
            remote_phone = str(phone_material.get("phone") or "")
            remote_sms_url = str(phone_material.get("sms_code_url") or "")
            if not conflict and accept_remote and remote_phone:
                pool_phone = next((row for row in phones if _phone_key(row.get("phone")) == _phone_key(remote_phone)), None)
                if pool_phone and str(pool_phone.get("sms_code_url") or "") not in {"", remote_sms_url}:
                    conflict = "手机号池中同一号码使用了不同的短信取码 URL"
            if conflict:
                if inserted:
                    accounts.remove(account)
                    by_email.pop(email, None)
                    next_seq -= 1
                stats["conflicts"] += 1
                errors.append({
                    "remote_account_id": remote_id,
                    "email": email,
                    "error": conflict,
                })
                continue

            changed = inserted
            if accept_remote:
                for key in merge_fields:
                    value = str(material.get(key) or "")
                    if str(account.get(key) or "") != value:
                        account[key] = value
                        changed = True
                remote_provider = str(material.get("email_provider") or "")
                if not remote_provider:
                    remote_provider = "outlook" if material.get("outlook_refresh_token") else "generic_api" if material.get("email_code_url") else ""
                if str(account.get("email_provider") or "") != remote_provider:
                    account["email_provider"] = remote_provider
                    changed = True
                if remote_phone:
                    _upsert_phone_locked(phones, remote_phone, remote_sms_url, now=now)
                    desired_phone = {
                        "phone": remote_phone,
                        "sms_code_url": remote_sms_url,
                        "last_sms_phone": remote_phone,
                        "last_sms_code_url": remote_sms_url,
                    }
                    for key, value in desired_phone.items():
                        if str(account.get(key) or "") != value:
                            account[key] = value
                            changed = True
                    if not account.get("phone_verified_at"):
                        account["phone_verified_at"] = now
                        changed = True
                else:
                    for key in ("phone", "sms_code_url", "phone_verified_at", "last_sms_phone", "last_sms_code_url"):
                        if account.get(key):
                            account[key] = ""
                            changed = True
            if _set_sub2_link(account, service, remote_item, parsed["notes"], now):
                changed = True
            if changed:
                account["updated_at"] = now
            if inserted:
                stats["inserted"] += 1
            elif changed:
                stats["updated"] += 1
            else:
                stats["unchanged"] += 1

        _ensure_assignments_locked(accounts, phones)
        _write(_ACCOUNTS_PATH, accounts)
        _write(_PHONES_PATH, phones)
    return {"ok": True, "service": _sub2_public_service(service), **stats, "errors": errors[:100]}


def _response_count(payload: dict, *keys: str) -> int:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    for key in keys:
        value = payload.get(key, summary.get(key))
        if isinstance(value, bool):
            continue
        if isinstance(value, (list, dict)):
            return len(value)
        try:
            if value is not None and str(value).strip() != "":
                return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return 0


def _sub2_response_data(payload: dict) -> dict:
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _sub2_error_messages(payload: dict, admin_key: str) -> list[str]:
    raw = payload.get("errors") or []
    if isinstance(raw, (str, dict)):
        raw = [raw]
    messages: list[str] = []
    for item in raw[:50] if isinstance(raw, list) else []:
        if isinstance(item, dict):
            value = item.get("error") or item.get("message") or item.get("reason") or "导入失败"
        else:
            value = item
        message = str(value or "导入失败")
        if admin_key:
            message = message.replace(admin_key, "[已隐藏]")
        messages.append(message[:500])
    return messages


def import_accounts_to_sub2(
    account_ids: list[str],
    service_id: str,
    *,
    delete_terminal: bool = False,
) -> dict:
    ids = list(dict.fromkeys(str(value) for value in account_ids if value))
    if not ids:
        raise ValueError("请先选择要导入的账号")
    if len(ids) > 1000:
        raise ValueError("单次最多导入 1000 个账号")
    service = _sub2_service(service_id)

    with _LOCK:
        known_accounts = {
            str(row.get("id") or ""): row
            for row in _read(_ACCOUNTS_PATH)
        }
    terminal_accounts = [
        known_accounts[account_id]
        for account_id in ids
        if account_id in known_accounts
        and str(known_accounts[account_id].get("codex_status") or "") in _TERMINAL_CODEX_STATUSES
    ]
    terminal_ids = {str(row.get("id") or "") for row in terminal_accounts}
    upload_ids = [account_id for account_id in ids if account_id not in terminal_ids]
    exported = export_credentials(upload_ids, "sub2") if upload_ids else {"items": [], "errors": []}
    items = exported.get("items") or []
    if not items and not (delete_terminal and terminal_accounts):
        if terminal_accounts:
            raise ValueError("选中账号均已禁用；删除 sub2api 远端账号需要明确确认")
        raise ValueError("选中账号没有可导入的 Codex 凭证，请先完成授权")
    contents = []
    notes_by_index: dict[int, str] = {}
    local_account_by_index: dict[int, str] = {}
    for item in items:
        credential = dict(item.get("data") or {})
        if item.get("email") and not credential.get("email"):
            credential["email"] = item["email"]
        contents.append(json.dumps(credential, ensure_ascii=False, separators=(",", ":")))
        note = str(item.get("sub2_notes") or "").replace("\r\n", "\n").replace("\r", "\n")
        if not note.strip():
            raise ValueError(f"账号 {item.get('email') or item.get('account_id') or '?'} 缺少严格三行 sub2api 备注")
        notes_by_index[len(contents)] = note
        local_account_by_index[len(contents)] = str(item.get("account_id") or "")

    if not contents:
        delete_result = _delete_terminal_accounts_from_sub2(terminal_accounts, service)
        return {
            "ok": delete_result["failed"] == 0,
            "service": _sub2_public_service(service),
            "selected": len(ids),
            "submitted": 0,
            "local_skipped": 0,
            "total": 0,
            "succeeded": 0,
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "failed": 0,
            "note_updated": 0,
            "note_failed": 0,
            "terminated_selected": len(terminal_accounts),
            "delete_attempted": delete_result["requested"],
            "deleted": delete_result["deleted"],
            "delete_not_found": delete_result["already_absent"],
            "delete_failed": delete_result["failed"],
            "errors": delete_result["errors"],
        }

    import requests

    endpoint = _sub2_import_endpoint(service.get("api_base") or "")
    admin_key = str(service.get("admin_key") or "")
    try:
        response = requests.post(
            endpoint,
            headers={
                **_sub2_headers(admin_key, idempotency_key=f"relay-import-{uuid.uuid4().hex}"),
                "Content-Type": "application/json",
            },
            json={
                "contents": contents,
                "update_existing": True,
                "concurrency": 10,
                "priority": 1,
                "confirm_mixed_channel_risk": True,
            },
            timeout=90,
            verify=_sub2_verify_ssl(service),
        )
    except requests.RequestException as exc:
        detail = _sub2_safe_detail(f"{type(exc).__name__}: {exc}", admin_key)
        raise RuntimeError(f"连接 sub2api 服务失败：{detail}") from exc

    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if response.status_code < 200 or response.status_code >= 300:
        detail = payload.get("error") or payload.get("message") or response.text[:500] or "未知错误"
        detail = _sub2_safe_detail(detail, admin_key)
        raise RuntimeError(f"sub2api 导入失败（HTTP {response.status_code}）：{detail}")
    if payload.get("ok") is False or payload.get("code") not in (None, 0):
        detail = payload.get("error") or payload.get("message") or "服务返回业务失败"
        detail = _sub2_safe_detail(detail, admin_key)
        raise RuntimeError(f"sub2api 导入失败：{detail}")

    result_data = _sub2_response_data(payload)
    created = _response_count(result_data, "created", "created_count")
    updated = _response_count(result_data, "updated", "updated_count")
    skipped = _response_count(result_data, "skipped", "skipped_count")
    failed = _response_count(result_data, "failed", "failed_count", "failure_count")
    succeeded = _response_count(result_data, "succeeded", "success", "success_count") or created + updated
    total = _response_count(result_data, "total", "total_count") or len(contents)
    note_updated = 0
    note_errors: list[dict] = []
    link_updates: list[dict] = []
    import_results = result_data.get("items") if isinstance(result_data.get("items"), list) else []
    for remote_item in import_results:
        if not isinstance(remote_item, dict) or str(remote_item.get("action") or "") not in {"created", "updated"}:
            continue
        try:
            index = int(remote_item.get("index") or 0)
            remote_account_id = int(remote_item.get("account_id") or 0)
        except (TypeError, ValueError):
            index = remote_account_id = 0
        note = notes_by_index.get(index)
        if not note or remote_account_id <= 0:
            note_errors.append({"error": f"第 {index or '?'} 个账号未返回有效 account_id，备注未写入"})
            continue
        note_endpoint = endpoint.rsplit("/accounts/import/codex-session", 1)[0] + f"/accounts/{remote_account_id}"
        try:
            note_response = requests.put(
                note_endpoint,
                headers={
                    **_sub2_headers(admin_key, idempotency_key=f"relay-note-{uuid.uuid4().hex}"),
                    "Content-Type": "application/json",
                },
                json={"notes": note, "concurrency": 10, "priority": 1},
                timeout=30,
                verify=_sub2_verify_ssl(service),
            )
            if note_response.status_code < 200 or note_response.status_code >= 300:
                try:
                    note_payload = note_response.json()
                except ValueError:
                    note_payload = {}
                detail = note_payload.get("message") or note_payload.get("error") or note_response.text[:300] or "未知错误"
                raise RuntimeError(f"HTTP {note_response.status_code}: {detail}")
            try:
                note_payload = note_response.json()
            except ValueError:
                note_payload = {}
            if isinstance(note_payload, dict) and (
                note_payload.get("ok") is False or note_payload.get("code") not in (None, 0)
            ):
                detail = note_payload.get("message") or note_payload.get("error") or "服务返回业务失败"
                raise RuntimeError(_sub2_safe_detail(detail, admin_key, limit=300))
            note_updated += 1
            link_updates.append({
                "local_account_id": local_account_by_index.get(index, ""),
                "remote_account_id": str(remote_account_id),
                "notes": note,
            })
        except requests.RequestException as exc:
            note_errors.append({"error": f"第 {index} 个账号备注写入失败：{type(exc).__name__}: {exc}"})
        except Exception as exc:
            note_errors.append({"error": f"第 {index} 个账号备注写入失败：{exc}"})
    if succeeded and not import_results:
        note_errors.append({"error": "sub2api 响应未返回账号 ID，无法逐账号写入备注"})
    local_errors = [
        {
            "account_id": str(item.get("account_id") or ""),
            "email": str(item.get("email") or ""),
            "error": str(item.get("error") or "导入前校验失败")[:500],
        }
        for item in (exported.get("errors") or [])
    ]
    if terminal_accounts and not delete_terminal:
        local_errors.extend({
            "account_id": str(account.get("id") or ""),
            "email": str(account.get("email") or ""),
            "error": "账号已禁用，未确认删除 sub2api 远端账号",
        } for account in terminal_accounts)
    if link_updates:
        with _LOCK:
            rows = _read(_ACCOUNTS_PATH)
            by_id = {str(row.get("id") or ""): row for row in rows}
            synced_at = _now()
            changed = False
            for update in link_updates:
                account = by_id.get(update["local_account_id"])
                if not account:
                    continue
                changed = _set_sub2_link(
                    account,
                    service,
                    {"id": update["remote_account_id"], "status": "synced"},
                    update["notes"],
                    synced_at,
                ) or changed
                account["updated_at"] = synced_at
            if changed:
                _write(_ACCOUNTS_PATH, rows)
    delete_result = (
        _delete_terminal_accounts_from_sub2(terminal_accounts, service)
        if delete_terminal
        else {"requested": 0, "deleted": 0, "already_absent": 0, "failed": 0, "errors": []}
    )
    return {
        "ok": delete_result["failed"] == 0,
        "service": _sub2_public_service(service),
        "selected": len(ids),
        "submitted": len(contents),
        "local_skipped": len(local_errors),
        "total": total,
        "succeeded": succeeded,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
        "note_updated": note_updated,
        "note_failed": len(note_errors),
        "terminated_selected": len(terminal_accounts),
        "delete_attempted": delete_result["requested"],
        "deleted": delete_result["deleted"],
        "delete_not_found": delete_result["already_absent"],
        "delete_failed": delete_result["failed"],
        "errors": (
            local_errors
            + [{"error": message} for message in _sub2_error_messages(result_data, admin_key)]
            + note_errors
            + delete_result["errors"]
        ),
    }


def credential_download_zip(account_ids: list[str]) -> tuple[bytes, str, dict]:
    """Build a CPA-compatible ZIP containing one JSON file per selected account."""
    result = export_credentials(account_ids, "cpa")
    if not result["items"]:
        raise ValueError("没有可下载的 CPA 凭证")
    buf = BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in result["items"]:
            name = item["filename"]
            if name in used:
                stem, dot, ext = name.rpartition(".")
                name = f"{stem}-{len(used)+1}.{ext}" if dot else f"{name}-{len(used)+1}"
            used.add(name)
            zf.writestr(name, json.dumps(item["data"], ensure_ascii=False, indent=2) + "\n")
        zf.writestr("manifest.json", json.dumps({"format": "cpa", "count": len(result["items"]), "errors": result["errors"]}, ensure_ascii=False, indent=2) + "\n")
    return buf.getvalue(), f"codex-cpa-relay-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip", result


def delete_accounts(account_ids: list[str]) -> int:
    ids = {str(x) for x in account_ids if x}
    with _LOCK:
        if ids & _active_accounts:
            raise ValueError("运行中的账号不能删除")
        rows = _read(_ACCOUNTS_PATH)
        kept = [x for x in rows if x.get("id") not in ids]
        phones = _read(_PHONES_PATH)
        _ensure_assignments_locked(kept, phones)
        _write(_ACCOUNTS_PATH, kept)
        _write(_PHONES_PATH, phones)
        return len(rows) - len(kept)


def _public_job(row: dict) -> dict:
    allowed = (
        "id", "account_id", "email", "action", "status", "stage", "message", "error",
        "result_file", "created_at", "started_at", "completed_at", "waiting_since",
        "browser_url", "browser_assist_reason", "browser_focus_available",
    )
    result = {key: row.get(key) for key in allowed if row.get(key) not in (None, "")}
    if result.get("message"):
        result["message"] = str(result["message"]).replace("（未登录、未接码）", "")
    if result.get("error"):
        result["error"] = _normalize_exception_message(result["error"])
    if not _show_full_urls() and result.get("browser_url"):
        result["browser_url"] = _safe_diagnostic_url(result["browser_url"])
    return result


def list_jobs(q: str = "", status: str = "", action: str = "") -> list[dict]:
    q = str(q or "").strip().lower()
    status = str(status or "").strip()
    action = str(action or "").strip()
    with _LOCK:
        rows = [_public_job(x) for x in _read(_JOBS_PATH)]
    if q:
        rows = [x for x in rows if q in str(x.get("email") or "").lower()]
    if status:
        rows = [x for x in rows if x.get("status") == status]
    if action == "codex":
        rows = [x for x in rows if not x.get("action")]
    elif action:
        rows = [x for x in rows if x.get("action") == action]
    return sorted(rows, key=lambda x: str(x.get("created_at") or ""), reverse=True)


def _update_job(job_id: str, **changes) -> None:
    with _LOCK:
        rows = _read(_JOBS_PATH)
        for row in rows:
            if row.get("id") == job_id:
                row.update(changes)
                break
        _write(_JOBS_PATH, rows)


def _redact(message: str) -> str:
    text = _SENSITIVE_RE.sub(lambda m: f"{m.group(1)}=[已隐藏]", str(message or ""))
    def replace_url(match):
        if _show_full_urls():
            return match.group(0).rstrip(".,;:)]}")
        raw = match.group(0).rstrip(".,;:)]}")
        try:
            parsed = urlparse(raw)
            host = (parsed.hostname or "").lower()
            safe_host = host in {"localhost", "127.0.0.1"} or host.endswith(".openai.com")
            if not safe_host or not parsed.path:
                return "[URL已隐藏]"
            netloc = host
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            safe = f"{parsed.scheme}://{netloc}{parsed.path}"
            return safe + ("?[参数已隐藏]" if parsed.query or parsed.fragment else "")
        except Exception:
            return "[URL已隐藏]"
    text = re.sub(r"https?://\S+", replace_url, text)
    text = _PHONE_RE.sub("[手机号已隐藏]", text)
    return text[:2000]


def _normalize_exception_message(message: object) -> str:
    text = str(message or "")
    duplicate = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)):\s+\1:\s+")
    while duplicate.match(text):
        text = duplicate.sub(r"\1: ", text, count=1)
    return text


def _safe_diagnostic_url(value: str) -> str:
    try:
        parsed = urlparse(str(value or ""))
        host = (parsed.hostname or "").lower()
        if host not in {"localhost", "127.0.0.1"} and not host.endswith(".openai.com"):
            return "[URL已隐藏]"
        if not parsed.path:
            return "[URL已隐藏]"
        netloc = host + (f":{parsed.port}" if parsed.port else "")
        safe = f"{parsed.scheme}://{netloc}{parsed.path}"
        return safe + ("?[参数已隐藏]" if parsed.query or parsed.fragment else "")
    except Exception:
        return "[URL已隐藏]"


def _log_path(job_id: str) -> Path:
    # The path is a stable SQLite file key. Logging never materializes the
    # historical relay-log directory.
    return _LOG_DIR / f"{job_id}.log"


def _append_log(job_id: str, message: str) -> None:
    path = _log_path(job_id)
    with _LOCK:
        sqlite_store.append_file(
            path,
            f"[{_now()}] {_redact(message)}\n",
            category="relay_logs",
            encoding="utf-8",
            mode=0o600,
            mirror=sqlite_store.legacy_mirror_allowed(path),
        )


class _TaskLogHandler(logging.Handler):
    def __init__(self, job_id: str):
        super().__init__(logging.INFO)
        self.job_id = job_id
        self.thread_id = threading.get_ident()

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread != self.thread_id:
            return
        try:
            _append_log(self.job_id, self.format(record))
        except Exception:
            pass


def read_log(job_id: str) -> str:
    path = _log_path(job_id)
    if not sqlite_store.file_exists(path, category="relay_logs"):
        return ""
    return sqlite_store.read_text_file(path, category="relay_logs")[-20000:]


def _stop_event(job_id: str) -> threading.Event:
    with _LOCK:
        return _stop_events.setdefault(job_id, threading.Event())


def _check_stopped(job_id: str) -> None:
    if _stop_event(job_id).is_set():
        raise RelayStopped("任务已停止")


def _pop_code(job_id: str, stage: str) -> str:
    key = (job_id, stage)
    with _LOCK:
        queue = _verification_codes.get(key) or []
        if not queue:
            return ""
        code = queue.pop(0)
        if not queue:
            _verification_events.setdefault(key, threading.Event()).clear()
        return code


def submit_verification(job_id: str, stage: str, code: str) -> dict:
    stage = str(stage or "").strip().lower()
    code = str(code or "").strip().replace(" ", "")
    if stage not in ("email", "sms", "totp"):
        raise ValueError("stage 仅支持 email/sms/totp")
    if not _CODE_RE.match(code):
        raise ValueError("验证码应为 4-8 位数字")
    jobs = {x.get("id"): x for x in list_jobs()}
    job = jobs.get(job_id)
    if not job:
        raise ValueError("任务不存在")
    if job.get("status") != f"waiting_{stage}":
        raise ValueError("任务当前未等待该验证码")
    key = (job_id, stage)
    with _LOCK:
        _verification_codes.setdefault(key, []).append(code)
        _verification_events.setdefault(key, threading.Event()).set()
    _append_log(job_id, f"已收到手动{stage}验证码，继续任务")
    return {"ok": True, "job_id": job_id, "stage": stage}


def submit_browser_assist(job_id: str) -> dict:
    jobs = {x.get("id"): x for x in list_jobs()}
    job = jobs.get(job_id)
    if not job:
        raise ValueError("任务不存在")
    if job.get("status") != "waiting_browser":
        raise ValueError("任务当前未等待浏览器人工协助")
    with _LOCK:
        background_action = (_browser_controls.get(job_id) or {}).get("background")
    if callable(background_action):
        try:
            background_action()
        except Exception as exc:
            _append_log(job_id, f"浏览器窗口转入后台失败：{type(exc).__name__}")
    _verification_events.setdefault((job_id, "browser"), threading.Event()).set()
    _append_log(job_id, "已收到人工继续指令，恢复浏览器流程")
    return {"ok": True, "job_id": job_id, "status": "running"}


def focus_browser_assist(job_id: str) -> dict:
    jobs = {x.get("id"): x for x in list_jobs()}
    job = jobs.get(job_id)
    if not job:
        raise ValueError("任务不存在")
    if job.get("status") != "waiting_browser":
        raise ValueError("任务当前未等待浏览器人工协助")
    with _LOCK:
        focus_action = (_browser_controls.get(job_id) or {}).get("focus")
    if not callable(focus_action):
        raise ValueError("对应浏览器会话不可用，可能已在服务重启后失效")
    focus_action()
    _append_log(job_id, "已从 WebUI 打开对应的人工处理页面")
    return {
        "ok": True,
        "job_id": job_id,
        "status": "waiting_browser",
        "browser_url": job.get("browser_url") or "",
    }


def _wait_manual_code(job_id: str, stage: str, message: str | None = None) -> str:
    key = (job_id, stage)
    event = _verification_events.setdefault(key, threading.Event())
    _update_job(
        job_id,
        status=f"waiting_{stage}",
        stage=stage,
        message=message or "自动取码未成功，请手动填写验证码",
        waiting_since=_now(),
    )
    _append_log(job_id, f"自动获取{stage}验证码超时，等待 WebUI 手动输入")
    while True:
        _check_stopped(job_id)
        code = _pop_code(job_id, stage)
        if code:
            _update_job(job_id, status="running", stage=stage, message="已收到手动验证码，继续授权", waiting_since="")
            return code
        event.wait(timeout=1.0)


def _wait_browser_assist(
    job_id: str,
    reason: str,
    browser_url: str = "",
    execution_slot: _ExecutionSlot | None = None,
    resolved_check=None,
    focus_action=None,
    background_action=None,
) -> None:
    key = (job_id, "browser")
    event = _verification_events.setdefault(key, threading.Event())
    event.clear()
    controls = {
        "focus": focus_action if callable(focus_action) else None,
        "background": background_action if callable(background_action) else None,
    }
    with _LOCK:
        _browser_controls[job_id] = controls
    _update_job(
        job_id,
        status="waiting_browser",
        stage="browser_assist",
        message="浏览器流程需要人工协助，请点击“打开处理页面”完成后再继续",
        browser_assist_reason=str(reason or "页面需要人工处理"),
        browser_url=browser_url or "",
        browser_focus_available=callable(focus_action),
        waiting_since=_now(),
    )
    _append_log(job_id, f"浏览器等待人工协助：{reason}")

    def page_is_resolved() -> bool:
        if not callable(resolved_check):
            return False
        try:
            return bool(resolved_check())
        except Exception as exc:
            _append_log(job_id, f"浏览器人工协助状态检测失败：{type(exc).__name__}")
            return False

    def move_to_background() -> None:
        if not callable(background_action):
            return
        try:
            background_action()
        except Exception as exc:
            _append_log(job_id, f"浏览器窗口转入后台失败：{type(exc).__name__}")

    try:
        grace_seconds = _browser_assist_grace_seconds()
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            _check_stopped(job_id)
            if page_is_resolved():
                move_to_background()
                _update_job(job_id, status="running", stage="authorize", message="已自动检测到 Cloudflare 验证完成，继续浏览器流程", waiting_since="")
                _append_log(job_id, "已自动检测到浏览器人工验证完成")
                return
            remaining = max(0.0, deadline - time.monotonic())
            if event.wait(timeout=min(1.0, remaining)):
                event.clear()
                move_to_background()
                _update_job(job_id, status="running", stage="authorize", message="已检测到人工处理，继续浏览器流程", waiting_since="")
                return

        _append_log(job_id, f"等待人工处理超过 {grace_seconds:g}s，终止当前账号并关闭浏览器")
        raise BrowserAssistTimeout(f"等待浏览器人工处理超时（{reason}）")
    finally:
        with _LOCK:
            if _browser_controls.get(job_id) is controls:
                _browser_controls.pop(job_id, None)
        _update_job(job_id, browser_focus_available=False)


def _email_provider(job_id: str, account: dict):
    def provider(email: str, after_ts: float | None = None) -> str:
        _check_stopped(job_id)
        configured_provider = str(account.get("email_provider") or "").strip().lower()
        if configured_provider in _DYNAMIC_EMAIL_PROVIDERS:
            try:
                from core.email_provider import wait_for_otp

                _update_job(
                    job_id,
                    status="running",
                    stage="email",
                    message=f"正在从{_EMAIL_PROVIDER_LABELS[configured_provider]}获取验证码",
                )
                return wait_for_otp(
                    email,
                    after_ts=after_ts,
                    max_wait=35,
                    poll_interval=3,
                    settle_seconds=0,
                    source=configured_provider,
                    context=account.get("email_provider_context") or {},
                )
            except Exception as exc:
                _append_log(
                    job_id,
                    f"{_EMAIL_PROVIDER_LABELS[configured_provider]}自动取码未成功：{type(exc).__name__}",
                )
        outlook_client_id = account.get("outlook_client_id") or ""
        outlook_refresh_token = account.get("outlook_refresh_token") or ""
        if outlook_client_id and outlook_refresh_token:
            try:
                from core.outlook_client import OutlookAccount, fetch_otp_with_account

                outlook = OutlookAccount(
                    email=email,
                    password=account.get("mailbox_password") or "",
                    client_id=outlook_client_id,
                    refresh_token=outlook_refresh_token,
                )
                _update_job(job_id, status="running", stage="email", message="正在从微软邮箱获取验证码")
                return fetch_otp_with_account(
                    outlook,
                    after_ts=after_ts,
                    max_wait=35,
                    poll_interval=3,
                    settle_seconds=0,
                )
            except Exception as exc:
                _append_log(job_id, f"微软邮箱自动取码未成功：{type(exc).__name__}")
        code_url = account.get("email_code_url") or ""
        if code_url:
            try:
                from core import generic_api_mail_client as generic

                generic._CONTEXT_CACHE[email] = generic.GenericApiEmailAccount(email=email, code_url=code_url)
                _update_job(job_id, status="running", stage="email", message="正在自动获取邮箱验证码")
                return generic.fetch_latest_otp(
                    email,
                    after_ts=after_ts,
                    max_wait=25,
                    poll_interval=3,
                    settle_seconds=0,
                )
            except Exception as exc:
                _append_log(job_id, f"邮箱自动取码未成功：{type(exc).__name__}")
        if not outlook_client_id and not outlook_refresh_token and not code_url and configured_provider not in _DYNAMIC_EMAIL_PROVIDERS:
            return _wait_manual_code(job_id, "email", "账号未配置邮箱取码来源；当前流程要求邮箱验证码，请手动填写")
        return _wait_manual_code(job_id, "email", "邮箱自动取码未成功，请手动填写邮箱验证码")

    return provider


def _totp_provider(job_id: str, account: dict):
    def provider() -> str:
        _check_stopped(job_id)
        secret = str(account.get("totp_secret") or "")
        if secret:
            try:
                otp = pyotp.parse_uri(secret) if secret.lower().startswith("otpauth://") else pyotp.TOTP(secret)
                _update_job(job_id, status="running", stage="totp", message="正在生成本地 2FA 验证码")
                return otp.now()
            except Exception as exc:
                _append_log(job_id, f"本地 2FA 生成失败：{type(exc).__name__}")
        return _wait_manual_code(job_id, "totp")

    return provider


def _sms_provider(job_id: str, account: dict, *, code_url_override: str = "", on_code=None):
    def provider() -> str:
        _check_stopped(job_id)
        code_url = str(code_url_override or account.get("sms_code_url") or "")
        if code_url:
            try:
                http = sms_provider._http()
                try:
                    _update_job(job_id, status="running", stage="sms", message="正在自动获取手机验证码")
                    deadline = time.time() + 30
                    while time.time() < deadline:
                        _check_stopped(job_id)
                        response = http.get(code_url)
                        if response.status_code == 200:
                            code = sms_provider._fixed_sms_code(response.text)
                            if code:
                                if callable(on_code):
                                    on_code()
                                return code
                        time.sleep(3)
                    raise TimeoutError("短信取码 URL 在 30 秒内未返回验证码")
                finally:
                    http.close()
            except Exception as exc:
                _append_log(job_id, f"短信自动取码未成功：{type(exc).__name__}")
        code = _wait_manual_code(job_id, "sms")
        if callable(on_code):
            on_code()
        return code

    return provider


def _check_email_source(account: dict) -> dict:
    """Check whether the configured mailbox source is reachable.

    This deliberately does not extract or return message bodies/OTP values. It only
    validates the source credentials or URL, so it can be used as a safe mailbox
    liveness check before starting an OAuth task.
    """
    configured_provider = str(account.get("email_provider") or "").strip().lower()
    if configured_provider in _DYNAMIC_EMAIL_PROVIDERS:
        from core.email_provider import email_source_statuses

        status = email_source_statuses([configured_provider])[0]
        return {
            "ok": bool(status.get("ready")),
            "provider": configured_provider,
            "message": str(status.get("message") or "邮箱渠道配置检查完成"),
        }

    outlook_client_id = str(account.get("outlook_client_id") or "").strip()
    outlook_refresh_token = str(account.get("outlook_refresh_token") or "").strip()
    if outlook_client_id and outlook_refresh_token:
        from core.outlook_client import OutlookAccount, _ms_access_token

        http = None
        try:
            outlook = OutlookAccount(
                email=account.get("email") or "",
                password=account.get("mailbox_password") or "",
                client_id=outlook_client_id,
                refresh_token=outlook_refresh_token,
            )
            http = __import__("core.outlook_client", fromlist=["_ms_http"])._ms_http()
            _ms_access_token(outlook, http=http)
            return {"ok": True, "provider": "outlook", "message": "微软邮箱凭证可用"}
        finally:
            if http is not None:
                try:
                    http.close()
                except Exception:
                    pass
    code_url = str(account.get("email_code_url") or "").strip()
    if code_url:
        import requests

        response = requests.get(
            code_url,
            headers={"Accept": "application/json,text/plain,*/*", "User-Agent": "Mozilla/5.0"},
            timeout=20,
            verify=False,
        )
        if response.status_code < 200 or response.status_code >= 300:
            return {"ok": False, "provider": "generic_api", "message": f"邮箱取码接口 HTTP {response.status_code}"}
        return {"ok": True, "provider": "generic_api", "message": "通用 API 邮箱接口可访问"}
    return {"ok": False, "provider": "", "message": "未配置邮箱取码来源"}


def _safe_quota_result(result: dict) -> dict:
    allowed = (
        "ok", "checked_at", "http_status", "current_plan_type", "subscription_plan",
        "has_active_subscription", "expires_at", "renews_at", "cancels_at",
        "billing_period", "is_delinquent", "plus_trial_eligible", "plus_trial_title",
        "plus_trial_summary", "features_count", "can_access_with_session", "error",
        "needs_live_check", "token_expired",
        "plan_type", "rate_limit_allowed", "rate_limit_reached",
        "short_used_percent", "short_reset_at", "short_reset_after_seconds", "short_limit_window_seconds",
        "weekly_used_percent", "weekly_reset_at", "weekly_reset_after_seconds", "weekly_limit_window_seconds",
        "monthly_used_percent", "monthly_reset_at", "monthly_reset_after_seconds", "monthly_limit_window_seconds",
        "additional_rate_limits_count",
    )
    return {key: result.get(key) for key in allowed if result.get(key) not in (None, "")}


def _oauth_failure_stage(message: str) -> str:
    text = str(message or "").lower()
    rules = (
        (("cloudflare", "人机验证", "security verification", "verify you are human"), "browser_challenge"),
        (("cloakbrowser", "playwright", "selenium", "driver"), "driver"),
        (("授权地址", "auth url", "pkce"), "authorize_url"),
        (("提交邮箱", "password", "登录"), "login"),
        (("邮箱 otp", "email otp"), "email"),
        (("2fa", "totp", "authenticator"), "totp"),
        (("手机", "phone", "sms"), "sms"),
        (("workspace", "callback", "state"), "callback"),
        (("换 token", "refresh_token", "access_token"), "token_exchange"),
    )
    for hints, stage in rules:
        if any(hint in text for hint in hints):
            return stage
    return "failed"


def _run_job(
    job_id: str,
    account_id: str,
    phone_override: dict | None = None,
    execution_slot: _ExecutionSlot | None = None,
) -> None:
    account = _get_account(account_id)
    override = phone_override if isinstance(phone_override, dict) else {}
    selected_phone_ids = [str(item) for item in (override.get("phone_ids") or []) if item]
    preferred_phone_ids = selected_phone_ids or ([str(override.get("phone_hint_id"))] if override.get("phone_hint_id") else [])
    phone_state = {"phone_id": "", "phone": "", "sms_url": "", "activation_id": "", "consumed": False}
    handler_added = False
    handler = _TaskLogHandler(job_id)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    root_logger = logging.getLogger()
    try:
        if not account:
            raise ValueError("ChatGPT账号不存在")
        if execution_slot is not None:
            execution_slot.acquire()
        root_logger.addHandler(handler)
        handler_added = True
        _check_stopped(job_id)
        _update_job(job_id, status="running", stage="authorize", message="正在打开 Codex 授权页", started_at=_now())
        _append_log(job_id, "开始既有 GPT 账号 Codex OAuth")
        email_provider = _email_provider(job_id, account)
        totp_provider = _totp_provider(job_id, account)
        sms_state = {"code_received": False}
        def acquire_phone_material():
            if str(override.get("source_type") or "") == "platform":
                platform_provider = str(override.get("platform_provider") or "").strip().lower()
                if not platform_provider:
                    raise sms_provider.SmsProviderError("手机号池动态接码平台未选择")
                _update_job(job_id, stage="sms", message=f"检测到手机验证，正在从{_SMS_PROVIDER_LABELS.get(platform_provider, platform_provider)}取号")
                with sms_provider.direct_provider_context(platform_provider):
                    activation_id, phone = sms_provider.acquire_number()
                phone_state.update(activation_id=activation_id, phone=phone)
                return {"activation_id": activation_id, "phone_id": "", "phone": phone, "code_url": "", "source_type": "platform", "provider": platform_provider}
            material = _acquire_phone_for_job(job_id, account, preferred_phone_ids, prefer_bound=not bool(selected_phone_ids))
            phone_state.update(
                phone_id=str(material.get("phone_id") or ""),
                phone=str(material.get("phone") or ""),
                sms_url=str(material.get("code_url") or ""),
                activation_id=str(material.get("activation_id") or ""),
            )
            _update_job(job_id, stage="sms", message=f"检测到手机验证，已分配手机号（{phone_state['phone'][-4:] or '未知'}）")
            return material

        def sms_code_provider():
            if str(override.get("source_type") or "") == "platform":
                with sms_provider.direct_provider_context(str(override.get("platform_provider") or "")):
                    sms_code = sms_provider.wait_for_sms_code(phone_state.get("activation_id") or "")
                sms_state["code_received"] = True
                return sms_code
            return _sms_provider(
                job_id,
                account,
                code_url_override=phone_state.get("sms_url") or "",
                on_code=lambda: sms_state.update(code_received=True),
            )()

        def sms_failure_provider(_activation_id):
            if phone_state.get("phone_id") and not _stop_event(job_id).is_set():
                _mark_phone_invalid(phone_state["phone_id"], "短信接码失败或取码 URL 失效")

        def sms_success_provider():
            if phone_state.get("phone_id") and not phone_state.get("consumed"):
                _bind_verified_phone(
                    account_id,
                    phone_state.get("phone_id") or "",
                    phone_state.get("phone") or "",
                    phone_state.get("sms_url") or "",
                )
                phone_state["consumed"] = True

        platform_job = str(override.get("source_type") or "") == "platform"
        sms_context = sms_provider.fixed_sms_context(
            provider=str(override.get("platform_provider") or "") if platform_job else "",
            code_provider=None if platform_job else sms_code_provider,
            acquire_provider=acquire_phone_material,
            failure_provider=None if platform_job else sms_failure_provider,
            success_provider=None if platform_job else sms_success_provider,
        )
        with sms_context:
            from core.codex_oauth import run_codex_oauth

            result = run_codex_oauth(
                account["email"],
                otp_provider=email_provider,
                force=True,
                login_password=account.get("chatgpt_password") or None,
                totp_provider=totp_provider,
                require_browser=True,
                browser_assist_provider=lambda reason, url="", resolved_check=None, focus_action=None, background_action=None: _wait_browser_assist(
                    job_id,
                    reason,
                    url,
                    execution_slot=execution_slot,
                    resolved_check=resolved_check,
                    focus_action=focus_action,
                    background_action=background_action,
                ),
            )
        _check_stopped(job_id)
        if not result.get("ok"):
            failure = result.get("message") or result.get("error") or "Codex OAuth 失败"
            terminal_status = _terminal_codex_status(result.get("status"), failure)
            if terminal_status:
                _update_account(
                    account_id,
                    last_status="failed",
                    last_job_id=job_id,
                    codex_status=terminal_status,
                    liveness_status="dead",
                    liveness_checked_at=_now(),
                )
            _update_job(job_id, stage=_oauth_failure_stage(failure), message="Codex 授权失败")
            raise RuntimeError(failure)
        phone_verified = bool(result.get("phone_verified"))
        _update_account(
            account_id,
            last_status="success",
            last_job_id=job_id,
            codex_status="authorized",
            codex_authorized_at=_now(),
            result_file=result.get("file_path") or "",
        )
        if phone_verified and str(override.get("source_type") or "") == "platform":
            # Dynamic provider numbers are external resources; do not create
            # a fake SQLite phone row. Keep the actual number in the account
            # audit fields while the provider owns release/usage accounting.
            _update_account(
                account_id,
                last_sms_phone=phone_state.get("phone") or "",
                last_sms_provider=_SMS_PROVIDER_LABELS.get(str(override.get("platform_provider") or ""), str(override.get("platform_provider") or "")),
                phone_verified_at=_now(),
            )
        elif phone_verified:
            _bind_verified_phone(
                account_id,
                phone_state.get("phone_id") or "",
                phone_state.get("phone") or "",
                phone_state.get("sms_url") or "",
                consume_use=not bool(phone_state.get("consumed")),
            )
            phone_state["consumed"] = True
        _update_job(
            job_id,
            status="success",
            stage="done",
            message=(
                "Codex 授权完成，手机接码验证通过，RT 凭证已保存"
                if phone_verified
                else "Codex 授权完成，RT 凭证已保存"
            ),
            result_file=result.get("file_path") or "",
            error="",
            completed_at=_now(),
            waiting_since="",
        )
        _append_log(job_id, "Codex OAuth 完成，凭证已保存")
    except RelayStopped:
        _update_job(job_id, status="stopped", stage="stopped", message="任务已停止", completed_at=_now(), waiting_since="")
        if account:
            _update_account(account_id, last_status="stopped", last_job_id=job_id)
        _append_log(job_id, "任务已停止")
    except Exception as exc:
        detail = str(exc)
        prefix = f"{type(exc).__name__}: "
        message = _redact(_normalize_exception_message(detail if detail.startswith(prefix) else f"{prefix}{detail}"))
        current = next((item for item in list_jobs() if item.get("id") == job_id), {})
        failure_stage = current.get("stage") if current.get("stage") not in (None, "", "authorize") else _oauth_failure_stage(message)
        _update_job(job_id, status="failed", stage=failure_stage, message="Codex 授权失败", error=message, completed_at=_now(), waiting_since="")
        if account:
            latest = _get_account(account_id) or {}
            changes = {"last_status": "failed", "last_job_id": job_id}
            if str(latest.get("codex_status") or "") not in _TERMINAL_CODEX_STATUSES:
                changes["codex_status"] = "failed"
            _update_account(account_id, **changes)
        _append_log(job_id, message)
    finally:
        if handler_added:
            root_logger.removeHandler(handler)
        if execution_slot is not None:
            execution_slot.release()
        _release_phone_reservation(job_id, phone_state.get("phone_id") or "")
        with _LOCK:
            _browser_controls.pop(job_id, None)
        with _LOCK:
            _active_accounts.discard(account_id)
            _stop_events.pop(job_id, None)


def _run_maintenance_job(job_id: str, account_id: str, action: str) -> None:
    account = _get_account(account_id)
    if not account:
        _update_job(job_id, status="failed", stage="failed", error="ChatGPT账号不存在", completed_at=_now())
        with _LOCK:
            _active_accounts.discard(account_id)
            _stop_events.pop(job_id, None)
        return
    handler = _TaskLogHandler(job_id)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    browser_session = None
    try:
        _check_stopped(job_id)
        if action == "check_email_liveness":
            _update_job(job_id, status="running", stage="email_liveness", message="正在检查邮箱取码来源", started_at=_now())
            result = _check_email_source(account)
            _update_account(
                account_id,
                email_liveness_status="alive" if result.get("ok") else "dead",
                email_liveness_checked_at=_now(),
                maintenance_status="success" if result.get("ok") else "failed",
                maintenance_action=action,
            )
            if not result.get("ok"):
                raise RuntimeError(result.get("message") or "邮箱验活失败")
            _update_job(job_id, status="success", stage="done", message=result.get("message") or "邮箱可用", completed_at=_now(), waiting_since="")
        elif action == "check_gpt_liveness":
            _update_job(job_id, status="running", stage="gpt_liveness", message="正在使用 RT 验活并查询套餐与限额", started_at=_now())
            from core.chatgpt_plan import check_codex_usage
            from core.codex_oauth import CodexTokenRefreshError, refresh_codex_credential

            credential_path = _credential_path_for_account(account)
            if not credential_path:
                _update_account(
                    account_id,
                    liveness_status="error",
                    liveness_checked_at=_now(),
                    maintenance_status="failed",
                    maintenance_action=action,
                )
                raise RuntimeError("未找到本地 Codex 凭证，请先完成 Codex 授权")
            try:
                credential = refresh_codex_credential(credential_path, proxy="")
            except CodexTokenRefreshError as exc:
                changes = {
                    "liveness_status": "error",
                    "liveness_checked_at": _now(),
                    "maintenance_status": "failed",
                    "maintenance_action": action,
                }
                if exc.reauthorization_required:
                    changes["codex_status"] = "reauthorize"
                _update_account(account_id, **changes)
                raise

            token = str(credential.get("access_token") or "").strip()
            account_id_value = str(credential.get("account_id") or "").strip()
            quota = check_codex_usage(token, account_id_value, proxy="")
            safe = _safe_quota_result(quota)
            _update_account(
                account_id,
                codex_status="authorized", liveness_status="alive", liveness_checked_at=_now(),
                gpt_access_token=token, result_file=str(credential_path),
                quota_status="available" if quota.get("ok") else "error",
                quota_checked_at=quota.get("checked_at") or _now(),
                quota_plan=quota.get("plan_type") or "",
                quota_summary=json.dumps(safe, ensure_ascii=False, separators=(",", ":")),
                quota_weekly_used_percent=quota.get("weekly_used_percent"),
                quota_weekly_reset_at=quota.get("weekly_reset_at"),
                quota_monthly_used_percent=quota.get("monthly_used_percent"),
                quota_monthly_reset_at=quota.get("monthly_reset_at"),
                maintenance_status="success", maintenance_action=action,
            )
            if quota.get("ok"):
                plan = quota.get("plan_type") or "未知套餐"
                weekly = quota.get("weekly_used_percent")
                monthly = quota.get("monthly_used_percent")
                weekly_label = f"{float(weekly):g}%" if weekly is not None else "-"
                monthly_label = f"-月{float(monthly):g}%" if monthly is not None else ""
                message = f"GPT 账号存活：{plan}-周{weekly_label}{monthly_label}"
            else:
                message = "GPT 账号存活，套餐与限额查询失败"
            _update_job(
                job_id,
                status="success",
                stage="done",
                message=message,
                completed_at=_now(),
                waiting_since="",
            )
        elif action == "check_quota":
            _update_job(job_id, status="running", stage="quota", message="正在查询 Codex 周限额", started_at=_now())
            from core.chatgpt_plan import check_codex_usage
            from core.codex_oauth import refresh_codex_credential

            credential_path = _credential_path_for_account(account)
            if not credential_path:
                raise RuntimeError("未找到本地 Codex 凭证，请先完成 Codex 授权")
            try:
                credential = json.loads(sqlite_store.read_text_file(credential_path, category="codex_credentials"))
            except Exception as exc:
                raise RuntimeError(f"无法读取 Codex 凭证：{type(exc).__name__}") from exc
            token = str(credential.get("access_token") or account.get("gpt_access_token") or "").strip()
            account_id_value = str(credential.get("account_id") or "").strip()
            result = check_codex_usage(token, account_id_value, proxy="")
            if result.get("needs_live_check"):
                credential = refresh_codex_credential(credential_path, proxy="")
                token = str(credential.get("access_token") or "").strip()
                account_id_value = str(credential.get("account_id") or account_id_value).strip()
                _update_account(account_id, gpt_access_token=token, codex_status="authorized")
                result = check_codex_usage(token, account_id_value, proxy="")
            safe = _safe_quota_result(result)
            _update_account(
                account_id,
                quota_status="available" if result.get("ok") else "error",
                quota_checked_at=result.get("checked_at") or _now(),
                quota_plan=result.get("plan_type") or "",
                quota_summary=json.dumps(safe, ensure_ascii=False, separators=(",", ":")),
                quota_weekly_used_percent=result.get("weekly_used_percent"),
                quota_weekly_reset_at=result.get("weekly_reset_at"),
                quota_monthly_used_percent=result.get("monthly_used_percent"),
                quota_monthly_reset_at=result.get("monthly_reset_at"),
                maintenance_status="success" if result.get("ok") else "failed",
                maintenance_action=action,
            )
            if not result.get("ok"):
                raise RuntimeError(result.get("error") or "Codex 套餐/限额查询失败")
            plan = result.get("plan_type") or "未知套餐"
            weekly_used = result.get("weekly_used_percent")
            monthly_used = result.get("monthly_used_percent")
            weekly_label = f"{float(weekly_used):g}%" if weekly_used is not None else "-"
            monthly_label = f"-月{float(monthly_used):g}%" if monthly_used is not None else ""
            _update_job(job_id, status="success", stage="done", message=f"Codex 限额查询完成：{plan}-周{weekly_label}{monthly_label}", completed_at=_now(), waiting_since="")
        elif action == "check_sub2_status":
            _update_job(job_id, status="running", stage="sub2_status", message="正在读取 sub2api 账号状态与用量窗口", started_at=_now())
            result = sync_sub2_account_status(account_id)
            runtime = result.get("runtime") if isinstance(result.get("runtime"), dict) else {}
            account_status = str(runtime.get("sub2_account_status") or "未知")
            schedulable = runtime.get("sub2_schedulable")
            schedule_label = "可调度" if schedulable is True else "暂不可调度" if schedulable is False else "调度状态未知"
            plan = str(runtime.get("sub2_quota_plan") or "")
            five_hour = runtime.get("sub2_five_hour_utilization")
            seven_day = runtime.get("sub2_seven_day_utilization")
            windows = []
            for label, value in (("5h", five_hour), ("7d", seven_day)):
                if value is None:
                    continue
                try:
                    windows.append(f"{label} {float(value):g}%")
                except (TypeError, ValueError):
                    windows.append(f"{label} {value}")
            message = f"sub2 状态已同步：{account_status}/{schedule_label}"
            if plan:
                message += f"；{plan}"
            if windows:
                message += "；" + "，".join(windows)
            if result.get("partial"):
                message += "；部分限额接口未返回"
            _update_account(account_id, maintenance_status="success", maintenance_action=action)
            _update_job(job_id, status="success", stage="done", message=message, completed_at=_now(), waiting_since="")
        elif action == "refresh_sub2":
            _update_job(job_id, status="running", stage="sub2_refresh", message="正在刷新本地 RT，并更新 sub2api 远端 OAuth 凭证", started_at=_now())
            result = refresh_sub2_account(account_id)
            errors = [str(item) for item in (result.get("errors") or []) if str(item).strip()]
            if result.get("remote_refreshed"):
                message = "sub2api OAuth 凭证已更新，远端账号刷新成功"
            else:
                message = "sub2api OAuth 凭证已更新，但远端账号刷新失败，可重试"
            if errors:
                message += "；" + "；".join(_redact(item)[:300] for item in errors[:3])
            _update_account(
                account_id,
                maintenance_status="success" if not errors else "partial",
                maintenance_action=action,
            )
            _update_job(job_id, status="success", stage="done", message=message, completed_at=_now(), waiting_since="")
        elif action == "enable_2fa":
            if account.get("totp_secret"):
                _update_job(job_id, status="success", stage="done", message="账号已开启 2FA，已跳过", completed_at=_now(), waiting_since="")
                _update_account(account_id, maintenance_status="success", maintenance_action=action)
                return
            _update_job(job_id, status="running", stage="enable_2fa", message="正在登录账号并开启 2FA", started_at=_now())
            from core.account_liveness import authenticate_account_session
            from core.account_export import setup_2fa

            email_provider = _email_provider(job_id, account)
            browser_session, _session_info = authenticate_account_session(
                account["email"],
                otp_provider=email_provider,
                login_password=account.get("chatgpt_password") or None,
            )
            try:
                secret = setup_2fa(browser_session, account["email"], otp_provider=email_provider)
            finally:
                try:
                    browser_session.session.close()
                except Exception:
                    pass
            _update_account(account_id, totp_secret=secret, twofa_enabled_at=_now(), maintenance_status="success", maintenance_action=action)
            _update_job(job_id, status="success", stage="done", message="2FA 已开启并记录", completed_at=_now(), waiting_since="")
        elif action == "check_liveness":
            _update_job(job_id, status="running", stage="liveness", message="正在验活账号", started_at=_now())
            from core.account_liveness import check_account_liveness

            email_provider = _email_provider(job_id, account)
            result = check_account_liveness(
                account["email"],
                otp_provider=email_provider,
                login_password=account.get("chatgpt_password") or None,
                totp_provider=_totp_provider(job_id, account),
            )
            liveness = "alive" if result.get("ok") else "dead" if result.get("status") == "deactivated" else "error"
            _update_account(account_id, liveness_status=liveness, liveness_checked_at=result.get("checked_at") or _now(), maintenance_status="success" if result.get("ok") else "failed", maintenance_action=action)
            if not result.get("ok"):
                raise RuntimeError(result.get("error") or "账号验活失败")
            _update_job(job_id, status="success", stage="done", message="账号存活", completed_at=_now(), waiting_since="")
        else:
            raise ValueError("不支持的账号维护操作")
        _append_log(job_id, "账号维护操作完成")
    except RelayStopped:
        _update_job(job_id, status="stopped", stage="stopped", message="任务已停止", completed_at=_now(), waiting_since="")
        _update_account(account_id, maintenance_status="stopped", maintenance_action=action)
    except Exception as exc:
        message = _redact(f"{type(exc).__name__}: {exc}")
        _update_job(job_id, status="failed", stage="failed", message="账号维护失败", error=message, completed_at=_now(), waiting_since="")
        _update_account(account_id, maintenance_status="failed", maintenance_action=action)
        _append_log(job_id, message)
    finally:
        root_logger.removeHandler(handler)
        if browser_session is not None:
            try:
                browser_session.session.close()
            except Exception:
                pass
        with _LOCK:
            _active_accounts.discard(account_id)
            _stop_events.pop(job_id, None)


def start_account_actions(account_ids: list[str], action: str, workers: int = 1) -> dict:
    action = str(action or "").strip().lower()
    if action not in ("enable_2fa", "check_liveness", "check_email_liveness", "check_gpt_liveness", "check_quota", "check_sub2_status", "refresh_sub2"):
        raise ValueError("不支持的账号维护操作")
    ids = list(dict.fromkeys(str(x) for x in account_ids if x))
    if not ids:
        raise ValueError("请先选择账号")
    workers = max(1, min(8, int(workers or 1)))
    jobs = []
    with _LOCK:
        accounts = _read(_ACCOUNTS_PATH)
        phones = _read(_PHONES_PATH)
        _assigned, changed = _ensure_assignments_locked(accounts, phones)
        if changed:
            _write(_ACCOUNTS_PATH, accounts)
            _write(_PHONES_PATH, phones)
        known = {str(x.get("id")): x for x in accounts}
        if any(account_id not in known for account_id in ids):
            raise ValueError("包含不存在的账号")
        busy = [known[account_id].get("email") for account_id in ids if account_id in _active_accounts]
        if busy:
            raise ValueError("以下账号已有任务运行中：" + ", ".join(busy[:3]))
        rows = _read(_JOBS_PATH)
        for account_id in ids:
            job = {
                "id": uuid.uuid4().hex,
                "account_id": account_id,
                "email": known[account_id].get("email"),
                "action": action,
                "status": "pending",
                "stage": "queued",
                "message": "等待执行",
                "created_at": _now(),
            }
            rows.append(job)
            jobs.append(job)
            _active_accounts.add(account_id)
            _stop_events[job["id"]] = threading.Event()
            path = _log_path(job["id"])
            sqlite_store.write_file(path, b"", category="relay_logs", mode=0o600, mirror=sqlite_store.legacy_mirror_allowed(path))
        _write(_JOBS_PATH, rows)
        _write(_ACCOUNTS_PATH, accounts)
        _write(_PHONES_PATH, phones)

    def dispatch() -> None:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="codex-relay-maintenance") as executor:
            futures = [executor.submit(_run_maintenance_job, job["id"], job["account_id"], action) for job in jobs]
            for future in futures:
                try:
                    future.result()
                except Exception:
                    pass

    threading.Thread(target=dispatch, daemon=True, name=f"codex-relay-maint-{uuid.uuid4().hex[:8]}").start()
    return {"ok": True, "submitted": len(jobs), "workers": workers, "action": action, "jobs": [_public_job(x) for x in jobs]}


def start_jobs(account_ids: list[str], workers: int = 1, phone_ids: list[str] | None = None) -> dict:
    ids = list(dict.fromkeys(str(x) for x in account_ids if x))
    if not ids:
        raise ValueError("请先选择账号")
    workers = max(1, min(8, int(workers or 1)))
    jobs: list[dict] = []
    with _LOCK:
        accounts = _read(_ACCOUNTS_PATH)
        phones = _read(_PHONES_PATH)
        _assigned, changed = _ensure_assignments_locked(accounts, phones)
        if changed:
            _write(_ACCOUNTS_PATH, accounts)
            _write(_PHONES_PATH, phones)
        known = {x.get("id"): x for x in accounts}
        unknown = [x for x in ids if x not in known]
        if unknown:
            raise ValueError("包含不存在的账号")
        skipped = [
            {
                "id": account_id,
                "email": known[account_id].get("email") or "",
                "reason": "账号已禁用",
            }
            for account_id in ids
            if known[account_id].get("codex_status") in _TERMINAL_CODEX_STATUSES
        ]
        ids = [account_id for account_id in ids if known[account_id].get("codex_status") not in _TERMINAL_CODEX_STATUSES]
        if not ids:
            return {"ok": True, "submitted": 0, "workers": workers, "jobs": [], "skipped": skipped}
        busy = [known[x].get("email") for x in ids if x in _active_accounts]
        if busy:
            raise ValueError("以下账号已有任务运行中：" + ", ".join(busy[:3]))
        selected_phone_ids = list(dict.fromkeys(str(x) for x in (phone_ids or []) if x))
        platform_selected = [
            phone_id for phone_id in selected_phone_ids
            if phone_id.startswith(_SMS_PLATFORM_ROW_PREFIX)
        ]
        if platform_selected:
            raise ValueError("接码平台特殊来源不能手动选择；请在设置中启用后由系统自动分配")
        phones_by_id = {str(x.get("id") or ""): x for x in phones}
        selected_phones = [phones_by_id[x] for x in selected_phone_ids if x in phones_by_id]
        if selected_phone_ids and len(selected_phones) != len(selected_phone_ids):
            raise ValueError("包含不存在的手机号素材")
        selected_phones.sort(key=lambda row: int(row.get("seq") or 0))
        if selected_phone_ids and any(not row.get("phone") or not row.get("sms_code_url") for row in selected_phones):
            raise ValueError("选中的手机号缺少短信取码 URL")
        if selected_phone_ids and any(row.get("invalid") for row in selected_phones):
            raise ValueError("选中的手机号或短信 URL 已被标记为失效，请更换素材")
        candidate_phones = [
            row for row in sorted(phones, key=lambda item: int(item.get("seq") or 0))
            if row.get("phone") and row.get("sms_code_url") and not row.get("invalid")
        ]
        if selected_phone_ids:
            candidate_phones = selected_phones

        # A configured SMS platform is a dynamic source rather than a
        # persisted phone row. It has no static ``available_uses`` value, so
        # jobs are admitted here and the real provider reports NO_BALANCE or
        # NO_NUMBERS when the worker actually requests a number.
        platform_state = _sms_platform_state()
        platform_candidate = None
        if not selected_phone_ids and platform_state["enabled"] and platform_state["ready"]:
            platform_candidate = {
                "id": platform_state["id"],
                "special": True,
                "provider": platform_state["provider"],
                "provider_label": platform_state["label"],
                "seq": -1,
                "phone": "",
                "sms_code_url": "",
                "reserved_job_ids": [],
            }
            candidate_phones = [platform_candidate, *candidate_phones]
        if platform_state["enabled"] and not platform_state["ready"] and not candidate_phones:
            raise ValueError("已开启接码平台，但配置未完成：" + "、".join(platform_state["missing"]))

        free_slots: dict[str, int] = {}
        for phone in candidate_phones:
            if phone.get("special"):
                continue
            reserved, available = _phone_capacity(phone)
            free_slots[str(phone.get("id") or "")] = max(0, available - reserved)
        available_capacity = sum(free_slots.values())
        if platform_candidate is not None:
            # Dynamic platform capacity is intentionally not guessed from a
            # balance API that these providers do not expose.
            free_slots[str(platform_candidate["id"])] = len(ids)
            available_capacity += len(ids)
        if available_capacity < len(ids):
            raise ValueError(
                f"手机号池可用资源不足：本批授权需要 {len(ids)} 次，当前可预留 {available_capacity} 次；"
                "请先在手机号池导入号码或增加可用次数"
            )

        rows = _read(_JOBS_PATH)
        for account_id in ids:
            known[account_id]["codex_status"] = "reauthorize" if known[account_id].get("codex_status") == "authorized" else known[account_id].get("codex_status") or "not_authorized"
            known[account_id]["updated_at"] = _now()
            job = {
                "id": uuid.uuid4().hex,
                "account_id": account_id,
                "email": known[account_id].get("email"),
                "status": "pending",
                "stage": "queued",
                "message": "等待执行",
                "created_at": _now(),
            }
            bound_key = _phone_key(known[account_id].get("phone")) if known[account_id].get("phone_verified_at") else ""
            eligible = [
                phone for phone in candidate_phones
                if free_slots.get(str(phone.get("id") or ""), 0) > 0
            ]
            eligible.sort(key=lambda phone: (
                0 if bound_key and _phone_key(phone.get("phone")) == bound_key else 1,
                int(phone.get("seq") or 0),
            ))
            hint = eligible[0]
            hint_id = str(hint.get("id") or "")
            free_slots[hint_id] -= 1
            job["phone_override"] = {
                "phone_ids": selected_phone_ids,
                "phone_hint_id": hint_id,
                "phone_id": hint_id,
                "phone": hint.get("phone") or "",
                "sms_code_url": hint.get("sms_code_url") or "",
                "source_type": "platform" if hint.get("special") else "phone_pool",
                "platform_provider": hint.get("provider") or "",
            }
            if not hint.get("special"):
                reservations = _id_list(hint.get("reserved_job_ids"))
                reservations.append(job["id"])
                hint["reserved_job_ids"] = reservations
                hint.pop("deferred_job_ids", None)
                hint["updated_at"] = _now()
            elif hint.get("special"):
                # Keep dynamic reservations in the job payload only. The
                # synthetic row is intentionally not persisted in relay_phones.
                pass
            rows.append(job)
            jobs.append(job)
            _active_accounts.add(account_id)
            _stop_events[job["id"]] = threading.Event()
            path = _log_path(job["id"])
            sqlite_store.write_file(path, b"", category="relay_logs", mode=0o600, mirror=sqlite_store.legacy_mirror_allowed(path))
        _write(_JOBS_PATH, rows)
        _write(_ACCOUNTS_PATH, accounts)
        _write(_PHONES_PATH, phones)

    def dispatch() -> None:
        semaphore = threading.BoundedSemaphore(workers)
        slots = {job["id"]: _ExecutionSlot(semaphore) for job in jobs}
        # Waiting browser jobs keep their browser stack alive while releasing
        # the shared execution slot, so every submitted job needs its own thread.
        with ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="codex-relay") as executor:
            futures = [
                executor.submit(
                    _run_job,
                    job["id"],
                    job["account_id"],
                    job.get("phone_override"),
                    slots[job["id"]],
                )
                for job in jobs
            ]
            for future in futures:
                try:
                    future.result()
                except Exception:
                    pass

    threading.Thread(target=dispatch, daemon=True, name=f"codex-relay-batch-{uuid.uuid4().hex[:8]}").start()
    return {
        "ok": True,
        "submitted": len(jobs),
        "workers": workers,
        "jobs": [_public_job(x) for x in jobs],
        "skipped": skipped,
    }


def stop_job(job_id: str) -> dict:
    jobs = {x.get("id"): x for x in list_jobs()}
    job = jobs.get(job_id)
    if not job:
        raise ValueError("任务不存在")
    if job.get("status") not in (_ACTIVE_JOB_STATUSES - {"stopping"}):
        return {"ok": True, "status": job.get("status"), "message": "任务已结束"}
    _stop_event(job_id).set()
    _update_job(job_id, status="stopping", message="正在停止")
    for stage in ("email", "sms", "totp", "browser"):
        _verification_events.setdefault((job_id, stage), threading.Event()).set()
    return {"ok": True, "status": "stopping"}


def delete_jobs(job_ids) -> dict:
    requested_ids = list(dict.fromkeys(
        str(job_id or "").strip() for job_id in (job_ids or []) if str(job_id or "").strip()
    ))
    if not requested_ids:
        return {"ok": True, "deleted": 0, "deleted_ids": [], "skipped": []}

    with _LOCK:
        rows = _read(_JOBS_PATH)
        jobs_by_id = {str(row.get("id") or ""): row for row in rows}
        deleted_ids = []
        skipped = []
        for job_id in requested_ids:
            job = jobs_by_id.get(job_id)
            if not job:
                skipped.append({"job_id": job_id, "reason": "任务不存在"})
            elif job.get("status") in _ACTIVE_JOB_STATUSES:
                skipped.append({"job_id": job_id, "reason": "运行中或等待人工处理的任务不能删除"})
            else:
                deleted_ids.append(job_id)
        deleted_set = set(deleted_ids)
        if deleted_set:
            _write(_JOBS_PATH, [row for row in rows if str(row.get("id") or "") not in deleted_set])
            for job_id in deleted_set:
                _stop_events.pop(job_id, None)
                _browser_controls.pop(job_id, None)
            for key in [key for key in _verification_events if key[0] in deleted_set]:
                _verification_events.pop(key, None)
                _verification_codes.pop(key, None)

    for job_id in deleted_ids:
        _release_phone_reservation(job_id)
        sqlite_store.delete_file(_log_path(job_id), category="relay_logs", delete_mirror=True)
    return {
        "ok": True,
        "deleted": len(deleted_ids),
        "deleted_ids": deleted_ids,
        "skipped": skipped,
    }


def delete_job(job_id: str) -> bool:
    result = delete_jobs([job_id])
    if result["deleted"]:
        return True
    skipped = result.get("skipped") or []
    if not skipped or skipped[0].get("reason") == "任务不存在":
        return False
    raise ValueError(skipped[0].get("reason") or "任务不能删除")


def recover_interrupted_jobs() -> int:
    recovered = 0
    with _LOCK:
        rows = _read(_JOBS_PATH)
        for row in rows:
            if row.get("status") in _ACTIVE_JOB_STATUSES:
                row.update(status="stopped", stage="stopped", message="WebUI 重启，任务已停止", completed_at=_now(), waiting_since="")
                recovered += 1
        if recovered:
            _write(_JOBS_PATH, rows)
        accounts = _read(_ACCOUNTS_PATH)
        accounts_by_id = {str(row.get("id") or ""): row for row in accounts}
        accounts_changed = False
        for account in accounts:
            if account.get("codex_status") == "deleted":
                account["codex_status"] = "deactivated"
                account["updated_at"] = _now()
                accounts_changed = True
        for job in rows:
            terminal_status = _terminal_codex_status(job.get("error"), job.get("message"))
            account = accounts_by_id.get(str(job.get("account_id") or ""))
            if not terminal_status or account is None or account.get("codex_status") == terminal_status:
                continue
            account.update(
                codex_status=terminal_status,
                last_status="failed",
                last_job_id=job.get("id") or account.get("last_job_id"),
                liveness_status="dead",
                liveness_checked_at=job.get("completed_at") or _now(),
                updated_at=_now(),
            )
            accounts_changed = True
        if accounts_changed:
            _write(_ACCOUNTS_PATH, accounts)
        phones = _read(_PHONES_PATH)
        phone_changed = False
        for phone in phones:
            if _id_list(phone.get("reserved_job_ids")) or _id_list(phone.get("deferred_job_ids")):
                phone["reserved_job_ids"] = []
                phone.pop("deferred_job_ids", None)
                phone["updated_at"] = _now()
                phone_changed = True
        if phone_changed:
            _write(_PHONES_PATH, phones)
        _browser_controls.clear()
        _phone_locks.clear()
    return recovered
