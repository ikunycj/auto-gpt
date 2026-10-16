"""GPT account operations use one persisted account ID and one account row."""
import pytest

from core import gpt_account_service as service


@pytest.fixture
def isolated_accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("TURB_SQLITE_PATH", str(tmp_path / "accounts.sqlite3"))
    account_path = tmp_path / "accounts.json"
    monkeypatch.setattr(service.db, "_ACCOUNTS_JSON", account_path)
    monkeypatch.setattr(service.relay, "_ACCOUNTS_PATH", account_path)
    monkeypatch.setattr(service.relay, "_PHONES_PATH", tmp_path / "phones.json")
    monkeypatch.setattr(service.relay, "_JOBS_PATH", tmp_path / "relay-jobs.json")
    monkeypatch.setattr(service.db, "_JOBS_JSON", tmp_path / "registration-jobs.json")
    monkeypatch.setattr(service.db, "list_jobs", lambda **_: [])
    monkeypatch.setattr(service.relay, "list_jobs", lambda: [])
    return account_path


def _seed(path, rows):
    service.sqlite_store.write_json(path, rows, collection="gpt_accounts")


def test_registered_account_exposes_one_id_and_masks_unverified_phone(isolated_accounts):
    _seed(isolated_accounts, [{
        "id": "gpt-one", "email": "user@example.com",
        "chatgpt_password": "gpt-password", "mailbox_password": "mailbox-password",
        "email_code_url": "https://mail.example/code", "totp_secret": "JBSWY3DPEHPK3PXP",
        "phone": "+15550001", "sms_code_url": "https://sms.example/code", "phone_verified_at": "",
        "codex_status": "authorized", "created_at": "2026-01-01T00:00:00",
    }])

    rows = service.list_accounts()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "gpt-one"
    assert row["email"] == "user@example.com"
    assert row["registration_status"] == "registered"
    assert row["codex_status"] == "authorized"
    assert row["password"] == "gpt-password"
    assert row["has_totp"] is True
    assert row["has_email_code_url"] is True
    assert row["has_phone"] is False
    assert row["has_sms_code_url"] is False
    assert row["phone_status"] == "unverified"
    assert row["phone"] == ""
    assert service.get_account("gpt-one")["id"] == "gpt-one"


def test_registered_account_exposes_verified_phone_state(isolated_accounts):
    _seed(isolated_accounts, [{
        "id": "gpt-phone", "email": "phone@example.com",
        "chatgpt_password": "gpt-password",
        "email_code_url": "https://mail.example/code",
        "phone": "+15550002", "sms_code_url": "https://sms.example/code",
        "phone_verified_at": "2026-01-02T00:00:00", "codex_status": "not_authorized",
    }])

    row = service.list_accounts()[0]

    assert row["phone_status"] == "verified"
    assert row["has_email_code_url"] is True
    assert row["has_phone"] is True
    assert row["has_sms_code_url"] is True
    assert row["phone_number"] == "+15550002"
    assert row["sms_code_url"] == "https://sms.example/code"


def test_claimed_registration_mailbox_remains_visible_with_failure_detail(isolated_accounts, monkeypatch):
    _seed(isolated_accounts, [{
        "id": "gpt-pending",
        "email": "pending@example.com",
        "email_source": "mailnest",
        "email_provider": "mailnest",
        "registration_pending": True,
    }])
    monkeypatch.setattr(service.db, "list_jobs", lambda **_: [{
        "id": 37,
        "job_type": "registration",
        "account_id": "gpt-pending",
        "email": "pending@example.com",
        "status": "failed",
        "error_message": "Chrome CDP proxy route reset",
        "created_at": "2026-10-15T16:21:49",
    }])

    row = service.list_accounts()[0]

    assert row["registration_status"] == "failed"
    assert row["registration_status_raw"] == "failed"
    assert row["registration_job"]["error"] == "Chrome CDP proxy route reset"
    assert row["email_provider"] == "mailnest"
    assert "registration_pending" not in row


@pytest.mark.parametrize("raw_status", ["deleted", "deactivated"])
@pytest.mark.parametrize("previous_job_status", ["failed", "success"])
def test_terminal_account_status_is_preserved_for_display_and_filtering(
    isolated_accounts, monkeypatch, raw_status, previous_job_status,
):
    _seed(isolated_accounts, [{
        "id": "terminal", "email": "terminal@example.com", "codex_status": raw_status,
        "liveness_status": "dead",
    }])
    monkeypatch.setattr(service.relay, "list_jobs", lambda: [{
        "id": "old-auth", "account_id": "terminal", "status": previous_job_status,
    }])

    row = service.get_account("terminal")
    assert row["codex_status"] == "deactivated"
    assert row["codex_status_raw"] == raw_status
    assert row["gpt_status"] == "dead"
    assert [item["id"] for item in service.list_accounts(codex_status="deactivated")] == ["terminal"]
    assert service.list_accounts(codex_status="failed") == []
    assert service.list_accounts(codex_status="authorized") == []


def test_jobs_follow_account_id_when_email_changes(isolated_accounts, monkeypatch):
    _seed(isolated_accounts, [{
        "id": "gpt-renamed", "email": "new@example.com",
        "chatgpt_password": "secret", "codex_status": "not_authorized",
    }, {
        "id": "gpt-reused", "email": "old@example.com",
        "chatgpt_password": "secret", "codex_status": "not_authorized",
    }])
    monkeypatch.setattr(service.relay, "list_jobs", lambda: [{
        "id": "old-auth", "account_id": "gpt-renamed", "email": "old@example.com",
        "status": "success", "created_at": "2026-01-01T00:00:00",
    }, {
        "id": "old-maintenance", "account_id": "gpt-renamed", "email": "old@example.com",
        "action": "check_quota", "status": "running", "created_at": "2026-01-02T00:00:00",
    }])

    rows = {row["id"]: row for row in service.list_accounts()}
    assert set(rows) == {"gpt-renamed", "gpt-reused"}
    assert rows["gpt-renamed"]["codex_job_id"] == "old-auth"
    assert rows["gpt-renamed"]["codex_status"] == "authorized"
    assert rows["gpt-renamed"]["maintenance_job"]["id"] == "old-maintenance"
    assert rows["gpt-renamed"]["active_operation"] == "maintenance"
    assert rows["gpt-reused"]["codex_job_id"] is None
    assert rows["gpt-reused"]["codex_status"] == "unauthorized"


def test_edit_and_2fa_update_canonical_row_without_creating_another(isolated_accounts):
    _seed(isolated_accounts, [{
        "id": "gpt-edit", "email": "editable@example.com",
        "chatgpt_password": "original", "codex_status": "not_authorized",
    }])

    updated = service.update_account("gpt-edit", {
        "note": "edited in GPT page", "totp_secret": "JBSWY3DPEHPK3PXP",
    })
    assert updated["id"] == "gpt-edit"
    assert updated["note"] == "edited in GPT page"
    assert updated["totp_secret"] == "JBSWY3DPEHPK3PXP"
    stored = service.account_store.read_gpt_accounts(isolated_accounts)
    assert len(stored) == 1
    assert stored[0]["id"] == "gpt-edit"
    assert stored[0]["note"] == "edited in GPT page"
    assert stored[0]["totp_secret"] == "JBSWY3DPEHPK3PXP"


def test_edit_writes_canonical_account_without_relay_account_api(isolated_accounts, monkeypatch):
    _seed(isolated_accounts, [{
        "id": "gpt-direct", "email": "direct@example.com",
        "chatgpt_password": "original", "codex_status": "not_authorized",
    }])

    def relay_update_must_not_run(*_args, **_kwargs):
        raise AssertionError("account edit must not call relay.update_account")

    monkeypatch.setattr(service.relay, "update_account", relay_update_must_not_run)
    updated = service.update_account("gpt-direct", {"note": "direct write"})

    assert updated["note"] == "direct write"
    stored = service.account_store.read_gpt_accounts(isolated_accounts)
    assert len(stored) == 1
    assert stored[0]["id"] == "gpt-direct"
    assert stored[0]["note"] == "direct write"
    assert stored[0]["chatgpt_password"] == "original"


def test_delete_permanently_removes_only_selected_gpt_accounts(isolated_accounts, monkeypatch):
    _seed(isolated_accounts, [
        {"id": "gpt-one", "email": "one@example.com", "chatgpt_password": "first"},
        {"id": "gpt-two", "email": "two@example.com", "chatgpt_password": "second"},
    ])
    monkeypatch.setattr(service.db, "list_jobs", lambda **_: [{
        "id": 3, "job_type": "registration", "email": "job-only@example.com",
        "status": "failed", "created_at": "2026-01-02",
    }])
    assert {row["id"] for row in service.list_accounts()} == {"gpt-one", "gpt-two"}

    result = service.delete_accounts(["gpt-one", "job-only@example.com"])
    assert result["deleted_count"] == 1
    assert result["skipped"] == [{"id": "job-only@example.com", "reason": "账号不存在或已删除"}]
    assert [row["id"] for row in service.list_accounts()] == ["gpt-two"]
    assert [row["id"] for row in service.account_store.read_gpt_accounts(isolated_accounts)] == ["gpt-two"]


def test_delete_rejects_running_account(isolated_accounts, monkeypatch):
    _seed(isolated_accounts, [{
        "id": "gpt-busy", "email": "busy@example.com", "chatgpt_password": "secret",
    }])
    monkeypatch.setattr(service.relay, "list_jobs", lambda: [{
        "id": "job-1", "account_id": "gpt-busy", "email": "busy@example.com",
        "status": "running", "created_at": "2026-01-02",
    }])
    with pytest.raises(service.AccountBusyError, match="先停止任务"):
        service.delete_accounts(["gpt-busy"])
    assert [row["id"] for row in service.account_store.read_gpt_accounts(isolated_accounts)] == ["gpt-busy"]


@pytest.mark.parametrize("terminal_status", ["stopped", "cancelled"])
def test_stopped_registration_does_not_create_gpt_account(isolated_accounts, monkeypatch, terminal_status):
    monkeypatch.setattr(service.db, "list_jobs", lambda **_: [{
        "id": 1, "job_type": "registration", "email": "stopped@example.com",
        "status": terminal_status, "created_at": "2026-01-02",
    }])
    assert service.list_accounts() == []


def test_authorization_material_uses_flat_gpt_credentials_and_provider_context():
    material = service.authorization_material({
        "id": "gpt-auth", "email": "mailnest@example.com",
        "chatgpt_password": "gpt-password", "mailbox_password": "mailbox-password",
        "outlook_client_id": "client-id", "outlook_refresh_token": "refresh-token",
        "totp_secret": "JBSWY3DPEHPK3PXP", "email_provider": "mailnest",
        "login_method": "email_otp", "email_provider_context": {"project_code": "chatgpt001"},
        "proxy": "http://proxy.example:8080", "proxy_source": "registration",
    })
    assert material["chatgpt_password"] == "gpt-password"
    assert material["mailbox_password"] == "mailbox-password"
    assert material["outlook_client_id"] == "client-id"
    assert material["outlook_refresh_token"] == "refresh-token"
    assert material["totp_secret"] == "JBSWY3DPEHPK3PXP"
    assert material["email_provider"] == "mailnest"
    assert material["login_method"] == "email_otp"
    assert material["email_provider_context"] == {"project_code": "chatgpt001"}
    assert material["proxy"] == "http://proxy.example:8080"


def test_public_account_exposes_verified_route_snapshot_for_local_audit(isolated_accounts):
    _seed(isolated_accounts, [{
        "id": "route-private", "email": "route@example.com",
        "_codex_verified_proxy": "socks5h://user:private@proxy.example:1080",
    }])
    row = service.get_account("route-private")
    assert row["_codex_verified_proxy"] == "socks5h://user:private@proxy.example:1080"
    assert "private@" in str(row)
    assert service.db.get_account("route-private")["_codex_verified_proxy"] == "socks5h://user:private@proxy.example:1080"
