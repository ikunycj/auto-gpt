"""Both entry points run connectivity-only probes and preserve the saved route."""
import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TURB_SQLITE_PATH", str(tmp_path / "test.sqlite3"))
    from apps.web.app import create_app
    return create_app().test_client()


def test_stale_browser_mode_only_runs_connectivity(client, monkeypatch):
    from core import proxy_test_service
    calls = []
    monkeypatch.setattr(proxy_test_service, "test_proxies", lambda values, **kw: calls.append((values, kw)) or [])
    response = client.post("/api/proxies/test", json={"proxies": [""], "mode": "registration"})
    assert response.status_code == 200
    assert calls == [([""], {})]


def test_saved_credentials_stay_on_server(client, monkeypatch):
    from apps.web.app import codex_relay_service
    from core import proxy_test_service
    route = "http://private-user:private-pass@proxy.example:8080"
    monkeypatch.setattr(codex_relay_service, "_get_account", lambda account_id: {"proxy": route})
    calls = []
    monkeypatch.setattr(proxy_test_service, "test_proxies", lambda values, **kw: calls.append((values, kw)) or [])
    response = client.post("/api/gpt-accounts/1/proxy/test", json={"mode": "registration"})
    assert response.status_code == 200
    assert calls == [([route], {})]
    assert "private-pass" not in response.get_data(as_text=True)


@pytest.mark.parametrize("payload", [
    {"proxies": []},
    {"proxies": [""] * 21},
])
def test_invalid_batches_return_400_without_connections(client, payload):
    assert client.post("/api/proxies/test", json=payload).status_code == 400


def test_connectivity_batch_no_longer_has_browser_limit(client, monkeypatch):
    from core import proxy_test_service
    calls = []
    monkeypatch.setattr(proxy_test_service, "test_proxies", lambda values: calls.append(values) or [])
    response = client.post("/api/proxies/test", json={"proxies": [""] * 4})
    assert response.status_code == 200 and calls == [[""] * 4]
