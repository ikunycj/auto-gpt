# -*- coding: utf-8 -*-
"""Ensure 2FA diagnostics never include authentication material."""

import json

import pytest

from core import account_export


class _Response:
    def __init__(self, payload=None, *, status_code=200, url="https://example.test/", headers=None):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.url = url
        self.headers = headers or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Session:
    device_id = "device-id"

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.posts = []
        self.gets = []

    def get_auth_headers(self, **_kwargs):
        return {}

    def get_auth_navigate_headers(self, **_kwargs):
        return {}

    def get_nextauth_headers(self, **_kwargs):
        return {}

    def get_chatgpt_headers(self, **_kwargs):
        return {}

    def navigator_language(self):
        return "en-US"

    def get_chatgpt_navigate_headers(self, **_kwargs):
        return {}

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.responses.pop(0)


def test_reauth_otp_is_never_written_to_logs(caplog):
    otp = "otp-private-482901"
    continue_url = "https://auth.openai.com/authorize/continue?code=callback-private"
    session = _Session([_Response({"continue_url": continue_url})])

    with caplog.at_level("INFO"):
        result = account_export._validate_reauth_otp(session, otp)

    assert result == continue_url
    assert otp not in caplog.text
    assert otp in session.posts[0][1]["data"]


def test_2fa_tokens_and_totp_secret_are_never_written_to_logs(caplog):
    access_token = "access-token-private-123"
    secret = "JBSWY3DPEHPK3PXP"
    session = _Session([
        _Response({"secret": secret, "session_id": "session-private-456"}),
        _Response({"success": True}),
    ])

    with caplog.at_level("INFO"):
        enrolled_secret, session_id = account_export._enroll_totp(session, access_token)
        assert enrolled_secret == secret
        assert session_id == "session-private-456"
        account_export._activate_totp(session, access_token, secret, session_id)

    assert access_token not in caplog.text
    assert secret not in caplog.text
    assert "code=" not in caplog.text
    activation_body = json.loads(session.posts[1][1]["data"])
    assert activation_body["code"]
    assert activation_body["code"] not in caplog.text


def test_2fa_error_logs_do_not_include_response_bodies(caplog):
    secret = "secret-private-response-789"
    session = _Session([_Response({"error": secret}, status_code=400)])

    with caplog.at_level("INFO"), pytest.raises(RuntimeError, match="HTTP 400"):
        account_export._enroll_totp(session, "access-token-private")

    assert secret not in caplog.text
    assert "status=400" in caplog.text


def test_enroll_rejects_malformed_totp_secret_without_logging_it(caplog):
    secret = "not-a-valid-base32-secret-private"
    session = _Session([_Response({"secret": secret, "session_id": "session-private"})])

    with caplog.at_level("INFO"), pytest.raises(RuntimeError, match="TOTP secret 格式无效"):
        account_export._enroll_totp(session, "access-token-private")

    assert secret not in caplog.text


@pytest.mark.parametrize("success", ["false", 1, None, False])
def test_activate_requires_explicit_boolean_success(success):
    session = _Session([_Response({"success": success})])

    with pytest.raises(RuntimeError, match="未明确返回 success=true"):
        account_export._activate_totp(
            session,
            "access-token-private",
            "JBSWY3DPEHPK3PXP",
            "session-private",
        )


def test_setup_2fa_returns_secret_only_after_activation(monkeypatch):
    events = []
    secret = "JBSWY3DPEHPK3PXP"
    monkeypatch.setattr(account_export, "human_delay", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(account_export, "_trigger_reauth", lambda *_args: events.append("reauth") or "auth-url")
    monkeypatch.setattr(account_export, "_follow_reauth", lambda *_args: events.append("follow"))
    monkeypatch.setattr(account_export, "_validate_reauth_otp", lambda *_args: events.append("otp") or "continue-url")
    monkeypatch.setattr(account_export, "_exchange_new_token", lambda *_args: events.append("token") or "fresh-token")
    monkeypatch.setattr(account_export, "_enroll_totp", lambda *_args: events.append("enroll") or (secret, "enroll-session"))

    def activate(*_args):
        events.append("activate")
        raise RuntimeError("activation not confirmed")

    monkeypatch.setattr(account_export, "_activate_totp", activate)

    with pytest.raises(RuntimeError, match="activation not confirmed"):
        account_export.setup_2fa(_Session(), "user@example.com", otp_code="email-otp")

    assert events == ["reauth", "follow", "otp", "token", "enroll", "activate"]


@pytest.mark.parametrize("stage,route", [
    ("reauth_csrf", "csrf"),
    ("reauth_signin", "signin/openai"),
    ("reauth_authorize", "authorize"),
    ("reauth_otp", "email-otp/validate"),
    ("session_callback", "authorize/continue"),
    ("session_refresh", "session"),
    ("totp_enroll", "mfa/enroll"),
    ("totp_activate", "mfa/activate_enrollment"),
])
def test_2fa_403_logs_only_allowlisted_response_metadata(caplog, stage, route):
    secret = "secret-response-should-never-appear"
    authorization_code = "authorization-code-should-never-appear"
    response = _Response(
        {"error": secret},
        status_code=403,
        url=f"https://auth.openai.com/path?code={authorization_code}",
        headers={
            "Content-Type": "text/html; charset=UTF-8",
            "Server-Timing": f"chlray;desc={secret}",
            "Set-Cookie": f"token={secret}",
        },
    )
    with caplog.at_level("INFO"), pytest.raises(RuntimeError, match=f"2FA {stage} HTTP 403"):
        account_export._check_2fa_response(response, stage=stage, route=route)

    assert f"stage={stage}" in caplog.text
    assert "host=auth.openai.com" in caplog.text
    assert f"route={route}" in caplog.text
    assert "status=403 mime=text/html chlray=True" in caplog.text
    assert secret not in caplog.text
    assert authorization_code not in caplog.text
    assert "Set-Cookie" not in caplog.text


def test_reauth_signin_403_identifies_failing_step_without_logging_email(caplog):
    email = "private-address@example.test"
    session = _Session([
        _Response({"csrfToken": "private-csrf"}, url="https://chatgpt.com/api/auth/csrf"),
        _Response(
            {"error": "private-response-body"}, status_code=403,
            url="https://chatgpt.com/api/auth/signin/openai?login_hint=private-address%40example.test",
            headers={"Content-Type": "text/html", "Server-Timing": "cf;desc=chlray"},
        ),
    ])

    with caplog.at_level("INFO"), pytest.raises(RuntimeError, match="2FA reauth_signin HTTP 403"):
        account_export._trigger_reauth(session, email)

    assert "stage=reauth_csrf" in caplog.text
    assert "stage=reauth_signin host=chatgpt.com route=signin/openai status=403 mime=text/html chlray=True" in caplog.text
    assert email not in caplog.text
    assert "private-response-body" not in caplog.text
    assert "private-csrf" not in caplog.text
