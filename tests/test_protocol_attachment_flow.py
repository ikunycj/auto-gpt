from registration.drivers.protocol import attachment_implementation as protocol


def test_attachment_protocol_keeps_attachment_request_order_and_current_storage(monkeypatch):
    events = []

    class Session:
        proxy = "http://proxy.example:3128"
        device_id = "device-1"
        sentinel_sid = "sentinel-1"
        browser_profile = {"user_agent": "test"}

        def __init__(self, proxy=None):
            self.proxy = proxy

    monkeypatch.setattr(protocol, "BrowserSession", Session)
    monkeypatch.setattr(protocol._twofa_cfg, "ENABLE_2FA", False)
    monkeypatch.setattr(protocol, "human_delay", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(protocol, "get_providers", lambda _session: events.append("providers") or {})
    monkeypatch.setattr(protocol, "get_csrf_token", lambda _session: events.append("csrf") or "csrf")
    monkeypatch.setattr(protocol, "signin_openai", lambda *_args: events.append("signin") or "authorize")
    monkeypatch.setattr(protocol, "follow_authorize", lambda *_args: events.append("follow"))
    monkeypatch.setattr(
        protocol,
        "request_sentinel_token",
        lambda _session, flow: events.append(f"sentinel:{flow}") or {"flow": flow},
    )
    monkeypatch.setattr(
        protocol,
        "build_sentinel_header",
        lambda _session, _response, flow: (events.append(f"header:{flow}") or (f"token:{flow}", None)),
    )
    monkeypatch.setattr(
        protocol,
        "validate_email_otp",
        lambda *_args: events.append("validate") or {"page": {"type": "about_you"}},
    )
    monkeypatch.setattr(
        protocol,
        "create_account",
        lambda *_args: events.append("create") or {"continue_url": "continue"},
    )
    monkeypatch.setattr(
        protocol,
        "_finalize_registration_session",
        lambda *_args: events.append("finalize")
        or ({"accessToken": "access", "user": {}, "account": {}}, "access"),
    )
    monkeypatch.setattr(protocol, "resolve_email_source", lambda _email: "mailnest")
    monkeypatch.setattr(protocol, "snapshot_email_context", lambda *_args, **_kwargs: {"project": "x"})
    monkeypatch.setattr(protocol, "save_account_data", lambda **_kwargs: events.append("save") or "account-1")
    monkeypatch.setattr(protocol, "trigger_flow", lambda _token: events.append("flow") or {"ok": True})

    result = protocol.run_attachment_protocol_registration(
        "user@example.com",
        "Test User",
        birthday="1990-01-01",
        proxy="http://proxy.example:3128",
        otp_code="123456",
    )

    assert result["success"] is True
    assert result["account_id"] == "account-1"
    assert events == [
        "providers",
        "csrf",
        "signin",
        "follow",
        "sentinel:authorize_continue",
        "header:authorize_continue",
        "validate",
        "sentinel:oauth_create_account",
        "header:oauth_create_account",
        "create",
        "finalize",
        "save",
        "flow",
    ]
