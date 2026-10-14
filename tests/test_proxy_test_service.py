"""Proxy diagnostics are bounded, safe, and use exactly the requested route."""
import json
import threading
from types import SimpleNamespace

import pytest
from curl_cffi import CurlECode, CurlOpt

from core import proxy_test_service as service


class NetworkFailure(Exception):
    def __init__(self, code):
        super().__init__("http://sensitive-user:sensitive-pass@proxy.example:8080")
        self.code = code


@pytest.fixture
def fake_session(monkeypatch):
    state = SimpleNamespace(options=[], calls=[], responses=[(200, b'{"ip":"203.0.113.7"}'), (200, b'ok')])

    class Session:
        def __init__(self, **kwargs):
            state.options.append(kwargs)
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def get(self, url, **kwargs):
            state.calls.append((url, kwargs))
            response = state.responses[(len(state.calls) - 1) % len(state.responses)]
            if isinstance(response, Exception):
                raise response
            status, body = response
            assert kwargs["content_callback"](body) == len(body)
            return SimpleNamespace(status_code=status)

    monkeypatch.setattr(service.requests, "Session", Session)
    return state


@pytest.mark.parametrize("proxy", ["", "socks5h://sensitive-user:sensitive-pass@proxy.example:1080"])
def test_exact_route_ignores_environment_and_keeps_tls_validation(fake_session, monkeypatch, proxy):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://unwanted.example:9999")
    monkeypatch.setenv("NO_PROXY", "*")
    result = service.test_proxy(proxy)
    assert result["ok"] is True
    assert result["exit_ip"] == "203.0.113.7"
    assert result["route"] == ("proxy" if proxy else "direct")
    assert fake_session.options == [{"trust_env": False, "verify": True, "curl_options": {
        CurlOpt.PROXY: proxy, CurlOpt.NOPROXY: "",
        CurlOpt.PROXY_SSL_VERIFYPEER: 1, CurlOpt.PROXY_SSL_VERIFYHOST: 2,
    }}]
    assert [call[0] for call in fake_session.calls] == [service._IP_URL, service._TARGET_URL]
    assert service._TARGET_URL.endswith("/log-in")
    for _, kwargs in fake_session.calls:
        assert kwargs["allow_redirects"] is False
        assert kwargs["timeout"] <= 6
        assert kwargs["discard_cookies"] is True
    assert "sensitive-user" not in json.dumps(result)
    assert "sensitive-pass" not in json.dumps(result)


@pytest.mark.parametrize("status, expected", [(403, "http_rejected"), (407, "proxy_auth_error"),
                                               (500, "http_rejected"), (404, "http_rejected")])
def test_http_rejection_is_not_proxy_success(fake_session, status, expected):
    fake_session.responses[1] = (status, b"response must stay private")
    result = service.test_proxy("")
    assert result["ok"] is False
    assert result["network"]["ok"] is True
    assert result["target"]["reachable"] is True
    assert result["target"]["status"] == expected
    assert "response must stay private" not in json.dumps(result)


@pytest.mark.parametrize("code, expected", [
    (CurlECode.OPERATION_TIMEDOUT, "timeout"), (CurlECode.SSL_CONNECT_ERROR, "tls_error"),
    (CurlECode.PEER_FAILED_VERIFICATION, "tls_error"), (CurlECode.COULDNT_RESOLVE_PROXY, "dns_error"),
    (CurlECode.COULDNT_CONNECT, "connection_error"), (CurlECode.PROXY, "proxy_error"),
    (99999, "network_error"),
])
def test_connection_failure_is_safe_and_never_falls_back(fake_session, code, expected):
    fake_session.responses = [NetworkFailure(code)]
    proxy = "http://sensitive-user:sensitive-pass@proxy.example:8080"
    result = service.test_proxies([proxy])[0]
    assert result["ok"] is False
    assert result["network"]["status"] == expected
    assert result["target"]["status"] == expected
    assert len(fake_session.options) == 1
    assert fake_session.options[0]["curl_options"][CurlOpt.PROXY] == proxy
    assert "sensitive-user" not in json.dumps(result)
    assert "sensitive-pass" not in json.dumps(result)


@pytest.mark.parametrize("status, body", [(302, b""), (200, b"not JSON"),
    (200, b'{"ip":"not an IP"}'), (200, b'{}'), (200, b'x' * 4096)])
def test_missing_exit_ip_is_not_reported_as_success(fake_session, status, body):
    fake_session.responses[0] = (status, body)
    result = service.test_proxy("")
    assert result["ok"] is False
    assert result["exit_ip"] == ""
    assert result["network"]["status"] == "invalid_response"


@pytest.mark.parametrize("value", [None, "text", [], [""] * 21, [None], ["x" * 4097]])
def test_invalid_batches_do_not_open_connections(fake_session, value):
    with pytest.raises(ValueError):
        service.test_proxies(value)
    assert fake_session.calls == []
    assert fake_session.options == []


def test_subscription_error_does_not_echo_secret_or_open_connection(fake_session):
    result = service.test_proxies(["https://user:private@proxy.example/sub?token=sensitive"])
    assert result[0]["status"] == "invalid_proxy"
    assert fake_session.options == []
    assert "sensitive" not in json.dumps(result)
    assert "private" not in json.dumps(result)


def test_global_concurrency_bound(fake_session, monkeypatch):
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(service, "_SLOTS", slots)
    result = service.test_proxy("")
    assert result["status"] == "busy"
    assert fake_session.options == []
    slots.release()
    assert service.test_proxy("")["ok"] is True


def test_session_creation_failure_is_safe_and_releases_slot(monkeypatch):
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(service, "_SLOTS", slots)
    def fail(**_kwargs):
        raise RuntimeError("private credentials")
    monkeypatch.setattr(service.requests, "Session", fail)
    result = service.test_proxies([""])[0]
    assert result["status"] == "network_error"
    assert "private" not in json.dumps(result)
    assert slots.acquire(blocking=False)
    slots.release()
