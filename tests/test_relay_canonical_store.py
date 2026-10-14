"""Relay actions update the canonical GPT account row in place."""
import json
import sqlite3

import pytest

from core import codex_relay_service as relay, sqlite_store


def _collection(path):
    with sqlite3.connect(sqlite_store.database_path(path)) as conn:
        return [json.loads(item[0]) for item in conn.execute(
            "SELECT value_json FROM storage_items WHERE collection='gpt_accounts' ORDER BY position"
        )]


def _collections(path):
    with sqlite3.connect(sqlite_store.database_path(path)) as conn:
        return {row[0] for row in conn.execute("SELECT collection FROM storage_collections")}


def test_relay_account_material_and_2fa_share_one_gpt_row(tmp_path, monkeypatch):
    account_path = tmp_path / "accounts.json"
    monkeypatch.setattr(relay, "_ACCOUNTS_PATH", account_path)
    monkeypatch.setattr(relay, "_PHONES_PATH", tmp_path / "phones.json")
    monkeypatch.setattr(relay, "_JOBS_PATH", tmp_path / "jobs.json")

    created = relay.ensure_account_material({
        "email": "User@example.com",
        "chatgpt_password": "gpt-password",
        "mailbox_password": "mail-password",
        "outlook_client_id": "client-id",
        "outlook_refresh_token": "refresh-token",
    })
    account_id = created["id"]
    assert created["created"] is True

    updated = relay.update_account(account_id, {
        "email": "user@example.com",
        "note": "local note",
        "totp_secret": "JBSWY3DPEHPK3PXP",
    })
    assert updated["id"] == account_id
    assert updated["email"] == "user@example.com"

    rows = _collection(account_path)
    assert len(rows) == 1
    assert rows[0]["id"] == account_id
    assert rows[0]["email"] == "user@example.com"
    assert rows[0]["chatgpt_password"] == "gpt-password"
    assert rows[0]["mailbox_password"] == "mail-password"
    assert rows[0]["totp_secret"] == "JBSWY3DPEHPK3PXP"
    assert rows[0]["note"] == "local note"
    assert _collections(account_path) == {"gpt_accounts", "phones"}


def test_editing_canonical_account_does_not_duplicate_by_email(tmp_path, monkeypatch):
    account_path = tmp_path / "accounts.json"
    monkeypatch.setattr(relay, "_ACCOUNTS_PATH", account_path)
    monkeypatch.setattr(relay, "_PHONES_PATH", tmp_path / "phones.json")
    monkeypatch.setattr(relay, "_JOBS_PATH", tmp_path / "jobs.json")

    first = relay.ensure_account_material({
        "email": "first@example.com", "chatgpt_password": "first-password",
    })
    second = relay.ensure_account_material({
        "email": "second@example.com", "chatgpt_password": "second-password",
    })
    relay.update_account(first["id"], {"email": "renamed@example.com", "note": "changed"})

    rows = {row["id"]: row for row in relay._read(account_path)}
    assert set(rows) == {first["id"], second["id"]}
    assert rows[first["id"]]["email"] == "renamed@example.com"
    assert rows[first["id"]]["note"] == "changed"
    assert rows[second["id"]]["email"] == "second@example.com"
    assert len({row["email"] for row in rows.values()}) == 2


def test_update_rollback_preserves_canonical_account(tmp_path, monkeypatch):
    account_path = tmp_path / "accounts.json"
    monkeypatch.setattr(relay, "_ACCOUNTS_PATH", account_path)
    monkeypatch.setattr(relay, "_PHONES_PATH", tmp_path / "phones.json")
    monkeypatch.setattr(relay, "_JOBS_PATH", tmp_path / "jobs.json")
    account = relay.ensure_account_material({
        "email": "rollback@example.com", "chatgpt_password": "initial",
    })
    before = relay._read(account_path)

    with pytest.raises(RuntimeError, match="simulated"):
        with sqlite_store.write_transaction(account_path):
            relay.update_account(account["id"], {
                "email": "rollback@example.com", "note": "must roll back",
            })
            raise RuntimeError("simulated write failure")
    assert relay._read(account_path) == before
