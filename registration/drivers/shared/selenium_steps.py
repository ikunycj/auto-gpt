"""Common Selenium page operations used by legacy browser adapters.

The functions are exposed from a neutral package so Cloak and Roxy adapters
depend on a shared browser port rather than importing each other's entrypoint.
The implementation remains delegated to the legacy module during migration.
"""
from __future__ import annotations

def _call(name: str, *args, **kwargs):
    """Resolve one legacy page operation only when it is actually used."""
    from registration.drivers.roxy import implementation as roxy_registration

    return getattr(roxy_registration, name)(*args, **kwargs)


def _maybe_accept(*args, **kwargs):
    return _call("_maybe_accept", *args, **kwargs)


def _submit_email_and_wait_next(*args, **kwargs):
    return _call("_submit_email_and_wait_next", *args, **kwargs)


def _fill_password_page_if_present(*args, **kwargs):
    return _call("_fill_password_page_if_present", *args, **kwargs)


def _is_email_verification_page(*args, **kwargs):
    return _call("_is_email_verification_page", *args, **kwargs)


def _has_access_token(*args, **kwargs):
    return _call("_has_access_token", *args, **kwargs)


def _clear_otp_inputs(*args, **kwargs):
    return _call("_clear_otp_inputs", *args, **kwargs)


def _type_otp(*args, **kwargs):
    return _call("_type_otp", *args, **kwargs)


def _click_continue(*args, **kwargs):
    return _call("_click_continue", *args, **kwargs)


def _wait_after_email_otp_submit(*args, **kwargs):
    return _call("_wait_after_email_otp_submit", *args, **kwargs)


def _click_resend_email_otp(*args, **kwargs):
    return _call("_click_resend_email_otp", *args, **kwargs)


def _complete_profile_page(*args, **kwargs):
    return _call("_complete_profile_page", *args, **kwargs)


def _fetch_chatgpt_session(*args, **kwargs):
    return _call("_fetch_chatgpt_session", *args, **kwargs)


def _check_manual_stop(*args, **kwargs):
    return _call("_check_manual_stop", *args, **kwargs)

__all__ = [
    "_maybe_accept",
    "_submit_email_and_wait_next",
    "_fill_password_page_if_present",
    "_is_email_verification_page",
    "_has_access_token",
    "_clear_otp_inputs",
    "_type_otp",
    "_click_continue",
    "_wait_after_email_otp_submit",
    "_click_resend_email_otp",
    "_complete_profile_page",
    "_fetch_chatgpt_session",
    "_check_manual_stop",
]
