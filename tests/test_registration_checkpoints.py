from config import email as email_config
from registration.application import job_service, use_case


def test_protocol_registration_persists_as_not_authorized_without_running_codex(monkeypatch):
    events = []
    saves = []
    secret = "JBSWY3DPEHPK3PXP"

    class Session:
        def __init__(self, proxy=None):
            self.proxy = proxy
            self.device_id = "device-1"
            self.auth_session_logging_id = "session-log-1"
            self.sentinel_sid = "sentinel-1"
            self.browser_profile = {"user_agent": "test"}

    monkeypatch.setattr(use_case, "BrowserSession", Session)
    monkeypatch.setattr(use_case, "human_delay", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(use_case, "network_preflight", lambda _session: None)
    monkeypatch.setattr(use_case, "get_providers", lambda _session: {})
    monkeypatch.setattr(use_case, "get_csrf_token", lambda _session: "csrf")
    monkeypatch.setattr(use_case, "signin_openai", lambda *_args: "https://auth.openai.com/authorize")
    monkeypatch.setattr(use_case, "follow_authorize", lambda *_args: "https://auth.openai.com/email-verification")
    monkeypatch.setattr(
        use_case,
        "validate_email_otp",
        lambda *_args, **_kwargs: {
            "page": {"type": "external_url"},
            "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=test",
        },
    )
    monkeypatch.setattr(
        use_case,
        "_finalize_registration_session",
        lambda *_args, **_kwargs: (
            {
                "accessToken": "access-token",
                "user": {"id": "user-1", "name": "Test"},
                "account": {"planType": "free"},
                "expires": "2026-09-01T00:00:00Z",
            },
            "access-token",
        ),
    )
    def setup_2fa(_session, email, otp_provider=None, **_kwargs):
        assert email == "new@mailnest.test"
        assert otp_provider is use_case.wait_for_otp
        return secret

    monkeypatch.setattr(use_case, "setup_2fa", setup_2fa)
    monkeypatch.setattr(use_case._protocol_cfg, "CHATGPT_ANON_BOOTSTRAP_ENABLED", False)
    monkeypatch.setattr(use_case._protocol_cfg, "CHATGPT_AUTH_BOOTSTRAP_ENABLED", False)
    monkeypatch.setattr(use_case._twofa_cfg, "ENABLE_2FA", True)
    monkeypatch.setattr(email_config, "USE_EMAIL_SERVICE", False)

    from core import codex_oauth, email_provider, flow_trigger

    monkeypatch.setattr(email_provider, "resolve_email_source", lambda _email: "mailnest")
    monkeypatch.setattr(email_provider, "snapshot_email_context", lambda *_args, **_kwargs: {"project_code": "chatgpt001"})
    otp_calls = []

    def wait_for_otp(email, after_ts, **_kwargs):
        otp_calls.append((email, after_ts))
        return "123456"

    monkeypatch.setattr(use_case, "wait_for_otp", wait_for_otp)

    def save(**kwargs):
        events.append(f"save:{kwargs['extra']['codex']['status']}")
        saves.append(kwargs)
        return 17

    monkeypatch.setattr(use_case, "save_account_data", save)

    monkeypatch.setattr(
        codex_oauth,
        "run_codex_oauth",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("注册流程不应调用 Codex OAuth")),
    )
    monkeypatch.setattr(flow_trigger, "trigger_flow", lambda _token: {"ok": True, "status": "success"})

    with email_provider.bind_email_sources(["mailnest"]):
        result = use_case._run_protocol_registration(
            "new@mailnest.test",
            "Test User",
            birthday="1990-01-01",
        )

    assert result["success"] is True
    assert len(otp_calls) == 1
    assert otp_calls[0][0] == "new@mailnest.test"
    assert result["codex"]["status"] == "not_authorized"
    assert events == ["save:not_authorized", "save:not_authorized", "save:not_authorized"]
    assert [item["totp_secret"] for item in saves] == [None, secret, secret]
    assert [item["archive"] for item in saves] == [False, False, True]
    assert saves[0]["extra"]["login_method"] == "email_otp"
    assert saves[0]["extra"]["email_provider"] == "mailnest"


def test_registration_retry_never_turns_into_codex_authorization(monkeypatch):
    source = {"id": 7, "status": "failed", "account_id": 11, "email": "done@example.com"}
    monkeypatch.setattr(job_service.db, "get_successful_retry_for_job", lambda _job_id: None)
    monkeypatch.setattr(job_service, "_account_for_job", lambda _job: {
        "id": 11,
        "email": "done@example.com",
        "codex_status": "failed",
    })

    info = job_service.get_retry_info(source)

    assert info["display_status"] == "success"
    assert info["retryable"] is False
    assert info["retry_action"] is None
    assert "GPT账号" in info["retry_reason"]


def test_registration_retry_preserves_target_email(monkeypatch):
    source = {
        "id": 8,
        "status": "failed",
        "email": "target@example.com",
        "email_source": "mailnest",
    }
    captured = {}
    monkeypatch.setattr(job_service.db, "get_job", lambda _job_id: source)
    monkeypatch.setattr(job_service.db, "get_successful_retry_for_job", lambda _job_id: None)
    monkeypatch.setattr(job_service, "_account_for_job", lambda _job: None)

    def create_retry_job(source_job_id, **kwargs):
        captured.update(source_job_id=source_job_id, **kwargs)
        return {"id": 9, "status": "pending", "log_file": "retry.log"}, False

    monkeypatch.setattr(job_service.db, "create_retry_job", create_retry_job)

    result = job_service.retry_job(8)

    assert result["ok"] is True
    assert captured == {
        "source_job_id": 8,
        "job_type": "registration",
        "email_source": "mailnest",
        "email": "target@example.com",
        "account_id": None,
    }
