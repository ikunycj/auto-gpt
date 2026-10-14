"""Phone records live in the single phones collection."""
import json
import sqlite3

import pytest

from core import codex_relay_service as relay, phone_store, sqlite_store


def _collection(path):
    with sqlite3.connect(sqlite_store.database_path(path)) as conn:
        return [json.loads(row[0]) for row in conn.execute(
            "SELECT value_json FROM storage_items WHERE collection='phones' ORDER BY position"
        )]


def test_phone_pool_round_trip_keeps_ids_and_reservations(tmp_path, monkeypatch):
    path = tmp_path / "phones.json"
    monkeypatch.setattr(relay, "_PHONES_PATH", path)
    initial = [{
        "id": "phone-1", "phone": "+14155550123", "sms_code_url": "https://sms.example/code",
        "reserved_job_ids": ["job-active"],
        "available_uses": 2,
    }]
    phone_store.write(path, initial)
    assert relay._read(path) == initial
    assert _collection(path) == initial

    updated = relay._read(path)
    updated[0]["available_uses"] = 1
    relay._write(path, updated)
    assert phone_store.read(path) == updated
    assert phone_store.read(path)[0]["reserved_job_ids"] == ["job-active"]

    relay._write(path, [])
    assert phone_store.read(path) == []
    assert _collection(path) == []


def test_fresh_phone_store_starts_empty_and_does_not_create_json(tmp_path):
    path = tmp_path / "phones.json"
    assert phone_store.read(path) == []
    assert _collection(path) == []
    assert not path.exists()


def test_duplicate_phone_ids_leave_previous_collection_unchanged(tmp_path):
    path = tmp_path / "phones.json"
    previous = [{"id": "phone-1", "phone": "+14155550000"}]
    phone_store.write(path, previous)
    invalid = [{"id": "same", "phone": "+14155550001"}, {"id": "same", "phone": "+14155550002"}]
    with pytest.raises(ValueError, match="ID"):
        phone_store.write(path, invalid)
    assert phone_store.read(path) == previous


def test_outer_transaction_rollback_restores_phone_and_job(tmp_path):
    path = tmp_path / "phones.json"
    job_path = tmp_path / "jobs.json"
    previous = [{"id": "phone-1", "phone": "+14155550000"}]
    phone_store.write(path, previous)
    with pytest.raises(RuntimeError, match="rollback"):
        with sqlite_store.write_transaction(path):
            phone_store.write(path, [{"id": "phone-2", "phone": "+14155550001"}])
            sqlite_store.write_json(job_path, [{"id": "job-1", "phone_id": "phone-2"}], collection="relay_jobs")
            raise RuntimeError("rollback")
    assert phone_store.read(path) == previous
    with sqlite3.connect(sqlite_store.database_path(path)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM storage_items WHERE collection='relay_jobs'"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("unsupported_field, value", [
    ("assigned_account_id", "gpt-1"),
    ("assigned_account_ids", ["gpt-1"]),
    ("remaining_uses", 1),
    ("max_uses", 1),
    ("use_count", 1),
])
def test_read_rejects_old_phone_fields_without_changing_storage(tmp_path, unsupported_field, value):
    path = tmp_path / "phones.json"
    row = {"id": "phone-1", "phone": "+14155550123", unsupported_field: value}
    sqlite_store.write_json(path, [row], collection="phones")
    before = _collection(path)
    with pytest.raises(ValueError, match="旧字段"):
        phone_store.read(path)
    assert _collection(path) == before
