import json
import stat
import threading
import time
from contextlib import nullcontext

import pytest

from core import account_store, codex_relay_service as relay, sqlite_store


@pytest.fixture
def relay_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(relay, "_ACCOUNTS_PATH", tmp_path / "accounts.json")
    monkeypatch.setattr(relay, "_PHONES_PATH", tmp_path / "phones.json")
    monkeypatch.setattr(relay, "_JOBS_PATH", tmp_path / "jobs.json")
    monkeypatch.setattr(relay, "_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(relay, "_CREDENTIAL_DIR", tmp_path / "codex_accounts")
    relay._stop_events.clear()
    relay._verification_events.clear()
    relay._verification_codes.clear()
    relay._active_accounts.clear()
    relay._browser_controls.clear()
    relay._phone_locks.clear()
    return tmp_path


def test_gpt_liveness_refreshes_saved_rt_without_login_or_otp(relay_storage, monkeypatch):
    relay.import_accounts("rt-live@example.com----https://mail.example/code")
    account = relay._read(relay._ACCOUNTS_PATH)[0]
    proxy = "http://proxy-user:proxy-pass@proxy.example:3128"
    relay._update_account(account["id"], proxy=proxy)
    relay._CREDENTIAL_DIR.mkdir(parents=True)
    credential_path = relay._CREDENTIAL_DIR / "codex-rt-live@example.com.json"
    sqlite_store.write_file(credential_path, json.dumps({
        "email": "rt-live@example.com",
        "refresh_token": "old-refresh-token",
        "access_token": "old-access-token",
    }), category="codex_credentials")
    relay._write(relay._JOBS_PATH, [{
        "id": "job-rt-live", "account_id": account["id"], "email": account["email"],
        "action": "check_gpt_liveness", "status": "queued", "stage": "queued",
    }])

    monkeypatch.setattr(
        relay,
        "_email_provider",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("RT 验活不应创建邮箱接码 Provider")),
    )
    from core import account_liveness, chatgpt_plan, codex_oauth
    monkeypatch.setattr(
        account_liveness,
        "check_account_liveness",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("RT 验活不应重新登录")),
    )
    calls = []
    monkeypatch.setattr(codex_oauth, "refresh_codex_credential", lambda path, proxy="": (
        calls.append(("refresh", proxy)) or {
            "email": "rt-live@example.com",
            "account_id": "account-live",
            "refresh_token": "new-refresh-token",
            "access_token": "new-access-token",
        }
    ))
    monkeypatch.setattr(chatgpt_plan, "check_codex_usage", lambda token, account_id="", proxy=None: (
        calls.append(("usage", proxy)) or {
            "ok": True,
            "checked_at": "2026-08-09T18:00:00",
            "plan_type": "plus",
            "weekly_used_percent": 70,
            "weekly_reset_at": 1786825935,
            "monthly_used_percent": 30,
            "monthly_reset_at": 1789417935,
        }
    ))

    relay._run_maintenance_job("job-rt-live", account["id"], "check_gpt_liveness")

    job = relay._read(relay._JOBS_PATH)[0]
    stored = relay._read(relay._ACCOUNTS_PATH)[0]
    assert job["status"] == "success"
    assert job["message"] == "GPT 账号存活：plus-周70%-月30%"
    assert stored["liveness_status"] == "alive"
    assert stored["codex_status"] == "authorized"
    assert stored["gpt_access_token"] == "new-access-token"
    assert stored["quota_plan"] == "plus"
    assert stored["quota_weekly_used_percent"] == 70
    assert stored["quota_monthly_used_percent"] == 30
    assert calls == [("refresh", proxy), ("usage", proxy)]


def test_gpt_liveness_fails_when_quota_query_fails(relay_storage, monkeypatch):
    relay.import_accounts("quota-failed@example.com----https://mail.example/code")
    account = relay._read(relay._ACCOUNTS_PATH)[0]
    relay._CREDENTIAL_DIR.mkdir(parents=True)
    credential_path = relay._CREDENTIAL_DIR / "codex-quota-failed@example.com.json"
    sqlite_store.write_file(credential_path, json.dumps({
        "email": "quota-failed@example.com",
        "refresh_token": "valid-refresh-token",
    }), category="codex_credentials")
    relay._write(relay._JOBS_PATH, [{
        "id": "job-quota-failed", "account_id": account["id"], "email": account["email"],
        "action": "check_gpt_liveness", "status": "queued", "stage": "queued",
    }])

    from core import chatgpt_plan, codex_oauth
    monkeypatch.setattr(codex_oauth, "refresh_codex_credential", lambda path, proxy="": {
        "email": "quota-failed@example.com",
        "account_id": "account-live",
        "refresh_token": "valid-refresh-token",
        "access_token": "fresh-access-token",
    })
    monkeypatch.setattr(chatgpt_plan, "check_codex_usage", lambda *_args, **_kwargs: {
        "ok": False,
        "checked_at": "2026-08-09T18:30:00",
        "error": "Codex 用量接口 HTTP 403",
    })

    relay._run_maintenance_job("job-quota-failed", account["id"], "check_gpt_liveness")

    job = relay._read(relay._JOBS_PATH)[0]
    stored = relay._read(relay._ACCOUNTS_PATH)[0]
    assert job["status"] == "failed"
    assert job["message"] == "账号维护失败"
    assert stored["codex_status"] == "failed"
    assert stored["liveness_status"] == "error"
    assert stored["quota_status"] == "error"
    assert stored["maintenance_status"] == "failed"
    assert not stored.get("gpt_access_token")


@pytest.mark.parametrize("refreshed", [
    {"email": "partial@example.com", "access_token": "fresh-access-token"},
    {"email": "wrong@example.com", "access_token": "fresh-access-token", "refresh_token": "fresh-refresh-token"},
])
def test_gpt_liveness_rejects_incomplete_or_foreign_credential(relay_storage, monkeypatch, refreshed):
    relay.import_accounts("partial@example.com----https://mail.example/code")
    account = relay._read(relay._ACCOUNTS_PATH)[0]
    credential_path = relay._CREDENTIAL_DIR / "codex-partial@example.com.json"
    sqlite_store.write_file(credential_path, json.dumps({
        "email": account["email"], "refresh_token": "old-refresh-token",
    }), category="codex_credentials")
    relay._write(relay._JOBS_PATH, [{
        "id": "job-partial", "account_id": account["id"], "email": account["email"],
        "action": "check_gpt_liveness", "status": "queued", "stage": "queued",
    }])

    from core import chatgpt_plan, codex_oauth
    monkeypatch.setattr(codex_oauth, "refresh_codex_credential", lambda path, proxy="": refreshed)
    monkeypatch.setattr(chatgpt_plan, "check_codex_usage", lambda *_args, **_kwargs: (
        _ for _ in ()
    ).throw(AssertionError("不完整或异账号凭证不能查询用量")))

    relay._run_maintenance_job("job-partial", account["id"], "check_gpt_liveness")

    job = relay._read(relay._JOBS_PATH)[0]
    stored = relay._read(relay._ACCOUNTS_PATH)[0]
    assert job["status"] == "failed"
    assert stored["codex_status"] == "reauthorize"
    assert stored["liveness_status"] == "error"
    assert stored["maintenance_status"] == "failed"
    assert not stored.get("gpt_access_token")


def test_gpt_liveness_omits_month_when_usage_has_no_monthly_window(relay_storage, monkeypatch):
    relay.import_accounts("weekly-only@example.com----https://mail.example/code")
    account = relay._read(relay._ACCOUNTS_PATH)[0]
    relay._CREDENTIAL_DIR.mkdir(parents=True)
    credential_path = relay._CREDENTIAL_DIR / "codex-weekly-only@example.com.json"
    sqlite_store.write_file(credential_path, json.dumps({
        "email": "weekly-only@example.com", "refresh_token": "valid-refresh-token",
    }), category="codex_credentials")
    relay._write(relay._JOBS_PATH, [{
        "id": "job-weekly-only", "account_id": account["id"], "email": account["email"],
        "action": "check_gpt_liveness", "status": "queued", "stage": "queued",
    }])

    from core import chatgpt_plan, codex_oauth
    monkeypatch.setattr(codex_oauth, "refresh_codex_credential", lambda path, proxy="": {
        "email": "weekly-only@example.com", "account_id": "account-live",
        "refresh_token": "valid-refresh-token", "access_token": "fresh-access-token",
    })
    monkeypatch.setattr(chatgpt_plan, "check_codex_usage", lambda *_args, **_kwargs: {
        "ok": True, "plan_type": "plus", "weekly_used_percent": 75,
    })

    relay._run_maintenance_job("job-weekly-only", account["id"], "check_gpt_liveness")

    job = relay._read(relay._JOBS_PATH)[0]
    assert job["message"] == "GPT 账号存活：plus-周75%"


def test_gpt_liveness_marks_revoked_rt_for_reauthorization(relay_storage, monkeypatch):
    relay.import_accounts("rt-dead@example.com----https://mail.example/code")
    account = relay._read(relay._ACCOUNTS_PATH)[0]
    relay._CREDENTIAL_DIR.mkdir(parents=True)
    sqlite_store.write_file(relay._CREDENTIAL_DIR / "codex-rt-dead@example.com.json", json.dumps({
        "email": "rt-dead@example.com", "refresh_token": "revoked-refresh-token",
    }), category="codex_credentials")
    relay._write(relay._JOBS_PATH, [{
        "id": "job-rt-dead", "account_id": account["id"], "email": account["email"],
        "action": "check_gpt_liveness", "status": "queued", "stage": "queued",
    }])

    from core import codex_oauth
    def revoked(*_args, **_kwargs):
        raise codex_oauth.CodexTokenRefreshError(
            "Codex RT 已失效或被撤销，需要重新授权",
            http_status=400,
            error_code="invalid_grant",
            reauthorization_required=True,
        )
    monkeypatch.setattr(codex_oauth, "refresh_codex_credential", revoked)

    relay._run_maintenance_job("job-rt-dead", account["id"], "check_gpt_liveness")

    job = relay._read(relay._JOBS_PATH)[0]
    stored = relay._read(relay._ACCOUNTS_PATH)[0]
    assert job["status"] == "failed"
    assert "重新授权" in job["error"]
    assert stored["liveness_status"] == "error"
    assert stored["codex_status"] == "reauthorize"


def test_quota_action_refreshes_expired_at_and_stores_weekly_usage(relay_storage, monkeypatch):
    relay.import_accounts("quota@example.com----https://mail.example/code")
    account = relay._read(relay._ACCOUNTS_PATH)[0]
    relay._CREDENTIAL_DIR.mkdir(parents=True)
    credential_path = relay._CREDENTIAL_DIR / "codex-quota@example.com.json"
    sqlite_store.write_file(credential_path, json.dumps({
        "email": "quota@example.com",
        "account_id": "account-old",
        "access_token": "expired-at",
        "refresh_token": "valid-rt",
    }), category="codex_credentials")
    relay._write(relay._JOBS_PATH, [{
        "id": "job-quota", "account_id": account["id"], "email": account["email"],
        "action": "check_quota", "status": "queued", "stage": "queued",
    }])

    from core import chatgpt_plan, codex_oauth
    results = iter([
        {"ok": False, "needs_live_check": True, "error": "AT 已过期"},
        {
            "ok": True,
            "checked_at": "2026-08-09T17:00:00",
            "plan_type": "plus",
            "weekly_used_percent": 67,
            "weekly_reset_at": 1786825935,
            "weekly_limit_window_seconds": 604800,
            "monthly_used_percent": 30,
            "monthly_reset_at": 1789417935,
            "monthly_limit_window_seconds": 2592000,
        },
    ])
    calls = []
    monkeypatch.setattr(
        chatgpt_plan,
        "check_codex_usage",
        lambda token, account_id="", proxy=None: calls.append((token, account_id, proxy)) or next(results),
    )
    monkeypatch.setattr(codex_oauth, "refresh_codex_credential", lambda path, proxy="": {
        "email": "quota@example.com",
        "account_id": "account-new",
        "access_token": "fresh-at",
        "refresh_token": "rotated-rt",
    })

    relay._run_maintenance_job("job-quota", account["id"], "check_quota")

    job = relay._read(relay._JOBS_PATH)[0]
    stored = relay._read(relay._ACCOUNTS_PATH)[0]
    assert calls == [
        ("expired-at", "account-old", ""),
        ("fresh-at", "account-new", ""),
    ]
    assert job["status"] == "success"
    assert job["message"] == "Codex 限额查询完成：plus-周67%-月30%"
    assert stored["quota_status"] == "available"
    assert stored["quota_weekly_used_percent"] == 67
    assert stored["quota_weekly_reset_at"] == 1786825935
    assert stored["quota_monthly_used_percent"] == 30
    assert stored["quota_monthly_reset_at"] == 1789417935
    assert stored["gpt_access_token"] == "fresh-at"


def test_oauth_records_phone_only_after_sms_code_is_received(relay_storage, monkeypatch):
    relay.import_accounts(
        "sms@example.com----https://mail.example/code----+14155550123----https://sms.example/code"
    )
    account = relay._read(relay._ACCOUNTS_PATH)[0]
    relay._write(relay._JOBS_PATH, [{
        "id": "job-sms", "account_id": account["id"], "email": account["email"],
        "status": "pending", "stage": "queued",
    }])

    class Response:
        status_code = 200
        text = "OpenAI verification code: 123456"

    class Http:
        def get(self, _url):
            return Response()

        def close(self):
            return None

    monkeypatch.setattr(relay.sms_provider, "_http", lambda: Http())
    from core import codex_oauth

    def oauth(*_args, **_kwargs):
        activation_id, phone = relay.sms_provider.acquire_number()
        assert phone == "14155550123"
        assert relay.sms_provider.wait_for_sms_code(activation_id) == "123456"
        relay.sms_provider.complete(activation_id)
        return {
            "ok": True,
            "phone_verified": True,
            "file_path": str(relay._CREDENTIAL_DIR / "codex-sms@example.com.json"),
        }

    monkeypatch.setattr(codex_oauth, "run_codex_oauth", oauth)
    credential_path = relay._CREDENTIAL_DIR / "codex-sms@example.com.json"
    sqlite_store.write_file(credential_path, json.dumps({
        "email": account["email"], "access_token": "test-access-token",
        "refresh_token": "test-refresh-token",
    }), category="codex_credentials")
    from core import chatgpt_plan
    monkeypatch.setattr(chatgpt_plan, "check_codex_usage", lambda *_args, **_kwargs: {"ok": True})
    phone_row = relay._read(relay._PHONES_PATH)[0]

    relay._run_job("job-sms", account["id"], {
        "phone_id": phone_row["id"],
        "phone": phone_row["phone"],
        "sms_code_url": phone_row["sms_code_url"],
    })

    job = relay._read(relay._JOBS_PATH)[0]
    stored = relay._read(relay._ACCOUNTS_PATH)[0]
    assert job["status"] == "success"
    assert "手机接码验证通过" in job["message"]
    assert stored["phone_verified_at"]
    assert stored["phone"] == "+14155550123"
    assert stored["sms_code_url"] == "https://sms.example/code"
    assert relay._read(relay._PHONES_PATH)[0]["available_uses"] == 0


def test_oauth_does_not_bind_phone_when_sms_acceptance_is_unconfirmed(relay_storage, monkeypatch):
    relay.import_accounts("unconfirmed@example.com----https://mail.example/code")
    relay.import_phones("+14155550123----https://sms.example/code")
    account = relay._read(relay._ACCOUNTS_PATH)[0]
    phone_row = relay._read(relay._PHONES_PATH)[0]
    relay._write(relay._JOBS_PATH, [{
        "id": "job-unconfirmed", "account_id": account["id"], "email": account["email"],
        "status": "pending", "stage": "queued",
    }])
    from core import codex_oauth
    monkeypatch.setattr(codex_oauth, "run_codex_oauth", lambda *_args, **_kwargs: {
        "ok": True,
        "file_path": str(relay._CREDENTIAL_DIR / "codex-unconfirmed@example.com.json"),
    })
    credential_path = relay._CREDENTIAL_DIR / "codex-unconfirmed@example.com.json"
    sqlite_store.write_file(credential_path, json.dumps({
        "email": account["email"], "access_token": "test-access-token",
        "refresh_token": "test-refresh-token",
    }), category="codex_credentials")
    from core import chatgpt_plan
    monkeypatch.setattr(chatgpt_plan, "check_codex_usage", lambda *_args, **_kwargs: {"ok": True})

    relay._run_job("job-unconfirmed", account["id"], {
        "phone_id": phone_row["id"],
        "phone": phone_row["phone"],
        "sms_code_url": phone_row["sms_code_url"],
    })

    stored = relay._read(relay._ACCOUNTS_PATH)[0]
    phone_stored = relay._read(relay._PHONES_PATH)[0]
    assert relay._read(relay._JOBS_PATH)[0]["status"] == "success"
    assert not stored.get("phone")
    assert not stored.get("phone_verified_at")
    assert not relay.list_phones()[0]["assigned"]
    assert phone_stored["available_uses"] == 1


def test_import_supports_email_url_and_password_totp_formats(relay_storage):
    result = relay.import_accounts(
        "\n".join([
            "mail@example.com----https://mail.example/code----+14155550123----https://sms.example/code",
            "totp@example.com----secret-password----JBSWY3DPEHPK3PXP----8613412345678----https://sms.example/totp",
        ])
    )

    assert result == {
        "ok": True, "inserted": 2, "updated": 0, "total": 2,
        "assigned": 0, "unassigned_accounts": 2,
    }
    rows = {row["email"]: row for row in relay.list_accounts()}
    assert rows["mail@example.com"]["has_email_code_url"] is True
    assert rows["mail@example.com"]["has_password"] is False
    assert rows["totp@example.com"]["has_password"] is True
    assert rows["totp@example.com"]["has_totp"] is True

    serialized = json.dumps(rows)
    assert "secret-password" in serialized
    assert "JBSWY3DPEHPK3PXP" in serialized
    assert "https://mail.example/code" in serialized
    phone_serialized = json.dumps(relay.list_phones())
    assert "https://sms.example/totp" in phone_serialized
    docs = account_store.read_gpt_accounts(relay._ACCOUNTS_PATH)
    assert len(docs) == 2
    assert {doc["email"] for doc in docs} == set(rows)
    assert all("registered" not in doc and "relay" not in doc for doc in docs)
    assert not relay._ACCOUNTS_PATH.exists()
    db_path = sqlite_store.database_path(relay._ACCOUNTS_PATH)
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


def test_import_supports_chatgpt_password_and_email_code_url(relay_storage):
    result = relay.import_accounts(
        "password-mail@example.com----chatgpt-password----https://mail.example/latest"
    )

    assert result["inserted"] == 1
    stored = relay._read(relay._ACCOUNTS_PATH)[0]
    assert stored["chatgpt_password"] == "chatgpt-password"
    assert stored["email_code_url"] == "https://mail.example/latest"
    assert stored.get("totp_secret") == ""


def test_import_json_proxy_alias_normalizes_and_exposes_proxy_in_public_account(relay_storage):
    raw_proxy = "proxy.example:8080:proxy-user:proxy-pass"
    relay.import_accounts(json.dumps({
        "email": "proxy-import@example.com",
        "password": "chatgpt-password",
        "email_code_url": "https://mail.example/code",
        "proxy_used": raw_proxy,
    }))

    stored = relay._read(relay._ACCOUNTS_PATH)[0]
    assert stored["proxy"] == "http://proxy-user:proxy-pass@proxy.example:8080"
    assert stored["proxy_source"] == "proxy_used"

    public = relay.list_accounts()[0]
    serialized = json.dumps(public)
    assert public["has_proxy"] is True
    assert public["proxy_source"] == "proxy_used"
    assert public["proxy"] == "http://proxy-user:proxy-pass@proxy.example:8080"
    assert "proxy-pass" in serialized


def test_ensure_account_material_persists_proxy_and_preserves_existing_relay_value(relay_storage):
    first = relay.ensure_account_material({
        "email": "proxy-material@example.com",
        "chatgpt_password": "chatgpt-password",
        "proxy_url": "socks5h://proxy-user:proxy-pass@proxy.example:1080",
    })
    account_id = first["id"]
    assert first["has_proxy"] is True
    assert first["proxy_source"] == "proxy_url"

    relay.ensure_account_material({
        "email": "proxy-material@example.com",
        "chatgpt_password": "replacement-password",
        "proxy": "http://replacement-user:replacement-pass@other.example:3128",
    })

    stored = relay._get_account(account_id)
    assert stored["proxy"] == "socks5h://proxy-user:proxy-pass@proxy.example:1080"
    assert stored["proxy_source"] == "proxy_url"


def test_start_jobs_keeps_proxy_in_private_job_record_only(relay_storage, monkeypatch):
    relay.import_accounts(json.dumps({
        "email": "proxy-job@example.com",
        "password": "chatgpt-password",
        "email_code_url": "https://mail.example/code",
        "proxy": "http://proxy-user:proxy-pass@proxy.example:3128",
    }))
    account = relay._read(relay._ACCOUNTS_PATH)[0]
    monkeypatch.setattr(relay, "_run_job", lambda *args: None)

    result = relay.start_jobs([account["id"]], workers=1)
    private_job = relay._read(relay._JOBS_PATH)[0]

    assert private_job["proxy"] == "http://proxy-user:proxy-pass@proxy.example:3128"
    assert "proxy" not in result["jobs"][0]
    assert "proxy-pass" not in json.dumps(result)
    assert "proxy" not in relay.list_jobs()[0]


def test_run_job_passes_proxy_to_codex_oauth_and_online_check(relay_storage, monkeypatch, tmp_path):
    proxy = "http://proxy-user:proxy-pass@proxy.example:3128"
    relay.import_accounts(json.dumps({
        "email": "proxy-oauth@example.com",
        "password": "chatgpt-password",
        "email_code_url": "https://mail.example/code",
        "proxy": proxy,
    }))
    account = relay._read(relay._ACCOUNTS_PATH)[0]
    calls = []
    usage_calls = []
    credential_path = relay._CREDENTIAL_DIR / "codex-proxy-oauth@example.com.json"
    sqlite_store.write_file(credential_path, json.dumps({
        "email": "proxy-oauth@example.com",
        "account_id": "account-proxy",
        "access_token": "access-token",
        "refresh_token": "refresh-token",
    }), category="codex_credentials")

    monkeypatch.setattr(relay, "_email_provider", lambda *_args: lambda *_args, **_kwargs: "")
    monkeypatch.setattr(relay, "_totp_provider", lambda *_args: None)
    monkeypatch.setattr(relay.sms_provider, "fixed_sms_context", lambda **_kwargs: nullcontext())
    monkeypatch.setattr(relay, "_check_stopped", lambda _job_id: None)
    monkeypatch.setattr(relay, "_append_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(relay, "_update_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(relay, "_update_account", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(relay, "_log_path", lambda job_id: tmp_path / f"{job_id}.log")

    from core import chatgpt_plan, codex_oauth

    monkeypatch.setattr(
        codex_oauth,
        "run_codex_oauth",
        lambda email, **kwargs: calls.append({"email": email, **kwargs}) or {
            "ok": True,
            "email": email,
            "phone_verified": False,
            "file_path": str(credential_path),
        },
    )
    monkeypatch.setattr(
        chatgpt_plan,
        "check_codex_usage",
        lambda token, account_id="", proxy=None: usage_calls.append({
            "token": token, "account_id": account_id, "proxy": proxy,
        }) or {"ok": True, "checked_at": "2026-09-30T12:00:00", "plan_type": "plus"},
    )

    relay._run_job("job-proxy-oauth", account["id"], {}, None, proxy)

    assert calls
    assert calls[0]["email"] == "proxy-oauth@example.com"
    assert calls[0]["proxy"] == proxy
    assert usage_calls == [{
        "token": "access-token", "account_id": "account-proxy", "proxy": proxy,
    }]


def test_run_job_rejects_oauth_success_without_token_pair(relay_storage, monkeypatch, tmp_path):
    relay.import_accounts("missing-token@example.com----https://mail.example/code")
    account = relay._read(relay._ACCOUNTS_PATH)[0]
    credential_path = relay._CREDENTIAL_DIR / "codex-missing-token@example.com.json"
    sqlite_store.write_file(credential_path, json.dumps({
        "email": "missing-token@example.com",
        "access_token": "access-token",
    }), category="codex_credentials")
    updates = []
    usage_calls = []

    monkeypatch.setattr(relay, "_email_provider", lambda *_args: lambda *_args, **_kwargs: "")
    monkeypatch.setattr(relay, "_totp_provider", lambda *_args: None)
    monkeypatch.setattr(relay.sms_provider, "fixed_sms_context", lambda **_kwargs: nullcontext())
    monkeypatch.setattr(relay, "_check_stopped", lambda _job_id: None)
    monkeypatch.setattr(relay, "_append_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(relay, "_update_job", lambda _job_id, **changes: updates.append(changes))
    monkeypatch.setattr(relay, "_log_path", lambda job_id: tmp_path / f"{job_id}.log")

    from core import chatgpt_plan, codex_oauth
    monkeypatch.setattr(codex_oauth, "run_codex_oauth", lambda _email, **_kwargs: {
        "ok": True, "email": "missing-token@example.com", "file_path": str(credential_path),
    })
    monkeypatch.setattr(
        chatgpt_plan,
        "check_codex_usage",
        lambda *_args, **_kwargs: usage_calls.append(True) or {"ok": True},
    )

    relay._run_job("job-missing-token", account["id"], {}, None, "")

    stored = relay._read(relay._ACCOUNTS_PATH)[0]
    assert stored["codex_status"] == "failed"
    assert usage_calls == []
    assert updates[-1]["status"] == "failed"
    assert "缺少 refresh_token" in updates[-1]["error"]


def test_run_job_rejects_oauth_success_when_online_check_fails(relay_storage, monkeypatch, tmp_path):
    relay.import_accounts("offline-token@example.com----https://mail.example/code")
    account = relay._read(relay._ACCOUNTS_PATH)[0]
    credential_path = relay._CREDENTIAL_DIR / "codex-offline-token@example.com.json"
    sqlite_store.write_file(credential_path, json.dumps({
        "email": "offline-token@example.com",
        "account_id": "account-offline",
        "access_token": "access-token",
        "refresh_token": "refresh-token",
    }), category="codex_credentials")
    updates = []

    monkeypatch.setattr(relay, "_email_provider", lambda *_args: lambda *_args, **_kwargs: "")
    monkeypatch.setattr(relay, "_totp_provider", lambda *_args: None)
    monkeypatch.setattr(relay.sms_provider, "fixed_sms_context", lambda **_kwargs: nullcontext())
    monkeypatch.setattr(relay, "_check_stopped", lambda _job_id: None)
    monkeypatch.setattr(relay, "_append_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(relay, "_update_job", lambda _job_id, **changes: updates.append(changes))
    monkeypatch.setattr(relay, "_log_path", lambda job_id: tmp_path / f"{job_id}.log")

    from core import chatgpt_plan, codex_oauth
    monkeypatch.setattr(codex_oauth, "run_codex_oauth", lambda _email, **_kwargs: {
        "ok": True, "email": "offline-token@example.com", "file_path": str(credential_path),
    })
    monkeypatch.setattr(chatgpt_plan, "check_codex_usage", lambda *_args, **_kwargs: {
        "ok": False, "http_status": 401, "error": "AT 已过期，需要使用 RT 刷新",
    })

    relay._run_job("job-online-failed", account["id"], {}, None, "")

    stored = relay._read(relay._ACCOUNTS_PATH)[0]
    assert stored["codex_status"] == "failed"
    assert updates[-1]["status"] == "failed"
    assert "在线验证失败" in updates[-1]["error"]


def test_explicit_chatgpt_email_url_format_validates_the_url(relay_storage):
    result = relay.import_accounts(
        "explicit@example.com----chatgpt-password----https://mail.example/latest",
        format_name="chatgpt_email_url",
    )
    assert result["inserted"] == 1

    with pytest.raises(ValueError, match=r"邮箱取码 URL必须是 http\(s\) 地址"):
        relay.import_accounts(
            "bad@example.com----chatgpt-password----not-a-url",
            format_name="chatgpt_email_url",
        )


def test_import_supports_outlook_four_part_material(relay_storage):
    result = relay.import_accounts(
        "outlook@example.com----mailbox-password----client-id----refresh-token"
    )

    assert result["inserted"] == 1
    stored = relay._read(relay._ACCOUNTS_PATH)[0]
    assert stored["email_provider"] == "outlook"
    assert stored["mailbox_password"] == "mailbox-password"
    assert stored["outlook_client_id"] == "client-id"
    assert stored["outlook_refresh_token"] == "refresh-token"


def test_ensure_account_material_is_idempotent_and_preserves_relay_state(relay_storage):
    first = relay.ensure_account_material({
        "email": "Existing@Example.com",
        "chatgpt_password": "chatgpt-password",
        "email_code_url": "https://mail.example/code?token=a----b",
        "totp_secret": "JBSWY3DPEHPK3PXP",
    })
    assert first["created"] is True
    account_id = first["id"]

    relay._update_account(account_id, codex_status="authorized", note="keep this note")
    second = relay.ensure_account_material({
        "email": "existing@example.com",
        "chatgpt_password": "replacement-should-not-win",
        "email_code_url": "https://mail.example/other",
        "totp_secret": "JBSWY3DPEHPK3PXP",
    })

    assert second["id"] == account_id
    assert second["created"] is False
    stored = relay._read(relay._ACCOUNTS_PATH)[0]
    assert stored["chatgpt_password"] == "chatgpt-password"
    assert stored["email_code_url"] == "https://mail.example/code?token=a----b"
    assert stored["codex_status"] == "authorized"
    assert stored["note"] == "keep this note"


def test_ensure_account_material_supports_outlook_credentials(relay_storage):
    result = relay.ensure_account_material({
        "email": "outlook-existing@example.com",
        "mailbox_password": "mailbox-password",
        "outlook_client_id": "client-id",
        "outlook_refresh_token": "refresh-token",
    })
    assert result["email_provider"] == "outlook"
    stored = relay._read(relay._ACCOUNTS_PATH)[0]
    assert stored["mailbox_password"] == "mailbox-password"
    assert stored["outlook_client_id"] == "client-id"
    assert stored["outlook_refresh_token"] == "refresh-token"
    public = relay.list_accounts()[0]
    assert public["email_provider_label"] == "微软邮箱"
    serialized = json.dumps(public)
    assert "mailbox-password" in serialized
    assert "refresh-token" in serialized


def test_ensure_account_material_supports_mailnest_without_password_or_url(relay_storage):
    result = relay.ensure_account_material({
        "email": "mailnest@example.com",
        "email_provider": "mailnest",
        "email_provider_context": {"project_code": "chatgpt001"},
        "login_method": "email_otp",
        "totp_secret": "JBSWY3DPEHPK3PXP",
    })

    assert result["created"] is True
    assert result["email_provider"] == "mailnest"
    assert result["email_provider_label"] == "MailNest"
    stored = relay._read(relay._ACCOUNTS_PATH)[0]
    assert stored["email_provider_context"] == {"project_code": "chatgpt001"}
    assert "project_code" not in result


def test_relay_mailnest_provider_uses_channel_adapter(monkeypatch):
    from core import email_provider

    calls = []
    monkeypatch.setattr(relay, "_check_stopped", lambda _job_id: None)
    monkeypatch.setattr(relay, "_update_job", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(relay, "_append_log", lambda *_args, **_kwargs: None)

    def wait(email, **kwargs):
        calls.append((email, kwargs))
        return "123456"

    monkeypatch.setattr(email_provider, "wait_for_otp", wait)
    provider = relay._email_provider("job-1", {
        "email_provider": "mailnest",
        "email_provider_context": {"project_code": "chatgpt001"},
    })

    assert provider("mailnest@example.com", after_ts=123.0) == "123456"
    email, kwargs = calls[-1]
    assert email == "mailnest@example.com"
    assert kwargs["source"] == "mailnest"
    assert kwargs["context"] == {"project_code": "chatgpt001"}


def test_update_account_records_note_and_password_timestamp(relay_storage):
    relay.import_accounts("user@example.com----old-password----JBSWY3DPEHPK3PXP")
    account_id = relay._read(relay._ACCOUNTS_PATH)[0]["id"]

    public = relay.update_account(account_id, {
        "chatgpt_password": "new-password",
        "note": "Plus account",
    })

    assert public["note"] == "Plus account"
    assert public["has_password"] is True
    stored = relay._read(relay._ACCOUNTS_PATH)[0]
    assert stored["chatgpt_password"] == "new-password"
    assert stored["password_changed_at"]


def test_import_rejects_malformed_or_incomplete_material(relay_storage):
    # A one-character value is too short to be treated as a password.
    with pytest.raises(ValueError, match="邮箱取码 URL"):
        relay.import_accounts("bad@example.com----x")
    with pytest.raises(ValueError, match="必须同时填写或同时留空"):
        relay.import_accounts("bad@example.com----https://mail.example/code----+14155550123----")


def test_separate_account_and_phone_imports_remain_unbound_until_verification(relay_storage):
    accounts_result = relay.import_accounts("\n".join([
        "first@example.com----password-one----JBSWY3DPEHPK3PXP",
        "second@example.com----https://mail.example/second",
    ]))
    assert accounts_result["assigned"] == 0
    assert accounts_result["unassigned_accounts"] == 2

    phones_result = relay.import_phones("\n".join([
        "+14155550111----https://sms.example/one",
        "+14155550222----https://sms.example/two",
    ]))
    assert phones_result["assigned"] == 0
    assert phones_result["unassigned_phones"] == 2

    stored = {row["email"]: row for row in relay._read(relay._ACCOUNTS_PATH)}
    assert not stored["first@example.com"].get("phone")
    assert not stored["second@example.com"].get("phone")
    assert stored["first@example.com"]["chatgpt_password"] == "password-one"
    assert stored["first@example.com"]["totp_secret"] == "JBSWY3DPEHPK3PXP"

    public_phones = relay.list_phones()
    serialized = json.dumps(public_phones)
    assert "https://sms.example/one" in serialized
    assert "+14155550111" in serialized
    assert all(row["candidate"] for row in public_phones)
    assert all(row["available_uses"] == 1 for row in public_phones)


def test_phone_import_default_and_explicit_available_uses(relay_storage):
    relay.import_phones(
        "+14155550123----https://sms.example/default\n"
        "+14155550124----https://sms.example/zero----0\n"
        "+14155550125----https://sms.example/three----3"
    )
    phones = {row["phone"]: row for row in relay.list_phones()}
    assert phones["+14155550123"]["available_uses"] == 1
    assert phones["+14155550124"]["available_uses"] == 0
    assert phones["+14155550125"]["available_uses"] == 3
    assert phones["+14155550123"]["status"] == "available"
    assert phones["+14155550124"]["status"] == "used"
    assert [row["phone"] for row in relay.list_phones(status="used")] == ["+14155550124"]
    assert "max_uses" not in phones["+14155550123"]
    assert "use_count" not in phones["+14155550123"]
    assert "remaining_uses" not in phones["+14155550123"]


def test_adjust_phone_available_uses_supports_single_batch_and_boundaries(relay_storage):
    relay.import_phones(
        "+14155550123----https://sms.example/one\n"
        "+14155550124----https://sms.example/three----3"
    )
    rows = relay._read(relay._PHONES_PATH)
    ids = [row["id"] for row in rows]

    increased = relay.adjust_phone_available_uses(ids, 1)
    assert increased["updated"] == 1
    assert increased["unchanged"] == 1
    assert [row["available_uses"] for row in relay._read(relay._PHONES_PATH)] == [2, 3]

    decreased = relay.adjust_phone_available_uses(ids, -1)
    assert decreased["updated"] == 2
    assert [row["available_uses"] for row in relay._read(relay._PHONES_PATH)] == [1, 2]

    relay.adjust_phone_available_uses([ids[0]], -1)
    assert relay.list_phones()[0]["available_uses"] == 0
    assert relay.list_phones()[0]["candidate"] is False


def test_phone_import_is_idempotent_and_updates_candidate(relay_storage):
    relay.import_accounts("user@example.com----https://mail.example/code")
    relay.import_phones("+14155550123----https://sms.example/old")
    result = relay.import_phones("+14155550123----https://sms.example/new")

    assert result["inserted"] == 0
    assert result["updated"] == 1
    account = relay._read(relay._ACCOUNTS_PATH)[0]
    assert not account.get("sms_code_url")
    assert relay._read(relay._PHONES_PATH)[0]["sms_code_url"] == "https://sms.example/new"


def test_phones_imported_first_stay_candidates_when_accounts_arrive(relay_storage):
    phone_result = relay.import_phones("+14155550123----https://sms.example/code")
    assert phone_result["unassigned_phones"] == 1

    account_result = relay.import_accounts("user@example.com----https://mail.example/code")
    assert account_result["assigned"] == 0
    assert account_result["unassigned_accounts"] == 1
    account = relay._read(relay._ACCOUNTS_PATH)[0]
    assert not account.get("phone")


def test_phone_candidates_are_sorted_by_import_order_and_reusable(relay_storage, monkeypatch):
    relay.import_accounts("first@example.com----https://mail.example/first\nsecond@example.com----https://mail.example/second")
    relay.import_phones("+14155550222----https://sms.example/new----2\n+14155550111----https://sms.example/old")
    phones = relay.list_phones(status="available")
    assert [row["phone"] for row in phones] == ["+14155550222", "+14155550111"]
    assert all(row["candidate"] for row in phones)

    accounts = {row["email"]: row["id"] for row in relay._read(relay._ACCOUNTS_PATH)}
    phone_rows = relay._read(relay._PHONES_PATH)
    phone_ids = [row["id"] for row in sorted(phone_rows, key=lambda row: int(row["seq"]))]
    monkeypatch.setattr(relay, "_run_job", lambda *args: None)
    result = relay.start_jobs(
        [accounts["first@example.com"], accounts["second@example.com"]],
        workers=1,
        phone_ids=phone_ids[:1],
    )
    assert result["submitted"] == 2
    jobs = relay._read(relay._JOBS_PATH)
    assert [job["phone_override"]["phone"] for job in jobs[-2:]] == ["+14155550222", "+14155550222"]


def test_reused_phone_lists_every_bound_account_email(relay_storage):
    relay.import_accounts(
        "first@example.com----https://mail.example/first\n"
        "second@example.com----https://mail.example/second"
    )
    relay.import_phones("+14155550123----https://sms.example/code----2")
    accounts = {row["email"]: row["id"] for row in relay._read(relay._ACCOUNTS_PATH)}
    phone = relay._read(relay._PHONES_PATH)[0]

    relay._bind_verified_phone(accounts["first@example.com"], phone["id"], phone["phone"], phone["sms_code_url"])
    relay._bind_verified_phone(accounts["second@example.com"], phone["id"], phone["phone"], phone["sms_code_url"])

    public = relay.list_phones()[0]
    assert public["assigned"] is True
    assert public["assigned_count"] == 2
    assert public["assigned_account_emails"] == ["first@example.com", "second@example.com"]
    assert public["available_uses"] == 0


def test_default_authorization_does_not_reserve_candidates_before_sms(relay_storage, monkeypatch):
    relay.import_accounts("first@example.com----https://mail.example/first\nsecond@example.com----https://mail.example/second")
    relay.import_phones("+14155550111----https://sms.example/one\n+14155550222----https://sms.example/two")
    accounts = {row["email"]: row["id"] for row in relay._read(relay._ACCOUNTS_PATH)}
    monkeypatch.setattr(relay, "_run_job", lambda *args: None)

    relay.start_jobs([accounts["first@example.com"], accounts["second@example.com"]], workers=2)

    jobs = relay._read(relay._JOBS_PATH)
    assert [job["phone_override"]["phone"] for job in jobs] == ["", ""]
    phones = relay._read(relay._PHONES_PATH)
    assert all(len(row.get("reserved_job_ids") or []) == 0 for row in phones)
    assert all(not row["assigned"] for row in relay.list_phones())


def test_phone_reservation_tracks_capacity_without_consuming_use_count(relay_storage):
    relay.import_accounts("one@example.com----https://mail.example/one\ntwo@example.com----https://mail.example/two")
    relay.import_phones("+14155550123----https://sms.example/code----2")
    accounts = {row["email"]: row for row in relay._read(relay._ACCOUNTS_PATH)}
    phone = relay._read(relay._PHONES_PATH)[0]

    public = relay.list_phones(status="available")[0]
    assert public["candidate"] is True
    assert public["available_uses"] == 2

    first = relay._acquire_phone_for_job("job-one", accounts["one@example.com"], [])
    assert first["phone_id"] == phone["id"]
    second = relay._acquire_phone_for_job("job-two", accounts["two@example.com"], [])
    assert second["phone_id"] == phone["id"]
    assert relay.list_phones()[0]["available_uses"] == 2
    assert relay.list_phones(status="available") == []


def test_authorization_starts_without_phone_pool_when_sms_is_not_needed(relay_storage, monkeypatch):
    relay.import_accounts("user@example.com----https://mail.example/code")
    account = relay._read(relay._ACCOUNTS_PATH)[0]
    monkeypatch.setattr(relay, "_run_job", lambda *args: None)
    monkeypatch.setattr(relay.sms_provider._cfg, "SMS_PROVIDER", "fixed_url")
    monkeypatch.setattr(relay.sms_provider._cfg, "FIXED_SMS_PHONE", "+8613464925132", raising=False)
    monkeypatch.setattr(relay.sms_provider._cfg, "FIXED_SMS_CODE_URL", "https://sms.example/fixed", raising=False)

    result = relay.start_jobs([account["id"]], workers=1)
    assert result["submitted"] == 1
    assert len(relay._read(relay._JOBS_PATH)) == 1
    assert relay._read(relay._PHONES_PATH) == []


def test_authorization_does_not_reserve_phone_pool_capacity_up_front(relay_storage, monkeypatch):
    relay.import_accounts("one@example.com----https://mail.example/one\ntwo@example.com----https://mail.example/two")
    relay.import_phones("+14155550123----https://sms.example/code----1")
    accounts = [row["id"] for row in relay._read(relay._ACCOUNTS_PATH)]
    monkeypatch.setattr(relay, "_run_job", lambda *args: None)

    result = relay.start_jobs(accounts, workers=2)
    assert result["submitted"] == 2
    assert len(relay._read(relay._JOBS_PATH)) == 2
    assert relay._read(relay._PHONES_PATH)[0].get("reserved_job_ids") == []


def test_failed_phone_material_is_marked_invalid_and_not_reused(relay_storage):
    relay.import_accounts("one@example.com----https://mail.example/one")
    relay.import_phones("+14155550123----https://sms.example/code")
    account = relay._read(relay._ACCOUNTS_PATH)[0]
    phone = relay._read(relay._PHONES_PATH)[0]

    relay._acquire_phone_for_job("job-one", account, [])
    relay._mark_phone_invalid(phone["id"], "短信 URL 过期")

    public = relay.list_phones()[0]
    assert public["invalid"] is True
    assert public["candidate"] is False
    assert public["available_uses"] == 1
    with pytest.raises(relay.sms_provider.SmsProviderError, match="没有可用手机号"):
        relay._acquire_phone_for_job("job-two", account, [])


def test_account_filters_cover_provider_status_and_materials(relay_storage):
    relay.import_accounts(
        "outlook@example.com----mailbox----client----refresh\n"
        "api@example.com----https://mail.example/code"
    )
    rows = relay._read(relay._ACCOUNTS_PATH)
    relay._update_account(rows[0]["id"], codex_status="authorized", liveness_status="alive", quota_status="available", totp_secret="SECRET")
    assert [row["email"] for row in relay.list_accounts(provider="outlook")] == ["outlook@example.com"]
    assert [row["email"] for row in relay.list_accounts(codex_status="authorized", liveness="alive", quota_status="available", twofa="enabled")] == ["outlook@example.com"]
    assert {row["email"] for row in relay.list_accounts(phone_status="unbound")} == {"api@example.com", "outlook@example.com"}


def test_manual_codes_are_isolated_by_job_and_stage(relay_storage):
    relay._write(relay._JOBS_PATH, [
        {"id": "job-a", "email": "a@example.com", "status": "waiting_email"},
        {"id": "job-b", "email": "b@example.com", "status": "waiting_email"},
        {"id": "job-c", "email": "c@example.com", "status": "waiting_sms"},
    ])

    relay.submit_verification("job-a", "email", "123456")
    relay.submit_verification("job-c", "sms", "654321")

    assert relay._pop_code("job-a", "email") == "123456"
    assert relay._pop_code("job-b", "email") == ""
    assert relay._pop_code("job-c", "email") == ""
    assert relay._pop_code("job-c", "sms") == "654321"


def test_manual_wait_resumes_only_after_submission(relay_storage):
    relay._write(relay._JOBS_PATH, [
        {"id": "job-wait", "email": "wait@example.com", "status": "running"},
    ])
    relay._stop_events["job-wait"] = threading.Event()
    result = []
    waiter = threading.Thread(target=lambda: result.append(relay._wait_manual_code("job-wait", "email")))
    waiter.start()

    deadline = time.time() + 2
    while time.time() < deadline:
        if relay.list_jobs()[0]["status"] == "waiting_email":
            break
        time.sleep(0.01)
    assert waiter.is_alive()

    relay.submit_verification("job-wait", "email", "123456")
    waiter.join(timeout=2)
    assert result == ["123456"]


def test_browser_assist_wait_resumes_after_user_continue(relay_storage):
    relay._write(relay._JOBS_PATH, [{"id": "job-browser", "email": "browser@example.com", "status": "running"}])
    relay._stop_events["job-browser"] = threading.Event()
    result = []
    waiter = threading.Thread(target=lambda: result.append(relay._wait_browser_assist(
        "job-browser", "cloudflare", "https://auth.openai.com/log-in?state=secret"
    )))
    waiter.start()
    deadline = time.time() + 2
    while time.time() < deadline and relay.list_jobs()[0].get("status") != "waiting_browser":
        time.sleep(0.01)
    assert waiter.is_alive()
    assert relay.list_jobs()[0]["browser_assist_reason"] == "cloudflare"
    relay.submit_browser_assist("job-browser")
    waiter.join(timeout=2)
    assert result == [None]
    assert relay.list_jobs()[0]["status"] == "running"


def test_browser_assist_focuses_exact_waiting_session(relay_storage):
    relay._write(relay._JOBS_PATH, [{"id": "job-browser", "email": "browser@example.com", "status": "running"}])
    relay._stop_events["job-browser"] = threading.Event()
    actions = []
    waiter = threading.Thread(target=lambda: relay._wait_browser_assist(
        "job-browser",
        "cloudflare",
        "https://auth.openai.com/log-in",
        focus_action=lambda: actions.append("focus"),
        background_action=lambda: actions.append("background"),
    ))
    waiter.start()
    deadline = time.time() + 2
    while time.time() < deadline and relay.list_jobs()[0].get("status") != "waiting_browser":
        time.sleep(0.01)

    result = relay.focus_browser_assist("job-browser")
    assert result["status"] == "waiting_browser"
    assert actions == ["focus"]
    relay.submit_browser_assist("job-browser")
    waiter.join(timeout=2)
    assert not waiter.is_alive()
    assert "background" in actions
    assert relay.list_jobs()[0]["browser_focus_available"] is False


def test_browser_assist_timeout_keeps_slot_until_browser_cleanup(relay_storage, monkeypatch):
    relay._write(relay._JOBS_PATH, [{"id": "job-browser", "email": "browser@example.com", "status": "running"}])
    relay._stop_events["job-browser"] = threading.Event()
    monkeypatch.setattr(relay, "_browser_assist_grace_seconds", lambda: 0.05)
    semaphore = threading.BoundedSemaphore(1)
    slot = relay._ExecutionSlot(semaphore)
    slot.acquire()

    errors = []

    def wait_for_assist():
        try:
            relay._wait_browser_assist(
                "job-browser",
                "cloudflare",
                "https://auth.openai.com/log-in?state=secret",
                execution_slot=slot,
            )
        except Exception as exc:
            errors.append(exc)

    waiter = threading.Thread(target=wait_for_assist)
    waiter.start()
    waiter.join(timeout=2)
    assert not waiter.is_alive()
    assert isinstance(errors[0], relay.BrowserAssistTimeout)
    assert slot.held is True
    slot.release()


def test_browser_assist_auto_detects_resolution_within_grace_period(relay_storage, monkeypatch):
    relay._write(relay._JOBS_PATH, [{"id": "job-browser", "email": "browser@example.com", "status": "running"}])
    relay._stop_events["job-browser"] = threading.Event()
    monkeypatch.setattr(relay, "_browser_assist_grace_seconds", lambda: 1.0)
    resolved = threading.Event()
    semaphore = threading.BoundedSemaphore(1)
    slot = relay._ExecutionSlot(semaphore)
    slot.acquire()

    waiter = threading.Thread(target=lambda: relay._wait_browser_assist(
        "job-browser",
        "cloudflare",
        execution_slot=slot,
        resolved_check=resolved.is_set,
    ))
    waiter.start()
    resolved.set()
    waiter.join(timeout=2)
    assert not waiter.is_alive()
    job = relay.list_jobs()[0]
    assert job["status"] == "running"
    assert "自动检测到 Cloudflare 验证完成" in job["message"]
    assert slot.held is True
    slot.release()


def test_stop_wakes_a_waiting_verification(relay_storage):
    relay._write(relay._JOBS_PATH, [
        {"id": "job-a", "email": "a@example.com", "status": "waiting_sms"},
    ])
    relay._stop_events["job-a"] = threading.Event()

    result = relay.stop_job("job-a")

    assert result["status"] == "stopping"
    assert relay._stop_events["job-a"].is_set()
    assert relay._verification_events[("job-a", "sms")].is_set()


def test_stop_wakes_browser_assist(relay_storage):
    relay._write(relay._JOBS_PATH, [{"id": "job-browser", "email": "a@example.com", "status": "waiting_browser"}])
    relay._stop_events["job-browser"] = threading.Event()
    relay._verification_events[("job-browser", "browser")] = threading.Event()
    result = relay.stop_job("job-browser")
    assert result["status"] == "stopping"
    assert relay._verification_events[("job-browser", "browser")].is_set()


def test_batch_delete_jobs_removes_terminal_rows_and_skips_active_rows(relay_storage):
    relay._write(relay._JOBS_PATH, [
        {"id": "job-success", "status": "success"},
        {"id": "job-failed", "status": "failed"},
        {"id": "job-active", "status": "waiting_browser"},
    ])
    relay._write(relay._PHONES_PATH, [{
        "id": "phone-1", "phone": "+14155550123", "sms_code_url": "https://sms.example/code",
        "reserved_job_ids": ["job-success", "job-active"],
    }])
    sqlite_store.write_file(relay._log_path("job-success"), "done", category="relay_logs")
    sqlite_store.write_file(relay._log_path("job-active"), "waiting", category="relay_logs")

    result = relay.delete_jobs(["job-success", "job-failed", "job-active", "job-missing"])

    assert result["deleted"] == 2
    assert result["deleted_ids"] == ["job-success", "job-failed"]
    assert result["skipped"] == [
        {"job_id": "job-active", "reason": "运行中或等待人工处理的任务不能删除"},
        {"job_id": "job-missing", "reason": "任务不存在"},
    ]
    assert [row["id"] for row in relay._read(relay._JOBS_PATH)] == ["job-active"]
    assert relay._read(relay._PHONES_PATH)[0]["reserved_job_ids"] == ["job-active"]
    assert not sqlite_store.file_exists(relay._log_path("job-success"), category="relay_logs")
    assert sqlite_store.file_exists(relay._log_path("job-active"), category="relay_logs")


def test_recover_interrupted_jobs(relay_storage):
    relay._write(relay._JOBS_PATH, [
        {"id": "a", "status": "running"},
        {"id": "b", "status": "waiting_email"},
        {"id": "c", "status": "success"},
    ])
    relay._write(relay._PHONES_PATH, [{
        "id": "phone-1", "phone": "+14155550123", "sms_code_url": "https://sms.example/code",
        "reserved_job_ids": ["a", "b"],
    }])

    assert relay.recover_interrupted_jobs() == 2
    statuses = {row["id"]: row["status"] for row in relay._read(relay._JOBS_PATH)}
    assert statuses == {"a": "stopped", "b": "stopped", "c": "success"}
    assert relay._read(relay._PHONES_PATH)[0]["reserved_job_ids"] == []


def test_public_job_collapses_duplicate_runtime_error_prefix():
    public = relay._public_job({
        "id": "job-1",
        "error": "RuntimeError: RuntimeError: 提交密码后页面未推进",
    })

    assert public["error"] == "RuntimeError: 提交密码后页面未推进"


def test_start_jobs_skips_deactivated_accounts(relay_storage):
    relay._write(relay._ACCOUNTS_PATH, [
        {"id": "account-disabled", "email": "disabled@example.com", "codex_status": "deactivated"},
    ])

    result = relay.start_jobs(["account-disabled"])

    assert result["submitted"] == 0
    assert [item["reason"] for item in result["skipped"]] == ["账号已禁用"]
    assert relay._read(relay._JOBS_PATH) == []


def test_fixed_sms_context_is_thread_local(monkeypatch):
    seen = {}
    barrier = threading.Barrier(2)

    def worker(name, phone, url):
        with relay.sms_provider.fixed_sms_context(phone, url):
            barrier.wait()
            seen[name] = relay.sms_provider._fixed_sms_config()

    a = threading.Thread(target=worker, args=("a", "+14155550123", "https://sms.example/a"))
    b = threading.Thread(target=worker, args=("b", "+8613412345678", "https://sms.example/b"))
    a.start(); b.start(); a.join(); b.join()

    assert seen["a"] == ("14155550123", "https://sms.example/a")
    assert seen["b"] == ("8613412345678", "https://sms.example/b")


def test_task_logs_keep_complete_values_visible(relay_storage, monkeypatch):
    monkeypatch.delenv("CODEX_RELAY_SHOW_FULL_URLS", raising=False)
    relay._append_log("job-phone", "手机号 +14155550123 已提交，URL https://sms.example/code")
    log = relay.read_log("job-phone")
    assert "14155550123" in log
    assert "https://sms.example/code" in log
    assert "手机号已隐藏" not in log


def test_redaction_keeps_phone_and_browser_diagnostics_readable():
    text = (
        "phone=+14155550123 chrome=154.0.8037.44 "
        "debuggerAddress=127.0.0.1:51471 0 chromedriver 0x0000000102ca3884"
    )
    redacted = relay._diagnostic_text(text)
    assert "+14155550123" in redacted
    assert "手机号已隐藏" not in redacted
    assert "154.0.8037.44" in redacted
    assert "127.0.0.1:51471" in redacted
    assert "0x0000000102ca3884" in redacted


def test_task_logs_keep_otp_values_in_every_context(relay_storage):
    otp = "654321"
    for message in (
        f"验证码：{otp}",
        f"邮箱 OTP 收到：{otp}",
        f"提交手机验证码={otp}",
        f"code={otp}",
    ):
        assert otp in relay._diagnostic_text(message)

    # Ordinary numeric diagnostics remain useful to operators.
    assert otp in relay._diagnostic_text(f"任务已重试 {otp} 次")


def test_browser_assist_wait_defaults_to_five_minutes(monkeypatch):
    monkeypatch.delenv("CODEX_RELAY_BROWSER_ASSIST_GRACE_SECONDS", raising=False)
    assert relay._browser_assist_grace_seconds() == 300.0
    monkeypatch.setenv("CODEX_RELAY_BROWSER_ASSIST_GRACE_SECONDS", "999")
    assert relay._browser_assist_grace_seconds() == 300.0


def _configure_dynamic_sms_platform(monkeypatch, *, provider="l", ready=True):
    """Configure the synthetic phone-pool source without contacting a provider."""
    monkeypatch.setattr(relay._codex_config, "SMS_PROVIDER", provider)
    monkeypatch.setattr(relay._codex_config, "SMS_POOL_PLATFORM_ENABLED", True)
    if provider == "grizzly":
        monkeypatch.setattr(relay._codex_config, "SMS_API_BASE", "https://sms.example/api" if ready else "")
        monkeypatch.setattr(relay._codex_config, "SMS_API_KEY", "test-key" if ready else "")
    elif provider == "h":
        monkeypatch.setattr(relay._codex_config, "H_API_BASE", "http://h.example" if ready else "")
        monkeypatch.setattr(relay._codex_config, "H_ADMIN_AUTH_CODE", "test-auth" if ready else "")
    else:
        monkeypatch.setattr(relay._codex_config, "L_API_BASE", "http://l.example" if ready else "")
        monkeypatch.setattr(relay._codex_config, "L_ADMIN_AUTH_CODE", "test-auth" if ready else "")


def test_list_phones_places_ready_sms_platform_special_row_first(relay_storage, monkeypatch):
    _configure_dynamic_sms_platform(monkeypatch, provider="l", ready=True)
    relay.import_phones("+14155550123----https://sms.example/one")

    rows = relay.list_phones()

    assert len(rows) == 2
    special = rows[0]
    assert special["special"] is True
    assert special["id"] == "sms-provider:l"
    assert special["provider"] == "l"
    assert special["status"] == "platform"
    assert special["ready"] is True
    assert special["candidate"] is True
    assert special["phone"] == ""
    assert rows[1]["phone"] == "+14155550123"
    assert relay.list_phones(status="platform")[0]["id"] == "sms-provider:l"


def test_list_phones_marks_enabled_but_unconfigured_sms_platform_unavailable(relay_storage, monkeypatch):
    _configure_dynamic_sms_platform(monkeypatch, provider="l", ready=False)

    rows = relay.list_phones()

    assert len(rows) == 1
    special = rows[0]
    assert special["special"] is True
    assert special["id"] == "sms-provider:l"
    assert special["status"] == "unavailable"
    assert special["ready"] is False
    assert special["candidate"] is False
    assert "未就绪" in special["message"]
    assert relay.list_phones(status="available") == []


def test_sms_platform_special_row_cannot_be_deleted_or_adjusted(relay_storage, monkeypatch):
    _configure_dynamic_sms_platform(monkeypatch, provider="h", ready=True)
    special_id = relay.list_phones()[0]["id"]

    with pytest.raises(ValueError, match="特殊来源不能删除"):
        relay.delete_phones([special_id])
    with pytest.raises(ValueError, match="特殊来源没有固定次数"):
        relay.adjust_phone_available_uses([special_id], 1)


def test_start_jobs_uses_enabled_sms_platform_as_phone_override(relay_storage, monkeypatch):
    _configure_dynamic_sms_platform(monkeypatch, provider="grizzly", ready=True)
    relay.import_accounts("platform@example.com----https://mail.example/code")
    account_id = relay._read(relay._ACCOUNTS_PATH)[0]["id"]
    monkeypatch.setattr(relay, "_run_job", lambda *args: None)

    result = relay.start_jobs([account_id], workers=1)

    assert result["submitted"] == 1
    jobs = relay._read(relay._JOBS_PATH)
    assert len(jobs) == 1
    override = jobs[0]["phone_override"]
    assert override["source_type"] == "platform"
    assert override["platform_provider"] == "grizzly"
    assert override["phone_hint_id"] == "sms-provider:grizzly"
    assert override["phone_id"] == "sms-provider:grizzly"
    assert override["phone"] == ""
    assert override["sms_code_url"] == ""


def test_start_jobs_rejects_direct_selection_of_synthetic_platform_row(relay_storage, monkeypatch):
    _configure_dynamic_sms_platform(monkeypatch, provider="l", ready=True)
    relay.import_accounts("platform-direct@example.com----https://mail.example/code")
    account_id = relay._read(relay._ACCOUNTS_PATH)[0]["id"]

    with pytest.raises(ValueError, match="特殊来源不能手动选择"):
        relay.start_jobs([account_id], phone_ids=["sms-provider:l"])


@pytest.mark.parametrize("pool, requested, account_proxy, actual, expected", [
    (["http://pool.example:8080"], "", "http://account.example:3128", None, ""),
    (["http://pool.example:8080"], None, "", None, ""),
    ([], None, "", None, ""),
    (["http://pool.example:8080"], None, "http://account.example:3128", None, "http://account.example:3128"),
    (["http://pool.example:8080"], None, "", "socks5h://user:private@profile.example:1080", ""),
    (["http://pool.example:8080"], None, "", "", ""),
])
def test_oauth_route_is_selected_once_and_saved_privately(relay_storage, monkeypatch, pool, requested, account_proxy, actual, expected):
    from config import proxy as proxy_cfg
    from core import codex_oauth

    relay.import_accounts(json.dumps({
        "email": "routes@example.com", "email_code_url": "https://mail.example/code", "proxy": account_proxy,
    }))
    account = relay._read(relay._ACCOUNTS_PATH)[0]
    relay._write(relay._JOBS_PATH, [{"id": "job-routes", "account_id": account["id"], "status": "pending"}])
    monkeypatch.setattr(proxy_cfg, "PROXY_POOL", pool)
    calls = []

    def oauth(email, **kwargs):
        calls.append(kwargs["proxy"])
        result = {"ok": True, "_profile_id": "temporary-profile"}
        if actual is not None:
            result["_verification_proxy"] = actual
        return result

    monkeypatch.setattr(codex_oauth, "run_codex_oauth", oauth)
    monkeypatch.setattr(relay, "_validate_saved_codex_credential", lambda _r, _a, proxy: (
        calls.append(proxy) or relay_storage / "credential", {}, {"ok": True},
    ))
    monkeypatch.setattr(relay, "_email_provider", lambda *_a: None)
    monkeypatch.setattr(relay, "_totp_provider", lambda *_a: None)
    relay._run_job("job-routes", account["id"], proxy=requested)
    route = actual if actual is not None else expected
    assert calls == [expected, route]
    stored = relay._get_account(account["id"])
    assert stored["_codex_verified_proxy"] == route
    assert stored["codex_profile_id"] == "temporary-profile"
    assert relay._read(relay._JOBS_PATH)[0]["status"] == "success"
    assert relay._public_account(stored)["_codex_verified_proxy"] == route
