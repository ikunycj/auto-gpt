# -*- coding: utf-8 -*-
"""SQLite-backed persistence for the local registration console.

The application historically stored several independent JSON documents.  This
module keeps the dictionary/list API those callers use while making SQLite the
authoritative store.  Legacy files are imported lazily on first access and are
written back as compatibility exports so existing CLI tools and downloads do
not break during the migration.
"""
from __future__ import annotations

import hashlib
import fnmatch
import json
import logging
import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "turb_gpt.sqlite3"
_SCHEMA_LOCK = threading.RLock()


def database_path(source_path: str | Path | None = None) -> Path:
    """Return the SQLite file for a source path.

    Production files under the repository share one database.  Tests and
    callers that point a storage path at a temporary directory get a database
    beside that directory, preventing test data from touching the real store.
    ``TURB_SQLITE_PATH`` can explicitly select a database for deployments.
    """
    configured = str(os.environ.get("TURB_SQLITE_PATH") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if source_path is None:
        return DEFAULT_DB_PATH
    source = Path(source_path).expanduser().resolve()
    try:
        source.relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        # For isolated callers outside the repository, keep a directory and
        # its child files on the same sidecar database.  ``list_files(dir)``
        # and ``read_file(dir / name)`` must resolve identically.
        anchor = source if source.exists() and source.is_dir() else source.parent
        return anchor / ".turb-gpt.sqlite3"
    return DEFAULT_DB_PATH


def _source_key(source_path: str | Path) -> str:
    """Use a stable relative key for repository files and absolute keys for tests."""
    source = Path(source_path).expanduser().resolve()
    try:
        return source.relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return str(source)


def legacy_mirror_allowed(path: str | Path) -> bool:
    """Return whether a caller may materialize a legacy file mirror.

    Runtime paths inside this repository are SQLite-only. External paths are
    still supported for isolated tests and explicit export tooling.
    """
    target = Path(path).expanduser().resolve()
    try:
        target.relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        return True
    return False


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = Path(path or database_path())
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL allows readers to continue while a background registration task is
    # committing a change.  It is persisted per database and harmless on old
    # SQLite builds that already use the default journal mode.
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        logger.debug("无法启用 SQLite WAL", exc_info=True)
    conn.execute("PRAGMA synchronous = NORMAL")
    _ensure_schema(conn)
    _secure_database_files(db_path)
    return conn


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open a configured SQLite connection for advanced/read-only callers."""
    return _connect(path)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    with _SCHEMA_LOCK:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS storage_collections (
                collection TEXT PRIMARY KEY,
                source_key TEXT NOT NULL,
                value_type TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                source_sha256 TEXT
            );

            CREATE TABLE IF NOT EXISTS storage_items (
                collection TEXT NOT NULL,
                position INTEGER NOT NULL,
                record_key TEXT,
                email TEXT,
                status TEXT,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (collection, position),
                FOREIGN KEY (collection) REFERENCES storage_collections(collection)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_storage_items_key
                ON storage_items(collection, record_key);
            CREATE INDEX IF NOT EXISTS idx_storage_items_email
                ON storage_items(collection, email);
            CREATE INDEX IF NOT EXISTS idx_storage_items_status
                ON storage_items(collection, status);

            CREATE TABLE IF NOT EXISTS storage_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS storage_files (
                file_key TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                content BLOB NOT NULL,
                size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                mtime TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_storage_files_category
                ON storage_files(category, file_key);

            INSERT OR IGNORE INTO storage_meta(key, value)
                VALUES ('schema_version', '2');
            UPDATE storage_meta SET value='2' WHERE key='schema_version';

            CREATE VIEW IF NOT EXISTS v_registered_accounts AS
                SELECT position, record_key AS id, email, status, value_json
                FROM storage_items WHERE collection='registered_accounts';
            CREATE VIEW IF NOT EXISTS v_outlook_pool AS
                SELECT position, record_key AS id, email, status, value_json
                FROM storage_items WHERE collection='outlook_pool';
            CREATE VIEW IF NOT EXISTS v_registration_jobs AS
                SELECT position, record_key AS id, email, status, value_json
                FROM storage_items WHERE collection='registration_jobs';
            CREATE VIEW IF NOT EXISTS v_relay_accounts AS
                SELECT position, record_key AS id, email, status, value_json
                FROM storage_items WHERE collection='relay_accounts';
            CREATE VIEW IF NOT EXISTS v_relay_phones AS
                SELECT position, record_key AS id, status, value_json
                FROM storage_items WHERE collection='relay_phones';
            CREATE VIEW IF NOT EXISTS v_relay_jobs AS
                SELECT position, record_key AS id, email, status, value_json
                FROM storage_items WHERE collection='relay_jobs';
            """
        )


def _secure_database_files(db_path: Path) -> None:
    for path in (db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")):
        try:
            if path.exists():
                os.chmod(path, 0o600)
        except OSError:
            logger.debug("无法收紧 SQLite 文件权限：%s", path, exc_info=True)


def _json_value(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _record_key(item: Any, position: int) -> str:
    if isinstance(item, dict):
        for key in ("id", "email", "filename", "job_uuid", "name"):
            value = item.get(key)
            if value is not None and str(value) != "":
                return str(value)
    return str(position)


def _record_email(item: Any) -> str | None:
    if isinstance(item, dict):
        value = item.get("email")
        return str(value) if value is not None else None
    return None


def _record_status(item: Any) -> str | None:
    if isinstance(item, dict):
        value = item.get("status")
        return str(value) if value is not None else None
    return None


def _read_legacy_file(source: Path, default: Any) -> tuple[bool, Any, str | None]:
    if not source.exists() or not source.is_file():
        return False, default, None
    try:
        raw = source.read_text(encoding="utf-8")
        return True, json.loads(raw), hashlib.sha256(raw.encode("utf-8")).hexdigest()
    except Exception:
        logger.warning("无法读取旧 JSON 文件 %s，使用默认值", source, exc_info=True)
        return True, default, None


def _decode_collection(conn: sqlite3.Connection, collection: str, value_type: str) -> Any:
    rows = conn.execute(
        "SELECT value_json FROM storage_items WHERE collection=? ORDER BY position",
        (collection,),
    ).fetchall()
    values = []
    for row in rows:
        try:
            values.append(json.loads(row["value_json"]))
        except (TypeError, ValueError):
            values.append(None)
    if value_type == "list":
        return values
    return values[0] if values else None


def _write_collection(
    conn: sqlite3.Connection,
    collection: str,
    source_key: str,
    data: Any,
    source_sha256: str | None = None,
    *,
    transaction: bool = True,
) -> None:
    value_type = "list" if isinstance(data, list) else "scalar"
    now = _now()
    if transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT INTO storage_collections(collection, source_key, value_type, updated_at, source_sha256)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(collection) DO UPDATE SET
                source_key=excluded.source_key,
                value_type=excluded.value_type,
                updated_at=excluded.updated_at,
                source_sha256=excluded.source_sha256
            """,
            (collection, source_key, value_type, now, source_sha256),
        )
        conn.execute("DELETE FROM storage_items WHERE collection=?", (collection,))
        items: Iterable[Any] = data if isinstance(data, list) else [data]
        conn.executemany(
            """
            INSERT INTO storage_items(collection, position, record_key, email, status, value_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    collection,
                    position,
                    _record_key(item, position),
                    _record_email(item),
                    _record_status(item),
                    _json_value(item),
                    now,
                )
                for position, item in enumerate(items)
            ),
        )
        if transaction:
            conn.execute("COMMIT")
    except Exception:
        if transaction:
            conn.execute("ROLLBACK")
        raise


def _mirror_json(source: Path, data: Any, mode: int | None = None) -> None:
    """Write a compatibility JSON export without making it the source of truth."""
    source.parent.mkdir(parents=True, exist_ok=True)
    tmp = source.with_suffix(source.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        if mode is not None:
            os.chmod(tmp, mode)
        tmp.replace(source)
        if mode is not None:
            os.chmod(source, mode)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        logger.warning("SQLite 已写入，但兼容 JSON 导出失败：%s", source, exc_info=True)


def read_json(
    source_path: str | Path,
    default: Any = None,
    *,
    collection: str | None = None,
) -> Any:
    """Read a JSON document from SQLite, importing the legacy file once."""
    source = Path(source_path).expanduser().resolve()
    collection_name = collection or _source_key(source)
    db_path = database_path(source)
    source_key = _source_key(source)
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT value_type FROM storage_collections WHERE collection=?",
            (collection_name,),
        ).fetchone()
        if row is not None:
            return _decode_collection(conn, collection_name, row["value_type"])

        exists, data, digest = _read_legacy_file(source, default)
        if not exists and default is None:
            return None
        if isinstance(data, list) or isinstance(data, dict) or data is None:
            _write_collection(conn, collection_name, source_key, data, digest)
            return data
        # JSON scalar values are valid too; preserve them as a one-item record.
        _write_collection(conn, collection_name, source_key, data, digest)
        return data


def write_json(
    source_path: str | Path,
    data: Any,
    *,
    collection: str | None = None,
    mirror: bool = True,
    mode: int | None = None,
) -> None:
    """Write a JSON document to SQLite and optionally materialize its export."""
    source = Path(source_path).expanduser().resolve()
    collection_name = collection or _source_key(source)
    with closing(_connect(database_path(source))) as conn:
        _write_collection(conn, collection_name, _source_key(source), data)
    if mirror:
        _mirror_json(source, data, mode=mode)


def update_json(
    source_path: str | Path,
    updater: Callable[[Any], Any],
    default: Any = None,
    *,
    collection: str | None = None,
    mode: int | None = 0o600,
    mirror: bool = True,
) -> Any:
    """Atomically transform a JSON collection and optionally refresh its mirror.

    ``updater`` runs while SQLite holds a write transaction.  This is useful
    for read-modify-write workflows such as deduplicating an exported account
    without allowing concurrent workers to lose each other's changes.
    """
    source = Path(source_path).expanduser().resolve()
    name = collection or _source_key(source)
    with closing(_connect(database_path(source))) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT value_type FROM storage_collections WHERE collection=?", (name,)
            ).fetchone()
            if row is not None:
                current = _decode_collection(conn, name, row["value_type"])
            else:
                exists, legacy, digest = _read_legacy_file(source, default)
                current = legacy if exists else default
                if current is None and default is None:
                    current = None
                # Seed the collection before applying the transformation so a
                # callback can safely treat an absent file as its default type.
                _write_collection(
                    conn,
                    name,
                    _source_key(source),
                    current,
                    digest if exists else None,
                    transaction=False,
                )
            updated = updater(current)
            _write_collection(
                conn,
                name,
                _source_key(source),
                updated,
                transaction=False,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    if mirror:
        _mirror_json(source, updated, mode=mode)
    return updated


def delete_json(
    source_path: str | Path,
    *,
    collection: str | None = None,
    delete_mirror: bool = False,
) -> bool:
    """Delete a collection; mirrors are retained by default for rollback."""
    source = Path(source_path).expanduser().resolve()
    collection_name = collection or _source_key(source)
    deleted = False
    with closing(_connect(database_path(source))) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            deleted = conn.execute(
                "DELETE FROM storage_collections WHERE collection=?", (collection_name,)
            ).rowcount > 0
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    if delete_mirror and source.exists():
        source.unlink()
    return deleted


def migrate_json_file(
    source_path: str | Path,
    *,
    default: Any = None,
    collection: str | None = None,
) -> bool:
    """Import one legacy file if its collection has not been imported yet."""
    source = Path(source_path).expanduser().resolve()
    name = collection or _source_key(source)
    db_path = database_path(source)
    with closing(_connect(db_path)) as conn:
        exists = conn.execute(
            "SELECT 1 FROM storage_collections WHERE collection=?", (name,)
        ).fetchone()
    if exists:
        return False
    read_json(source, default, collection=name)
    return True


def append_json_item(
    source_path: str | Path,
    item: Any,
    *,
    collection: str | None = None,
    category: str | None = None,
    mode: int | None = 0o600,
    mirror: bool = True,
) -> list[Any]:
    """Append one item to a JSON list in one SQLite transaction.

    This is used for batch archives where a read-modify-write sequence must
    remain safe even when more than one worker/process records a result at the
    same time. The updated list is materialized as a legacy JSON compatibility
    file only when ``mirror`` is enabled.
    """
    source = Path(source_path).expanduser().resolve()
    name = collection or _source_key(source)
    with closing(_connect(database_path(source))) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT value_type FROM storage_collections WHERE collection=?", (name,)
            ).fetchone()
            if row is not None:
                current = _decode_collection(conn, name, row["value_type"])
                values = current if isinstance(current, list) else []
            else:
                exists, legacy, digest = _read_legacy_file(source, [])
                values = legacy if isinstance(legacy, list) else []
                _write_collection(
                    conn,
                    name,
                    _source_key(source),
                    values,
                    digest if exists else None,
                    transaction=False,
                )
            values = list(values)
            values.append(item)
            _write_collection(
                conn,
                name,
                _source_key(source),
                values,
                transaction=False,
            )
            if category:
                # Some callers expose the JSON archive through both the
                # collection API and the generated-file API. Keep their
                # metadata/content synchronized in this same transaction.
                _write_file_row(
                    conn,
                    source,
                    json.dumps(values, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
                    category,
                    transaction=False,
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    if mirror:
        _mirror_json(source, values, mode=mode)
    return values


def _write_file_row(
    conn: sqlite3.Connection,
    path: Path,
    content: bytes,
    category: str,
    *,
    transaction: bool = True,
) -> None:
    now = _now()
    if transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT INTO storage_files(file_key, category, content, size, sha256, mtime, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(file_key) DO UPDATE SET
                category=excluded.category,
                content=excluded.content,
                size=excluded.size,
                sha256=excluded.sha256,
                mtime=excluded.mtime,
                updated_at=excluded.updated_at
            """,
            (
                _source_key(path),
                str(category or "files"),
                sqlite3.Binary(content),
                len(content),
                hashlib.sha256(content).hexdigest(),
                now,
                now,
            ),
        )
        if transaction:
            conn.execute("COMMIT")
    except Exception:
        if transaction:
            conn.execute("ROLLBACK")
        raise


def _mirror_bytes(path: Path, content: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(content)
    try:
        if mode is not None:
            os.chmod(tmp, mode)
        tmp.replace(path)
        if mode is not None:
            os.chmod(path, mode)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        logger.warning("SQLite 文件内容已写入，但兼容文件导出失败：%s", path, exc_info=True)


def write_file(
    path: str | Path,
    content: bytes | str,
    *,
    category: str = "files",
    encoding: str = "utf-8",
    mirror: bool = True,
    mode: int | None = 0o600,
) -> None:
    """Store a generated file in SQLite and optionally materialize an export."""
    target = Path(path).expanduser().resolve()
    raw = content.encode(encoding) if isinstance(content, str) else bytes(content)
    with closing(_connect(database_path(target))) as conn:
        _write_file_row(conn, target, raw, category)
    if mirror:
        _mirror_bytes(target, raw, mode=mode)


def append_file(
    path: str | Path,
    content: bytes | str,
    *,
    category: str = "files",
    encoding: str = "utf-8",
    mode: int | None = 0o600,
    mirror: bool = True,
) -> None:
    """Atomically append to a SQLite-backed file."""
    target = Path(path).expanduser().resolve()
    raw = content.encode(encoding) if isinstance(content, str) else bytes(content)
    # Do the read and write under the same RESERVED lock.  The previous
    # implementation called read_file() and write_file() separately, which
    # allowed concurrent loggers to overwrite one another between transactions.
    with closing(_connect(database_path(target))) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT content FROM storage_files WHERE file_key=?",
                (_source_key(target),),
            ).fetchone()
            if row is not None:
                previous = bytes(row["content"])
            elif target.exists() and target.is_file():
                # Import a legacy mirror while holding the SQLite write lock so
                # two first writers cannot both base their append on stale data.
                previous = target.read_bytes()
            else:
                previous = b""
            combined = previous + raw
            now = _now()
            conn.execute(
                """
                INSERT INTO storage_files(file_key, category, content, size, sha256, mtime, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_key) DO UPDATE SET
                    category=excluded.category,
                    content=excluded.content,
                    size=excluded.size,
                    sha256=excluded.sha256,
                    mtime=excluded.mtime,
                    updated_at=excluded.updated_at
                """,
                (
                    _source_key(target),
                    str(category or "files"),
                    sqlite3.Binary(combined),
                    len(combined),
                    hashlib.sha256(combined).hexdigest(),
                    now,
                    now,
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    # Materialize only after the authoritative transaction commits.  A failed
    # compatibility export must not make the SQLite append appear to fail.
    if mirror:
        _mirror_bytes(target, combined, mode=mode)


class SQLiteFileHandler(logging.Handler):
    """Logging handler whose authoritative content is kept in SQLite."""

    def __init__(
        self,
        path: str | Path,
        *,
        category: str = "logs",
        encoding: str = "utf-8",
        mode: int | None = 0o600,
        mirror: bool = True,
    ) -> None:
        super().__init__()
        self.path = Path(path)
        self.category = category
        self.encoding = encoding
        self.mode = mode
        self.mirror = mirror

    def emit(self, record: logging.LogRecord) -> None:
        try:
            append_file(
                self.path,
                self.format(record) + "\n",
                category=self.category,
                encoding=self.encoding,
                mode=self.mode,
                mirror=self.mirror,
            )
        except Exception:
            self.handleError(record)


def read_file(
    path: str | Path,
    *,
    category: str = "files",
    import_legacy: bool = True,
) -> bytes:
    """Read generated file content from SQLite, importing an old file once."""
    target = Path(path).expanduser().resolve()
    with closing(_connect(database_path(target))) as conn:
        row = conn.execute(
            "SELECT content FROM storage_files WHERE file_key=? AND category=?",
            (_source_key(target), str(category or "files")),
        ).fetchone()
        if row is not None:
            return bytes(row["content"])
    if import_legacy and target.exists() and target.is_file():
        content = target.read_bytes()
        write_file(target, content, category=category, mirror=False)
        return content
    raise FileNotFoundError(str(target))


def read_text_file(
    path: str | Path,
    *,
    category: str = "files",
    encoding: str = "utf-8",
    import_legacy: bool = True,
) -> str:
    return read_file(path, category=category, import_legacy=import_legacy).decode(encoding)


def file_exists(path: str | Path, *, category: str = "files") -> bool:
    target = Path(path).expanduser().resolve()
    with closing(_connect(database_path(target))) as conn:
        row = conn.execute(
            "SELECT 1 FROM storage_files WHERE file_key=? AND category=?",
            (_source_key(target), str(category or "files")),
        ).fetchone()
    return row is not None or (target.exists() and target.is_file())


def list_files(
    directory: str | Path,
    pattern: str = "*",
    *,
    category: str = "files",
) -> list[dict[str, Any]]:
    """List SQLite-backed files, importing matching legacy directory entries."""
    root = Path(directory).expanduser().resolve()
    if root.exists():
        for legacy_path in root.glob(pattern):
            if legacy_path.is_file():
                try:
                    read_file(legacy_path, category=category)
                except OSError:
                    logger.warning("无法导入旧文件：%s", legacy_path, exc_info=True)

    prefix = _source_key(root).rstrip("/") + "/"
    # Resolve the database as if it were a child file.  This keeps an absent
    # directory (which may be created by the first write) on the same sidecar
    # database as its eventual files.
    directory_db = database_path(root / ".sqlite-storage-probe")
    with closing(_connect(directory_db)) as conn:
        rows = conn.execute(
            """
            SELECT file_key, size, sha256, mtime, updated_at
            FROM storage_files
            WHERE category=? AND file_key LIKE ?
            ORDER BY updated_at DESC, file_key
            """,
            (str(category or "files"), prefix + "%"),
        ).fetchall()
    result = []
    for row in rows:
        name = Path(row["file_key"]).name
        if not fnmatch.fnmatch(name, pattern):
            continue
        result.append({
            "filename": name,
            "path": str(root / name),
            "size": int(row["size"]),
            "sha256": row["sha256"],
            "mtime": row["mtime"],
            "updated_at": row["updated_at"],
        })
    return result


def delete_file(
    path: str | Path,
    *,
    category: str = "files",
    delete_mirror: bool = True,
) -> bool:
    target = Path(path).expanduser().resolve()
    with closing(_connect(database_path(target))) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            deleted = conn.execute(
                "DELETE FROM storage_files WHERE file_key=? AND category=?",
                (_source_key(target), str(category or "files")),
            ).rowcount > 0
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    mirror_existed = target.exists()
    if delete_mirror and mirror_existed:
        target.unlink()
    return deleted or mirror_existed


def storage_info(path: Path | None = None) -> dict[str, Any]:
    """Return safe metadata useful for diagnostics and WebUI health checks."""
    db_path = Path(path or database_path())
    if not db_path.exists():
        return {"path": str(db_path), "exists": False, "collections": 0, "items": 0, "files": 0}
    with closing(_connect(db_path)) as conn:
        collections = int(conn.execute("SELECT COUNT(*) FROM storage_collections").fetchone()[0])
        items = int(conn.execute("SELECT COUNT(*) FROM storage_items").fetchone()[0])
        files = int(conn.execute("SELECT COUNT(*) FROM storage_files").fetchone()[0])
    return {"path": str(db_path), "exists": True, "collections": collections, "items": items, "files": files}


__all__ = [
    "DEFAULT_DB_PATH",
    "SQLiteFileHandler",
    "append_file",
    "append_json_item",
    "connect",
    "database_path",
    "delete_json",
    "delete_file",
    "file_exists",
    "list_files",
    "legacy_mirror_allowed",
    "migrate_json_file",
    "read_json",
    "read_file",
    "read_text_file",
    "storage_info",
    "update_json",
    "write_json",
    "write_file",
]
