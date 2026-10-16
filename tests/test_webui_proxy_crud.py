from pathlib import Path

import pytest

from apps.web import app as web_app
from core import codex_relay_service, db
from config import proxy as proxy_config


@pytest.fixture
def proxy_client(tmp_path, monkeypatch):
    monkeypatch.setenv("TURB_SQLITE_PATH", str(tmp_path / "webui.sqlite3"))
    monkeypatch.setattr(db, "_ACCOUNTS_JSON", tmp_path / "accounts.json")
    monkeypatch.setattr(codex_relay_service, "_ACCOUNTS_PATH", tmp_path / "accounts.json")
    monkeypatch.setattr(codex_relay_service, "_PHONES_PATH", tmp_path / "phones.json")
    monkeypatch.setattr(codex_relay_service, "_JOBS_PATH", tmp_path / "relay-jobs.json")
    monkeypatch.setattr(codex_relay_service, "_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(proxy_config, "PROXY_POOL", [
        "http://one.example:8080",
        "http://two.example:8080",
    ])
    monkeypatch.setattr(proxy_config, "PROXY_POOL_DISABLED", ["http://off.example:8080"])
    monkeypatch.setattr(web_app.config_editor, "update_config", lambda updates: _apply_proxy_updates(proxy_config, updates))
    monkeypatch.setattr("config.reload_all", lambda: None)
    return web_app.create_app().test_client()


def _apply_proxy_updates(proxy_module, updates):
    proxy_module.PROXY_POOL = list(updates["PROXY_POOL"])
    proxy_module.PROXY_POOL_DISABLED = list(updates["PROXY_POOL_DISABLED"])
    return {"updated": list(updates), "ignored": [], "env_updated": list(updates)}


def test_proxy_crud_pagination_and_enabled_state(proxy_client):
    response = proxy_client.get("/api/proxies?page=1&page_size=2")
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["total"] == 3
    assert len(payload["items"]) == 2
    assert all(item["enabled"] for item in payload["items"])

    disabled = proxy_client.get("/api/proxies?status=disabled").get_json()
    assert [item["proxy"] for item in disabled["items"]] == ["http://off.example:8080"]

    created = proxy_client.post("/api/proxies", json={"proxy": "http://new.example:8080"})
    assert created.status_code == 201
    created_record = created.get_json()["proxy"]
    assert created_record["enabled"] is True

    toggled = proxy_client.patch(f"/api/proxies/{created_record['id']}", json={"enabled": False})
    assert toggled.status_code == 200
    assert toggled.get_json()["proxy"]["enabled"] is False

    edited = proxy_client.patch(f"/api/proxies/{created_record['id']}", json={"proxy": "http://renamed.example:8080"})
    assert edited.status_code == 200
    assert edited.get_json()["proxy"]["enabled"] is False

    deleted = proxy_client.delete(f"/api/proxies/{edited.get_json()['proxy']['id']}")
    assert deleted.status_code == 200
    assert proxy_client.get("/api/proxies").get_json()["total"] == 3


def test_proxy_bulk_delete_is_immediate(proxy_client):
    rows = proxy_client.get("/api/proxies?page_size=20").get_json()["items"]
    response = proxy_client.delete("/api/proxies", json={"ids": [rows[0]["id"], rows[1]["id"]]})
    assert response.status_code == 200
    assert response.get_json()["deleted"] == 2
    remaining = proxy_client.get("/api/proxies?page_size=20").get_json()
    assert remaining["total"] == 1
