import json
from types import SimpleNamespace

import pytest

from core import email_provider
from core.roxybrowser_client import RoxyOpenResult
from registration.application import job_service
from registration.drivers.roxy import implementation


class _Driver:
    def __init__(self):
        self.quit_called = False
        self.page_load_timeout = None
        self.script_timeout = None

    def set_page_load_timeout(self, timeout):
        self.page_load_timeout = timeout

    def set_script_timeout(self, timeout):
        self.script_timeout = timeout

    def quit(self):
        self.quit_called = True


class _Client:
    def __init__(self, opened):
        self.opened = opened
        self.cleaned = []

    def open_profile(self):
        return self.opened

    def cleanup_profile(self, opened):
        self.cleaned.append(opened)


def _patch_roxy_registration_shell(monkeypatch, *, next_state="password"):
    driver = _Driver()
    opened = RoxyOpenResult(
        profile_id="roxy-profile",
        raw={"proxy": "http://profile-proxy.example:8080"},
    )
    client = _Client(opened)

    monkeypatch.setattr(implementation, "RoxyBrowserClient", lambda: client)
    monkeypatch.setattr(implementation, "_build_driver", lambda _opened: driver)
    monkeypatch.setattr(implementation, "_center_browser_window", lambda _driver: None)
    monkeypatch.setattr(implementation, "_safe_get", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(implementation, "_page_warmup", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(implementation, "_maybe_accept", lambda _driver: None)
    monkeypatch.setattr(implementation, "_check_manual_stop", lambda: None)
    monkeypatch.setattr(implementation, "human_delay", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        implementation,
        "_submit_email_and_wait_next",
        lambda *_args, **_kwargs: next_state,
    )
    monkeypatch.setattr(implementation, "_is_email_verification_page", lambda _driver: False)
    monkeypatch.setattr(implementation, "_complete_profile_page", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        implementation,
        "_fetch_chatgpt_session",
        lambda *_args, **_kwargs: {
            "accessToken": "chatgpt-access-token",
            "user": {"id": "user-id", "name": "Test User"},
            "account": {"planType": "free"},
            "expires": "2099-01-01T00:00:00Z",
        },
    )
    monkeypatch.setattr(implementation, "resolve_email_source", lambda _email: "mailnest")
    monkeypatch.setattr(implementation._cfg, "ROXY_KEEP_BROWSER_OPEN", False)
    return driver, opened, client


def test_roxy_registration_with_2fa_persists_password_and_totp(monkeypatch):
    driver, opened, client = _patch_roxy_registration_shell(monkeypatch)
    saves = []
    password_calls = []
    setup_calls = []
    verification_calls = []

    monkeypatch.setattr(
        implementation, "verify_password_totp_login",
        lambda *args, **kwargs: (
            verification_calls.append((args, kwargs)),
            {"accessToken": "verified-at", "user": {"email": "user@example.com"}},
        )[1],
    )
    monkeypatch.setattr(implementation._twofa_cfg, "ENABLE_2FA", True)
    monkeypatch.setattr(
        implementation,
        "_fill_password_page_if_present",
        lambda *args, **kwargs: (
            password_calls.append((args, kwargs)),
            "Generated-Password-42!",
        )[1],
    )
    monkeypatch.setattr(
        implementation,
        "_setup_2fa_after_registration",
        lambda *args, **kwargs: (
            setup_calls.append((args, kwargs)),
            ("TOTP-SECRET", "socks5h://profile-route.example:8080"),
        )[1],
    )

    def save(**kwargs):
        saves.append(kwargs)
        return f"account-{len(saves)}"

    monkeypatch.setattr(implementation, "save_account_data", save)

    result = implementation.run_roxy_registration(
        "user@example.com",
        "Test User",
        "1990-01-02",
        proxy="http://requested-route.example:8080",
    )

    assert result["success"] is True
    assert result["account_id"] == "account-1"
    assert result["totp_secret"] == "TOTP-SECRET"
    assert driver.quit_called is True
    assert client.cleaned == [opened]

    assert len(password_calls) == 1
    assert password_calls[0][1] == {"timeout": 25, "allow_passwordless_signup": False}
    assert len(setup_calls) == 1
    assert setup_calls[0][0][0] is driver
    assert setup_calls[0][0][1] == "user@example.com"
    assert setup_calls[0][0][2] is opened
    assert setup_calls[0][0][3] is client
    assert setup_calls[0][0][4] == "http://requested-route.example:8080"
    assert verification_calls == [(
        ("user@example.com", "Generated-Password-42!", "TOTP-SECRET"),
        {"proxy": "socks5h://profile-route.example:8080", "driver": driver},
    )]

    # The only write happens after the fresh password + TOTP login
    # succeeds, so the account store never contains a password-only checkpoint.
    assert len(saves) == 1
    assert saves[0]["archive"] is True
    assert saves[0]["enqueue_plan"] is True
    assert saves[0]["chatgpt_password"] == "Generated-Password-42!"
    assert saves[0]["totp_secret"] == "TOTP-SECRET"
    assert saves[0]["login_method"] == "password"
    assert saves[0]["proxy"] == "socks5h://profile-route.example:8080"
    assert saves[0]["access_token"] == "verified-at"


def test_roxy_registration_2fa_failure_does_not_persist_password_checkpoint(monkeypatch):
    driver, opened, client = _patch_roxy_registration_shell(monkeypatch)
    saves = []
    releases = []

    monkeypatch.setattr(implementation._twofa_cfg, "ENABLE_2FA", True)
    monkeypatch.setattr(
        implementation,
        "_fill_password_page_if_present",
        lambda *_args, **_kwargs: "Generated-Password-42!",
    )
    monkeypatch.setattr(
        implementation,
        "_setup_2fa_after_registration",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("TOTP enrollment failed")),
    )
    monkeypatch.setattr(
        implementation,
        "save_account_data",
        lambda **kwargs: saves.append(kwargs) or "account-checkpoint",
    )
    monkeypatch.setattr(
        email_provider,
        "release_email",
        lambda email, **kwargs: releases.append((email, kwargs)),
    )

    result = implementation.run_roxy_registration("user@example.com", "Test User", "1990-01-02")

    assert result["success"] is False
    assert "2FA 设置失败" in result["error"]
    assert "TOTP enrollment failed" in result["error"]
    assert driver.quit_called is True
    assert client.cleaned == [opened]

    assert saves == []
    assert releases == [
        (
            "user@example.com",
            {
                "status": "failed",
                "note": "Roxy注册失败: Roxy 注册已创建密码，但 2FA 设置失败：RuntimeError: TOTP enrollment failed",
            },
        )
    ]


def test_roxy_registration_switches_from_otp_to_password_when_2fa_enabled(monkeypatch):
    driver, opened, client = _patch_roxy_registration_shell(monkeypatch, next_state="otp")
    password_calls = []
    switch_calls = []
    saves = []

    monkeypatch.setattr(implementation._twofa_cfg, "ENABLE_2FA", True)
    monkeypatch.setattr(
        implementation,
        "_click_continue_with_password_if_present",
        lambda *args, **kwargs: (
            switch_calls.append((args, kwargs)),
            {"ok": True, "reason": "clicked_continue_with_password"},
        )[1],
    )
    monkeypatch.setattr(
        implementation,
        "_fill_password_page_if_present",
        lambda *args, **kwargs: (
            password_calls.append((args, kwargs)),
            "Generated-Password-42!",
        )[1],
    )
    monkeypatch.setattr(implementation, "_is_email_verification_page", lambda _driver: False)
    monkeypatch.setattr(
        implementation,
        "_setup_2fa_after_registration",
        lambda *_args, **_kwargs: ("TOTP-SECRET", "http://profile-proxy.example:8080"),
    )
    monkeypatch.setattr(
        implementation,
        "verify_password_totp_login",
        lambda *_args, **_kwargs: {"accessToken": "verified-at"},
    )
    monkeypatch.setattr(
        implementation,
        "save_account_data",
        lambda **kwargs: saves.append(kwargs) or f"account-{len(saves)}",
    )
    monkeypatch.setattr(
        implementation,
        "wait_for_otp",
        lambda *_args, **_kwargs: pytest.fail("密码注册分支不应领取邮箱 OTP"),
    )

    result = implementation.run_roxy_registration("user@example.com", "Test User", "1990-01-02")

    assert result["success"] is True
    assert switch_calls == [((driver,), {})]
    assert len(password_calls) == 1
    assert password_calls[0][0][0] is driver
    assert password_calls[0][1] == {"timeout": 25, "allow_passwordless_signup": False}
    assert saves[0]["chatgpt_password"] == "Generated-Password-42!"
    assert saves[0]["login_method"] == "password"
    assert driver.quit_called is True
    assert client.cleaned == [opened]


def test_roxy_registration_stops_before_otp_when_2fa_has_no_password(monkeypatch):
    driver, opened, client = _patch_roxy_registration_shell(monkeypatch, next_state="otp")
    saves = []
    releases = []

    monkeypatch.setattr(implementation._twofa_cfg, "ENABLE_2FA", True)
    monkeypatch.setattr(
        implementation,
        "wait_for_otp",
        lambda *_args, **_kwargs: pytest.fail("启用 2FA 时没有密码，不应领取邮箱 OTP"),
    )
    monkeypatch.setattr(
        implementation,
        "_setup_2fa_after_registration",
        lambda *_args, **_kwargs: pytest.fail("没有密码时不应进入 2FA 设置"),
    )
    monkeypatch.setattr(implementation, "save_account_data", lambda **kwargs: saves.append(kwargs))
    monkeypatch.setattr(
        email_provider,
        "release_email",
        lambda email, **kwargs: releases.append((email, kwargs)),
    )

    result = implementation.run_roxy_registration("user@example.com", "Test User", "1990-01-02")

    assert result["success"] is False
    assert "未设置密码" in result["error"]
    assert saves == []
    assert releases and releases[0][0] == "user@example.com"
    assert releases[0][1]["status"] == "available"
    assert driver.quit_called is True
    assert client.cleaned == [opened]


def test_setup_2fa_after_registration_uses_live_browser_session_with_profile_proxy(monkeypatch):
    from core import browser_2fa_session

    class _Session:
        pass

    driver = object()
    opened = RoxyOpenResult(profile_id="roxy-profile", raw={})
    client = object()
    session = _Session()
    session_calls = []
    proxy_calls = []
    setup_calls = []

    def resolve_profile_proxy(current_opened, current_client, requested_proxy):
        proxy_calls.append((current_opened, current_client, requested_proxy))
        return "socks5h://profile-route.example:8080"

    def session_factory(current_driver):
        session_calls.append(current_driver)
        return session

    def setup(current_session, email, **kwargs):
        setup_calls.append((current_session, email, kwargs))
        return "TOTP-SECRET"

    monkeypatch.setattr(implementation, "_profile_proxy_for_2fa", resolve_profile_proxy)
    monkeypatch.setattr(browser_2fa_session, "BrowserBacked2FASession", session_factory)
    monkeypatch.setattr(implementation, "setup_2fa", setup)

    result = implementation._setup_2fa_after_registration(
        driver,
        "user@example.com",
        opened,
        client,
        "http://requested-route.example:8080",
    )

    assert result == ("TOTP-SECRET", "socks5h://profile-route.example:8080")
    assert proxy_calls == [(opened, client, "http://requested-route.example:8080")]
    assert session_calls == [driver]
    assert setup_calls[0][0] is session
    assert setup_calls[0][1] == "user@example.com"
    assert setup_calls[0][2]["otp_provider"] is implementation.wait_for_otp


def test_registration_request_monitor_keeps_only_response_metadata():
    class Driver:
        def __init__(self):
            self.events = [
                [],
                [
                    {"message": json.dumps({"message": {
                        "method": "Network.requestWillBeSent",
                        "params": {"requestId": "r1", "request": {
                            "url": "https://auth.openai.com/api/accounts/user/register?code=private",
                            "method": "POST", "postData": "password=private",
                        }},
                    }})},
                    {"message": json.dumps({"message": {
                        "method": "Network.responseReceived",
                        "params": {"requestId": "r1", "response": {
                            "url": "https://auth.openai.com/api/accounts/user/register?code=private",
                            "status": 403,
                            "headers": {
                                "Content-Type": "text/html; charset=UTF-8", "Set-Cookie": "private",
                                "Server-Timing": "chlray;desc=private",
                            },
                        }},
                    }})},
                ],
            ]

        def get_log(self, _kind):
            return self.events.pop(0) if self.events else []

    monitor = implementation._RegistrationRequestMonitor(Driver())
    assert monitor.poll() == {
        "method": "POST", "status": 403, "content_type": "text/html",
        "challenge_detected": True,
    }
    assert "private" not in str(monitor.last_response)


def test_password_register_403_stops_immediately_and_disables_mailbox(monkeypatch):
    driver, _opened, _client = _patch_roxy_registration_shell(monkeypatch)
    releases = []
    saves = []

    class Input:
        value = ""

        def get_attribute(self, _name):
            return self.value

    password_input = Input()
    driver.execute_script = lambda *_args: {"ok": True, "input": password_input, "button": object()}
    monkeypatch.setattr(implementation._twofa_cfg, "ENABLE_2FA", True)
    monkeypatch.setattr(implementation, "_password_page_state", lambda _driver: {"url": "/create-account/password"})
    monkeypatch.setattr(implementation, "_is_signup_password_page", lambda _driver: True)
    monkeypatch.setattr(implementation, "_is_login_password_page", lambda _driver: False)
    monkeypatch.setattr(implementation, "_registration_password", lambda: "correct-generated-password")
    monkeypatch.setattr(implementation, "_human_type_text", lambda _driver, el, value, **_kwargs: setattr(el, "value", value))
    monkeypatch.setattr(implementation, "_human_click", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(implementation, "_password_submission_route_error", lambda _driver: False)
    monkeypatch.setattr(
        implementation, "_RegistrationRequestMonitor",
        lambda _driver: SimpleNamespace(poll=lambda: {
            "method": "POST", "status": 403, "content_type": "text/html",
            "challenge_detected": True,
        }, close=lambda: None),
    )
    monkeypatch.setattr(implementation, "save_account_data", lambda **kwargs: saves.append(kwargs))
    monkeypatch.setattr(email_provider, "release_email", lambda email, **kwargs: releases.append((email, kwargs)))

    result = implementation.run_roxy_registration("user@example.com", "Test User", "1990-01-02")

    assert result["success"] is False
    assert "HTTP 403 Content-Type=text/html" in result["error"]
    assert "Cloudflare challenge" in result["error"]
    assert "请勿重用此邮箱" in result["error"]
    assert releases[0][1]["status"] == "disabled"
    assert job_service._should_disable_failed_registration_email(result["error"])
    assert saves == []


@pytest.mark.parametrize("error, expected", [
    (RuntimeError("net::ERR_SSL_PROTOCOL_ERROR https://name:private@proxy.example"), "ERR_SSL_PROTOCOL_ERROR"),
    (RuntimeError("账号已删除或停用（account_deactivated）"), "account_deactivated"),
])
def test_roxy_relogin_failure_is_not_saved_and_preserves_reason(monkeypatch, error, expected):
    driver, opened, client = _patch_roxy_registration_shell(monkeypatch)
    monkeypatch.setattr(implementation._twofa_cfg, "ENABLE_2FA", True)
    monkeypatch.setattr(implementation, "_fill_password_page_if_present", lambda *_a, **_kw: "password")
    monkeypatch.setattr(implementation, "_setup_2fa_after_registration", lambda *_a, **_kw: ("TOTP", ""))
    monkeypatch.setattr(implementation, "save_account_data", lambda **_kw: pytest.fail("unverified account must not be saved"))
    releases = []
    monkeypatch.setattr(email_provider, "release_email", lambda *_a, **kw: releases.append(kw))

    def verify(*_args, **kwargs):
        assert kwargs["driver"] is driver
        assert kwargs["proxy"] == ""
        raise error

    monkeypatch.setattr(implementation, "verify_password_totp_login", verify)
    result = implementation.run_roxy_registration("user@example.com", "User", "1990-01-02")
    assert result["success"] is False
    assert expected in result["error"]
    assert "private" not in result["error"]
    assert releases[0]["status"] == "failed"
    assert driver.quit_called and client.cleaned == [opened]


def test_roxy_without_2fa_keeps_original_email_otp_flow(monkeypatch):
    driver, opened, _client = _patch_roxy_registration_shell(monkeypatch, next_state="otp")
    monkeypatch.setattr(implementation._twofa_cfg, "ENABLE_2FA", False)
    opened.proxy = ""
    for name in ("_fill_password_page_if_present", "_setup_2fa_after_registration", "verify_password_totp_login"):
        monkeypatch.setattr(implementation, name, lambda *_a, **_kw: pytest.fail("OTP-only registration must not require password/2FA"))
    monkeypatch.setattr(implementation, "_is_email_verification_page", lambda _d: True)
    monkeypatch.setattr(implementation, "wait_for_otp", lambda *_a, **_kw: "123456")
    monkeypatch.setattr(implementation, "_clear_otp_inputs", lambda _d: None)
    codes = []
    monkeypatch.setattr(implementation, "_type_otp", lambda _d, code: codes.append(code))
    monkeypatch.setattr(implementation, "_click_continue", lambda _d: None)
    monkeypatch.setattr(implementation, "_wait_after_email_otp_submit", lambda *_a, **_kw: "accepted")
    saves = []
    monkeypatch.setattr(implementation, "save_account_data", lambda **kw: saves.append(kw) or "account-otp")

    result = implementation.run_roxy_registration("user@example.com", "User", "1990-01-02")
    assert result["success"] is True
    assert codes == ["123456"]
    assert len(saves) == 1
    assert saves[0]["login_method"] == "email_otp"
    assert saves[0]["chatgpt_password"] is None
    assert saves[0]["totp_secret"] is None
    assert saves[0]["proxy"] == ""
