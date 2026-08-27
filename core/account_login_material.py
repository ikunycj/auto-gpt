# -*- coding: utf-8 -*-
"""Extract verified ChatGPT login material from registered-account records."""
from __future__ import annotations

import json
from typing import Any


def _extra(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("extra")
    if isinstance(value, dict):
        return value
    value = row.get("extra_json")
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def registered_account_login_material(row: dict[str, Any] | None) -> dict[str, str]:
    row = row or {}
    extra = _extra(row)

    def first(*values: Any) -> str:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return ""

    return {
        "chatgpt_password": first(
            row.get("chatgpt_password"),
            row.get("openai_password"),
            extra.get("registration_password"),
            extra.get("openai_password"),
        ),
        "totp_secret": first(row.get("totp_secret"), extra.get("totp_secret")),
        "login_method": first(row.get("login_method"), extra.get("login_method")),
    }


__all__ = ["registered_account_login_material"]
