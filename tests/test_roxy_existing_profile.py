"""Fixed Roxy profiles must be reused without restarting or deleting a user's window."""
import logging
from types import SimpleNamespace

import pytest

from config import roxybrowser as cfg, proxy as proxy_cfg
from core import roxy_codex_oauth
from core import roxybrowser_client


@pytest.fixture(autouse=True)
def isolated_roxy_config(monkeypatch):
    monkeypatch.setattr(cfg, "ROXY_WORKSPACE_ID", "workspace")
    monkeypatch.setattr(cfg, "ROXY_PROFILE_ID", "")
    monkeypatch.setattr(cfg, "ROXY_PROFILE_CREATE_PAYLOAD", {})
    monkeypatch.setattr(cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", True)
    monkeypatch.setattr(cfg, "ROXY_KEEP_BROWSER_OPEN", False)
    monkeypatch.setattr(cfg, "ROXY_PROXY_UPDATE_PATH", "/browser/mdf")
    monkeypatch.setattr(cfg, "ROXY_PROXY_UPDATE_METHOD", "POST")
    monkeypatch.setattr(proxy_cfg, "PROXY_POOL", [])


def profile_payload(profile_id, proxy=""):
    return {"data": {"rows": [{"dirId": profile_id,
                              "proxyInfo": roxybrowser_client._proxy_url_to_roxy_info(proxy)}]}}


def test_existing_fixed_roxy_window_uses_readonly_connection_info(monkeypatch):
    monkeypatch.setattr(cfg, "ROXY_PROFILE_ID", "existing-profile")
    monkeypatch.setattr(cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", False)
    client = roxybrowser_client.RoxyBrowserClient()
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/browser/list_v3":
            return profile_payload("existing-profile")
        return {"data": [{"dirId": "other-profile", "http": "127.0.0.1:9223"},
                         {"dirId": "existing-profile", "http": "127.0.0.1:9222",
                          "driver": "/path/to/driver", "proxyPassword": "secret"}]}

    monkeypatch.setattr(client, "request", fake_request)
    opened = client.open_profile()

    assert [(m, p) for m, p, _ in calls] == [
        ("GET", "/browser/connection_info"), ("GET", "/browser/list_v3")]
    assert calls[0][2] == {"params": {"dirIds": "existing-profile"}}
    assert opened.proxy == ""
    assert opened.profile_id == "existing-profile"
    assert opened.debugger_address == "127.0.0.1:9222"
    assert opened.raw["data"]["driver"] == "/path/to/driver"
    assert opened.was_open is True
    assert opened.created_by_run is False
    client.cleanup_profile(opened)  # No mutation even if KEEP_BROWSER_OPEN is later changed.


def test_fixed_roxy_window_not_opened_never_force_restarts(monkeypatch):
    monkeypatch.setattr(cfg, "ROXY_PROFILE_ID", "fixed-profile")
    monkeypatch.setattr(cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", False)
    monkeypatch.setattr(cfg, "ROXY_OPEN_EXTRA_PARAMS", {"forceOpen": True})
    client = roxybrowser_client.RoxyBrowserClient()
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/browser/connection_info":
            return {"data": []}
        if path == "/browser/list_v3":
            return profile_payload("fixed-profile")
        return {"data": {"http": "127.0.0.1:9222"}}

    monkeypatch.setattr(client, "request", fake_request)
    opened = client.open_profile()
    assert calls[0][1] == "/browser/connection_info"
    assert calls[1][1] == "/browser/list_v3"
    assert calls[2][1] == cfg.ROXY_OPEN_PATH
    assert calls[2][2]["json_body"]["forceOpen"] is False
    assert opened.was_open is False


def test_fixed_roxy_window_unknown_connection_state_fails_closed(monkeypatch):
    monkeypatch.setattr(cfg, "ROXY_PROFILE_ID", "fixed-profile")
    monkeypatch.setattr(cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", False)
    client = roxybrowser_client.RoxyBrowserClient()
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append(path)
        return {"data": {"not": "a list"}}

    monkeypatch.setattr(client, "request", fake_request)
    with pytest.raises(RuntimeError, match="无法安全复用"):
        client.open_profile()
    assert calls == ["/browser/connection_info"]


def test_existing_roxy_driver_keeps_window_even_if_keep_open_is_false(monkeypatch):
    monkeypatch.setattr(cfg, "ROXY_KEEP_BROWSER_OPEN", False)
    opened = roxybrowser_client.RoxyOpenResult(
        "fixed-profile", {"data": {"http": "127.0.0.1:9222"}},
        debugger_address="127.0.0.1:9222", was_open=True,
    )
    calls = []

    class Client:
        def open_profile(self):
            return opened

        def cleanup_profile(self, _opened):
            calls.append("cleanup")

    class Driver:
        def set_page_load_timeout(self, _timeout):
            pass

        def quit(self):
            calls.append("quit")

    monkeypatch.setattr(roxy_codex_oauth, "RoxyBrowserClient", Client)
    monkeypatch.setattr(roxy_codex_oauth, "_build_driver", lambda _opened: Driver())
    monkeypatch.setattr(roxy_codex_oauth, "_center_browser_window", lambda _driver: None)
    monkeypatch.setattr(roxy_codex_oauth, "clear_roxy_browser_auth_state", lambda _driver: calls.append("clear"))
    monkeypatch.setattr(roxy_codex_oauth, "_fill_email_and_otp", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("stop")))
    result = roxy_codex_oauth._run_roxy_codex_oauth_once("user@example.com")
    assert result["status"] == "failed"
    assert calls == []


@pytest.mark.parametrize("status,code", [(500, 0), (200, 500)])
def test_roxy_api_errors_hide_proxy_credentials(monkeypatch, caplog, status, code):
    client = roxybrowser_client.RoxyBrowserClient(api_base="http://127.0.0.1:50000")
    secret = "proxy-credentials-very-secret"

    class Response:
        status_code = status
        text = "API error " + secret

        def json(self):
            return {"code": code, "msg": secret, "proxyInfo": {"proxyPassword": secret}}

    monkeypatch.setattr(client.http, "request", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(cfg, "ROXY_API_RETRIES", 1)
    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError) as exc:
        client.request("POST", "/browser/create", json_body={"proxyInfo": {"proxyPassword": secret}})
    assert secret not in str(exc.value)
    assert secret not in caplog.text


@pytest.mark.parametrize("selected", ["", "http://new:secret@proxy.example:3128", "socks5://proxy.example:1080"])
def test_new_profile_pins_selected_route_and_verifies_before_open(monkeypatch, selected, caplog):
    monkeypatch.setattr(proxy_cfg, "PROXY_POOL", [selected] if selected else [])
    # A stale configured create payload must never win over the selected route.
    monkeypatch.setattr(cfg, "ROXY_PROFILE_CREATE_PAYLOAD", {
        "proxyInfo": roxybrowser_client._proxy_url_to_roxy_info("http://old.example:8888")})
    client = roxybrowser_client.RoxyBrowserClient()
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == cfg.ROXY_CREATE_PATH:
            return {"data": {"dirId": "new-profile"}}
        if path == "/browser/list_v3":
            return profile_payload("new-profile", selected)
        if path == cfg.ROXY_OPEN_PATH:
            return {"data": {"http": "127.0.0.1:9222"}}
        pytest.fail(f"unexpected request {path}")

    monkeypatch.setattr(client, "request", request)
    with caplog.at_level(logging.DEBUG):
        opened = client.open_profile()
    assert [p for _, p, _ in calls] == [cfg.ROXY_CREATE_PATH, "/browser/list_v3", cfg.ROXY_OPEN_PATH]
    info = calls[0][2]["json_body"]["proxyInfo"]
    assert roxybrowser_client._same_proxy_route(roxybrowser_client._proxy_info_to_url(info), selected)
    assert roxybrowser_client._same_proxy_route(opened.proxy, selected)
    assert opened.created_by_run is True
    if not selected:
        assert info["proxyMethod"] == "custom"
        assert info["proxyCategory"] == "noproxy"
        assert not any(info[k] for k in ("host", "port", "proxyUserName", "proxyPassword"))
    assert "secret" not in caplog.text
    assert "secret" not in repr(opened)


@pytest.mark.parametrize("saved,selected", [
    ("http://proxy.example:3128", "http://proxy.example:3128"),
    ("socks5h://proxy.example:1080", "socks5://proxy.example:1080"),
    ("http://old.example:8080", ""),
    ("", "http://proxy.example:3128"),
])
def test_open_fixed_profile_checks_route_without_mutation(monkeypatch, saved, selected):
    monkeypatch.setattr(cfg, "ROXY_PROFILE_ID", "fixed")
    monkeypatch.setattr(cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", False)
    client = roxybrowser_client.RoxyBrowserClient()
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path))
        if path == "/browser/connection_info":
            return {"data": [{"dirId": "fixed", "http": "127.0.0.1:9222"}]}
        if path == "/browser/list_v3":
            return profile_payload("fixed", saved)
        pytest.fail("an already open Profile must not be modified, opened again or deleted")

    monkeypatch.setattr(client, "request", request)
    if roxybrowser_client._same_proxy_route(saved, selected):
        opened = client.open_profile(proxy=selected)
        assert opened.was_open and opened.proxy == saved
        client.cleanup_profile(opened)
    else:
        with pytest.raises(RuntimeError, match="其代理与当前任务网络不一致"):
            client.open_profile(proxy=selected)
    assert calls == [("GET", "/browser/connection_info"), ("GET", "/browser/list_v3")]


@pytest.mark.parametrize("selected", ["", "http://next:secret@new.example:3128"])
@pytest.mark.parametrize("update_took_effect", [True, False])
def test_closed_profile_updates_then_rereads_route(monkeypatch, selected, update_took_effect, caplog):
    monkeypatch.setattr(cfg, "ROXY_PROFILE_ID", "fixed")
    monkeypatch.setattr(cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", False)
    client = roxybrowser_client.RoxyBrowserClient()
    calls = []
    saved = "http://old.example:8080"

    def request(method, path, **kwargs):
        nonlocal saved
        calls.append(path)
        if path == "/browser/connection_info":
            return {"data": []}
        if path == "/browser/list_v3":
            return profile_payload("fixed", saved)
        if path == "/browser/mdf":
            body = kwargs["json_body"]
            assert body["workspaceId"] == "workspace" and body["dirId"] == "fixed"
            if update_took_effect:
                saved = roxybrowser_client._proxy_info_to_url(body["proxyInfo"])
            return {"code": 0}
        if path == cfg.ROXY_OPEN_PATH:
            assert roxybrowser_client._same_proxy_route(saved, selected)
            return {"data": {"http": "127.0.0.1:9222"}}
        pytest.fail(f"unexpected request {path}")

    monkeypatch.setattr(client, "request", request)
    with caplog.at_level(logging.DEBUG):
        if update_took_effect:
            assert client.open_profile(proxy=selected).proxy == selected
        else:
            with pytest.raises(RuntimeError, match="同步未生效"):
                client.open_profile(proxy=selected)
    assert calls[:5] == ["/browser/connection_info", "/browser/list_v3", "/browser/connection_info", "/browser/mdf", "/browser/list_v3"]
    assert (cfg.ROXY_OPEN_PATH in calls) is update_took_effect
    assert "secret" not in caplog.text


def test_profile_opened_during_sync_is_not_modified(monkeypatch):
    monkeypatch.setattr(cfg, "ROXY_PROFILE_ID", "fixed")
    monkeypatch.setattr(cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", False)
    client = roxybrowser_client.RoxyBrowserClient()
    calls = []

    def request(method, path, **kwargs):
        calls.append(path)
        if path == "/browser/connection_info":
            return {"data": [] if len(calls) == 1 else [{"dirId": "fixed"}]}
        if path == "/browser/list_v3":
            return profile_payload("fixed", "http://old.example:8080")
        pytest.fail("cannot modify an open Profile")

    monkeypatch.setattr(client, "request", request)
    with pytest.raises(RuntimeError, match="已停止修改网络"):
        client.open_profile(proxy="")
    assert len(calls) == 3


@pytest.mark.parametrize("lookup_fails", [True, False])
def test_new_profile_route_failure_deletes_only_created_profile(monkeypatch, lookup_fails):
    client = roxybrowser_client.RoxyBrowserClient()
    calls = []

    def request(method, path, **kwargs):
        calls.append(path)
        if path == cfg.ROXY_CREATE_PATH:
            return {"data": {"dirId": "new-profile"}}
        if path == "/browser/list_v3":
            return {"data": None} if lookup_fails else profile_payload("new-profile", "http://old.example:8080")
        if path == cfg.ROXY_DELETE_PATH:
            assert kwargs["json_body"]["dirIds"] == ["new-profile"]
            return {"code": 0}
        pytest.fail("must not launch an unverified route")

    monkeypatch.setattr(client, "request", request)
    with pytest.raises(RuntimeError):
        client.open_profile(proxy="")
    assert calls == [cfg.ROXY_CREATE_PATH, "/browser/list_v3", cfg.ROXY_DELETE_PATH]


def test_workspace_can_be_read_from_create_payload(monkeypatch):
    monkeypatch.setattr(cfg, "ROXY_WORKSPACE_ID", "")
    monkeypatch.setattr(cfg, "ROXY_PROFILE_CREATE_PAYLOAD", {"workspaceId": "123"})
    assert roxybrowser_client._workspace_id_value() == 123


def test_invalid_proxy_never_calls_roxy_or_falls_back(monkeypatch):
    monkeypatch.setattr(proxy_cfg, "PROXY_POOL", ["https://provider.example/subscription?token=private"])
    client = roxybrowser_client.RoxyBrowserClient()
    monkeypatch.setattr(client, "request", lambda *a, **k: pytest.fail("invalid route must not start a browser"))
    with pytest.raises(ValueError, match="订阅") as exc:
        client.open_profile()
    assert "private" not in str(exc.value)


@pytest.mark.parametrize("failure", ["request-error", "missing-debugger"])
def test_failed_new_profile_open_cleans_only_the_profile_created_by_this_run(monkeypatch, failure):
    client = roxybrowser_client.RoxyBrowserClient()
    calls = []
    monkeypatch.setattr(client, "create_profile", lambda _body: "new-profile")

    def request(method, path, **_kwargs):
        calls.append((method, path))
        if path == "/browser/list_v3":
            return profile_payload("new-profile")
        if path == cfg.ROXY_OPEN_PATH:
            if failure == "request-error":
                raise RuntimeError("open failed")
            return {"data": {}}
        return {"code": 0}

    monkeypatch.setattr(client, "request", request)
    with pytest.raises(RuntimeError):
        client.open_profile(proxy="")
    assert [path for _method, path in calls] == [
        "/browser/list_v3", cfg.ROXY_OPEN_PATH, cfg.ROXY_CLOSE_PATH, cfg.ROXY_DELETE_PATH,
    ]


def test_failed_fixed_profile_open_never_deletes_or_closes_it(monkeypatch):
    monkeypatch.setattr(cfg, "ROXY_PROFILE_ID", "fixed-profile")
    monkeypatch.setattr(cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", False)
    client = roxybrowser_client.RoxyBrowserClient()
    calls = []

    def request(method, path, **_kwargs):
        calls.append(path)
        if path == "/browser/connection_info":
            return {"data": []}
        if path == "/browser/list_v3":
            return profile_payload("fixed-profile")
        raise RuntimeError("open failed")

    monkeypatch.setattr(client, "request", request)
    with pytest.raises(RuntimeError, match="open failed"):
        client.open_profile(proxy="")
    assert cfg.ROXY_CLOSE_PATH not in calls
    assert cfg.ROXY_DELETE_PATH not in calls
