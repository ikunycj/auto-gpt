# -*- coding: utf-8 -*-
"""Persistence for the single, flat GPT account collection."""
from __future__ import annotations

import copy
import uuid
from pathlib import Path
from typing import Any

from core import sqlite_store

COLLECTION = "gpt_accounts"
_RETIRED_KEYS = frozenset({
    "registered", "relay", "registered_account_id", "relay_account_id",
    "extra", "extra_json",
})
def _validate(rows: Any) -> None:
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("gpt_accounts 必须是对象数组")
    seen_ids: set[str] = set()
    seen_emails: set[str] = set()
    for row in rows:
        unsupported = sorted(_RETIRED_KEYS.intersection(row))
        if unsupported:
            raise ValueError(f"gpt_accounts 只支持平铺记录，不接受非规范字段: {', '.join(unsupported)}")
        row_id = str(row.get("id") or "").strip()
        email = str(row.get("email") or "").strip().lower()
        if not row_id or not email:
            raise ValueError("GPT账号必须包含 id 和 email")
        if row_id in seen_ids or email in seen_emails:
            raise ValueError("GPT账号 ID 或邮箱重复")
        seen_ids.add(row_id)
        seen_emails.add(email)


def read_gpt_accounts(path: str | Path) -> list[dict]:
    """Read canonical rows without migrating or rewriting old account shapes."""
    rows = sqlite_store.read_json(path, [], collection=COLLECTION)
    _validate(rows)
    return copy.deepcopy(rows)


def write_gpt_accounts(path: str | Path, rows: list[dict]) -> None:
    _validate(rows)
    sqlite_store.write_json(path, copy.deepcopy(rows), collection=COLLECTION)


def ensure_account(path: str | Path, email: str, *, account_id: str | None = None) -> dict:
    normalized = str(email or "").strip().lower()
    if not normalized:
        raise ValueError("GPT账号邮箱不能为空")
    rows = read_gpt_accounts(path)
    for row in rows:
        if str(row.get("email") or "").strip().lower() == normalized:
            return copy.deepcopy(row)
    row = {"id": str(account_id or uuid.uuid4().hex), "email": normalized}
    rows.append(row)
    write_gpt_accounts(path, rows)
    return copy.deepcopy(row)
