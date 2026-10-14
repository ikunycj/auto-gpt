# -*- coding: utf-8 -*-
"""Canonical persistence for the local phone entity pool."""
from __future__ import annotations

import copy
import json
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Iterator

from core import sqlite_store

COLLECTION = "phones"
_UNSUPPORTED_KEYS = frozenset({
    "assigned_account_id", "assigned_account_ids",
    "remaining_uses", "max_uses", "use_count",
})


def _rows(conn) -> list[dict]:
    rows: list[dict] = []
    for item in conn.execute(
        "SELECT value_json FROM storage_items WHERE collection=? ORDER BY position",
        (COLLECTION,),
    ):
        value = json.loads(item["value_json"])
        if not isinstance(value, dict):
            raise ValueError("phones 必须只包含对象记录")
        rows.append(value)
    return rows


def _validate(rows: list[dict]) -> None:
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("手机号集合必须是对象数组")
    seen: set[str] = set()
    for row in rows:
        unsupported = sorted(_UNSUPPORTED_KEYS.intersection(row))
        if unsupported:
            raise ValueError(f"phones 只支持当前手机号记录，不接受旧字段: {', '.join(unsupported)}")
        phone_id = str(row.get("id") or "")
        if not phone_id or phone_id in seen:
            raise ValueError("手机号 ID 为空或重复，写入已取消")
        seen.add(phone_id)


@contextmanager
def _transaction(path: Path) -> Iterator[Any]:
    active = sqlite_store._transaction_for(path)
    with nullcontext() if active is not None else sqlite_store.write_transaction(path):
        state = sqlite_store._transaction_for(path)
        yield state.connection


def read(path: str | Path) -> list[dict]:
    source = Path(path).expanduser().resolve()
    with _transaction(source) as conn:
        rows = _rows(conn)
        _validate(rows)
        return copy.deepcopy(rows)


def write(path: str | Path, rows: list[dict]) -> None:
    _validate(rows)
    source = Path(path).expanduser().resolve()
    with _transaction(source):
        sqlite_store.write_json(source, copy.deepcopy(rows), collection=COLLECTION)
