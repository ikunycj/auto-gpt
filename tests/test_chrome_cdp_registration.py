from types import SimpleNamespace

import pytest

from registration.drivers.chrome_cdp import implementation


class _Driver:
    def __init__(self):
        self.urls = []
        self.quit_called = False

    def get(self, url):
        self.urls.append(url)

    def quit(self):
        self.quit_called = True


@pytest.mark.parametrize(
    (
        "next_state",
        "created_password",
        "expected_method",
        "expected_otp_calls",
        "enable_2fa",
        "expected_totp",
    ),
    [
        ("password", "Generated-Password-42", "password", 1, False, None),
        ("otp", None, "email_otp", 1, False, None),
        ("logged_in", None, "email_otp", 0, False, None),
        ("password", "Generated-Password-42", "password", 1, True, "TOTP-SECRET"),
    ],
)
def test_chrome_registration_persists_confirmed_password_and_optional_2fa(
    monkeypatch,
    next_state,
    created_password,
    expected_method,
    expected_otp_calls,
    enable_2fa,
    expected_totp,
):
    driver = _Driver()
    opened = SimpleNamespace(
        profile_id="chrome-cdp",
        raw={"driver": "chrome_cdp", "proxy": "http://127.0.0.1:18080"},
    )
    saves = []
    password_calls = []
    otp_calls = []
    twofa_calls = []
    verification_calls = []

    monkeypatch.setattr(
        implementation,
        "verify_password_totp_login",
        lambda *args, **kwargs: (
            verification_calls.append((args, kwargs)),
            {"accessToken": "verified-at", "user": {"email": "user@example.com"}},
        )[1],
    )

    monkeypatch.setattr(
        implementation,
        "build_chrome_cdp_driver",
        lambda **kwargs: (driver, opened),
    )
    monkeypatch.setattr(implementation, "human_delay", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(implementation, "_maybe_accept", lambda _driver: None)
    monkeypatch.setattr(implementation, "_check_manual_stop", lambda: None)
    monkeypatch.setattr(
        implementation,
        "_submit_email_and_wait_next",
        lambda *_args, **_kwargs: next_state,
    )

    def fill_password(*_args, **_kwargs):
        password_calls.append(True)
        return created_password

    monkeypatch.setattr(implementation, "_fill_password_page_if_present", fill_password)
    monkeypatch.setattr(implementation, "_has_access_token", lambda _driver: False)
    monkeypatch.setattr(
        implementation,
        "_is_email_verification_page",
        lambda _driver: next_state == "password",
    )
    monkeypatch.setattr(implementation, "_clear_otp_inputs", lambda _driver: None)
    monkeypatch.setattr(implementation, "_type_otp", lambda *_args: otp_calls.append(True))
    monkeypatch.setattr(implementation, "_click_continue", lambda _driver: None)
    monkeypatch.setattr(
        implementation,
        "_wait_after_email_otp_submit",
        lambda *_args, **_kwargs: "accepted",
    )
    monkeypatch.setattr(
        implementation,
        "_complete_profile_page",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        implementation,
        "_fetch_chatgpt_session",
        lambda *_args, **_kwargs: {
            "accessToken": "chatgpt-at",
            "user": {"id": "user-id"},
            "account": {"planType": "free"},
        },
    )
    monkeypatch.setattr(implementation, "resolve_email_source", lambda _email: "mailnest")
    monkeypatch.setattr(implementation._twofa_cfg, "ENABLE_2FA", enable_2fa)
    monkeypatch.setattr(
        implementation,
        "_setup_2fa_after_registration",
        lambda *args, **kwargs: (
            twofa_calls.append((args, kwargs)),
            expected_totp,
        )[1],
    )

    def save(**kwargs):
        saves.append(kwargs)
        return "account-id"

    monkeypatch.setattr(implementation, "save_account_data", save)

    result = implementation.run_chrome_cdp_registration(
        "user@example.com",
        "User",
        "1990-01-02",
        otp_code="123456",
    )

    assert result["success"] is True
    assert result["account_id"] == "account-id"
    assert driver.urls == ["https://chatgpt.com/auth/login"]
    assert driver.quit_called is True
    # The only account write is the final archive after all required security
    # steps.  In particular, 2FA-enabled registration must not leave a
    # password-only checkpoint in the account store.
    assert len(saves) == 1
    assert saves[0]["archive"] is True
    assert saves[0]["enqueue_plan"] is True
    assert saves[0]["proxy"] == "http://127.0.0.1:18080"
    assert saves[0]["chatgpt_password"] == created_password
    assert saves[0]["login_method"] == expected_method
    assert saves[0]["registration_driver"] == "chrome_cdp"
    assert saves[0]["registration_profile_id"] == "chrome-cdp"
    assert "extra" not in saves[0]
    assert len(twofa_calls) == (1 if enable_2fa else 0)
    assert len(verification_calls) == (1 if enable_2fa else 0)
    if enable_2fa:
        assert verification_calls[0] == (
            ("user@example.com", "Generated-Password-42", "TOTP-SECRET"),
            {"proxy": "http://127.0.0.1:18080"},
        )
        assert saves[0]["totp_secret"] == expected_totp
        assert saves[0]["access_token"] == "verified-at"
    assert len(password_calls) == (1 if next_state == "password" else 0)
    assert len(otp_calls) == expected_otp_calls


def test_setup_2fa_after_registration_uses_live_browser_session(monkeypatch):
    from core import browser_2fa_session

    class _Session:
        pass

    session = _Session()
    session_calls = []
    setup_calls = []

    def session_factory(driver):
        session_calls.append(driver)
        return session

    def setup(current, email, **kwargs):
        setup_calls.append((current, email, kwargs))
        return "TOTP-SECRET"

    monkeypatch.setattr(browser_2fa_session, "BrowserBacked2FASession", session_factory)
    monkeypatch.setattr(implementation, "setup_2fa", setup)

    assert implementation._setup_2fa_after_registration(
        "chrome-driver",
        "user@example.com",
        "socks5://127.0.0.1:7897",
    ) == "TOTP-SECRET"
    assert session_calls == ["chrome-driver"]
    assert setup_calls[0][0] is session
    assert setup_calls[0][1] == "user@example.com"
    assert setup_calls[0][2]["otp_provider"] is implementation.wait_for_otp


def test_chrome_registration_does_not_release_email_after_otp_was_accepted(
    monkeypatch,
):
    from core import email_provider

    driver = _Driver()
    opened = SimpleNamespace(profile_id="chrome-cdp", raw={"proxy": ""})
    releases = []

    monkeypatch.setattr(
        implementation,
        "build_chrome_cdp_driver",
        lambda **_kwargs: (driver, opened),
    )
    monkeypatch.setattr(implementation, "human_delay", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(implementation._twofa_cfg, "ENABLE_2FA", False)
    monkeypatch.setattr(implementation, "_maybe_accept", lambda _driver: None)
    monkeypatch.setattr(implementation, "_check_manual_stop", lambda: None)
    monkeypatch.setattr(
        implementation,
        "_submit_email_and_wait_next",
        lambda *_args, **_kwargs: "otp",
    )
    monkeypatch.setattr(implementation, "_clear_otp_inputs", lambda _driver: None)
    monkeypatch.setattr(implementation, "_type_otp", lambda *_args: None)
    monkeypatch.setattr(implementation, "_click_continue", lambda _driver: None)
    monkeypatch.setattr(
        implementation,
        "_wait_after_email_otp_submit",
        lambda *_args, **_kwargs: "accepted",
    )
    monkeypatch.setattr(
        implementation,
        "_complete_profile_page",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        implementation,
        "_fetch_chatgpt_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("session timeout")),
    )
    monkeypatch.setattr(
        email_provider,
        "release_email",
        lambda email, **kwargs: releases.append((email, kwargs)),
    )

    result = implementation.run_chrome_cdp_registration(
        "user@example.com",
        "User",
        "1990-01-02",
        otp_code="123456",
    )

    assert result["success"] is False
    assert releases == [
        (
            "user@example.com",
            {
                "status": "failed",
                "note": "Chrome注册失败: session timeout",
            },
        )
    ]
    assert driver.quit_called is True


def test_chrome_registration_switches_from_otp_to_password_when_2fa_enabled(monkeypatch):
    from core import email_provider

    driver = _Driver()
    opened = SimpleNamespace(profile_id="chrome-cdp", raw={"proxy": ""})
    switch_calls = []
    password_calls = []
    saves = []

    monkeypatch.setattr(implementation._twofa_cfg, "ENABLE_2FA", True)
    monkeypatch.setattr(implementation, "build_chrome_cdp_driver", lambda **_kwargs: (driver, opened))
    monkeypatch.setattr(implementation, "human_delay", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(implementation, "_maybe_accept", lambda _driver: None)
    monkeypatch.setattr(implementation, "_check_manual_stop", lambda: None)
    monkeypatch.setattr(implementation, "_submit_email_and_wait_next", lambda *_args, **_kwargs: "otp")
    monkeypatch.setattr(
        implementation,
        "_click_continue_with_password_if_present",
        lambda *args, **kwargs: (
            switch_calls.append((args, kwargs)),
            {"ok": True, "reason": "clicked_continue_with_password"},
        )[1],
        raising=False,
    )
    monkeypatch.setattr(
        implementation,
        "_fill_password_page_if_present",
        lambda *args, **kwargs: (
            password_calls.append((args, kwargs)),
            "Generated-Password-42!",
        )[1],
    )
    monkeypatch.setattr(implementation, "_has_access_token", lambda _driver: False)
    monkeypatch.setattr(implementation, "_is_email_verification_page", lambda _driver: False)
    monkeypatch.setattr(
        implementation,
        "_complete_profile_page",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        implementation,
        "_fetch_chatgpt_session",
        lambda *_args, **_kwargs: {
            "accessToken": "chatgpt-at",
            "user": {"id": "user-id"},
            "account": {"planType": "free"},
        },
    )
    monkeypatch.setattr(implementation, "resolve_email_source", lambda _email: "mailnest")
    monkeypatch.setattr(
        implementation,
        "_setup_2fa_after_registration",
        lambda *_args, **_kwargs: "TOTP-SECRET",
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
    monkeypatch.setattr(
        email_provider,
        "release_email",
        lambda *_args, **_kwargs: pytest.fail("成功注册后不应释放邮箱"),
    )

    result = implementation.run_chrome_cdp_registration("user@example.com", "User", "1990-01-02")

    assert result["success"] is True
    assert switch_calls == [((driver,), {})]
    assert len(password_calls) == 1
    assert password_calls[0][0][0] is driver
    assert password_calls[0][1] == {"timeout": 25, "allow_passwordless_signup": False}
    assert len(saves) == 1
    assert saves[0]["archive"] is True
    assert saves[0]["enqueue_plan"] is True
    assert saves[0]["chatgpt_password"] == "Generated-Password-42!"
    assert saves[0]["login_method"] == "password"
    assert driver.quit_called is True


def test_chrome_registration_requires_password_before_using_mailbox_when_2fa_enabled(monkeypatch):
    from core import email_provider

    driver = _Driver()
    opened = SimpleNamespace(profile_id="chrome-cdp", raw={"proxy": ""})
    saves, releases = [], []
    monkeypatch.setattr(implementation._twofa_cfg, "ENABLE_2FA", True)
    monkeypatch.setattr(implementation, "build_chrome_cdp_driver", lambda **_kwargs: (driver, opened))
    monkeypatch.setattr(implementation, "human_delay", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(implementation, "_maybe_accept", lambda _driver: None)
    monkeypatch.setattr(implementation, "_check_manual_stop", lambda: None)
    monkeypatch.setattr(implementation, "_submit_email_and_wait_next", lambda *_args, **_kwargs: "otp")
    monkeypatch.setattr(implementation, "wait_for_otp", lambda *_args, **_kwargs: pytest.fail("不能领取 OTP-only 邮箱码"))
    monkeypatch.setattr(implementation, "save_account_data", lambda **kwargs: saves.append(kwargs))
    monkeypatch.setattr(implementation, "_setup_2fa_after_registration", lambda *_args: pytest.fail("没有密码不能设置 2FA"))
    monkeypatch.setattr(email_provider, "release_email", lambda email, **kwargs: releases.append((email, kwargs)))

    result = implementation.run_chrome_cdp_registration("user@example.com", "User", "1990-01-02")

    assert result["success"] is False
    assert "未设置密码" in result["error"]
    assert saves == []
    assert releases[0][1]["status"] == "available"
    assert driver.quit_called is True


def test_chrome_registration_2fa_failure_does_not_persist_password_checkpoint(monkeypatch):
    driver = _Driver()
    opened = SimpleNamespace(profile_id="chrome-cdp", raw={"proxy": ""})
    saves = []
    monkeypatch.setattr(implementation._twofa_cfg, "ENABLE_2FA", True)
    monkeypatch.setattr(implementation, "build_chrome_cdp_driver", lambda **_kwargs: (driver, opened))
    monkeypatch.setattr(implementation, "human_delay", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(implementation, "_maybe_accept", lambda _driver: None)
    monkeypatch.setattr(implementation, "_check_manual_stop", lambda: None)
    monkeypatch.setattr(implementation, "_submit_email_and_wait_next", lambda *_args, **_kwargs: "password")
    monkeypatch.setattr(implementation, "_fill_password_page_if_present", lambda *_args, **_kwargs: "Generated-Password-42")
    monkeypatch.setattr(implementation, "_is_email_verification_page", lambda _driver: False)
    monkeypatch.setattr(implementation, "_complete_profile_page", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(implementation, "_fetch_chatgpt_session", lambda *_args, **_kwargs: {
        "accessToken": "chatgpt-at", "user": {"id": "user-id"}, "account": {"planType": "free"},
    })
    monkeypatch.setattr(implementation, "resolve_email_source", lambda _email: "mailnest")
    monkeypatch.setattr(implementation, "save_account_data", lambda **kwargs: saves.append(kwargs) or "account-id")
    monkeypatch.setattr(implementation, "_setup_2fa_after_registration", lambda *_args: (_ for _ in ()).throw(RuntimeError("reauth failed")))

    result = implementation.run_chrome_cdp_registration("user@example.com", "User", "1990-01-02")

    assert result["success"] is False
    assert "2FA 设置失败" in result["error"]
    assert saves == []
    assert driver.quit_called is True
