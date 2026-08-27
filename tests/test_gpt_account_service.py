import pytest

from core import gpt_account_service as service


@pytest.fixture(autouse=True)
def isolated_soft_deletions(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "_DELETIONS_KEY", tmp_path / "gpt-account-deletions.json")


def test_unified_projection_joins_sources_and_masks_unverified_phone(monkeypatch):
    monkeypatch.setattr(service.relay, "list_accounts", lambda: [
        {
            "id": "relay-1",
            "email": "User@example.com",
            "chatgpt_password": "secret",
            "totp_secret": "TOTP",
            "email_code_url": "https://mail.example/code",
            "phone": "+15550001",
            "sms_code_url": "https://sms.example/code",
            "phone_verified_at": "",
            "codex_status": "not_authorized",
            "liveness_status": "unknown",
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:02",
        },
        {
            "id": "relay-2",
            "email": "sms@example.com",
            "codex_status": "not_authorized",
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:01",
        },
    ])
    monkeypatch.setattr(service.db, "list_accounts", lambda **_: [{
        "id": 7,
        "email": "user@example.com",
        "password": "outlook-password",
        "totp_secret": "registered-totp",
        "plan_type": "plus",
        "codex_status": "success",
        "created_at": "2026-01-01T00:00:01",
        "updated_at": "2026-01-01T00:00:03",
    }])
    monkeypatch.setattr(service.db, "list_jobs", lambda **_: [{
        "id": 11,
        "job_type": "registration",
        "email": "user@example.com",
        "status": "success",
        "created_at": "2026-01-01T00:00:04",
    }])
    monkeypatch.setattr(service.db, "list_generic_api_email_pool", lambda **_: [])
    monkeypatch.setattr(service.db, "list_outlook_pool", lambda **_: [])
    monkeypatch.setattr(service.relay, "list_jobs", lambda: [{
        "id": "relay-job",
        "account_id": "relay-2",
        "email": "sms@example.com",
        "status": "waiting_sms",
        "stage": "sms",
        "message": "等待短信",
        "created_at": "2026-01-01T00:00:05",
    }])

    rows = service.list_accounts()
    by_email = {row["email"]: row for row in rows}
    joined = by_email["user@example.com"]
    assert len(rows) == 2
    assert joined["registered_account_id"] == 7
    assert joined["registration_status"] == "registered"
    assert joined["codex_status"] == "authorized"
    assert joined["phone_status"] == "unverified"
    assert joined["phone"] == ""
    assert joined["password"] == "secret"

    sms = by_email["sms@example.com"]
    assert sms["codex_status"] == "authorizing"
    assert sms["phone_status"] == "verifying"
    assert sms["codex_job_id"] == "relay-job"


def test_soft_delete_hides_all_projection_sources_without_removing_them(monkeypatch):
    relay_rows = [{"id": "relay-1", "email": "relay@example.com", "codex_status": "not_authorized"}]
    registered_rows = [{"id": 2, "email": "registered@example.com", "created_at": "2026-01-01"}]
    registration_jobs = [{
        "id": 3,
        "job_type": "registration",
        "email": "job-only@example.com",
        "status": "failed",
        "created_at": "2026-01-02",
    }]
    monkeypatch.setattr(service.relay, "list_accounts", lambda: relay_rows)
    monkeypatch.setattr(service.relay, "list_jobs", lambda: [])
    monkeypatch.setattr(service.db, "list_accounts", lambda **_: registered_rows)
    monkeypatch.setattr(service.db, "list_jobs", lambda **_: registration_jobs)
    monkeypatch.setattr(service.db, "list_generic_api_email_pool", lambda **_: [])
    monkeypatch.setattr(service.db, "list_outlook_pool", lambda **_: [])

    assert {row["email"] for row in service.list_accounts()} == {
        "relay@example.com", "registered@example.com", "job-only@example.com",
    }
    result = service.soft_delete_accounts([
        "relay-1", "registered:2", "JOB-ONLY@EXAMPLE.COM",
    ])

    assert result["deleted_count"] == 3
    assert service.list_accounts() == []
    assert relay_rows[0]["id"] == "relay-1"
    assert registered_rows[0]["id"] == 2
    assert registration_jobs[0]["id"] == 3
    assert {row["email"] for row in service._deletion_rows()} == {
        "relay@example.com", "registered@example.com", "job-only@example.com",
    }


def test_restore_deleted_accounts_removes_only_matching_markers():
    service.sqlite_store.write_json(
        service._DELETIONS_KEY,
        [
            {"email": "restore@example.com", "deleted_at": "2026-01-01"},
            {"email": "keep@example.com", "deleted_at": "2026-01-02"},
        ],
        collection=service._DELETIONS_COLLECTION,
        mirror=False,
    )

    result = service.restore_deleted_accounts(["RESTORE@EXAMPLE.COM", "missing@example.com"])

    assert result == {"restored": 1, "emails": ["restore@example.com"]}
    assert [row["email"] for row in service._deletion_rows()] == ["keep@example.com"]


def test_soft_delete_rejects_accounts_with_running_operations(monkeypatch):
    monkeypatch.setattr(service.relay, "list_accounts", lambda: [{
        "id": "relay-1", "email": "busy@example.com", "codex_status": "not_authorized",
    }])
    monkeypatch.setattr(service.relay, "list_jobs", lambda: [{
        "id": "job-1", "account_id": "relay-1", "email": "busy@example.com",
        "status": "running", "created_at": "2026-01-02",
    }])
    monkeypatch.setattr(service.db, "list_accounts", lambda **_: [])
    monkeypatch.setattr(service.db, "list_jobs", lambda **_: [])
    monkeypatch.setattr(service.db, "list_generic_api_email_pool", lambda **_: [])
    monkeypatch.setattr(service.db, "list_outlook_pool", lambda **_: [])

    with pytest.raises(service.AccountBusyError, match="先停止任务"):
        service.soft_delete_accounts(["relay-1"])
    assert service._deletion_rows() == []


def test_unified_projection_filters_normalized_statuses(monkeypatch):
    monkeypatch.setattr(service.relay, "list_accounts", lambda: [{
        "id": "relay-1", "email": "a@example.com", "codex_status": "failed",
        "created_at": "2026-01-01", "updated_at": "2026-01-01",
    }])
    monkeypatch.setattr(service.relay, "list_jobs", lambda: [])
    monkeypatch.setattr(service.db, "list_accounts", lambda **_: [])
    monkeypatch.setattr(service.db, "list_jobs", lambda **_: [{
        "id": 1, "job_type": "registration", "email": "a@example.com",
        "status": "failed", "created_at": "2026-01-02",
    }])
    monkeypatch.setattr(service.db, "list_generic_api_email_pool", lambda **_: [])
    monkeypatch.setattr(service.db, "list_outlook_pool", lambda **_: [])

    assert [row["email"] for row in service.list_accounts(registration_status="failed")] == ["a@example.com"]
    assert [row["email"] for row in service.list_accounts(codex_status="failed")] == ["a@example.com"]
    assert service.list_accounts(registration_status="registered") == []


@pytest.mark.parametrize("terminal_status", ["stopped", "cancelled"])
def test_stopped_registration_is_unregistered_not_failed(monkeypatch, terminal_status):
    monkeypatch.setattr(service.relay, "list_accounts", lambda: [])
    monkeypatch.setattr(service.relay, "list_jobs", lambda: [])
    monkeypatch.setattr(service.db, "list_accounts", lambda **_: [])
    monkeypatch.setattr(service.db, "list_jobs", lambda **_: [{
        "id": 1,
        "job_type": "registration",
        "email": "stopped@example.com",
        "status": terminal_status,
        "created_at": "2026-01-02",
    }])
    monkeypatch.setattr(service.db, "list_generic_api_email_pool", lambda **_: [])
    monkeypatch.setattr(service.db, "list_outlook_pool", lambda **_: [])

    row = service.list_accounts()[0]
    assert row["registration_status"] == "unregistered"
    assert row["registration_status_raw"] == terminal_status


@pytest.mark.parametrize("terminal_status", ["stopped", "cancelled"])
def test_terminal_registration_with_saved_account_is_registered(monkeypatch, terminal_status):
    monkeypatch.setattr(service.relay, "list_accounts", lambda: [])
    monkeypatch.setattr(service.relay, "list_jobs", lambda: [])
    monkeypatch.setattr(service.db, "list_accounts", lambda **_: [])
    monkeypatch.setattr(service.db, "list_jobs", lambda **_: [{
        "id": 2,
        "job_type": "registration",
        "email": "saved@example.com",
        "status": terminal_status,
        "account_id": 42,
        "created_at": "2026-01-02",
    }])
    monkeypatch.setattr(service.db, "list_generic_api_email_pool", lambda **_: [])
    monkeypatch.setattr(service.db, "list_outlook_pool", lambda **_: [])

    row = service.list_accounts()[0]
    assert row["registration_status"] == "registered"
    assert row["registration_status_raw"] == terminal_status


def test_authorization_material_uses_registration_password_from_extra_json():
    material = service.authorization_material({
        "id": "registered:4",
        "email": "registered@example.com",
        "password": "mailbox-password",
        "mailbox_password": "mailbox-password",
        "outlook_client_id": "client-id",
        "outlook_refresh_token": "refresh-token",
        "extra_json": '{"registration_password":"chatgpt-password"}',
        "totp_secret": "JBSWY3DPEHPK3PXP",
        "email_code_url": "",
        "registration_status": "registered",
        "codex_status": "unauthorized",
    })
    assert material["chatgpt_password"] == "chatgpt-password"
    assert material["mailbox_password"] == "mailbox-password"
    assert material["outlook_client_id"] == "client-id"
    assert material["outlook_refresh_token"] == "refresh-token"
    assert material["totp_secret"] == "JBSWY3DPEHPK3PXP"


def test_authorization_material_keeps_dynamic_email_provider_context(monkeypatch):
    monkeypatch.setattr(service.db, "get_account", lambda _account_id: {
        "id": 9,
        "email": "mailnest@example.com",
        "email_source": "mailnest",
        "totp_secret": "JBSWY3DPEHPK3PXP",
        "extra_json": '{"login_method":"email_otp","email_provider_context":{"project_code":"chatgpt001"}}',
    })

    material = service.authorization_material({
        "id": "registered:9",
        "registered_account_id": 9,
        "email": "mailnest@example.com",
        "email_provider": "mailnest",
        "login_method": "email_otp",
        "registration_status": "registered",
        "codex_status": "unauthorized",
    })

    assert material["chatgpt_password"] == ""
    assert material["email_provider"] == "mailnest"
    assert material["login_method"] == "email_otp"
    assert material["email_provider_context"] == {"project_code": "chatgpt001"}


def test_unified_projection_exposes_running_maintenance_on_the_account_row(monkeypatch):
    monkeypatch.setattr(service.relay, "list_accounts", lambda: [{
        "id": "relay-1",
        "email": "user@example.com",
        "chatgpt_password": "secret",
        "codex_status": "authorized",
    }])
    monkeypatch.setattr(service.relay, "list_jobs", lambda: [{
        "id": "maintenance-1",
        "account_id": "relay-1",
        "email": "user@example.com",
        "action": "check_quota",
        "status": "running",
        "stage": "quota",
        "message": "正在查询限额",
        "created_at": "2026-01-02T00:00:00",
    }])
    monkeypatch.setattr(service.db, "list_accounts", lambda **_: [])
    monkeypatch.setattr(service.db, "list_jobs", lambda **_: [])
    monkeypatch.setattr(service.db, "list_generic_api_email_pool", lambda **_: [])
    monkeypatch.setattr(service.db, "list_outlook_pool", lambda **_: [])

    row = service.list_accounts()[0]
    assert row["active_operation"] == "maintenance"
    assert row["active_operation_label"] == "正在查询限额"
    assert row["maintenance_job"]["id"] == "maintenance-1"
    assert row["maintenance_job"]["kind"] == "maintenance"
    assert row["latest_log"]["kind"] == "maintenance"
