from __future__ import annotations

import logging
from pathlib import Path
import config

from core import codex_retry_service


def test_account_proxy_reads_canonical_account_field_only():
    assert codex_retry_service._account_proxy(
        {"proxy": "http://bound.example:3128", "proxy_used": "http://old.example:8080"}
    ) == "http://bound.example:3128"
    assert codex_retry_service._account_proxy({"proxy_used": "http://old.example:8080"}) == ""


def test_run_worker_passes_bound_proxy_to_codex_oauth(monkeypatch, tmp_path):
    email = "account@example.com"
    proxy = "http://bound.example:3128"
    calls: list[dict] = []
    status_updates: list[tuple[str, str, str | None]] = []

    class FakeHandler(logging.Handler):
        def emit(self, record):
            return None

        def close(self):
            super().close()

    account = {
        "email": email,
        "chatgpt_password": "account-password",
        "totp_secret": "",
        "proxy": proxy,
    }

    monkeypatch.setattr(codex_retry_service.db, "get_account_by_email", lambda value: account)
    monkeypatch.setattr(
        codex_retry_service,
        "registered_account_login_material",
        lambda value: {"chatgpt_password": "account-password", "totp_secret": ""},
    )
    monkeypatch.setattr(codex_retry_service.sqlite_store, "write_file", lambda *args, **kwargs: None)
    monkeypatch.setattr(codex_retry_service.sqlite_store, "SQLiteFileHandler", lambda *args, **kwargs: FakeHandler())
    monkeypatch.setattr(config, "reload_all", lambda: None)
    monkeypatch.setattr(
        codex_retry_service.db,
        "update_account_codex_status",
        lambda target_email, status, error=None: status_updates.append((target_email, status, error)) or True,
    )

    import core.codex_oauth as codex_oauth

    monkeypatch.setattr(
        codex_oauth,
        "run_codex_oauth",
        lambda target_email, **kwargs: calls.append({"email": target_email, **kwargs})
        or {"ok": True, "status": "success", "file_path": str(tmp_path / "credential.json")},
    )

    result = codex_retry_service.run_worker(
        email,
        clear_log=False,
        target_log_path=Path(tmp_path) / "retry.log",
    )

    assert result["ok"] is True
    assert calls and calls[0]["email"] == email
    assert calls[0]["proxy"] == proxy
    assert calls[0]["login_password"] == "account-password"
    assert status_updates[-1] == (email, "success", None)
    assert not codex_retry_service.is_retrying(email)
