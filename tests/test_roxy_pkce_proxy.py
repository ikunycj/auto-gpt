"""The local PKCE exchange must use only the matching Roxy browser route."""
import logging
from types import SimpleNamespace

import pytest

from config import roxybrowser as cfg
from core import codex_oauth, roxy_codex_oauth


class ListClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return self.payload


def _profile_payload(info, *, profile_id="target"):
    return {"data": {"rows": [
        {"dirId": "another", "proxyInfo": {"protocol": "http", "host": "other", "port": "80"}},
        {"dirId": profile_id, "proxyInfo": info},
    ]}}


def _proxy_info():
    return {"protocol": "SOCKS5", "proxyCategory": "SOCKS5", "proxyMethod": "choose",
            "host": "proxy.example", "port": "8080", "proxyUserName": "name@a",
            "proxyPassword": "secret:/?#"}


def test_profile_proxy_readonly_lookup_and_encoded_socks_route(monkeypatch, caplog):
    monkeypatch.setattr(cfg, "ROXY_PROFILE_ID", "target")
    monkeypatch.setattr(cfg, "ROXY_WORKSPACE_ID", "workspace")
    client = ListClient(_profile_payload(_proxy_info()))
    with caplog.at_level(logging.DEBUG):
        proxy = roxy_codex_oauth._roxy_profile_token_proxy(SimpleNamespace(profile_id="target"), None, client)

    assert proxy == "socks5h://name%40a:secret%3A%2F%3F%23@proxy.example:8080"
    assert client.calls == [("GET", "/browser/list_v3", {"params": {
        "workspaceId": "workspace", "dirIds": "target", "page_index": 1, "page_size": 100,
    }})]
    assert "secret" in caplog.text or "secret" in proxy
    assert "proxy.example" in proxy


@pytest.mark.parametrize("payload,provided_proxy", [
    (_profile_payload({**_proxy_info(), "proxyUserName": "", "proxyPassword": "secret"}), None),
    (_profile_payload({**_proxy_info(), "proxyUserName": "name@a", "proxyPassword": ""}), None),
    (_profile_payload({**_proxy_info(), "protocol": "direct"}), None),
    (_profile_payload({**_proxy_info(), "host": "proxy.example/path"}), None),
    (_profile_payload(_proxy_info(), profile_id="wrong"), None),
    (_profile_payload(_proxy_info()), "socks5://name%40a:wrong@proxy.example:8080"),
])
def test_profile_proxy_fails_closed_without_matching_endpoint(monkeypatch, payload, provided_proxy):
    monkeypatch.setattr(cfg, "ROXY_PROFILE_ID", "target")
    monkeypatch.setattr(cfg, "ROXY_WORKSPACE_ID", "workspace")
    with pytest.raises(roxy_codex_oauth._RoxyProxyRouteError) as exc:
        roxy_codex_oauth._roxy_profile_token_proxy(
            SimpleNamespace(profile_id="target"), provided_proxy, ListClient(payload),
        )
    assert "secret" in str(exc.value) or "wrong" in str(exc.value) or "Roxy Profile" in str(exc.value)


def test_profile_proxy_allows_unauthenticated_route(monkeypatch):
    monkeypatch.setattr(cfg, "ROXY_PROFILE_ID", "target")
    monkeypatch.setattr(cfg, "ROXY_WORKSPACE_ID", "workspace")
    info = {**_proxy_info(), "proxyUserName": "", "proxyPassword": ""}
    client = ListClient(_profile_payload(info))

    proxy = roxy_codex_oauth._roxy_profile_token_proxy(
        SimpleNamespace(profile_id="target"), None, client,
    )

    assert proxy == "socks5h://proxy.example:8080"


def test_profile_proxy_allows_omitted_unauthenticated_credentials(monkeypatch):
    monkeypatch.setattr(cfg, "ROXY_PROFILE_ID", "target")
    monkeypatch.setattr(cfg, "ROXY_WORKSPACE_ID", "workspace")
    info = _proxy_info()
    info.pop("proxyUserName")
    info.pop("proxyPassword")

    proxy = roxy_codex_oauth._roxy_profile_token_proxy(
        SimpleNamespace(profile_id="target"), None, ListClient(_profile_payload(info)),
    )

    assert proxy == "socks5h://proxy.example:8080"


def test_roxy_local_pkce_exchange_uses_profile_proxy_without_global_pool(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "ROXY_PROFILE_ID", "target")
    monkeypatch.setattr(cfg, "ROXY_WORKSPACE_ID", "workspace")
    monkeypatch.setattr(cfg, "ROXY_KEEP_BROWSER_OPEN", False)
    monkeypatch.setattr(codex_oauth, "_codex_auth_url_source", lambda: "local")
    monkeypatch.setattr(codex_oauth, "_generate_pkce", lambda: ("verifier", "challenge"))
    monkeypatch.setattr(codex_oauth, "_generate_state", lambda: "state")
    monkeypatch.setattr(codex_oauth, "_build_authorize_url", lambda *_args, **_kwargs: "https://example.invalid/auth")
    monkeypatch.setattr(codex_oauth, "_extract_code", lambda *_args: "one-time-code")
    monkeypatch.setattr(codex_oauth, "_parse_id_token", lambda _token: {"email": "user@example.com"})
    monkeypatch.setattr(codex_oauth, "build_codex_storage", lambda *_args: {"redacted": True})
    monkeypatch.setattr(codex_oauth, "save_codex_credential", lambda *_args: tmp_path / "stored")
    monkeypatch.setattr(roxy_codex_oauth, "_fill_email_and_otp", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(roxy_codex_oauth, "_handle_totp_challenge_if_present", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(roxy_codex_oauth, "_do_phone_verification_if_present", lambda *_args: False)
    monkeypatch.setattr(roxy_codex_oauth, "_finish_consent_workspace", lambda *_args: "http://127.0.0.1:1455/auth/callback")
    monkeypatch.setattr(roxy_codex_oauth, "human_delay", lambda *_args: None)
    monkeypatch.setattr(roxy_codex_oauth, "_center_browser_window", lambda *_args: None)

    class Driver:
        def set_page_load_timeout(self, _timeout):
            pass

    monkeypatch.setattr(roxy_codex_oauth, "_build_driver", lambda _opened: Driver())
    opened = SimpleNamespace(profile_id="target", raw={"data": {}}, was_open=True)
    client = ListClient(_profile_payload(_proxy_info()))
    client.open_profile = lambda: opened
    monkeypatch.setattr(roxy_codex_oauth, "RoxyBrowserClient", lambda: client)
    sessions = []

    class Session:
        def __init__(self, *, proxy, detect_exit_geo):
            sessions.append((proxy, detect_exit_geo))
            self.session = SimpleNamespace(trust_env=True)
            sessions.append(self)

    monkeypatch.setattr(codex_oauth, "BrowserSession", Session)

    def exchange(session, code, verifier):
        assert session.session.trust_env is False
        assert code == "one-time-code" and verifier == "verifier"
        return {"id_token": "test"}

    monkeypatch.setattr(codex_oauth, "exchange_codex_token", exchange)
    result = roxy_codex_oauth._run_roxy_codex_oauth_once("user@example.com", proxy=None)

    assert result["status"] == "success"
    assert sessions[0] == ("socks5h://name%40a:secret%3A%2F%3F%23@proxy.example:8080", False)
    assert client.calls[0][1] == "/browser/list_v3"


def test_list_v3_failure_keeps_api_exception_in_error_and_log(monkeypatch, caplog):
    monkeypatch.setattr(cfg, "ROXY_PROFILE_ID", "target")
    monkeypatch.setattr(cfg, "ROXY_WORKSPACE_ID", "workspace")
    secret = "proxy-credential-private"

    class FailingClient:
        def request(self, *_args, **_kwargs):
            raise RuntimeError("API returned " + secret)

    with caplog.at_level(logging.DEBUG), pytest.raises(roxy_codex_oauth._RoxyProxyRouteError) as exc:
        roxy_codex_oauth._roxy_profile_token_proxy(
            SimpleNamespace(profile_id="target"), None, FailingClient(),
        )
    assert "list_v3" in str(exc.value)
    assert secret in str(exc.value)


@pytest.mark.parametrize("fixed_id", ["another-account-profile", ""])
def test_stored_profile_route_is_independent_of_current_global_profile(monkeypatch, fixed_id):
    monkeypatch.setattr(cfg, "ROXY_PROFILE_ID", fixed_id)
    monkeypatch.setattr(cfg, "ROXY_WORKSPACE_ID", "workspace")
    client = ListClient(_profile_payload(_proxy_info()))
    route = roxy_codex_oauth._roxy_profile_token_proxy(
        SimpleNamespace(profile_id="target"), None, client,
    )
    assert route == "socks5h://name%40a:secret%3A%2F%3F%23@proxy.example:8080"
    assert client.calls[0][2]["params"]["dirIds"] == "target"


def test_missing_profile_id_does_not_query_list_v3():
    client = ListClient({})
    with pytest.raises(roxy_codex_oauth._RoxyProxyRouteError):
        roxy_codex_oauth._roxy_profile_token_proxy(SimpleNamespace(profile_id=""), None, client)
    assert client.calls == []
