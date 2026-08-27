from types import SimpleNamespace

import pytest

from core import account_liveness
from core import chatgpt_browser_login


class _Http:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _Session:
    def __init__(self):
        self.session = _Http()
        self.device_id = "device-id"
        self.proxy = ""


def test_account_session_prefers_browser_password(monkeypatch):
    session = _Session()
    calls = []

    def authenticate(email, password, **kwargs):
        calls.append((email, password, kwargs))
        return session, {"accessToken": "password-at"}

    monkeypatch.setattr(chatgpt_browser_login, "authenticate_chatgpt_with_password", authenticate)
    monkeypatch.setattr(
        account_liveness,
        "_network_preflight_with_retry",
        lambda *_args, **_kwargs: pytest.fail("email OTP preflight must not run after password login succeeds"),
    )

    returned, info = account_liveness.authenticate_account_session(
        "user@example.com",
        login_password="chatgpt-password",
        totp_provider=lambda: "123456",
    )

    assert returned is session
    assert info["accessToken"] == "password-at"
    assert session.authentication_method == "password"
    assert calls[0][0:2] == ("user@example.com", "chatgpt-password")
    assert callable(calls[0][2]["totp_provider"])


def test_account_session_falls_back_only_when_password_page_is_unavailable(monkeypatch):
    session = _Session()
    monkeypatch.setattr(
        chatgpt_browser_login,
        "authenticate_chatgpt_with_password",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            chatgpt_browser_login.PasswordLoginUnavailable("email OTP only")
        ),
    )
    monkeypatch.setattr(account_liveness, "_network_preflight_with_retry", lambda *_args, **_kwargs: (session, "authorize"))
    monkeypatch.setattr(account_liveness, "follow_authorize", lambda *_args, **_kwargs: "email-verification")
    monkeypatch.setattr(
        account_liveness,
        "_validate_with_retry",
        lambda *_args, **_kwargs: {"continue_url": "https://chatgpt.com/api/auth/callback/openai"},
    )
    monkeypatch.setattr(account_liveness, "follow_oauth_callback", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(account_liveness, "fetch_session", lambda _session: {"accessToken": "otp-at"})

    returned, info = account_liveness.authenticate_account_session(
        "user@example.com",
        login_password="chatgpt-password",
        otp_provider=lambda *_args, **_kwargs: "123456",
    )

    assert returned is session
    assert info["accessToken"] == "otp-at"
    assert session.authentication_method == "email_otp"


def test_account_session_does_not_hide_explicit_password_failure(monkeypatch):
    monkeypatch.setattr(
        chatgpt_browser_login,
        "authenticate_chatgpt_with_password",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("ChatGPT 密码错误")),
    )
    monkeypatch.setattr(
        account_liveness,
        "_network_preflight_with_retry",
        lambda *_args, **_kwargs: pytest.fail("explicit password errors must not fall back to email OTP"),
    )

    with pytest.raises(RuntimeError, match="密码错误"):
        account_liveness.authenticate_account_session(
            "user@example.com",
            login_password="wrong-password",
        )


def test_liveness_passes_password_and_closes_authenticated_session(monkeypatch, tmp_path):
    session = _Session()
    captured = []
    monkeypatch.setattr(account_liveness, "_LOG_DIR", tmp_path)

    def authenticate(email, **kwargs):
        captured.append((email, kwargs))
        session.authentication_method = "password"
        return session, {
            "accessToken": "live-at",
            "user": {"id": "user-id"},
            "account": {"planType": "free"},
        }

    monkeypatch.setattr(account_liveness, "authenticate_account_session", authenticate)

    result = account_liveness.check_account_liveness(
        "user@example.com",
        login_password="chatgpt-password",
        totp_provider=lambda: "123456",
    )

    assert result["ok"] is True
    assert result["auth_method"] == "password"
    assert captured[0][1]["login_password"] == "chatgpt-password"
    assert session.session.closed is True


def test_browser_password_login_requires_real_session_token(monkeypatch):
    from core import chrome_cdp_driver
    from core import roxy_codex_oauth
    from registration.drivers.roxy import implementation as registration_browser

    class Driver:
        def __init__(self):
            self.quit_called = False

        def quit(self):
            self.quit_called = True

    driver = Driver()
    opened = SimpleNamespace(raw={"proxy": "http://127.0.0.1:18080"})
    build_calls = []

    def build_driver(**kwargs):
        build_calls.append(kwargs)
        return driver, opened

    monkeypatch.setattr(chrome_cdp_driver, "build_chrome_cdp_driver", build_driver)
    monkeypatch.setattr(roxy_codex_oauth, "clear_roxy_browser_auth_state", lambda _driver: None)
    monkeypatch.setattr(roxy_codex_oauth, "_type_email_address", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(roxy_codex_oauth, "_submit_email_step", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(roxy_codex_oauth, "_submit_login_password", lambda *_args, **_kwargs: "submitted")
    monkeypatch.setattr(roxy_codex_oauth, "_post_password_challenge", lambda *_args, **_kwargs: "next")
    monkeypatch.setattr(registration_browser, "_safe_get", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(registration_browser, "_maybe_accept", lambda _driver: None)
    monkeypatch.setattr(
        registration_browser,
        "_fetch_chatgpt_session",
        lambda *_args, **_kwargs: {"accessToken": "browser-at"},
    )
    transferred = _Session()
    transferred_proxies = []

    def transfer_session(_driver, proxy):
        transferred_proxies.append(proxy)
        return transferred

    monkeypatch.setattr(chatgpt_browser_login, "_session_from_browser", transfer_session)
    monkeypatch.setattr(chatgpt_browser_login, "fetch_session", lambda _session: {"accessToken": "transferred-at"})

    session, info = chatgpt_browser_login.authenticate_chatgpt_with_password(
        "user@example.com",
        "chatgpt-password",
    )

    assert session is transferred
    assert info["accessToken"] == "transferred-at"
    assert build_calls == [{"proxy": None, "background": True, "keep_open": False}]
    assert transferred_proxies == ["http://127.0.0.1:18080"]
    assert driver.quit_called is True


def test_browser_password_login_rejects_transferred_session_without_token(monkeypatch):
    from core import chrome_cdp_driver
    from core import roxy_codex_oauth
    from registration.drivers.roxy import implementation as registration_browser

    class Driver:
        def __init__(self):
            self.quit_called = False

        def quit(self):
            self.quit_called = True

    driver = Driver()
    opened = SimpleNamespace(raw={"proxy": ""})
    transferred = _Session()
    monkeypatch.setattr(chrome_cdp_driver, "build_chrome_cdp_driver", lambda **_kwargs: (driver, opened))
    monkeypatch.setattr(roxy_codex_oauth, "clear_roxy_browser_auth_state", lambda _driver: None)
    monkeypatch.setattr(roxy_codex_oauth, "_type_email_address", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(roxy_codex_oauth, "_submit_email_step", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(roxy_codex_oauth, "_submit_login_password", lambda *_args, **_kwargs: "submitted")
    monkeypatch.setattr(roxy_codex_oauth, "_post_password_challenge", lambda *_args, **_kwargs: "next")
    monkeypatch.setattr(registration_browser, "_safe_get", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(registration_browser, "_maybe_accept", lambda _driver: None)
    monkeypatch.setattr(
        registration_browser,
        "_fetch_chatgpt_session",
        lambda *_args, **_kwargs: {"accessToken": "browser-at"},
    )
    monkeypatch.setattr(chatgpt_browser_login, "_session_from_browser", lambda *_args, **_kwargs: transferred)
    monkeypatch.setattr(chatgpt_browser_login, "fetch_session", lambda _session: {"user": {"id": "user-id"}})

    with pytest.raises(RuntimeError, match="HTTP 会话未拿到 ChatGPT accessToken"):
        chatgpt_browser_login.authenticate_chatgpt_with_password(
            "user@example.com",
            "chatgpt-password",
        )

    assert transferred.session.closed is True
    assert driver.quit_called is True
