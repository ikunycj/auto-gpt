"""Browser-backed TOTP requests retain the registration tab's native session."""
from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from core import account_export, chatgpt_browser_login
from core.browser_2fa_session import BrowserBacked2FASession


class Browser:
    def __init__(self):
        self.current_url = "https://chatgpt.com/"
        self.requests = []
        self.navigations = []
        self.responses = [
            (200, {"csrfToken": "csrf-private"}),
            (200, {"url": "https://auth.openai.com/authorize?state=private"}),
            (200, {"continue_url": "https://auth.openai.com/authorize/continue?code=private"}),
            (200, {"accessToken": "access-private", "user": {"email": "user@example.com"}}),
            (200, {"secret": "JBSWY3DPEHPK3PXP", "session_id": "enroll-private"}),
            (200, {"success": True}),
        ]

    def execute_script(self, script):
        assert "navigator.language" in script
        return "en-US"

    def execute_async_script(self, script, method, url, headers, body):
        assert "fetch(url" in script
        assert urlsplit(url).hostname == urlsplit(self.current_url).hostname
        self.requests.append((method, urlsplit(url).path, headers, body))
        code, payload = self.responses.pop(0)
        return {"ok": True, "status": code, "payload": payload}

    def get(self, url):
        self.navigations.append(url)
        self.current_url = (
            "https://auth.openai.com/email-verification" if "authorize?" in url
            else "https://chatgpt.com/"
        )


def test_complete_2fa_runs_all_requests_in_registration_browser(monkeypatch, caplog):
    browser = Browser()
    monkeypatch.setattr(chatgpt_browser_login, "_browser_cookies", lambda _driver: [{"name": "oai-did", "value": "did-private"}])
    monkeypatch.setattr(account_export, "human_delay", lambda *_args: None)
    session = BrowserBacked2FASession(browser)

    with caplog.at_level("INFO"):
        secret = account_export.setup_2fa(
            session, "user@example.com", otp_provider=lambda *_args, **_kwargs: "otp-private",
        )

    assert secret == "JBSWY3DPEHPK3PXP"
    assert [path for _, path, _, _ in browser.requests] == [
        "/api/auth/csrf", "/api/auth/signin/openai", "/api/accounts/email-otp/validate",
        "/api/auth/session", "/backend-api/accounts/mfa/enroll",
        "/backend-api/accounts/mfa/user/activate_enrollment",
    ]
    assert len(browser.navigations) == 2
    assert browser.requests[4][2]["authorization"] == "Bearer access-private"
    for sensitive in ("csrf-private", "state=private", "code=private", "otp-private", "did-private", "access-private", "enroll-private", secret):
        assert sensitive not in caplog.text


def test_browser_2fa_does_not_allow_cross_origin_fetch(monkeypatch):
    browser = Browser()
    monkeypatch.setattr(chatgpt_browser_login, "_browser_cookies", lambda _driver: [{"name": "oai-did", "value": "did-private"}])
    session = BrowserBacked2FASession(browser)
    with pytest.raises(RuntimeError, match="不同源"):
        session.post("https://auth.openai.com/api/accounts/email-otp/validate")
    assert not browser.requests


def test_browser_2fa_rejects_untrusted_oauth_redirect(monkeypatch):
    browser = Browser()
    monkeypatch.setattr(chatgpt_browser_login, "_browser_cookies", lambda _driver: [{"name": "oai-did", "value": "did-private"}])
    session = BrowserBacked2FASession(browser)
    with pytest.raises(RuntimeError, match="允许的 HTTPS"):
        session.get("https://evil.example/authorize?token=private", allow_redirects=True)
    assert not browser.navigations


def test_browser_2fa_403_fails_with_status_only(monkeypatch, caplog):
    browser = Browser()
    browser.responses = [(403, {"reason": "secret-private"})]
    monkeypatch.setattr(chatgpt_browser_login, "_browser_cookies", lambda _driver: [{"name": "oai-did", "value": "did-private"}])
    monkeypatch.setattr(account_export, "human_delay", lambda *_args: None)
    with caplog.at_level("INFO"), pytest.raises(RuntimeError, match="HTTP 403") as exc:
        account_export.setup_2fa(BrowserBacked2FASession(browser), "user@example.com", otp_code="otp-private")
    assert "secret-private" not in str(exc.value) + caplog.text
