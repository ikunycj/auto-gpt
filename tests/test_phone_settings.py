"""Phone settings use isolated SQLite data; no SMS/OAuth network calls."""
from datetime import datetime, timedelta, timezone

import pytest

from apps.web.app import create_app
from core import codex_relay_service as relay


@pytest.fixture
def phone_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("TURB_SQLITE_PATH", str(tmp_path / "workspace.sqlite3"))
    for attribute, name in (
        ("_ACCOUNTS_PATH", "accounts.json"), ("_PHONES_PATH", "phones.json"),
        ("_JOBS_PATH", "jobs.json"), ("_LOG_DIR", "logs"),
        ("_CREDENTIAL_DIR", "credentials"), ("_SUB2_SERVICES_PATH", "sub2.json"),
    ):
        monkeypatch.setattr(relay, attribute, tmp_path / name)
    monkeypatch.setattr(relay._codex_config, "SMS_POOL_PLATFORM_ENABLED", False)
    clock = ["2026-10-05T12:00:00+08:00"]
    monkeypatch.setattr(relay, "_now", lambda: clock[0])
    return clock


@pytest.fixture
def phones(phone_storage):
    relay.import_phones(
        "+14155550123----https://sms.example/one\n"
        "+14155550124----https://sms.example/two----2"
    )
    return relay._read(relay._PHONES_PATH)


def test_new_and_legacy_phones_default_to_no_expiration(phones):
    assert all(row["expires_at"] == "" for row in phones)
    # Records written before this feature need no migration.
    phones[0].pop("expires_at")
    relay._write(relay._PHONES_PATH, phones)
    for row in relay.list_phones():
        assert row["expires_at"] == ""
        assert row["expired"] is False
        assert row["expires_in_days"] is None
        assert row["candidate"] is True
    assert relay._read(relay._PHONES_PATH) == phones  # GET does not mutate storage.
    material = relay._acquire_phone_for_job("job", {})
    assert material["phone_id"] == phones[0]["id"]


def test_single_and_batch_exact_settings_preserve_other_rows(phones):
    ids = [row["id"] for row in phones]
    result = relay.update_phone_settings([ids[0]], available_uses=0)
    assert result["updated"] == 1
    stored = relay._read(relay._PHONES_PATH)
    assert stored[0]["available_uses"] == 0
    assert stored[1] == phones[1]
    assert relay.list_phones()[0]["candidate"] is False

    result = relay.update_phone_settings([*ids, ids[0]], available_uses=3)
    assert result["requested"] == 2
    assert result["updated"] == 2
    assert [row["available_uses"] for row in relay.list_phones()] == [3, 3]
    stored = relay._read(relay._PHONES_PATH)
    result = relay.update_phone_settings(ids, available_uses=3, expiration_days=0)
    assert result["updated"] == 0
    assert result["unchanged"] == 2
    assert relay._read(relay._PHONES_PATH) == stored


def test_expiration_days_start_at_save_and_omitted_fields_are_preserved(phones, phone_storage):
    phone_id = phones[0]["id"]
    result = relay.update_phone_settings([phone_id], available_uses=3, expiration_days=7)
    deadline = result["items"][0]["expires_at"]
    assert datetime.fromisoformat(deadline) == datetime.fromisoformat(phone_storage[0]) + timedelta(days=7)
    assert result["items"][0]["expires_in_days"] == 7
    phone_storage[0] = "2026-10-06T12:00:01+08:00"
    relay.update_phone_settings([phone_id], available_uses=2)
    row = relay.list_phones()[0]
    assert row["expires_at"] == deadline
    assert row["expires_in_days"] == 6
    assert row["expired"] is False
    # Existing import/update and +/-1 must not accidentally clear/extend expiry.
    relay.import_phones("+14155550123----https://sms.example/new----1")
    relay.adjust_phone_available_uses([phone_id], 1)
    assert relay.list_phones()[0]["expires_at"] == deadline
    assert relay._read(relay._PHONES_PATH)[1] == phones[1]
    relay.update_phone_settings([phone_id], expiration_days=0)
    row = relay.list_phones()[0]
    assert row["available_uses"] == 2
    assert row["expires_at"] == ""
    assert row["expires_in_days"] is None
    assert not row["expired"]


@pytest.mark.parametrize("deadline", [
    "2026-10-05T11:59:59+08:00",  # before now
    "2026-10-05T12:00:00+08:00",  # exact boundary
    "2026-10-05T04:00:00Z",       # same instant in UTC
    "not-a-date",                 # corrupt deadline must not make a phone available
])
def test_expired_phones_are_never_allocated_even_if_reserved_or_preferred(phones, deadline):
    phones[0].update(expires_at=deadline, reserved_job_ids=["job"])
    relay._write(relay._PHONES_PATH, phones)
    expired = relay.list_phones(status="expired")
    assert [row["id"] for row in expired] == [phones[0]["id"]]
    assert expired[0]["expired"] is True
    assert expired[0]["expires_in_days"] == 0
    assert expired[0]["candidate"] is False
    assert expired[0]["status"] == "expired"
    assert [row["id"] for row in relay.list_phones(status="available")] == [phones[1]["id"]]
    assert relay._phone_capacity(phones[0]) == (1, 0)
    assert relay._acquire_phone_for_job("job", {}, [phones[0]["id"]])["phone_id"] == phones[1]["id"]
    relay._write(relay._PHONES_PATH, [phones[0]])
    with pytest.raises(relay.sms_provider.SmsProviderError, match="过期时间"):
        relay._acquire_phone_for_job("job", {}, [phones[0]["id"]])


def test_expiration_boundary_and_naive_timestamp_compatibility(phone_storage):
    now = datetime.fromisoformat(phone_storage[0])
    future = now + timedelta(seconds=1)
    assert relay._phone_expiration({"expires_at": future.isoformat()}) == (False, 1)
    local_naive = future.astimezone().replace(tzinfo=None).isoformat()
    assert relay._phone_expiration({"expires_at": local_naive}) == (False, 1)
    assert relay._phone_expiration({"expires_at": future.isoformat()}, now=future) == (True, 0)


def test_expired_numbers_need_expiry_reset_not_just_more_uses(phones, phone_storage):
    phone_id = phones[0]["id"]
    relay.update_phone_settings([phone_id], expiration_days=1)
    phone_storage[0] = "2026-10-06T12:00:00+08:00"
    relay.adjust_phone_available_uses([phone_id], 1)
    assert relay.list_phones()[0]["candidate"] is False
    relay.update_phone_settings([phone_id], expiration_days=0)
    assert relay.list_phones()[0]["candidate"] is True
    # Clearing expiry or setting a new deadline does not clear actual SMS errors.
    relay._mark_phone_invalid(phone_id, "SMS failure")
    relay.update_phone_settings([phone_id], expiration_days=7, available_uses=3)
    row = relay.list_phones()[0]
    assert row["invalid"] is True
    assert row["invalid_reason"] == "SMS failure"
    assert row["candidate"] is False


def test_batch_expiration_is_uniform_and_does_not_modify_balances(phones, phone_storage):
    ids = [row["id"] for row in phones]
    result = relay.update_phone_settings(ids, expiration_days="2")
    assert result["updated"] == 2
    rows = relay.list_phones()
    assert rows[0]["expires_at"] == rows[1]["expires_at"]
    assert [row["available_uses"] for row in rows] == [1, 2]
    # Saving a duration again explicitly renews from the new save time.
    phone_storage[0] = "2026-10-06T12:00:00+08:00"
    relay.update_phone_settings([ids[0]], expiration_days=2)
    new_rows = relay.list_phones()
    assert datetime.fromisoformat(new_rows[0]["expires_at"]) - datetime.fromisoformat(rows[0]["expires_at"]) == timedelta(days=1)
    assert new_rows[1]["expires_at"] == rows[1]["expires_at"]


@pytest.mark.parametrize("settings", [
    {"available_uses": -1}, {"available_uses": 4}, {"available_uses": True},
    {"available_uses": 1.5}, {"available_uses": "1.5"}, {"available_uses": None},
    {"available_uses": []}, {"available_uses": {}}, {"available_uses": ""},
    {"expiration_days": -1}, {"expiration_days": 2.5}, {"expiration_days": True},
    {"expiration_days": "2.5"}, {"expiration_days": ""}, {"expiration_days": None},
    {"expiration_days": []}, {"expiration_days": 10**20}, {},
])
def test_settings_validation_is_atomic(phones, settings):
    with pytest.raises(ValueError):
        relay.update_phone_settings([row["id"] for row in phones], **settings)
    assert relay._read(relay._PHONES_PATH) == phones


@pytest.mark.parametrize("ids", [None, [], "some-id", [None], [True], [""], [" "]])
def test_settings_reject_malformed_selection(phones, ids):
    with pytest.raises(ValueError):
        relay.update_phone_settings(ids, available_uses=3)
    assert relay._read(relay._PHONES_PATH) == phones


@pytest.mark.parametrize("missing", ["missing-id", "sms-provider:l"])
def test_invalid_batch_member_does_not_partially_update(phones, missing):
    with pytest.raises(ValueError):
        relay.update_phone_settings([phones[0]["id"], missing], available_uses=3, expiration_days=7)
    assert relay._read(relay._PHONES_PATH) == phones


def test_reserved_phone_cannot_be_reduced_or_have_deadline_changed(phones):
    phones[1]["reserved_job_ids"] = ["job"]
    relay._write(relay._PHONES_PATH, phones)
    ids = [row["id"] for row in phones]
    for settings in ({"available_uses": 0}, {"expiration_days": 0}, {"expiration_days": 7, "available_uses": 3}):
        with pytest.raises(ValueError, match="任务占用"):
            relay.update_phone_settings(ids, **settings)
        assert relay._read(relay._PHONES_PATH) == phones
    relay.update_phone_settings(ids, available_uses=3)
    assert [row["available_uses"] for row in relay.list_phones()] == [3, 3]
    assert relay._read(relay._PHONES_PATH)[1]["reserved_job_ids"] == ["job"]


@pytest.mark.parametrize("consume", [True, False])
def test_expired_phone_cannot_create_binding_or_consume_balance(phones, consume):
    relay.import_accounts("one@example.com----https://mail.example/code")
    account = relay._read(relay._ACCOUNTS_PATH)[0]
    phones[0]["expires_at"] = "2026-10-05T12:00:00+08:00"
    relay._write(relay._PHONES_PATH, phones)
    with pytest.raises(relay.sms_provider.SmsProviderError, match="已过期"):
        relay._bind_verified_phone(account["id"], phones[0]["id"], phones[0]["phone"], phones[0]["sms_code_url"], consume_use=consume)
    assert relay._read(relay._PHONES_PATH) == phones
    assert relay._read(relay._ACCOUNTS_PATH)[0] == account


def test_successful_verification_consumes_once_and_binding_survives_expiry(phones, phone_storage):
    relay.import_accounts("one@example.com----https://mail.example/code")
    account = relay._read(relay._ACCOUNTS_PATH)[0]
    phone = phones[0]
    relay.update_phone_settings([phone["id"]], expiration_days=1)
    args = (account["id"], phone["id"], phone["phone"], phone["sms_code_url"])
    relay._bind_verified_phone(*args)
    assert relay.list_phones()[0]["available_uses"] == 0
    phone_storage[0] = "2026-10-06T12:00:00+08:00"
    # Final OAuth bookkeeping after an already recorded SMS success is safe.
    relay._bind_verified_phone(*args, consume_use=False)
    row = relay.list_phones(status="bound")[0]
    assert row["available_uses"] == 0
    assert row["assigned_account_email"] == account["email"]
    assert row["status"] == "expired"
    assert row["candidate"] is False
    relay.update_phone_settings([phone["id"]], expiration_days=0, available_uses=2)
    assert relay.list_phones()[0]["assigned_account_email"] == account["email"]


@pytest.fixture
def client(phone_storage):
    return create_app().test_client()


def test_phone_settings_api_single_batch_and_legacy_adjustment(client, phones):
    ids = [row["id"] for row in phones]
    response = client.patch("/api/phones/settings", json={"phone_ids": [ids[0]], "available_uses": 0, "expiration_days": 7})
    assert response.status_code == 200
    assert response.json["updated"] == 1
    rows = client.get("/api/phones").json["items"]
    assert rows[0]["available_uses"] == 0
    assert rows[0]["expires_in_days"] == 7
    assert rows[1]["expires_at"] == ""
    deadline = rows[0]["expires_at"]
    response = client.patch("/api/phones/settings", json={"phone_ids": ids, "available_uses": 3})
    assert response.status_code == 200
    assert response.json["updated"] == 2
    response = client.patch("/api/phones/available-uses", json={"phone_ids": [ids[0]], "delta": -1})
    assert response.status_code == 200
    rows = client.get("/api/phones").json["items"]
    assert [row["available_uses"] for row in rows] == [2, 3]
    assert rows[0]["expires_at"] == deadline
    response = client.patch("/api/phones/settings", json={"phone_ids": ids, "expiration_days": 0})
    assert response.status_code == 200
    assert all(row["expires_at"] == "" for row in client.get("/api/phones").json["items"])


def test_phone_settings_api_expired_filter_and_dynamic_source(client, phones, monkeypatch):
    phones[0]["expires_at"] = "2026-10-05T12:00:00+08:00"
    relay._write(relay._PHONES_PATH, phones)
    monkeypatch.setattr(relay._codex_config, "SMS_POOL_PLATFORM_ENABLED", True)
    monkeypatch.setattr(relay._codex_config, "SMS_PROVIDER", "l")
    rows = client.get("/api/phones?status=expired&paged=1&page=1&page_size=20").json
    assert rows["total"] == 1
    assert rows["items"][0]["id"] == phones[0]["id"]
    assert rows["items"][0]["expired"] is True
    special = next(row for row in client.get("/api/phones").json["items"] if row.get("special"))
    assert special["expires_in_days"] is None
    response = client.patch("/api/phones/settings", json={"phone_ids": [special["id"]], "expiration_days": 7})
    assert response.status_code == 400
    assert "特殊来源" in response.json["error"]


@pytest.mark.parametrize("payload", [
    [], [1], "invalid", {}, {"available_uses": 1}, {"phone_ids": "wrong", "available_uses": 3},
    {"phone_ids": ["missing"], "available_uses": 3},
])
def test_phone_settings_api_rejects_invalid_payloads(client, phones, payload):
    response = client.patch("/api/phones/settings", json=payload)
    assert response.status_code == 400
    assert response.json["ok"] is False
    assert relay._read(relay._PHONES_PATH) == phones


@pytest.mark.parametrize("settings", [
    {"available_uses": False}, {"available_uses": 1.7}, {"available_uses": 4},
    {"expiration_days": None}, {"expiration_days": -1}, {"expiration_days": 1.5},
    {"available_uses": 3, "expiration_days": 10**20}, {},
])
def test_phone_settings_api_rejects_invalid_fields_atomically(client, phones, settings):
    response = client.patch("/api/phones/settings", json={"phone_ids": [row["id"] for row in phones], **settings})
    assert response.status_code == 400
    assert relay._read(relay._PHONES_PATH) == phones
