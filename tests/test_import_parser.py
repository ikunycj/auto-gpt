import pytest

from config import email as email_config
from core.import_parser import (
    parse_account_material_line,
    parse_email_code_url_line,
)


@pytest.mark.parametrize("separator", ["---", "----", "|", "===="])
def test_default_separators_parse_email_and_code_url(separator):
    result = parse_account_material_line(
        f"mail@example.com{separator}https://mail.example/code"
    )

    assert result == {
        "email": "mail@example.com",
        "email_code_url": "https://mail.example/code",
    }


def test_fields_are_classified_by_content_in_any_order():
    result = parse_account_material_line(
        "chatgpt-password|JBSWY3DPEHPK3PXP|https://mail.example/code|mail@example.com"
    )

    assert result == {
        "email": "mail@example.com",
        "email_code_url": "https://mail.example/code",
        "totp_secret": "JBSWY3DPEHPK3PXP",
        "chatgpt_password": "chatgpt-password",
    }


def test_markdown_link_and_backslash_escaping_are_normalized():
    result = parse_account_material_line(
        r"eric.walker\@example.com----[http://mail.example/rc?u=eric.walker%40example.com\&k=abc----XFSY5\&G@Wi@4\*V](http://mail.example/rc?u=eric.walker%40example.com\&k=abc----XFSY5\&G@Wi@4*V)"
    )

    assert result == {
        "email": "eric.walker@example.com",
        "email_code_url": "http://mail.example/rc?u=eric.walker%40example.com&k=abc----XFSY5&G@Wi@4*V",
    }


def test_url_separator_is_preserved_when_no_other_field_is_classifiable():
    result = parse_account_material_line(
        "mail@example.com----https://mail.example/rc?token=abc----XFSY5"
    )

    assert result == {
        "email": "mail@example.com",
        "email_code_url": "https://mail.example/rc?token=abc----XFSY5",
    }


def test_totp_and_password_win_over_separator_inside_url():
    result = parse_account_material_line(
        "mail@example.com----https://mail.example/rc?token=abc----XFSY5----JBSWY3DPEHPK3PXP----chatgpt-password"
    )

    assert result == {
        "email": "mail@example.com",
        "email_code_url": "https://mail.example/rc?token=abc----XFSY5",
        "totp_secret": "JBSWY3DPEHPK3PXP",
        "chatgpt_password": "chatgpt-password",
    }


def test_remaining_field_after_url_is_classified_as_password():
    result = parse_account_material_line(
        "mail@example.com----https://mail.example/code----chatgpt-password"
    )

    assert result == {
        "email": "mail@example.com",
        "email_code_url": "https://mail.example/code",
        "chatgpt_password": "chatgpt-password",
    }


def test_only_password_and_totp_are_supported():
    result = parse_account_material_line(
        "JBSWY3DPEHPK3PXP----mail@example.com----chatgpt-password"
    )

    assert result == {
        "email": "mail@example.com",
        "totp_secret": "JBSWY3DPEHPK3PXP",
        "chatgpt_password": "chatgpt-password",
    }


def test_custom_separator_setting_is_used(monkeypatch):
    monkeypatch.setattr(email_config, "EMAIL_IMPORT_SEPARATORS", ":::")

    result = parse_account_material_line(
        "chatgpt-password:::https://mail.example/code:::mail@example.com:::JBSWY3DPEHPK3PXP"
    )

    assert result == {
        "email": "mail@example.com",
        "email_code_url": "https://mail.example/code",
        "totp_secret": "JBSWY3DPEHPK3PXP",
        "chatgpt_password": "chatgpt-password",
    }


def test_overlapping_custom_separators_prefer_the_longest(monkeypatch):
    monkeypatch.setattr(email_config, "EMAIL_IMPORT_SEPARATORS", "::,:::")

    assert parse_account_material_line(
        "mail@example.com:::https://mail.example/code"
    ) == {
        "email": "mail@example.com",
        "email_code_url": "https://mail.example/code",
    }


def test_at_sign_inside_url_does_not_become_the_email_field():
    result = parse_account_material_line(
        "https://mail.example/code?u=user@example.com----JBSWY3DPEHPK3PXP----mail@example.net"
    )

    assert result == {
        "email": "mail@example.net",
        "email_code_url": "https://mail.example/code?u=user@example.com",
        "totp_secret": "JBSWY3DPEHPK3PXP",
    }


def test_email_code_url_parser_ignores_optional_password_and_totp():
    assert parse_email_code_url_line(
        "chatgpt-password|mail@example.com|https://mail.example/code|JBSWY3DPEHPK3PXP"
    ) == ("mail@example.com", "https://mail.example/code")


def test_email_code_url_parser_keeps_legacy_optional_columns():
    token = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ0ZXN0In0.synthetic_signature"
    assert parse_email_code_url_line(
        f"mail@example.com----https://mail.example/code----{token}----JBSWY3DPEHPK3PXP"
    ) == ("mail@example.com", "https://mail.example/code")
    assert parse_email_code_url_line(
        f"mail@example.com----https://mail.example/code----{token}"
    ) == ("mail@example.com", "https://mail.example/code")


def test_email_code_url_parser_preserves_ambiguous_url_suffix():
    assert parse_email_code_url_line(
        "mail@example.com----https://mail.example/rc?token=abc----XFSY5"
    ) == ("mail@example.com", "https://mail.example/rc?token=abc----XFSY5")


@pytest.mark.parametrize(
    "line",
    [
        "missing-at----https://mail.example/code",
        "mail@example.com----https://one.example/code----https://two.example/code",
        "mail@example.com----JBSWY3DPEHPK3PXP----KRSXG5A2L5XXE3DE",
        "mail@example.com",
    ],
)
def test_invalid_or_ambiguous_material_is_rejected(line):
    assert parse_account_material_line(line) is None
