import json
import stat
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from core import codex_oauth
from core import browser_use_codex_oauth as browser_use_oauth
from core import roxy_codex_oauth as browser_oauth


class FakeDriver:
    current_url = "https://auth.openai.com/log-in/password"

    def get(self, _url):
        return None


class FakeChallengeDriver(FakeDriver):
    current_url = "https://auth.openai.com/oauth/authorize"

    def execute_script(self, _script):
        return ["Just a moment...", "Performing security verification"]


def test_password_login_errors_are_not_treated_as_already_logged_in(monkeypatch):
    submitted_emails = []
    monkeypatch.setattr(browser_oauth, "human_delay", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(browser_oauth, "_maybe_accept", lambda _driver: None)
    monkeypatch.setattr(browser_oauth, "_type_email_address", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(browser_oauth, "_submit_email_step", lambda _driver, email: submitted_emails.append(email))
    monkeypatch.setattr(
        browser_oauth,
        "_submit_login_password",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("ChatGPT 密码错误")),
    )

    with pytest.raises(RuntimeError, match="密码错误"):
        browser_oauth._fill_email_and_otp(
            FakeDriver(),
            "user@example.com",
            lambda *_args, **_kwargs: "123456",
            "https://auth.openai.com/oauth/authorize",
            login_password="wrong-password",
        )
    assert submitted_emails == ["user@example.com"]


def test_password_step_accepts_direct_email_otp_page(monkeypatch):
    """An existing-account flow may skip the password DOM and show email OTP directly."""
    monkeypatch.setattr(browser_oauth, "_is_email_verification_page", lambda _driver: True)
    monkeypatch.setattr(
        browser_oauth,
        "_find_any",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("password input missing")),
    )

    outcome = browser_oauth._submit_login_password(FakeDriver(), "password", timeout=1)

    assert outcome == "email_otp"


def test_password_step_accepts_callback_before_password_input(monkeypatch):
    driver = FakeDriver()
    driver.current_url = "http://localhost:1455/auth/callback?code=secret"
    monkeypatch.setattr(browser_oauth, "_find_any", lambda *_args, **_kwargs: pytest.fail("should not search password"))

    outcome = browser_oauth._submit_login_password(driver, "password", timeout=1)

    assert outcome == "next"


def test_password_step_clicks_passwordless_entry_when_password_input_is_missing(monkeypatch):
    states = iter([False, True])
    clicked = []
    monkeypatch.setattr(browser_oauth, "_is_email_verification_page", lambda _driver: next(states, True))
    monkeypatch.setattr(
        browser_oauth,
        "_find_any",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("password input missing")),
    )
    monkeypatch.setattr(browser_oauth, "_click_passwordless_signup_if_present", lambda _driver: clicked.append(True) or {"ok": True})
    monkeypatch.setattr(browser_oauth, "human_delay", lambda *_args, **_kwargs: None)

    outcome = browser_oauth._submit_login_password(FakeDriver(), "password", timeout=1)

    assert outcome == "email_otp"
    assert clicked == [True]


def test_password_step_marks_explicitly_deleted_account_without_password_input(monkeypatch):
    monkeypatch.setattr(browser_oauth, "_page_text", lambda _driver: "Your account has been deleted")
    monkeypatch.setattr(browser_oauth, "_is_email_verification_page", lambda _driver: False)

    with pytest.raises(browser_oauth.AccountUnusableError) as exc:
        browser_oauth._submit_login_password(FakeDriver(), "password", timeout=1)

    assert exc.value.error_code == "account_deleted"


def test_dead_account_page_log_contains_redacted_page_summary(caplog):
    class DiagnosticDriver:
        current_url = "https://auth.openai.com/log-in/password?token=private"

        def execute_script(self, script):
            if "document.title" in script:
                return {
                    "url": self.current_url,
                    "title": "Account deactivated",
                    "text": "",
                    "buttons": ["Continue"],
                    "inputs": [{"type": "password", "name": "password"}],
                }
            return "Your account has been deactivated."

    with caplog.at_level("WARNING"):
        with pytest.raises(browser_oauth.AccountUnusableError) as exc:
            browser_oauth._raise_explicit_password_page_error(
                "Your account has been deactivated.",
                driver=DiagnosticDriver(),
            )

    assert exc.value.error_code == "account_deactivated"
    assert "页面类型=账号停用提示页" in caplog.text
    assert "标题=Account deactivated" in caplog.text
    assert "URL=https://auth.openai.com/log-in/password?[参数已隐藏]" in caplog.text
    assert "private-password" not in caplog.text
    assert "private-token" not in caplog.text


def test_cloudflare_challenge_is_reported_before_login_selectors(monkeypatch):
    monkeypatch.setattr(browser_oauth, "human_delay", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(browser_oauth, "_maybe_accept", lambda _driver: None)

    with pytest.raises(RuntimeError, match="Cloudflare"):
        browser_oauth._fill_email_and_otp(
            FakeChallengeDriver(),
            "user@example.com",
            lambda *_args, **_kwargs: "123456",
            "https://auth.openai.com/oauth/authorize",
        )


def test_run_codex_oauth_dispatches_existing_account_to_chrome_cdp(monkeypatch):
    from core import chrome_cdp_driver

    calls = []

    class Driver:
        def quit(self):
            calls.append("quit")

    opened = SimpleNamespace(raw={"driver": "chrome_cdp", "proxy": ""})
    monkeypatch.setattr(codex_oauth._cfg, "CODEX_OAUTH_DRIVER", "chrome_cdp")
    monkeypatch.setattr(chrome_cdp_driver, "build_chrome_cdp_driver", lambda **kwargs: (Driver(), opened))
    monkeypatch.setattr(
        browser_oauth,
        "run_roxy_codex_oauth",
        lambda email, **kwargs: calls.append((email, kwargs)) or {"ok": True, "status": "success"},
    )

    result = codex_oauth.run_codex_oauth(
        "user@example.com",
        force=True,
        login_password="password",
        require_browser=True,
    )

    assert result["ok"] is True
    assert calls[-1] == "quit"
    email, kwargs = calls[0]
    assert email == "user@example.com"
    assert kwargs["login_password"] == "password"
    assert kwargs["reuse_existing_profile"] is True
    assert kwargs["clear_existing_state"] is True


@pytest.mark.parametrize(
    ("driver_name", "module_name", "function_name"),
    [
        ("browser_use", "core.browser_use_codex_oauth", "run_browser_use_codex_oauth"),
        ("skyvern", "core.skyvern_codex_oauth", "run_skyvern_codex_oauth"),
    ],
)
def test_run_codex_oauth_passes_password_to_cloud_browser_drivers(
    monkeypatch,
    driver_name,
    module_name,
    function_name,
):
    import importlib

    calls = []
    module = importlib.import_module(module_name)
    monkeypatch.setattr(codex_oauth._cfg, "CODEX_OAUTH_DRIVER", driver_name)
    monkeypatch.setattr(
        module,
        function_name,
        lambda email, **kwargs: calls.append((email, kwargs)) or {"ok": True, "status": "success"},
    )
    totp_provider = lambda: "123456"

    result = codex_oauth.run_codex_oauth(
        "cloud@example.com",
        login_password="chatgpt-password",
        totp_provider=totp_provider,
        force=True,
    )

    assert result["ok"] is True
    email, kwargs = calls[0]
    assert email == "cloud@example.com"
    assert kwargs["login_password"] == "chatgpt-password"
    assert kwargs["totp_provider"] is totp_provider


def test_browser_use_submits_known_password_before_email_otp(monkeypatch):
    states = iter(["password", "next"])
    filled = []
    monkeypatch.setattr(browser_use_oauth, "_password_error_text", lambda _page: "")
    monkeypatch.setattr(browser_use_oauth, "_browser_login_step_state", lambda _page: next(states))
    monkeypatch.setattr(browser_use_oauth, "_has_visible_password_input", lambda _page: True)
    monkeypatch.setattr(
        browser_use_oauth,
        "_fill_first_any_frame",
        lambda _page, _selectors, value, timeout_ms=0: filled.append(value) or True,
    )
    monkeypatch.setattr(browser_use_oauth, "_click_first_any_frame", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(browser_use_oauth, "_bu_delay", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(browser_use_oauth.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        browser_use_oauth,
        "_click_passwordless_signup_if_present",
        lambda _page: pytest.fail("passwordless entry must not be used when a password input exists"),
    )

    outcome = browser_use_oauth._submit_browser_use_login_password(
        object(),
        "chatgpt-password",
        timeout=1,
    )

    assert outcome == "next"
    assert filled == ["chatgpt-password"]


def test_browser_use_password_flow_accepts_direct_email_otp(monkeypatch):
    monkeypatch.setattr(browser_use_oauth, "_password_error_text", lambda _page: "")
    monkeypatch.setattr(browser_use_oauth, "_browser_login_step_state", lambda _page: "email_otp")

    assert browser_use_oauth._submit_browser_use_login_password(object(), "password", timeout=1) == "email_otp"


def test_browser_use_password_error_is_terminal(monkeypatch):
    monkeypatch.setattr(browser_use_oauth, "_password_error_text", lambda _page: "wrong email or password")

    with pytest.raises(RuntimeError, match="密码错误"):
        browser_use_oauth._submit_browser_use_login_password(object(), "wrong-password", timeout=1)


def test_cloudflare_challenge_can_be_released_by_browser_assist(monkeypatch):
    seen = []
    states = iter([True, False])
    monkeypatch.setattr(browser_oauth, "human_delay", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(browser_oauth, "_maybe_accept", lambda _driver: None)
    monkeypatch.setattr(browser_oauth, "_is_cloudflare_challenge", lambda _driver: next(states, False))
    monkeypatch.setattr(browser_oauth, "_type_email_address", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(browser_oauth, "_submit_email_step", lambda _driver, _email: None)
    monkeypatch.setattr(browser_oauth, "_submit_login_password", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(browser_oauth, "_post_password_challenge", lambda *_args, **_kwargs: "next")

    browser_oauth._fill_email_and_otp(
        FakeDriver(),
        "user@example.com",
        lambda *_args, **_kwargs: "123456",
        "https://auth.openai.com/oauth/authorize",
        login_password="password",
        browser_assist_provider=lambda reason, url: seen.append((reason, url)),
    )
    assert seen == [("cloudflare", "https://auth.openai.com/log-in/password")]


def test_cloudflare_assist_receives_page_resolution_detector(monkeypatch):
    seen = []
    states = iter([True, False, False])
    monkeypatch.setattr(browser_oauth, "human_delay", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(browser_oauth, "_maybe_accept", lambda _driver: None)
    monkeypatch.setattr(browser_oauth, "_is_cloudflare_challenge", lambda _driver: next(states, False))
    monkeypatch.setattr(browser_oauth, "_type_email_address", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(browser_oauth, "_submit_email_step", lambda _driver, _email: None)
    monkeypatch.setattr(browser_oauth, "_submit_login_password", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(browser_oauth, "_post_password_challenge", lambda *_args, **_kwargs: "next")

    def assist(reason, url, resolved_check):
        seen.append((reason, url, resolved_check()))

    browser_oauth._fill_email_and_otp(
        FakeDriver(),
        "user@example.com",
        lambda *_args, **_kwargs: "123456",
        "https://auth.openai.com/oauth/authorize",
        login_password="password",
        browser_assist_provider=assist,
    )
    assert seen == [("cloudflare", "https://auth.openai.com/log-in/password", True)]


def test_cloudflare_assist_receives_focus_and_background_controls(monkeypatch):
    seen = []
    states = iter([True, False, False])
    driver = FakeDriver()
    driver.focus_window = lambda: seen.append("focus")
    driver.minimize_window = lambda: seen.append("background")
    monkeypatch.setattr(browser_oauth, "human_delay", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(browser_oauth, "_maybe_accept", lambda _driver: None)
    monkeypatch.setattr(browser_oauth, "_is_cloudflare_challenge", lambda _driver: next(states, False))
    monkeypatch.setattr(browser_oauth, "_type_email_address", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(browser_oauth, "_submit_email_step", lambda _driver, _email: None)
    monkeypatch.setattr(browser_oauth, "_submit_login_password", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(browser_oauth, "_post_password_challenge", lambda *_args, **_kwargs: "next")

    def assist(_reason, _url, _resolved_check, focus_action, background_action):
        focus_action()
        background_action()

    browser_oauth._fill_email_and_otp(
        driver,
        "user@example.com",
        lambda *_args, **_kwargs: "123456",
        "https://auth.openai.com/oauth/authorize",
        login_password="password",
        browser_assist_provider=assist,
    )

    assert seen == ["focus", "background"]


def test_callback_timeout_keeps_location_but_hides_query(monkeypatch):
    driver = FakeDriver()
    driver.current_url = "https://auth.openai.com/log-in/password?code=secret&state=private"
    with pytest.raises(RuntimeError) as exc:
        browser_oauth._wait_for_callback(driver, timeout=0.01)
    assert "auth.openai.com/log-in/password" in str(exc.value)
    assert "[参数已隐藏]" in str(exc.value)
    assert "secret" not in str(exc.value)


def test_totp_challenge_uses_local_provider_without_logging_code(monkeypatch, caplog):
    typed = []
    challenge_states = iter([True, False])
    monkeypatch.setattr(browser_oauth, "_page_text", lambda _driver: "enter the code from your authenticator app")
    monkeypatch.setattr(browser_oauth, "_is_totp_challenge_page", lambda _driver: next(challenge_states, False))
    monkeypatch.setattr(browser_oauth, "_type_otp", lambda _driver, code: typed.append(code))
    monkeypatch.setattr(browser_oauth, "_click_if_present", lambda *_args, **_kwargs: True)

    with caplog.at_level("INFO"):
        outcome = browser_oauth._post_password_challenge(FakeDriver(), lambda: "987654", timeout=1)

    assert outcome == "next"
    assert typed == ["987654"]
    assert "987654" not in caplog.text


def test_mfa_challenge_url_wins_over_generic_email_otp_input_detection(monkeypatch):
    class MfaDriver:
        current_url = "https://auth.openai.com/mfa-challenge/abc123"

    # The shared email detector deliberately recognizes generic code inputs;
    # an MFA route must take precedence over that broad heuristic.
    monkeypatch.setattr(browser_oauth, "_is_email_verification_page", lambda _driver: True)

    assert browser_oauth._is_totp_challenge_page(MfaDriver()) is True


def test_totp_page_text_wins_on_ambiguous_authorize_url(monkeypatch):
    class MfaDriver:
        current_url = "https://auth.openai.com/authorize"

    monkeypatch.setattr(browser_oauth, "_has_strict_add_phone_form", lambda _driver: False)
    monkeypatch.setattr(browser_oauth, "_is_phone_code_page", lambda _driver: False)
    monkeypatch.setattr(browser_oauth, "_page_text", lambda _driver: "enter the code from your authenticator app")
    monkeypatch.setattr(browser_oauth, "_is_email_verification_page", lambda _driver: True)

    assert browser_oauth._is_totp_challenge_page(MfaDriver()) is True


def test_password_challenge_fails_when_page_never_leaves_password_step(monkeypatch):
    monkeypatch.setattr(browser_oauth, "_page_text", lambda _driver: "")
    monkeypatch.setattr(browser_oauth, "_is_email_verification_page", lambda _driver: False)
    monkeypatch.setattr(browser_oauth, "_is_login_password_page", lambda _driver: True)

    with pytest.raises(RuntimeError, match="仍停留在密码页"):
        browser_oauth._post_password_challenge(FakeDriver(), None, timeout=0)


def test_password_challenge_recognizes_broader_incorrect_password_copy(monkeypatch):
    monkeypatch.setattr(browser_oauth, "_page_text", lambda _driver: "Wrong email or password")
    monkeypatch.setattr(browser_oauth, "_is_email_verification_page", lambda _driver: False)
    monkeypatch.setattr(browser_oauth, "_is_login_password_page", lambda _driver: True)

    with pytest.raises(RuntimeError, match="密码错误"):
        browser_oauth._post_password_challenge(FakeDriver(), None, timeout=0)


def test_password_challenge_marks_explicitly_deleted_account_unusable(monkeypatch):
    monkeypatch.setattr(browser_oauth, "_page_text", lambda _driver: "Your account has been deleted")
    monkeypatch.setattr(browser_oauth, "_is_email_verification_page", lambda _driver: False)
    monkeypatch.setattr(browser_oauth, "_is_login_password_page", lambda _driver: True)

    with pytest.raises(browser_oauth.AccountUnusableError) as exc:
        browser_oauth._post_password_challenge(FakeDriver(), None, timeout=0)

    assert exc.value.error_code == "account_deleted"


def test_codex_credential_is_written_with_owner_only_permissions(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_oauth, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(codex_oauth._cfg, "CODEX_OUTPUT_DIRNAME", "codex_accounts")

    path = codex_oauth.save_codex_credential(
        {"refresh_token": "private-refresh-token", "email": "user@example.com"},
        "user@example.com",
        "plus",
    )

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "private-refresh-token" in path.read_text(encoding="utf-8")


def test_refresh_codex_credential_rotates_tokens_and_preserves_permissions(tmp_path, monkeypatch):
    path = tmp_path / "codex-user@example.com.json"
    path.write_text('{"email":"user@example.com","refresh_token":"old-rt","access_token":"old-at"}', encoding="utf-8")

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "access_token": "new-at",
                "refresh_token": "new-rt",
                "expires_in": 3600,
            }

    seen = {}
    def post(url, **kwargs):
        seen["url"] = url
        seen["kwargs"] = kwargs
        return Response()
    monkeypatch.setattr(codex_oauth.curl_requests, "post", post)

    refreshed = codex_oauth.refresh_codex_credential(path)

    form = parse_qs(seen["kwargs"]["data"])
    assert seen["url"] == codex_oauth._cfg.CODEX_TOKEN_URL
    assert form == {
        "grant_type": ["refresh_token"],
        "refresh_token": ["old-rt"],
        "client_id": [codex_oauth._cfg.CODEX_CLIENT_ID],
    }
    assert refreshed["access_token"] == "new-at"
    assert refreshed["refresh_token"] == "new-rt"
    assert json.loads(path.read_text(encoding="utf-8"))["access_token"] == "new-at"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_refresh_codex_token_classifies_invalid_grant_without_leaking_rt(monkeypatch):
    class Response:
        status_code = 400

        @staticmethod
        def json():
            return {"error": "invalid_grant", "error_description": "refresh token revoked"}

    monkeypatch.setattr(codex_oauth.curl_requests, "post", lambda *_args, **_kwargs: Response())

    with pytest.raises(codex_oauth.CodexTokenRefreshError) as exc:
        codex_oauth.refresh_codex_token("private-refresh-token")

    assert exc.value.reauthorization_required is True
    assert exc.value.error_code == "invalid_grant"
    assert "private-refresh-token" not in str(exc.value)


def test_refresh_codex_token_classifies_403_as_retryable_network_block(monkeypatch):
    class Response:
        status_code = 403

        @staticmethod
        def json():
            return {"error": "access_denied"}

    monkeypatch.setattr(codex_oauth.curl_requests, "post", lambda *_args, **_kwargs: Response())

    with pytest.raises(codex_oauth.CodexTokenRefreshError) as exc:
        codex_oauth.refresh_codex_token("private-refresh-token")

    assert exc.value.http_status == 403
    assert exc.value.reauthorization_required is False
    assert "出口网络" in str(exc.value)
