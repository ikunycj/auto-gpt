import socket
import threading

import pytest

from core import chrome_cdp_driver as chrome_cdp


class FakeProcess:
    pid = 12345

    def __init__(self):
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class FakePage:
    url = "about:blank"

    def __init__(self, environment=None):
        self.environment = environment or {
            "webdriver": False,
            "language": "zh-CN",
            "timezone": "Asia/Shanghai",
            "userAgent": "Chrome/151",
        }
        self.navigation_timeout = None
        self.timeout = None

    def evaluate(self, _script):
        return self.environment

    def set_default_navigation_timeout(self, value):
        self.navigation_timeout = value

    def set_default_timeout(self, value):
        self.timeout = value


class FakeContext:
    def __init__(self, page):
        self.pages = [page]
        self.closed = False

    def close(self):
        self.closed = True


class FakeBrowser:
    version = "151.0.0.0"

    def __init__(self, context):
        self.contexts = [context]
        self.closed = False

    def close(self):
        self.closed = True


class FakePlaywright:
    def __init__(self, browser):
        self.browser = browser
        self.stopped = False

    def stop(self):
        self.stopped = True


def test_chrome_command_uses_normal_process_and_isolated_profile(tmp_path):
    command = chrome_cdp._build_chrome_command(
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        9333,
        str(tmp_path),
        "",
        True,
    )

    assert "--remote-debugging-address=127.0.0.1" in command
    assert "--remote-debugging-port=9333" in command
    assert f"--user-data-dir={tmp_path}" in command
    assert "--incognito" in command
    assert "--start-minimized" in command
    assert not any(arg in {"--enable-automation", "--disable-blink-features=AutomationControlled"} for arg in command)


def test_chrome_command_uses_bridge_without_proxy_credentials(tmp_path):
    command = chrome_cdp._build_chrome_command(
        "/path/to/chrome",
        9333,
        str(tmp_path),
        "http://127.0.0.1:45678",
        False,
    )

    assert "--proxy-server=http://127.0.0.1:45678" in command
    assert not any(item.startswith("--load-extension=") for item in command)


def test_driver_quit_closes_owned_browser_and_removes_profile(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    page = FakePage()
    context = FakeContext(page)
    browser = FakeBrowser(context)
    playwright = FakePlaywright(browser)
    process = FakeProcess()
    driver = chrome_cdp.ChromeCDPSeleniumDriver(
        browser,
        context,
        page,
        playwright=playwright,
        process=process,
        profile_dir=str(profile),
    )

    driver.quit()
    driver.quit()

    assert context.closed is True
    assert browser.closed is True
    assert playwright.stopped is True
    assert process.terminated is True
    assert not profile.exists()


def test_driver_keep_open_only_disconnects_playwright(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    page = FakePage()
    context = FakeContext(page)
    browser = FakeBrowser(context)
    playwright = FakePlaywright(browser)
    process = FakeProcess()
    driver = chrome_cdp.ChromeCDPSeleniumDriver(
        browser,
        context,
        page,
        playwright=playwright,
        process=process,
        profile_dir=str(profile),
        keep_open=True,
    )

    driver.quit()

    assert context.closed is False
    assert browser.closed is False
    assert playwright.stopped is True
    assert process.terminated is False
    assert profile.exists()


def test_find_chrome_executable_accepts_explicit_executable(tmp_path):
    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)

    assert chrome_cdp._find_chrome_executable(str(executable)) == str(executable)


def test_wait_for_cdp_reports_early_process_exit():
    process = FakeProcess()
    process.returncode = 17

    with pytest.raises(RuntimeError, match="exit=17"):
        chrome_cdp._wait_for_cdp(9333, process, timeout=1)


def test_proxy_endpoint_keeps_credentials_for_local_audit():
    assert chrome_cdp._proxy_endpoint(
        "http://user:password@proxy.example:8080"
    ) == "http://user:password@proxy.example:8080"


def test_proxy_preflight_reports_chatgpt_and_auth_routes(monkeypatch):
    class Response:
        def __init__(self, status_code):
            self.status_code = status_code

    calls = []

    def get(url, **_kwargs):
        calls.append(url)
        return Response(200 if "auth.openai.com" in url else 503)

    from curl_cffi import requests as curl_requests
    monkeypatch.setattr(curl_requests, "get", get)

    ok, detail = chrome_cdp._probe_chatgpt_route("http://proxy.example:8080")

    assert ok is False
    assert calls == [chrome_cdp._CHATGPT_LOGIN_URL, chrome_cdp._AUTH_OPENAI_PROBE_URL]
    assert "chatgpt.com=HTTP 503 upstream error" in detail
    assert "auth.openai.com=HTTP 200" in detail


def test_configured_invalid_proxy_pool_never_falls_back_to_direct(monkeypatch):
    from config import proxy as proxy_cfg

    monkeypatch.setattr(proxy_cfg, "PROXY_POOL", ["https://example.test/subscription?token=secret"])
    monkeypatch.setattr(
        chrome_cdp,
        "_probe_chatgpt_route",
        lambda _proxy: pytest.fail("invalid configured pool must not probe direct"),
    )

    with pytest.raises(RuntimeError, match="代理池没有有效代理端点") as exc_info:
        chrome_cdp._select_proxy(None)

    assert "secret" in str(exc_info.value)


def test_empty_proxy_pool_allows_direct_access(monkeypatch):
    from config import proxy as proxy_cfg

    monkeypatch.setattr(proxy_cfg, "PROXY_POOL", [])
    assert chrome_cdp._proxy_candidates(None) == [""]


def test_select_proxy_skips_failed_candidate(monkeypatch):
    candidates = [
        "http://bad.example:8080",
        "socks5://127.0.0.1:7897",
    ]
    monkeypatch.setattr(chrome_cdp, "_proxy_candidates", lambda _proxy: list(candidates))
    monkeypatch.setattr(chrome_cdp.random, "shuffle", lambda _items: None)

    def probe(proxy):
        return (proxy.endswith("7897"), "HTTP 403" if proxy.endswith("7897") else "reset")

    monkeypatch.setattr(chrome_cdp, "_probe_chatgpt_route", probe)

    assert chrome_cdp._select_proxy(None) == "socks5://127.0.0.1:7897"


def test_select_proxy_reports_all_failed_candidates(monkeypatch):
    monkeypatch.setattr(chrome_cdp, "_proxy_candidates", lambda _proxy: ["http://bad.example:8080"])
    monkeypatch.setattr(chrome_cdp, "_probe_chatgpt_route", lambda _proxy: (False, "reset"))

    with pytest.raises(RuntimeError, match="无可用的 ChatGPT 网络路由") as exc_info:
        chrome_cdp._select_proxy(None)

    message = str(exc_info.value)
    assert "http://bad.example:8080" in message
    assert "reset" in message


def test_authenticated_bridge_returns_502_when_upstream_closes():
    upstream_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    upstream_listener.bind(("127.0.0.1", 0))
    upstream_listener.listen(1)
    upstream_port = upstream_listener.getsockname()[1]

    def close_upstream():
        connection, _ = upstream_listener.accept()
        connection.close()
        upstream_listener.close()

    threading.Thread(target=close_upstream, daemon=True).start()
    bridge = chrome_cdp._AuthenticatedProxyBridge(
        f"http://user:password@127.0.0.1:{upstream_port}"
    )
    bridge.start()
    client, server = socket.socketpair()
    try:
        server.sendall(b"CONNECT chatgpt.com:443 HTTP/1.1\r\nHost: chatgpt.com\r\n\r\n")
        bridge._handle(client)
        assert server.recv(256).startswith(b"HTTP/1.1 502 Bad Gateway")
    finally:
        client.close()
        server.close()
        bridge.stop()
