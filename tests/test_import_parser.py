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


@pytest.mark.parametrize("separator", ["---", "----", "|", "===="])
@pytest.mark.parametrize("reverse", [False, True])
def test_two_column_email_and_password(separator, reverse):
    values = ["mail@example.com", "SyntheticPass123!"]
    if reverse:
        values.reverse()
    assert parse_account_material_line(separator.join(values)) == {
        "email": "mail@example.com",
        "chatgpt_password": "SyntheticPass123!",
    }


def test_two_column_password_uses_configured_separator(monkeypatch):
    monkeypatch.setattr(email_config, "EMAIL_IMPORT_SEPARATORS", ":::")
    assert parse_account_material_line("mail@example.com:::SyntheticPass123!") == {
        "email": "mail@example.com",
        "chatgpt_password": "SyntheticPass123!",
    }


@pytest.mark.parametrize("tail", [
    "a note with spaces",
    "ordinarytext",
    "prefixhttps://example.net/path",
    "prefixftp://example.net/path",
    "www.example.net/path",
    "SyntheticPass123!----unexpected",
])
def test_two_column_password_rejects_untyped_or_url_like_tail(tail):
    assert parse_account_material_line(f"mail@example.com----{tail}") is None


def test_two_column_url_is_classified_as_code_url_not_password():
    assert parse_account_material_line("mail@example.com----https://example.net/path") == {
        "email": "mail@example.com",
        "email_code_url": "https://example.net/path",
    }


def test_multi_column_untyped_tail_is_not_a_password():
    assert parse_account_material_line(
        "mail@example.com----https://mail.example/code----a note with spaces"
    ) == {
        "email": "mail@example.com",
        "email_code_url": "https://mail.example/code----a note with spaces",
    }
    assert parse_account_material_line(
        "mail@example.com----JBSWY3DPEHPK3PXP----prefixhttps://example.net/path"
    ) is None


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
        "email_code_url": "http://mail.example/rc?u=eric.walker%40example.com&k=abc",
        "chatgpt_password": "XFSY5&G@Wi@4*V",
    }


def test_password_punctuation_is_not_absorbed_into_code_url():
    result = parse_account_material_line(
        "mail@example.com----https://mail.example/rc?token=abc----Password&With#Chars"
    )

    assert result == {
        "email": "mail@example.com",
        "email_code_url": "https://mail.example/rc?token=abc",
        "chatgpt_password": "Password&With#Chars",
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


def test_token_shaped_password_is_not_imported_as_access_token():
    material = parse_account_material_line(
        "mail@example.com----https://mail.example/code"
        "----eyJ.synthetic.password----JBSWY3DPEHPK3PXP"
    )
    assert material == {
        "email": "mail@example.com",
        "email_code_url": "https://mail.example/code",
        "totp_secret": "JBSWY3DPEHPK3PXP",
        "chatgpt_password": "eyJ.synthetic.password",
    }
    assert parse_email_code_url_line(
        "mail@example.com----https://mail.example/code"
        "----eyJ.synthetic.password----JBSWY3DPEHPK3PXP"
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
