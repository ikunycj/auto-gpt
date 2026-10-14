"""Unsupported account shapes are rejected on reads and writes."""
from __future__ import annotations

import sqlite3

import pytest

from core import account_store, sqlite_store


def _stored_json(path):
    with sqlite3.connect(sqlite_store.database_path(path)) as conn:
        return [value for (value,) in conn.execute(
            "SELECT value_json FROM storage_items WHERE collection='gpt_accounts' ORDER BY position"
        )]


@pytest.mark.parametrize("legacy_field, value", [
    ("registered", {}), ("relay", {}), ("registered_account_id", "old-id"),
    ("relay_account_id", "old-id"), ("extra", {}), ("extra_json", "{}"),
])
def test_read_rejects_legacy_fields_without_rewriting(tmp_path, legacy_field, value):
    path = tmp_path / "accounts.json"
    sqlite_store.write_json(path, [{
        "id": "account", "email": "user@example.com", legacy_field: value,
    }], collection="gpt_accounts")
    before = _stored_json(path)
    with pytest.raises(ValueError, match="平铺"):
        account_store.read_gpt_accounts(path)
    assert _stored_json(path) == before


@pytest.mark.parametrize("legacy_field", [
    "registered", "relay", "registered_account_id", "relay_account_id", "extra", "extra_json",
])
def test_write_rejects_legacy_fields_before_creating_database(tmp_path, legacy_field):
    path = tmp_path / "accounts.json"
    with pytest.raises(ValueError, match="平铺"):
        account_store.write_gpt_accounts(path, [{
            "id": "account", "email": "user@example.com", legacy_field: {},
        }])
    assert not sqlite_store.database_path(path).exists()
