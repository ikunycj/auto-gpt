"""Focused tests for the SQLite-backed persistence adapter.

Every test points the adapter at a temporary database through
``TURB_SQLITE_PATH``.  The tests intentionally use synthetic values only:
the repository's real JSON and credential files are never touched.
"""

from __future__ import annotations

import json
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest

from core import sqlite_store


@pytest.fixture
def sqlite_workspace(tmp_path, monkeypatch):
    """Return an isolated workspace and database path for one test."""

    database = tmp_path / "storage" / "test.sqlite3"
    monkeypatch.setenv("TURB_SQLITE_PATH", str(database))
    return tmp_path, database


def test_json_round_trip_uses_sqlite_and_materializes_compatibility_copy(
    sqlite_workspace,
):
    root, database = sqlite_workspace
    source = root / "state.json"
    value = {
        "version": 1,
        "accounts": [{"id": "synthetic-1", "email": "one@example.test"}],
    }

    sqlite_store.write_json(source, value, collection="test-state")

    assert sqlite_store.read_json(source, collection="test-state") == value
    assert json.loads(source.read_text(encoding="utf-8")) == value
    assert database.exists()

    with sqlite_store.connect(database) as connection:
        row = connection.execute(
            "SELECT value_type, source_key FROM storage_collections "
            "WHERE collection = ?",
            ("test-state",),
        ).fetchone()
    assert row["value_type"] == "scalar"
    assert row["source_key"] == str(source.resolve())


def test_legacy_json_is_imported_once_and_later_mirror_edits_are_ignored(
    sqlite_workspace,
):
    root, _database = sqlite_workspace
    source = root / "legacy.json"
    original = {"items": ["first", "second"], "enabled": True}
    source.write_text(json.dumps(original), encoding="utf-8")

    assert sqlite_store.migrate_json_file(
        source, default={}, collection="legacy-state"
    ) is True
    assert sqlite_store.migrate_json_file(
        source, default={}, collection="legacy-state"
    ) is False
    assert sqlite_store.read_json(source, collection="legacy-state") == original

    # The old file remains a compatibility mirror.  Once imported, changing it
    # must not change the authoritative SQLite value.
    source.write_text(
        json.dumps({"items": ["tampered"], "enabled": False}),
        encoding="utf-8",
    )
    assert sqlite_store.read_json(source, default={}, collection="legacy-state") == original


def test_file_write_read_append_list_and_delete(sqlite_workspace):
    root, database = sqlite_workspace
    output_dir = root / "exports"
    first = output_dir / "first.txt"
    second = output_dir / "second.txt"

    sqlite_store.write_file(first, "alpha\n", category="test-exports")
    sqlite_store.append_file(first, "beta\n", category="test-exports")
    sqlite_store.write_file(second, b"other\n", category="test-exports")

    assert sqlite_store.read_file(first, category="test-exports") == b"alpha\nbeta\n"
    assert sqlite_store.read_text_file(first, category="test-exports") == "alpha\nbeta\n"
    assert sqlite_store.file_exists(first, category="test-exports")

    listed = sqlite_store.list_files(
        output_dir, pattern="*.txt", category="test-exports"
    )
    assert {entry["filename"] for entry in listed} == {"first.txt", "second.txt"}
    first_entry = next(entry for entry in listed if entry["filename"] == "first.txt")
    assert first_entry["size"] == len(b"alpha\nbeta\n")
    assert first_entry["sha256"]

    assert sqlite_store.delete_file(first, category="test-exports") is True
    assert not sqlite_store.file_exists(first, category="test-exports")
    assert not first.exists()
    assert sqlite_store.read_file(second, category="test-exports") == b"other\n"

    info = sqlite_store.storage_info(database)
    assert info["path"] == str(database)
    assert info["exists"] is True
    assert info["files"] == 1


def test_append_file_is_atomic_for_concurrent_writers(sqlite_workspace):
    root, _database = sqlite_workspace
    target = root / "logs" / "concurrent.log"
    lines = [f"worker-{index:03d}\n" for index in range(48)]

    # Each call opens its own connection.  If append_file used a separate
    # read and write transaction, concurrent calls would lose some lines.
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(
            executor.map(
                lambda line: sqlite_store.append_file(
                    target, line, category="test-logs"
                ),
                lines,
            )
        )

    stored_lines = sqlite_store.read_file(target, category="test-logs").decode(
        "utf-8"
    ).splitlines()
    assert len(stored_lines) == len(lines)
    assert set(stored_lines) == {line.rstrip("\n") for line in lines}


def test_storage_info_and_database_permissions(sqlite_workspace):
    root, database = sqlite_workspace
    assert sqlite_store.storage_info(database)["exists"] is False

    sqlite_store.write_json(root / "metadata.json", {"ok": True}, collection="meta")
    info = sqlite_store.storage_info(database)

    assert info["exists"] is True
    assert info["collections"] == 1
    assert info["items"] == 1
    assert info["files"] == 0
    assert stat.S_IMODE(database.stat().st_mode) == 0o600


def test_append_json_item_keeps_file_view_in_sync(sqlite_workspace):
    root, _database = sqlite_workspace
    source = root / "archives" / "accounts.json"
    collection = "archive-accounts"
    sqlite_store.write_file(
        source,
        "[]\n",
        category="batch-archives",
    )

    values = sqlite_store.append_json_item(
        source,
        {"email": "synthetic@example.test"},
        collection=collection,
        category="batch-archives",
    )

    assert values == [{"email": "synthetic@example.test"}]
    assert sqlite_store.read_json(source, collection=collection) == values
    assert json.loads(
        sqlite_store.read_text_file(source, category="batch-archives")
    ) == values


def test_update_json_serializes_concurrent_read_modify_write(sqlite_workspace):
    root, _database = sqlite_workspace
    source = root / "state.json"
    sqlite_store.write_json(source, {"count": 0}, collection="counter")

    def increment(value):
        updated = dict(value or {})
        updated["count"] = int(updated.get("count") or 0) + 1
        return updated

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(
            executor.map(
                lambda _index: sqlite_store.update_json(
                    source, increment, {}, collection="counter"
                ),
                range(24),
            )
        )

    assert sqlite_store.read_json(source, collection="counter")["count"] == 24


def test_sqlite_only_writes_do_not_materialize_legacy_mirrors(sqlite_workspace):
    root, _database = sqlite_workspace
    source = root / "runtime" / "state.json"
    log_path = root / "runtime" / "task.log"

    sqlite_store.write_json(source, {"ok": True}, collection="sqlite-only", mirror=False)
    sqlite_store.append_json_item(
        source,
        {"id": "one"},
        collection="sqlite-only-list",
        mirror=False,
    )
    sqlite_store.append_file(log_path, "stored\n", category="sqlite-only-logs", mirror=False)

    assert not source.exists()
    assert not log_path.exists()
    assert sqlite_store.read_json(source, collection="sqlite-only") == {"ok": True}
    assert sqlite_store.read_file(log_path, category="sqlite-only-logs") == b"stored\n"
    assert sqlite_store.legacy_mirror_allowed(source) is True
