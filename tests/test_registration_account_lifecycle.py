"""A mailbox enters the GPT workspace as soon as a registration job claims it."""
import pytest

from core import db, email_provider, gpt_account_service
from registration.application import job_service, use_case


@pytest.fixture
def registration_store(tmp_path, monkeypatch):
    monkeypatch.setenv("TURB_SQLITE_PATH", str(tmp_path / "registration.sqlite3"))
    account_path = tmp_path / "accounts.json"
    monkeypatch.setattr(db, "_ACCOUNTS_JSON", account_path)
    monkeypatch.setattr(db, "_ACCOUNTS_TXT", tmp_path / "accounts.txt")
    monkeypatch.setattr(db, "_TOKENS_TXT", tmp_path / "tokens.txt")
    monkeypatch.setattr(db, "_JOBS_JSON", tmp_path / "jobs.json")
    monkeypatch.setattr(db, "_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(gpt_account_service.relay, "_ACCOUNTS_PATH", account_path)
    monkeypatch.setattr(gpt_account_service.relay, "_PHONES_PATH", tmp_path / "phones.json")
    monkeypatch.setattr(gpt_account_service.relay, "_JOBS_PATH", tmp_path / "relay-jobs.json")
    monkeypatch.setattr(gpt_account_service.relay, "list_jobs", lambda: [])
    monkeypatch.setattr(use_case._twofa_cfg, "ENABLE_2FA", False)
    return account_path


def test_worker_keeps_claimed_email_visible_after_registration_failure(registration_store, monkeypatch):
    job = db.create_job("mailnest")
    monkeypatch.setattr(
        job_service,
        "_prepare_registration_args",
        lambda *_args, **_kwargs: ("claimed@mailnest.test", "Test User", "1990-01-01"),
    )
    monkeypatch.setattr(email_provider, "resolve_email_source", lambda _email: "mailnest")
    monkeypatch.setattr(
        email_provider,
        "snapshot_email_context",
        lambda *_args, **_kwargs: {"project_code": "test-project"},
    )
    monkeypatch.setattr(
        use_case,
        "run_registration",
        lambda **_kwargs: {
            "success": False,
            "email": "claimed@mailnest.test",
            "error": "Chrome CDP proxy route reset",
        },
    )

    job_service._run_one_job(job["id"], job["log_file"])

    row = gpt_account_service.list_accounts()[0]
    assert row["email"] == "claimed@mailnest.test"
    assert row["registration_status"] == "failed"
    assert row["registration_job"]["error"] == "Chrome CDP proxy route reset"
    assert db.get_job(job["id"])["account_id"] == row["id"]
    stored = db.get_account(row["id"])
    assert stored["email_provider_context"] == {"project_code": "test-project"}
    assert "access_token" not in stored


def test_failed_claimed_mailbox_is_not_returned_to_its_pool(registration_store, monkeypatch):
    db.import_outlook_accounts([{
        "email": "claimed@example.com",
        "password": "mail-pass",
        "client_id": "client",
        "refresh_token": "refresh",
    }])
    db.claim_next_outlook()
    db.ensure_registration_account("claimed@example.com", source="outlook")

    source = email_provider.release_email("claimed@example.com", status="available")

    assert source == "outlook"
    assert db._load_email_pool()[0]["status"] == "used"
    assert db.get_account_by_email("claimed@example.com")["registration_pending"] is True
