"""Unregistered mailboxes and GPT accounts use separate canonical entities."""
import json
import sqlite3

import pytest

from core import db, sqlite_store


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("TURB_SQLITE_PATH", str(tmp_path / "store.sqlite3"))
    for name in (
        "_ACCOUNTS_JSON", "_ACCOUNTS_TXT", "_TOKENS_TXT", "_OUTLOOK_JSON", "_OUTLOOK_TXT",
        "_GENERIC_API_EMAIL_JSON", "_GENERIC_API_EMAIL_TXT", "_DOMAIN_EMAIL_JSON",
    ):
        if hasattr(db, name):
            monkeypatch.setattr(db, name, tmp_path / f"{name}.json")
    return tmp_path


def _collection(path, name):
    with sqlite3.connect(sqlite_store.database_path(path)) as conn:
        return [json.loads(row[0]) for row in conn.execute(
            "SELECT value_json FROM storage_items WHERE collection=? ORDER BY position", (name,)
        )]


def test_imported_mailbox_moves_to_gpt_account_on_registration(isolated_store):
    assert db.import_outlook_accounts([{
        "email": "pending@example.com",
        "password": "mail-pass",
        "client_id": "client",
        "refresh_token": "refresh",
    }]) == (1, 0)
    pending = db._load_email_pool()
    assert len(pending) == 1
    assert pending[0]["email"] == "pending@example.com"
    assert pending[0]["source"] == "outlook"
    assert _collection(db._email_pool_path(), "email_accounts") == pending

    row_id = db.insert_account(email="pending@example.com", access_token="access-token")
    account = db.get_account(row_id)
    assert account["email"] == "pending@example.com"
    assert account["access_token"] == "access-token"
    assert account["mailbox_password"] == "mail-pass"
    assert account["outlook_client_id"] == "client"
    assert account["outlook_refresh_token"] == "refresh"
    assert db._load_email_pool() == []
    assert _collection(db._ACCOUNTS_JSON, "gpt_accounts")[0]["id"] == account["id"]


def test_registering_same_email_updates_one_gpt_entity(isolated_store):
    db.import_generic_api_emails([{
        "email": "api@example.com", "code_url": "https://example.test/otp",
    }])
    first_id = db.insert_account(email="api@example.com", access_token="first-token")
    second_id = db.insert_account(email="api@example.com", access_token="second-token")
    assert second_id == first_id
    assert db.count_accounts() == 1
    assert db.get_account(first_id)["access_token"] == "second-token"
    assert db._load_email_pool() == []


def test_claimed_mailbox_is_persisted_in_gpt_workspace_until_registration_finishes(isolated_store):
    db.import_outlook_accounts([{
        "email": "claimed@example.com",
        "password": "mail-pass",
        "client_id": "client",
        "refresh_token": "refresh",
    }])
    db.claim_next_outlook()
    job = db.create_job("outlook")

    account_id = db.ensure_registration_account(
        "claimed@example.com",
        source="outlook",
        job_id=job["id"],
    )

    pending = db.get_account(account_id)
    assert pending["email"] == "claimed@example.com"
    assert pending["registration_pending"] is True
    assert "mailbox_password" not in pending
    assert "outlook_refresh_token" not in pending
    assert db.get_job(job["id"])["account_id"] == account_id
    assert db.get_job(job["id"])["email"] == "claimed@example.com"
    assert db._load_email_pool()[0]["status"] == "used"
    assert b"claimed@example.com" not in sqlite_store.read_file(db._ACCOUNTS_TXT, category="text_exports")

    completed_id = db.insert_account(
        email="claimed@example.com",
        access_token="access-token",
    )

    completed = db.get_account(completed_id)
    assert completed_id == account_id
    assert completed.get("registration_pending") is None
    assert completed["mailbox_password"] == "mail-pass"
    assert db._load_email_pool() == []
    assert b"claimed@example.com" in sqlite_store.read_file(db._ACCOUNTS_TXT, category="text_exports")


def test_failed_registration_write_rolls_back_gpt_and_mailbox(isolated_store, monkeypatch):
    db.import_outlook_accounts([{
        "email": "rollback@example.com",
        "password": "mail-pass",
        "client_id": "client",
        "refresh_token": "refresh",
    }])

    def fail_after_account_write(_email):
        raise RuntimeError("simulated registration failure")

    monkeypatch.setattr(db, "_remove_registered_mailbox", fail_after_account_write)
    with pytest.raises(RuntimeError, match="simulated"):
        db.insert_account(email="rollback@example.com", access_token="access-token")

    assert db.get_account_by_email("rollback@example.com") is None
    restored = db._load_email_pool()
    assert len(restored) == 1
    assert restored[0]["email"] == "rollback@example.com"
    assert restored[0]["status"] == "available"


def test_claim_and_release_mailbox_does_not_create_gpt_account(isolated_store):
    db.import_outlook_accounts([{
        "email": "not-finished@example.com",
        "password": "mail-pass",
        "client_id": "client",
        "refresh_token": "refresh",
    }])
    claimed = db.claim_next_outlook()
    assert claimed["email"] == "not-finished@example.com"
    assert db.release_unconsumed_outlook("not-finished@example.com", note="registration failed")
    assert db.get_account_by_email("not-finished@example.com") is None
    mailbox = db._load_email_pool()[0]
    assert mailbox["status"] == "available"
    assert mailbox["note"] == "registration failed"


def test_registered_import_consumes_mailbox_and_preserves_2fa(isolated_store):
    db.import_outlook_accounts([{
        "email": "imported@example.com",
        "password": "old-pass",
        "client_id": "old-client",
        "refresh_token": "old-refresh",
    }])
    payload = [{
        "email": "imported@example.com",
        "password": "new-pass",
        "client_id": "new-client",
        "refresh_token": "new-refresh",
        "totp_secret": "SYNTHETIC",
    }]
    assert db.import_registered_email_accounts(payload, "outlook") == (1, 0)
    assert db.import_registered_email_accounts(payload, "outlook") == (0, 1)
    assert db.count_accounts() == 1
    assert db._load_email_pool() == []
    account = db.get_account_by_email("imported@example.com")
    assert account["totp_secret"] == "SYNTHETIC"
    assert account["mailbox_password"] == "new-pass"
    assert account["outlook_client_id"] == "new-client"
    assert account["outlook_refresh_token"] == "new-refresh"


def test_registration_credentials_use_flat_fields_and_clean_invalid_values(isolated_store):
    from core.account_login_material import registered_account_login_material

    row_id = db.insert_account(
        email="flat@example.com",
        access_token="access-token",
        chatgpt_password="gpt-pass",
        totp_secret="JBSWY3DPEHPK3PXP",
        login_method="password",
        proxy="http://proxy.example:8080",
    )
    account = db.get_account(row_id)
    assert account["chatgpt_password"] == "gpt-pass"
    assert account["totp_secret"] == "JBSWY3DPEHPK3PXP"
    assert account["login_method"] == "password"
    assert account["proxy"] == "http://proxy.example:8080"
    assert "extra" not in account
    assert "extra_json" not in account
    assert "proxy_used" not in account

    assert registered_account_login_material({
        "chatgpt_password": "  flat-pass  ",
        "totp_secret": "  FLATSECRET  ",
        "login_method": "  email_otp  ",
    }) == {
        "chatgpt_password": "flat-pass",
        "totp_secret": "FLATSECRET",
        "login_method": "email_otp",
    }
    assert registered_account_login_material({
        "chatgpt_password": {"malformed": True},
        "totp_secret": ["malformed"],
        "login_method": {"malformed": True},
    }) == {"chatgpt_password": "", "totp_secret": "", "login_method": ""}
    assert registered_account_login_material({
        "email": "flat@example.com",
        "chatgpt_password": "flat@example.com----actual-password",
    })["chatgpt_password"] == "actual-password"
