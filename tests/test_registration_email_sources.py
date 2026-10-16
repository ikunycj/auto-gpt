# -*- coding: utf-8 -*-
from unittest.mock import patch

import pytest

from config import twofa as twofa_config

from config import email as email_config
from config import register as register_config
from core.email_provider import (
    automatic_email_enabled,
    bind_email_sources,
    validate_email_sources,
    wait_for_otp,
)
from registration.application import job_service


@pytest.fixture(autouse=True)
def isolate_email_tests_from_local_twofa_settings(monkeypatch):
    # Email routing tests do not exercise password/TOTP driver eligibility.
    monkeypatch.setattr(twofa_config, "ENABLE_2FA", False)


def test_prepare_registration_uses_job_scoped_email_sources():
    events = []

    def on_email_acquired(email):
        events.append(("persist", email))

    def generate_birthday():
        events.append(("birthday", "generated"))
        return "1990-01-01"

    with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
        register_config, "REGISTER_EMAIL", ""
    ), patch.object(register_config, "REGISTER_NAME", ""), patch(
        "core.email_provider.acquire_email", return_value="fresh@mailnest.test"
    ) as acquire_email, patch(
        "registration.application.job_service._random_display_name", return_value="Test User"
    ), patch(
        "core.profile_utils.generate_random_birthday", side_effect=generate_birthday
    ):
        result = job_service._prepare_registration_args(
            email_sources="mailnest",
            on_email_acquired=on_email_acquired,
        )

    assert result == ("fresh@mailnest.test", "Test User", "1990-01-01")
    acquire_email.assert_called_once_with("mailnest")
    assert events == [("birthday", "generated"), ("persist", "fresh@mailnest.test")]


def test_explicit_sources_override_global_manual_mode():
    with patch.object(email_config, "USE_EMAIL_SERVICE", False), patch.object(
        register_config, "REGISTER_EMAIL", "old-manual@example.com"
    ), patch.object(register_config, "REGISTER_NAME", ""), patch(
        "core.email_provider.acquire_email", return_value="fresh@mailnest.test"
    ) as acquire_email, patch(
        "registration.application.job_service._random_display_name", return_value="Test User"
    ), patch(
        "core.profile_utils.generate_random_birthday", return_value="1990-01-01"
    ):
        result = job_service._prepare_registration_args(email_sources="mailnest,gptmail")

    assert result == ("fresh@mailnest.test", "Test User", "1990-01-01")
    acquire_email.assert_called_once_with("mailnest,gptmail")


def test_targeted_imported_email_takes_priority_over_selected_sources():
    with patch.object(email_config, "USE_EMAIL_SERVICE", False), patch.object(
        register_config, "REGISTER_EMAIL", "old-manual@example.com"
    ), patch.object(register_config, "REGISTER_NAME", ""), patch(
        "core.email_provider.acquire_email"
    ) as acquire_email, patch(
        "registration.application.job_service._random_display_name", return_value="Test User"
    ), patch(
        "core.profile_utils.generate_random_birthday", return_value="1990-01-01"
    ):
        result = job_service._prepare_registration_args(
            email_override="imported@example.com",
            email_sources="outlook,generic_api",
        )

    assert result == ("imported@example.com", "Test User", "1990-01-01")
    acquire_email.assert_not_called()


def test_validate_email_sources_preserves_order_and_removes_duplicates():
    assert validate_email_sources(["mailnest", "outlook", "mailnest"]) == ["mailnest", "outlook"]


def test_validate_email_sources_rejects_unknown_or_empty_selection():
    for value in ([], ["unknown"], ""):
        try:
            validate_email_sources(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected invalid source selection: {value!r}")


def test_task_sources_enable_automatic_otp_when_global_mode_is_manual():
    with patch.object(email_config, "USE_EMAIL_SERVICE", False), patch(
        "core.mailnest_client.fetch_latest_otp",
        return_value="112233",
    ) as fetch_latest_otp, patch(
        "core.email_provider.resolve_email_source",
        return_value="mailnest",
    ), bind_email_sources(["mailnest"]):
        assert automatic_email_enabled() is True
        code = wait_for_otp(
            "fresh@mailnest.test",
            after_ts=123.0,
        )

    assert code == "112233"
    fetch_latest_otp.assert_called_once_with("fresh@mailnest.test", after_ts=123.0)


def test_empty_task_sources_keep_manual_mode_even_when_global_mode_is_automatic():
    with patch.object(email_config, "USE_EMAIL_SERVICE", True), bind_email_sources(""):
        assert automatic_email_enabled() is False
