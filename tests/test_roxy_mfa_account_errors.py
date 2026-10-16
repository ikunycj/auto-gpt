"""Account errors must take precedence over the MFA route and its timeout."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import codex_oauth, codex_relay_service as relay, roxy_codex_oauth as oauth
from core.openai_auth import AccountUnusableError, detect_account_unusable_text


ACCOUNT_ERROR = (
    "You do not have an account because it has been deleted or deactivated. "
    "If you believe this was an error, please contact us through our help center "
    "at help.openai.com."
)


@pytest.mark.parametrize(("text", "expected"), [
    (ACCOUNT_ERROR, "account_deactivated"),
    (ACCOUNT_ERROR.upper().replace(" ", "\n"), "account_deactivated"),
    (ACCOUNT_ERROR + " account_deleted", "account_deleted"),
    ("Your account has been deleted.", "account_deleted"),
    ("Your account has been deactivated.", "account_deactivated"),
    ("account_banned", "account_banned"),
    ("Invalid code. Your code has expired.", ""),
    ("Enter the code from your authenticator app.", ""),
    ("Learn how to delete or deactivate your account.", ""),
])
def test_account_error_text_detection(text, expected):
    assert detect_account_unusable_text(text) == expected


class MfaDriver:
    current_url = "https://auth.openai.com/mfa-challenge/test?token=private-token"
    text = "Enter the code from your authenticator app."

    def execute_script(self, script):
        if "document.title" in script:
            return {"url": self.current_url, "title": "Login", "text": self.text}
        return self.text

    def set_page_load_timeout(self, _timeout):
        pass


@pytest.fixture
def mfa(monkeypatch):
    driver = MfaDriver()
    clock = SimpleNamespace(now=0.0)

    def sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr(oauth, "time", SimpleNamespace(time=lambda: clock.now, sleep=sleep))
    monkeypatch.setattr(oauth, "_clear_otp_inputs", lambda _driver: None)
    typed = []
    monkeypatch.setattr(oauth, "_type_otp", lambda _driver, code: typed.append(code))
    monkeypatch.setattr(oauth, "_has_strict_add_phone_form", lambda _driver: False)
    monkeypatch.setattr(oauth, "_is_phone_code_page", lambda _driver: False)
    monkeypatch.setattr(oauth, "_is_email_verification_page", lambda _driver: False)

    def submit(text, url=None):
        def click(*_args, **_kwargs):
            driver.text = text
            if url is not None:
                driver.current_url = url
            return True
        monkeypatch.setattr(oauth, "_click_if_present", click)

    return SimpleNamespace(driver=driver, clock=clock, typed=typed, submit=submit)


@pytest.mark.parametrize("url", [None, "https://auth.openai.com/error"])
@pytest.mark.parametrize(("text", "code"), [
    (ACCOUNT_ERROR, "account_deactivated"),
    ("Your account has been deleted.", "account_deleted"),
    ("Your account has been deactivated.", "account_deactivated"),
    (ACCOUNT_ERROR + " Invalid code. Code expired.", "account_deactivated"),
])
def test_account_error_after_totp_precedes_route_and_code_errors(mfa, caplog, url, text, code):
    mfa.submit(text, url)
    with caplog.at_level("INFO"), pytest.raises(AccountUnusableError) as exc:
        oauth._handle_totp_challenge_if_present(mfa.driver, lambda: "987654", transition_timeout=1)

    assert exc.value.error_code == code
    assert mfa.typed == ["987654"]
    assert mfa.clock.now == 0  # no timeout or extra code attempt
    assert "页面类型=账号停用提示页" in caplog.text
    assert "2FA 验证通过" not in caplog.text
    assert "未跳转" not in str(exc.value)


@pytest.mark.parametrize("already_detected", [False, True])
def test_existing_account_error_stops_before_generating_totp(mfa, already_detected):
    mfa.driver.text = ACCOUNT_ERROR
    provider = Mock(side_effect=AssertionError("must not generate another code"))
    with pytest.raises(AccountUnusableError):
        oauth._handle_totp_challenge_if_present(
            mfa.driver, provider, already_detected=already_detected,
        )
    provider.assert_not_called()
    assert mfa.typed == []


def test_login_step_detects_account_error_before_stale_mfa_url(mfa):
    mfa.driver.text = ACCOUNT_ERROR
    with pytest.raises(AccountUnusableError):
        oauth._login_step_state(mfa.driver)


def test_password_wait_deadline_does_not_skip_account_error(mfa):
    mfa.driver.text = ACCOUNT_ERROR
    with pytest.raises(AccountUnusableError):
        oauth._post_password_challenge(mfa.driver, None, timeout=0)


def test_account_error_arriving_at_totp_deadline_is_not_generic_timeout(mfa, monkeypatch):
    mfa.submit(mfa.driver.text)

    def sleep(seconds):
        mfa.clock.now += seconds
        if mfa.clock.now >= 1:
            mfa.driver.text = ACCOUNT_ERROR

    monkeypatch.setattr(oauth.time, "sleep", sleep)
    with pytest.raises(AccountUnusableError):
        oauth._handle_totp_challenge_if_present(mfa.driver, lambda: "987654", transition_timeout=1)


@pytest.mark.parametrize("text,expected", [
    ("Invalid code", "2FA 验证码错误或已过期"),
    ("Enter the code from your authenticator app.", "提交 2FA 后页面未跳转"),
])
def test_nonterminal_mfa_errors_are_not_account_deactivation(mfa, text, expected):
    mfa.submit(text)
    with pytest.raises(RuntimeError, match=expected):
        oauth._handle_totp_challenge_if_present(mfa.driver, lambda: "987654", transition_timeout=1)


def test_successful_totp_still_continues(mfa):
    mfa.submit("Allow access?", "https://auth.openai.com/consent")
    assert oauth._handle_totp_challenge_if_present(mfa.driver, lambda: "987654") is True


def test_mfa_account_error_reaches_oauth_result_account_status_and_task_log(mfa, tmp_path, monkeypatch):
    # Run the real MFA handler, OAuth error mapping and relay job persistence;
    # only replace browser/Roxy transport and use an isolated SQLite database.
    monkeypatch.setenv("TURB_SQLITE_PATH", str(tmp_path / "workspace.sqlite3"))
    for name, filename in (
        ("_ACCOUNTS_PATH", "accounts.json"), ("_PHONES_PATH", "phones.json"),
        ("_JOBS_PATH", "jobs.json"), ("_LOG_DIR", "logs"),
        ("_CREDENTIAL_DIR", "credentials"),
    ):
        monkeypatch.setattr(relay, name, tmp_path / filename)
    monkeypatch.setattr(codex_oauth, "_codex_auth_url_source", lambda: "local")
    monkeypatch.setattr(oauth, "_fill_email_and_otp", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(relay, "_email_provider", lambda *_args: None)
    monkeypatch.setattr(relay, "_totp_provider", lambda *_args: lambda: "987654")
    monkeypatch.setattr(relay, "_acquire_phone_for_job", Mock(side_effect=AssertionError("no SMS")))
    monkeypatch.setattr(oauth, "_wait_for_callback", Mock(side_effect=AssertionError("no callback")))
    monkeypatch.setattr(codex_oauth, "exchange_codex_token", Mock(side_effect=AssertionError("no token exchange")))
    results = []

    def run_oauth(email, **kwargs):
        result = oauth.run_roxy_codex_oauth(
            email, totp_provider=kwargs["totp_provider"], existing_driver=mfa.driver,
            existing_opened=SimpleNamespace(profile_id="test-profile"),
            reuse_existing_profile=True, clear_existing_state=False,
        )
        results.append(result)
        return result

    monkeypatch.setattr(codex_oauth, "run_codex_oauth", run_oauth)
    mfa.submit(ACCOUNT_ERROR)
    relay.import_accounts("mfa-test@example.com----https://mail.example/code")
    account = relay._read(relay._ACCOUNTS_PATH)[0]
    job_id = "mfa-account-error"
    relay._write(relay._JOBS_PATH, [{
        "id": job_id, "account_id": account["id"], "status": "queued", "stage": "queued",
    }])

    relay._run_job(job_id, account["id"])

    assert len(results) == 1  # terminal errors must not trigger OAuth retries
    assert results[0]["status"] == "deactivated"
    assert results[0]["ok"] is False
    assert "账号已删除或已停用" in results[0]["message"]
    stored = relay._get_account(account["id"])
    assert stored["codex_status"] == "deactivated"
    assert stored["liveness_status"] == "dead"
    job = relay.list_jobs()[0]
    assert job["status"] == "failed"
    assert "account_deactivated" in job["error"]
    assert "账号已删除或已停用" in job["error"]
    assert "提交 2FA 后页面未跳转" not in job["error"]
    log = relay.read_log(job_id)
    assert "账号已删除或已停用" in log
    assert "deleted or deactivated" in log
    assert relay._read(relay._PHONES_PATH) == []


def test_mfa_server_error_is_detected_even_when_dom_and_url_do_not_change(mfa, monkeypatch):
    from core import browser_auth_errors

    state = {"error_code": ""}
    original = mfa.driver.execute_script

    def execute(script):
        if "error_code: window.__accountAuthError" in script:
            return dict(state)
        return original(script)

    monkeypatch.setattr(mfa.driver, "execute_script", execute)
    monkeypatch.setattr(oauth, "_click_if_present", lambda *_a, **_kw: state.update(error_code="account_deactivated") or True)
    with pytest.raises(AccountUnusableError) as exc:
        oauth._handle_totp_challenge_if_present(mfa.driver, lambda: "987654", transition_timeout=1)
    assert exc.value.error_code == "account_deactivated"
    assert mfa.clock.now == 0
    assert "未跳转" not in str(exc.value)
    assert browser_auth_errors.read_account_error(mfa.driver) == "account_deactivated"
