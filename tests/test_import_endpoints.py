from pathlib import Path

import pytest

from apps.web.app import create_app
from core import generic_api_mail_client


@pytest.fixture
def web_client(tmp_path, monkeypatch):
    monkeypatch.setenv("TURB_SQLITE_PATH", str(tmp_path / "storage.sqlite3"))
    app = create_app()
    client = app.test_client()
    return client


def test_webui_generic_api_import_uses_semantic_fields_and_configured_separators(web_client, monkeypatch):
    monkeypatch.setattr("config.email.EMAIL_IMPORT_SEPARATORS", ":::")

    response = web_client.post("/api/outlook/import", json={
        "source": "generic_api",
        "text": "chatgpt-password:::https://mail.example/code:::mail@example.com:::JBSWY3DPEHPK3PXP",
    })

    assert response.status_code == 200
    assert response.get_json()["parsed"] == 1
    rows = web_client.get("/api/outlook?source=generic_api").get_json()
    assert rows[0]["email"] == "mail@example.com"
    assert rows[0]["code_url"] == "https://mail.example/code"


def test_webui_all_email_pool_includes_unregistered_domain_mailboxes(web_client, monkeypatch):
    """All three mailbox sources share the one unregistered email workspace."""
    monkeypatch.setattr(
        "apps.web.app.db.list_outlook_pool",
        lambda status=None, limit=500: [{"email": "outlook@example.com", "created_at": "2026-01-02"}],
    )
    monkeypatch.setattr(
        "apps.web.app.db.list_generic_api_email_pool",
        lambda status=None, limit=500: [{"email": "api@example.com", "created_at": "2026-01-03"}],
    )
    monkeypatch.setattr(
        "apps.web.app.db.list_domain_email_pool",
        lambda status=None, limit=500: [{"email": "generated@example.com", "created_at": "2026-01-04"}],
    )

    response = web_client.get("/api/outlook?source=all")

    assert response.status_code == 200
    assert {row["email"] for row in response.get_json()} == {
        "outlook@example.com",
        "api@example.com",
        "generated@example.com",
    }
    explicit_domain = web_client.get("/api/outlook?source=cloudflare_domain")
    assert explicit_domain.status_code == 200
    assert [row["email"] for row in explicit_domain.get_json()] == ["generated@example.com"]


def test_webui_registered_import_preserves_semantic_password_and_totp(web_client):
    response = web_client.post("/api/outlook/import", json={
        "source": "generic_api",
        "as_registered": True,
        "text": (
            "semantic@example.com----https://mail.example/code"
            "----chatgpt-password----JBSWY3DPEHPK3PXP"
        ),
    })

    assert response.status_code == 200
    from core.db import get_account_by_email

    account = get_account_by_email("semantic@example.com")
    assert account["access_token"] == ""
    assert account["chatgpt_password"] == "chatgpt-password"
    assert account["totp_secret"] == "JBSWY3DPEHPK3PXP"


def test_explicit_registered_account_import_keeps_access_token(web_client):
    from core import db

    inserted, skipped = db.import_registered_email_accounts([{
        "email": "token@example.com",
        "code_url": "https://mail.example/code",
        "access_token": "synthetic-access-token",
        "totp_secret": "JBSWY3DPEHPK3PXP",
    }], source="generic_api")

    assert (inserted, skipped) == (1, 0)
    account = db.get_account_by_email("token@example.com")
    assert account["access_token"] == "synthetic-access-token"
    assert account["totp_secret"] == "JBSWY3DPEHPK3PXP"


def test_generic_api_file_import_uses_shared_parser(tmp_path, monkeypatch):
    monkeypatch.setenv("TURB_SQLITE_PATH", str(tmp_path / "storage.sqlite3"))
    source = Path(tmp_path) / "mail-material.txt"
    source.write_text(
        "ignored line\n"
        "password|https://mail.example/code|mail@example.com|JBSWY3DPEHPK3PXP\n"
        "second@example.com---https://mail.example/second\n",
        encoding="utf-8",
    )
    inserted, skipped = generic_api_mail_client.import_from_file(source)

    assert (inserted, skipped) == (2, 0)
    from core.db import list_generic_api_email_pool

    rows = list_generic_api_email_pool()
    assert {row["email"] for row in rows} == {"mail@example.com", "second@example.com"}
