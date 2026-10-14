# -*- coding: utf-8 -*-
"""Avoid leaking reauthentication URL query parameters during 403 handling."""
from types import SimpleNamespace

import pytest

from core.session import BrowserSession


def test_circuit_breaker_omits_auth_query_and_callback_code(caplog):
    session = BrowserSession.__new__(BrowserSession)
    session.blocked_until = 0.0
    session.blocked_reason = ""
    session._observe_cf_cookie_changes = lambda *_args: None
    response = SimpleNamespace(status_code=403, headers={"retry-after": "2"})
    url = "https://chatgpt.com/api/auth/signin/openai?login_hint=private%40example.test&code=private-code"

    with caplog.at_level("WARNING"):
        result = session._observe_response_for_circuit_breaker(response, url)
    assert result is response
    assert "host=chatgpt.com" in caplog.text
    assert "private" not in caplog.text
    assert "?" not in caplog.text
    assert "private" not in session.blocked_reason
    assert "chatgpt.com" in session.blocked_reason
    with pytest.raises(RuntimeError, match="熔断冷却") as exc:
        session._raise_if_circuit_open()
    assert "private" not in str(exc.value)
