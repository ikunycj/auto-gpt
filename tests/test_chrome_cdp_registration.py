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
    ("next_state", "created_password", "expected_method", "expected_otp_calls"),
    [
        ("password", "Generated-Password-42", "password", 1),
        ("otp", None, "email_otp", 1),
        ("logged_in", None, "email_otp", 0),
    ],
)
def test_chrome_registration_persists_only_confirmed_password(
    monkeypatch,
    next_state,
    created_password,
    expected_method,
    expected_otp_calls,
):
    driver = _Driver()
    opened = SimpleNamespace(
        profile_id="chrome-cdp",
        raw={"driver": "chrome_cdp", "proxy": "http://127.0.0.1:18080"},
    )
    saves = []
    password_calls = []
    otp_calls = []

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
    monkeypatch.setattr(implementation._twofa_cfg, "ENABLE_2FA", False)

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
    assert len(saves) == 2
    assert saves[0]["proxy_used"] == "http://127.0.0.1:18080"
    assert saves[0]["extra"]["registration_password"] == created_password
    assert saves[0]["extra"]["login_method"] == expected_method
    assert len(password_calls) == (1 if next_state == "password" else 0)
    assert len(otp_calls) == expected_otp_calls


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
