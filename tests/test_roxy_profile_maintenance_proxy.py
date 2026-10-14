import json

import pytest

from core import codex_relay_service as relay
from core import sqlite_store


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


def _save_credential(relay_storage, account, *, access_token="old-access-token"):
    relay._CREDENTIAL_DIR.mkdir(parents=True)
    path = relay._CREDENTIAL_DIR / f"codex-{account['email']}.json"
    sqlite_store.write_file(
        path,
        json.dumps({
            "email": account["email"],
            "account_id": "account-before-refresh",
            "access_token": access_token,
            "refresh_token": "old-refresh-token",
        }),
        category="codex_credentials",
    )
    return path


def _save_job(account, action):
    relay._write(relay._JOBS_PATH, [{
        "id": f"job-{action}",
        "account_id": account["id"],
        "email": account["email"],
        "action": action,
        "status": "queued",
        "stage": "queued",
    }])
    return f"job-{action}"


def test_account_effective_proxy_prefers_explicit_proxy_over_profile(monkeypatch):
    explicit = "http://account-proxy.example:3128"

    from core import roxy_codex_oauth

    monkeypatch.setattr(
        roxy_codex_oauth,
        "_roxy_profile_token_proxy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("显式账号代理存在时不应查询 Roxy Profile")
        ),
    )

    assert relay._account_effective_proxy({
        "proxy": explicit,
        "codex_profile_id": "profile-id",
    }) == explicit


@pytest.mark.parametrize("profile_proxy", ["http://profile-proxy.example:3128", ""], ids=["proxy", "confirmed-direct"])
def test_gpt_liveness_uses_same_roxy_profile_proxy_for_refresh_and_usage(
    relay_storage, monkeypatch, profile_proxy
):
    relay.import_accounts("profile-route@example.com----https://mail.example/code")
    account = relay._read(relay._ACCOUNTS_PATH)[0]
    relay._update_account(account["id"], codex_profile_id="profile-id")
    account = relay._get_account(account["id"])
    _save_credential(relay_storage, account)
    job_id = _save_job(account, "check_gpt_liveness")

    from core import chatgpt_plan, codex_oauth, roxy_codex_oauth

    monkeypatch.setattr(
        roxy_codex_oauth,
        "_roxy_profile_token_proxy",
        lambda opened, requested_proxy=None: profile_proxy,
    )
    calls = []
    monkeypatch.setattr(
        codex_oauth,
        "refresh_codex_credential",
        lambda path, proxy="": calls.append(("refresh", proxy)) or {
            "email": account["email"],
            "account_id": "account-after-refresh",
            "access_token": "fresh-access-token",
            "refresh_token": "fresh-refresh-token",
        },
    )
    monkeypatch.setattr(
        chatgpt_plan,
        "check_codex_usage",
        lambda token, account_id="", proxy=None: calls.append(("usage", proxy)) or {
            "ok": True,
            "checked_at": "2026-09-30T12:00:00",
            "plan_type": "plus",
            "weekly_used_percent": 20,
        },
    )

    relay._run_maintenance_job(job_id, account["id"], "check_gpt_liveness")

    job = relay._read(relay._JOBS_PATH)[0]
    assert job["status"] == "success"
    assert calls == [("refresh", profile_proxy), ("usage", profile_proxy)]


@pytest.mark.parametrize("action", ["check_gpt_liveness", "check_quota"])
@pytest.mark.parametrize("route_error", [True, False], ids=["lookup-failed", "unknown-route"])
def test_profile_route_failure_stops_maintenance_before_network_calls(
    relay_storage, monkeypatch, action, route_error
):
    relay.import_accounts("profile-failure@example.com----https://mail.example/code")
    account = relay._read(relay._ACCOUNTS_PATH)[0]
    relay._update_account(account["id"], codex_profile_id="missing-profile")
    account = relay._get_account(account["id"])
    _save_credential(relay_storage, account)
    job_id = _save_job(account, action)

    from core import chatgpt_plan, codex_oauth, roxy_codex_oauth

    def profile_route(*_args, **_kwargs):
        if route_error:
            raise RuntimeError("Roxy route lookup failed")
        return None

    monkeypatch.setattr(roxy_codex_oauth, "_roxy_profile_token_proxy", profile_route)
    calls = []
    monkeypatch.setattr(
        codex_oauth,
        "refresh_codex_credential",
        lambda *_args, **_kwargs: calls.append("refresh") or {},
    )
    monkeypatch.setattr(
        chatgpt_plan,
        "check_codex_usage",
        lambda *_args, **_kwargs: calls.append("usage") or {"ok": True},
    )

    relay._run_maintenance_job(job_id, account["id"], action)

    job = relay._read(relay._JOBS_PATH)[0]
    stored = relay._get_account(account["id"])
    assert job["status"] == "failed"
    assert "Roxy Profile" in job["error"]
    assert stored["maintenance_status"] == "failed"
    if action == "check_gpt_liveness":
        assert stored["liveness_status"] == "error"
    else:
        assert stored["quota_status"] == "error"
    assert calls == []


@pytest.mark.parametrize("route", ["", "socks5h://user:private@proxy.example:1080"])
@pytest.mark.parametrize("action", ["check_gpt_liveness", "check_quota"])
def test_persisted_verified_route_survives_profile_deletion(relay_storage, monkeypatch, route, action):
    from core import chatgpt_plan, codex_oauth, roxy_codex_oauth

    relay.import_accounts("snapshot@example.com----https://mail.example/code")
    account = relay._read(relay._ACCOUNTS_PATH)[0]
    relay._update_account(account["id"], codex_profile_id="deleted-temporary-profile", _codex_verified_proxy=route)
    account = relay._get_account(account["id"])
    _save_credential(relay_storage, account)
    job_id = _save_job(account, action)
    monkeypatch.setattr(roxy_codex_oauth, "_roxy_profile_token_proxy", lambda *_a, **_kw: pytest.fail("saved route must not depend on a deleted Profile"))
    calls = []
    monkeypatch.setattr(codex_oauth, "refresh_codex_credential", lambda _path, proxy: calls.append(("refresh", proxy)) or {
        "email": account["email"], "account_id": "id", "access_token": "at", "refresh_token": "rt",
    })
    monkeypatch.setattr(chatgpt_plan, "check_codex_usage", lambda *_a, proxy: calls.append(("usage", proxy)) or {
        "ok": True, "checked_at": "2026-10-01T12:00:00", "plan_type": "free",
    })
    relay._run_maintenance_job(job_id, account["id"], action)
    assert relay._read(relay._JOBS_PATH)[0]["status"] == "success"
    assert calls == ([("refresh", route), ("usage", route)] if action == "check_gpt_liveness" else [("usage", route)])
    assert "_codex_verified_proxy" not in relay._public_account(account)
    assert "private" not in json.dumps(relay.list_jobs())


def test_proxy_reimport_invalidates_old_oauth_snapshot(relay_storage):
    relay.import_accounts("snapshot@example.com----https://mail.example/code")
    account = relay._read(relay._ACCOUNTS_PATH)[0]
    relay._update_account(account["id"], codex_profile_id="old-profile", _codex_verified_proxy="")
    relay.import_accounts(json.dumps({
        "email": account["email"], "email_code_url": "https://mail.example/code",
        "proxy": "http://new-proxy.example:8080",
    }))
    stored = relay._get_account(account["id"])
    assert "_codex_verified_proxy" not in stored
    assert "codex_profile_id" not in stored
    assert relay._account_effective_proxy(stored) == "http://new-proxy.example:8080"
