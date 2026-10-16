
import pytest

from registration.drivers.roxy import implementation as selenium_registration


class _PasswordInput:
    def __init__(self):
        self.value = ""

    def get_attribute(self, name):
        return self.value if name == "value" else None


class _SeleniumDriver:
    def __init__(self):
        self.password_input = _PasswordInput()

    def execute_script(self, script):
        if "text.includes('Route Error')" in script:
            return False
        return {"ok": True, "input": self.password_input, "button": object()}


def _patch_selenium_password_submit(monkeypatch, *, otp_states):
    states = iter(otp_states)
    monkeypatch.setattr(
        selenium_registration,
        "_is_email_verification_page",
        lambda _driver: next(states),
    )
    monkeypatch.setattr(selenium_registration, "_has_access_token", lambda _driver: False)
    monkeypatch.setattr(selenium_registration, "_password_page_state", lambda _driver: {"url": "signup-password"})
    monkeypatch.setattr(selenium_registration, "_is_signup_password_page", lambda _driver: True)
    monkeypatch.setattr(selenium_registration, "_is_login_password_page", lambda _driver: False)
    monkeypatch.setattr(
        selenium_registration,
        "_click_passwordless_signup_if_present",
        lambda _driver: {"ok": False, "reason": "missing_passwordless_button"},
    )
    monkeypatch.setattr(selenium_registration, "_registration_password", lambda: "Generated-Password-42")
    monkeypatch.setattr(
        selenium_registration,
        "_human_type_text",
        lambda _driver, element, value, **_kwargs: setattr(element, "value", value),
    )
    monkeypatch.setattr(selenium_registration, "_human_click", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(selenium_registration, "human_delay", lambda *_args, **_kwargs: None)


def test_selenium_registration_password_is_returned_only_after_otp_transition(monkeypatch):
    _patch_selenium_password_submit(monkeypatch, otp_states=[False, True])

    password = selenium_registration._fill_password_page_if_present(
        _SeleniumDriver(),
        "user@example.com",
    )

    assert password == "Generated-Password-42"


def test_selenium_registration_prefers_email_otp_over_creating_password(monkeypatch):
    otp_states = iter([False, True])
    monkeypatch.setattr(
        selenium_registration,
        "_is_email_verification_page",
        lambda _driver: next(otp_states),
    )
    monkeypatch.setattr(selenium_registration, "_has_access_token", lambda _driver: False)
    monkeypatch.setattr(selenium_registration, "_password_page_state", lambda _driver: {"url": "signup-password"})
    monkeypatch.setattr(selenium_registration, "_is_signup_password_page", lambda _driver: True)
    monkeypatch.setattr(selenium_registration, "_is_login_password_page", lambda _driver: False)
    monkeypatch.setattr(
        selenium_registration,
        "_click_passwordless_signup_if_present",
        lambda _driver: {"ok": True, "reason": "clicked_passwordless_send_otp"},
    )
    monkeypatch.setattr(
        selenium_registration,
        "_registration_password",
        lambda: pytest.fail("有邮箱 OTP 入口时不应创建密码"),
    )

    password = selenium_registration._fill_password_page_if_present(
        _SeleniumDriver(),
        "user@example.com",
    )

    assert password is None


def test_selenium_registration_password_stuck_page_is_not_saved(monkeypatch):
    clock = [0.0]
    _patch_selenium_password_submit(monkeypatch, otp_states=[False, False])
    monkeypatch.setattr(selenium_registration, "_page_snapshot", lambda _driver: {})
    monkeypatch.setattr(selenium_registration, "_is_profile_like", lambda _state: False)
    monkeypatch.setattr(selenium_registration.time, "time", lambda: clock[0])
    monkeypatch.setattr(
        selenium_registration.time,
        "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + max(float(seconds), 21.0)),
    )

    with pytest.raises(selenium_registration.RegistrationPasswordSubmissionError, match="注册密码提交结果不确定"):
        selenium_registration._fill_password_page_if_present(
            _SeleniumDriver(),
            "user@example.com",
        )


def test_selenium_registration_password_repairs_controlled_input_before_submit(monkeypatch):
    _patch_selenium_password_submit(monkeypatch, otp_states=[False, True])
    driver = _SeleniumDriver()
    clicks = []

    monkeypatch.setattr(
        selenium_registration,
        "_human_type_text",
        lambda _driver, element, _value, **_kwargs: setattr(element, "value", "scrambled"),
    )
    monkeypatch.setattr(
        selenium_registration,
        "_set_element_value",
        lambda _driver, element, value: setattr(element, "value", value),
    )
    monkeypatch.setattr(
        selenium_registration,
        "_human_click",
        lambda *_args, **_kwargs: clicks.append(True),
    )

    password = selenium_registration._fill_password_page_if_present(
        driver,
        "user@example.com",
    )

    assert password == "Generated-Password-42"
    assert driver.password_input.value == password
    assert clicks == [True]
