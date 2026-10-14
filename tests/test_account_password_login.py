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


def test_browser_cookies_prefers_cdp_all_domains_for_roxy(monkeypatch):
    class Driver:
        def __init__(self):
            self.cdp_calls = []

        def execute_cdp_cmd(self, command, params):
            self.cdp_calls.append((command, params))
            return {
                "cookies": [
                    {"name": "chatgpt-session", "value": "chatgpt", "domain": "chatgpt.com", "path": "/"},
                    {"name": "auth-session", "value": "auth", "domain": "auth.openai.com", "path": "/"},
                ]
            }

        def get_cookies(self):
            raise AssertionError("current-domain Selenium cookies must not be used when CDP is available")

    driver = Driver()
    rows = chatgpt_browser_login._browser_cookies(driver)

    assert [row["name"] for row in rows] == ["chatgpt-session", "auth-session"]
    assert driver.cdp_calls == [("Network.getAllCookies", {})]


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


def test_account_session_never_falls_back_when_saved_password_page_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        chatgpt_browser_login,
        "authenticate_chatgpt_with_password",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            chatgpt_browser_login.PasswordLoginUnavailable("email OTP only")
        ),
    )
    monkeypatch.setattr(
        account_liveness,
        "_network_preflight_with_retry",
        lambda *_args, **_kwargs: pytest.fail("saved-password account must not request email OTP"),
    )

    with pytest.raises(chatgpt_browser_login.PasswordLoginUnavailable, match="已停止，未读取临时邮箱"):
        account_liveness.authenticate_account_session(
            "user@example.com",
            login_password="chatgpt-password",
            totp_provider=lambda: "123456",
            otp_provider=lambda *_args, **_kwargs: pytest.fail("must not read expired email"),
        )


def test_account_session_without_password_still_accepts_email_otp(monkeypatch):
    session = _Session()
    monkeypatch.setattr(
        chatgpt_browser_login,
        "authenticate_chatgpt_with_password",
        lambda *_args, **_kwargs: pytest.fail("no saved password must not use browser password login"),
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


@pytest.mark.parametrize(
    "info",
    [
        {"accessToken": "verified-at", "user": {}},
        {"accessToken": "verified-at", "user": {"email": "other@example.com"}},
    ],
)
def test_verify_password_totp_login_requires_matching_account_email(monkeypatch, info):
    session = _Session()
    monkeypatch.setattr(
        chatgpt_browser_login,
        "authenticate_chatgpt_with_password",
        lambda *_args, **_kwargs: (session, info),
    )

    with pytest.raises(RuntimeError, match="(未返回账号邮箱|不一致)"):
        chatgpt_browser_login.verify_password_totp_login(
            "user@example.com",
            "chatgpt-password",
            "TOTP-SECRET",
        )

    assert session.session.closed is True


def test_verify_password_totp_login_accepts_matching_account_email(monkeypatch):
    session = _Session()
    monkeypatch.setattr(
        chatgpt_browser_login,
        "authenticate_chatgpt_with_password",
        lambda *_args, **_kwargs: (
            session,
            {"accessToken": "verified-at", "user": {"email": "USER@example.com"}},
        ),
    )

    info = chatgpt_browser_login.verify_password_totp_login(
        "user@example.com",
        "chatgpt-password",
        "TOTP-SECRET",
    )

    assert info["accessToken"] == "verified-at"
    assert session.session.closed is True


def test_strict_browser_login_rejects_password_session_that_skips_totp(monkeypatch):
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
    monkeypatch.setattr(chrome_cdp_driver, "build_chrome_cdp_driver", lambda **_kwargs: (driver, opened))
    monkeypatch.setattr(roxy_codex_oauth, "clear_roxy_browser_auth_state", lambda _driver: None)
    monkeypatch.setattr(roxy_codex_oauth, "_type_email_address", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(roxy_codex_oauth, "_submit_email_step", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(roxy_codex_oauth, "_submit_login_password", lambda *_args, **_kwargs: "submitted")
    challenge_calls = []

    def skip_totp(_driver, provider, **_kwargs):
        challenge_calls.append(provider)
        return "next"

    monkeypatch.setattr(roxy_codex_oauth, "_post_password_challenge", skip_totp)
    monkeypatch.setattr(registration_browser, "_safe_get", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(registration_browser, "_maybe_accept", lambda _driver: None)
    monkeypatch.setattr(
        registration_browser,
        "_fetch_chatgpt_session",
        lambda *_args, **_kwargs: pytest.fail("login without a TOTP challenge must not be accepted"),
    )

    with pytest.raises(RuntimeError, match="未出现并完成 TOTP challenge"):
        chatgpt_browser_login.authenticate_chatgpt_with_password(
            "user@example.com",
            "chatgpt-password",
            totp_provider=lambda: "123456",
            require_totp=True,
        )

    assert len(challenge_calls) == 1
    assert callable(challenge_calls[0])
    assert driver.quit_called is True


@pytest.fixture
def existing_browser_login(monkeypatch):
    """Exercise the real verifier without launching a browser or reading mail."""
    from core import chrome_cdp_driver, roxy_codex_oauth
    from registration.drivers.roxy import implementation as registration_browser

    events = []
    driver = SimpleNamespace(quit=lambda: pytest.fail("the supplied browser is caller-owned"))
    state = {
        "password": "submitted",
        "challenge": "next",
        "submit_totp": True,
        "info": {"accessToken": "verified-at", "user": {"email": "USER@example.com"}},
    }
    monkeypatch.setattr(chrome_cdp_driver, "build_chrome_cdp_driver", lambda **_kw: pytest.fail("must stay in Roxy"))
    monkeypatch.setattr(chatgpt_browser_login, "_session_from_browser", lambda *_a, **_kw: pytest.fail("must not transfer cookies"))
    monkeypatch.setattr(chatgpt_browser_login, "fetch_session", lambda *_a, **_kw: pytest.fail("must not use an HTTP session"))
    monkeypatch.setattr(roxy_codex_oauth, "clear_roxy_browser_auth_state", lambda d: events.append(("clear", d)))
    monkeypatch.setattr(registration_browser, "_safe_get", lambda d, *_a, **_kw: events.append(("navigate", d)))
    monkeypatch.setattr(registration_browser, "_maybe_accept", lambda _d: None)
    monkeypatch.setattr(roxy_codex_oauth, "_type_email_address", lambda *_a, **_kw: None)
    monkeypatch.setattr(roxy_codex_oauth, "_submit_email_step", lambda *_a, **_kw: None)
    monkeypatch.setattr(roxy_codex_oauth, "_submit_login_password", lambda *_a, **_kw: state["password"])

    def challenge(d, provider, **_kwargs):
        if state["submit_totp"]:
            assert len(provider()) == 6
            events.append(("totp", d))
        return state["challenge"]

    monkeypatch.setattr(roxy_codex_oauth, "_post_password_challenge", challenge)
    monkeypatch.setattr(registration_browser, "_fetch_chatgpt_session", lambda *_a, **_kw: state["info"])
    return driver, state, events


def test_verify_password_totp_stays_in_existing_browser(existing_browser_login):
    driver, state, events = existing_browser_login
    info = chatgpt_browser_login.verify_password_totp_login(
        "user@example.com", "password", "JBSWY3DPEHPK3PXP", driver=driver,
    )
    assert info == state["info"]
    assert events == [("clear", driver), ("navigate", driver), ("totp", driver)]


@pytest.mark.parametrize("changes, message", [
    ({"password": "email_otp"}, "未提供密码登录页"),
    ({"password": "next"}, "无法证明密码"),
    ({"challenge": "email_otp"}, "仍要求邮箱验证码"),
    ({"submit_totp": False}, "未出现并完成 TOTP"),
    ({"info": {"user": {"email": "user@example.com"}}}, "未拿到 ChatGPT accessToken"),
    ({"info": {"accessToken": "at", "user": {}}}, "未返回账号邮箱"),
    ({"info": {"accessToken": "at", "user": {"email": "other@example.com"}}}, "不一致"),
])
def test_existing_browser_verifier_rejects_incomplete_login(existing_browser_login, changes, message):
    driver, state, _events = existing_browser_login
    state.update(changes)
    with pytest.raises(RuntimeError, match=message):
        chatgpt_browser_login.verify_password_totp_login(
            "user@example.com", "password", "JBSWY3DPEHPK3PXP", driver=driver,
        )


def test_email_otp_preflight_keeps_explicit_direct_route(monkeypatch):
    calls = []
    session = _Session()
    monkeypatch.setattr(account_liveness, "BrowserSession", lambda *, proxy: calls.append(proxy) or session)
    monkeypatch.setattr(account_liveness, "get_providers", lambda _s: None)
    monkeypatch.setattr(account_liveness, "get_csrf_token", lambda _s: "csrf")
    monkeypatch.setattr(account_liveness, "signin_openai", lambda *_a: "authorize")
    returned, _url = account_liveness._network_preflight_with_retry("user@example.com", "")
    assert returned is session
    assert calls == [""]
