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


def test_webui_generic_api_registered_import_keeps_legacy_token_and_totp(web_client):
    response = web_client.post("/api/outlook/import", json={
        "source": "generic_api",
        "as_registered": True,
        "text": (
            "legacy@example.com----https://mail.example/code"
            "----eyJ.synthetic.access.token----JBSWY3DPEHPK3PXP"
        ),
    })

    assert response.status_code == 200
    from core.db import get_account_by_email

    account = get_account_by_email("legacy@example.com")
    assert account["access_token"] == "eyJ.synthetic.access.token"
    assert account["totp_secret"] == "JBSWY3DPEHPK3PXP"


def test_webui_semantic_password_is_not_mistaken_for_legacy_token(web_client):
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
