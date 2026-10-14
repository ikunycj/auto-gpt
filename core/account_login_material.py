# -*- coding: utf-8 -*-
"""Read and sanitize ChatGPT login material from a GPT account row."""
from __future__ import annotations

from typing import Any

from core.import_parser import configured_import_separators


def _credential_text(value: Any) -> str:
    """Return a trimmed credential string; malformed values are empty."""
    return value.strip() if isinstance(value, str) else ""


def _clean_password(value: Any, email: str) -> str:
    """Strip a mistakenly imported ``email<separator>password`` wrapper.

    Only a prefix that exactly matches the account email is removed.  This
    avoids splitting legitimate passwords that happen to contain a configured
    separator.
    """
    text = _credential_text(value)
    if not text:
        return ""
    normalized_email = _credential_text(email).lower()
    if normalized_email and text.lower() == normalized_email:
        return ""
    if normalized_email and text.lower().startswith(normalized_email):
        tail = text[len(normalized_email):]
        for separator in configured_import_separators():
            if tail.startswith(separator):
                password = tail[len(separator):].strip()
                return password if password and password.lower() != normalized_email else ""
    return text


def registered_account_login_material(row: dict[str, Any] | None) -> dict[str, str]:
    """Return credentials from the canonical flat GPT account fields."""
    row = row or {}
    email = _credential_text(row.get("email"))
    return {
        "chatgpt_password": _clean_password(row.get("chatgpt_password"), email),
        "totp_secret": _credential_text(row.get("totp_secret")),
        "login_method": _credential_text(row.get("login_method")),
    }


__all__ = ["registered_account_login_material"]
