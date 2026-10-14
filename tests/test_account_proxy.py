"""Saved account routes never inherit pool/environment/profile proxy settings."""
import socket

import pytest

from core import account_proxy as routing


@pytest.fixture(autouse=True)
def local_host(monkeypatch):
    monkeypatch.setattr(routing.socket, "gethostname", lambda: "test-mac.local")
    monkeypatch.setattr(routing.socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", 0)),
    ])


@pytest.mark.parametrize("route", [None, "", "127.0.0.1:7890", "http://localhost:7890",
    "http://api.localhost:7890", "http://test-mac.local:7890", "http://test-mac:7890",
    "socks5://[::1]:1080", "http://0.0.0.0:7890", "http://[::ffff:127.0.0.1]:7890",
    "http://192.168.1.10:7890"])
def test_registration_does_not_bind_local_machine_proxy(route):
    assert routing.registration_proxy(route) == ""


@pytest.mark.parametrize("route, expected", [
    ("proxy.example:8080", "http://proxy.example:8080"),
    ("proxy.example:8080:user:private", "http://user:private@proxy.example:8080"),
    ("http://192.168.1.20:7890", "http://192.168.1.20:7890"),
    ("socks5h://user:private@proxy.example:1080", "socks5h://user:private@proxy.example:1080"),
])
def test_registration_saves_nonlocal_endpoint(route, expected):
    assert routing.registration_proxy(route) == expected


def test_failure_resolving_own_hostname_does_not_lose_remote_binding(monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("host unavailable")
    monkeypatch.setattr(routing.socket, "getaddrinfo", fail)
    assert routing.registration_proxy("http://192.168.1.20:7890") == "http://192.168.1.20:7890"


@pytest.mark.parametrize("route", ["", "http://user:private@proxy.example:8080", "http://127.0.0.1:7890"])
def test_explicit_account_route_wins_over_old_snapshot_and_pool(monkeypatch, route):
    from config import proxy as cfg
    monkeypatch.setattr(cfg, "PROXY_POOL", ["http://wrong.example:8080"])
    assert routing.account_proxy({"proxy": route, "_codex_verified_proxy": "http://old.example:8080",
                                  "codex_profile_id": "profile"}) == route
    assert routing.account_proxy({"_codex_verified_proxy": route, "codex_profile_id": "profile"}) == ""


@pytest.mark.parametrize("route, expected", [
    (None, "本机网络"), ("", "本机网络"),
    ("http://user:private@proxy.example:8080", "http://proxy.example:8080（已配置认证）"),
    ("socks5h://user:private@[2001:db8::1]:1080", "socks5h://[2001:db8::1]:1080（已配置认证）"),
    ("https://user:private@proxy.example/subscribe?token=hidden", "代理配置无效"),
])
def test_proxy_display_has_no_credentials(route, expected):
    assert routing.proxy_display(route) == expected
