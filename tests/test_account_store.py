"""GPT accounts use only one canonical flat collection."""
import sqlite3

import pytest

from core import account_store, sqlite_store


def _stored_json(path):
    with sqlite3.connect(sqlite_store.database_path(path)) as conn:
        return [value for (value,) in conn.execute(
            "SELECT value_json FROM storage_items WHERE collection='gpt_accounts' ORDER BY position"
        )]


def test_read_accepts_flat_records_without_rewriting(tmp_path):
    path = tmp_path / "accounts.json"
    rows = [{"id": "one", "email": "one@example.com", "chatgpt_password": "password"}]
    account_store.write_gpt_accounts(path, rows)
    before = _stored_json(path)
    assert account_store.read_gpt_accounts(path) == rows
    assert _stored_json(path) == before


def test_read_rejects_duplicate_email_without_rewriting(tmp_path):
    path = tmp_path / "accounts.json"
    original = [
        {"id": "first", "email": "same@example.com"},
        {"id": "second", "email": "SAME@example.com"},
    ]
    sqlite_store.write_json(path, original, collection="gpt_accounts")
    before = _stored_json(path)
    with pytest.raises(ValueError, match="重复"):
        account_store.read_gpt_accounts(path)
    assert _stored_json(path) == before


def test_read_rejects_old_nested_records_without_migrating(tmp_path):
    path = tmp_path / "accounts.json"
    original = [{"id": "one", "email": "one@example.com", "registered": {"password": "old"}}]
    sqlite_store.write_json(path, original, collection="gpt_accounts")
    before = _stored_json(path)
    with pytest.raises(ValueError, match="平铺"):
        account_store.read_gpt_accounts(path)
    assert _stored_json(path) == before


def test_write_rejects_nested_record_before_creating_database(tmp_path):
    path = tmp_path / "accounts.json"
    with pytest.raises(ValueError, match="平铺"):
        account_store.write_gpt_accounts(path, [{"id": "one", "email": "one@example.com", "relay": {}}])
    assert not sqlite_store.database_path(path).exists()
