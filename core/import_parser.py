# -*- coding: utf-8 -*-
"""Shared parsing for pasted account and mailbox materials."""
from __future__ import annotations

import html
import itertools
import re
from urllib.parse import urlparse


DEFAULT_IMPORT_SEPARATORS = ("---", "----", "|", "====")
_MARKDOWN_LINK_RE = re.compile(r"^\s*\[[^\]]*\]\(\s*(https?://[^)\s]+)\s*\)\s*$", re.IGNORECASE)
_INLINE_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\(\s*(https?://[^)\s]+)\s*\)", re.IGNORECASE)
_ESCAPED_PUNCTUATION_RE = re.compile(r"\\([@&*?=()\[\]{}|#.!_~<>:+-])")
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_TOTP_RE = re.compile(r"^[A-Z2-7]{16,64}$", re.IGNORECASE)
# Older generic-API imports optionally used ``email | code_url |
# access_token | totp``.  A third field is otherwise ambiguous with a
# separator embedded in the URL, so only token shapes with a strong and
# recognizable signature are treated as that legacy column.
_JWT_ACCESS_TOKEN_RE = re.compile(r"^eyJ[A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+){2,}$")
_BEARER_ACCESS_TOKEN_RE = re.compile(r"^Bearer\s+[A-Za-z0-9._~+/=-]{12,}$", re.IGNORECASE)
_SK_ACCESS_TOKEN_RE = re.compile(r"^sk[-_][A-Za-z0-9_-]{16,}$")


def configured_import_separators() -> tuple[str, ...]:
    """Read the hot-reloadable import separator setting."""
    try:
        from config import email as email_config
        raw = getattr(email_config, "EMAIL_IMPORT_SEPARATORS", DEFAULT_IMPORT_SEPARATORS)
    except Exception:
        raw = DEFAULT_IMPORT_SEPARATORS
    if isinstance(raw, str):
        values = re.split(r"[,\n;]", raw)
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        values = [raw]
    result: list[str] = []
    for value in values:
        separator = str(value or "").strip()
        if separator and separator not in result:
            result.append(separator)
    return tuple(sorted(result, key=len, reverse=True)) or DEFAULT_IMPORT_SEPARATORS


def clean_import_value(value: object) -> str:
    """Normalize Markdown links and common backslash escaping from chat paste."""
    text = html.unescape(str(value or "").strip())
    text = _INLINE_MARKDOWN_LINK_RE.sub(r"\1", text)
    match = _MARKDOWN_LINK_RE.fullmatch(text)
    if match:
        text = match.group(1).strip()
    return _ESCAPED_PUNCTUATION_RE.sub(r"\1", text).strip()


def _separator_matches(line: str) -> list[re.Match[str]]:
    matches: list[re.Match[str]] = []
    for separator in configured_import_separators():
        if set(separator) <= {"-", "="}:
            pattern = rf"(?<!{re.escape(separator[0])}){re.escape(separator)}(?!{re.escape(separator[0])})"
        else:
            pattern = re.escape(separator)
        matches.extend(re.finditer(pattern, line))
    ordered = sorted(matches, key=lambda match: (match.start(), -len(match.group(0))))
    non_overlapping: list[re.Match[str]] = []
    for match in ordered:
        if non_overlapping and match.start() < non_overlapping[-1].end():
            continue
        non_overlapping.append(match)
    return non_overlapping


def split_import_line(line: object, max_fields: int | None = None) -> list[str]:
    """Split a line, retaining the complete tail after the last requested field."""
    text = str(line or "").strip()
    if not text:
        return []
    if max_fields is not None:
        max_fields = max(1, int(max_fields))
    matches = _separator_matches(text)
    if not matches:
        if "\t" in text:
            limit = max(0, (max_fields or 0) - 1) or -1
            return [piece.strip() for piece in text.split("\t", limit)]
        return [text]
    if max_fields is None:
        pieces: list[str] = []
        start = 0
        for match in matches:
            pieces.append(text[start:match.start()].strip())
            start = match.end()
        pieces.append(text[start:].strip())
        return [piece for piece in pieces if piece]
    fields: list[str] = []
    start = 0
    for match in matches[: max_fields - 1]:
        fields.append(text[start:match.start()].strip())
        start = match.end()
    fields.append(text[start:].strip())
    return fields


def iter_import_splits(line: object, field_count: int):
    """Yield candidate partitions for semantic classification."""
    text = str(line or "").strip()
    count = max(1, int(field_count))
    if count == 1:
        yield (text,)
        return
    matches = _separator_matches(text)
    if len(matches) < count - 1:
        return
    if len(matches) > 24:
        yield tuple(split_import_line(text, max_fields=count))
        return
    for indexes in itertools.combinations(range(len(matches)), count - 1):
        fields: list[str] = []
        start = 0
        for index in indexes:
            match = matches[index]
            fields.append(text[start:match.start()].strip())
            start = match.end()
        fields.append(text[start:].strip())
        yield tuple(fields)


def is_http_url(value: object) -> bool:
    text = clean_import_value(value)
    try:
        parsed = urlparse(text)
    except Exception:
        return False
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def is_email(value: object) -> bool:
    """Return whether a value is a complete, normalized email address."""
    return bool(_EMAIL_RE.fullmatch(clean_import_value(value).lower()))


def looks_like_totp(value: object) -> bool:
    """Recognize common TOTP secrets by URI or Base32 length/charset."""
    raw = clean_import_value(value)
    if any(separator in raw for separator in configured_import_separators()):
        return False
    text = raw.replace(" ", "").replace("-", "")
    return text.lower().startswith("otpauth://") or bool(_TOTP_RE.fullmatch(text))


def looks_like_access_token(value: object) -> bool:
    """Recognize strong markers used by the legacy generic-API token column.

    Arbitrary short strings intentionally do not qualify: for a line such as
    ``email | https://host/path----suffix`` there is no information-theoretic
    way to tell a password/token from a URL suffix.  Keeping the suffix in
    the URL is the least surprising behavior unless the value has a known
    token signature.
    """
    text = clean_import_value(value)
    return bool(
        _JWT_ACCESS_TOKEN_RE.fullmatch(text)
        or _BEARER_ACCESS_TOKEN_RE.fullmatch(text)
        or _SK_ACCESS_TOKEN_RE.fullmatch(text)
    )


def looks_like_phone(value: object) -> bool:
    text = clean_import_value(value)
    digits = "".join(char for char in text if char.isdigit())
    return bool(digits) and len(digits) in range(7, 16) and all(char.isdigit() or char in "+ -()." for char in text)


def _looks_like_password_candidate(value: object) -> bool:
    """Return whether an opaque field is plausibly a password.

    The configured separator is an explicit field boundary. Passwords commonly
    contain query-string punctuation such as ``&`` and ``#``, so those
    characters must not make a trailing password look like part of the URL.
    """
    text = clean_import_value(value)
    if not text or len(text) < 6 or len(text) > 512:
        return False
    if is_email(text) or is_http_url(text) or looks_like_totp(text) or looks_like_phone(text):
        return False
    if "://" in text:
        return False
    return True


def _candidate_material(record_fields: tuple[str, ...]) -> dict | None:
    values = [clean_import_value(value) for value in record_fields]
    separators = configured_import_separators()
    email_indexes = [
        index
        for index, value in enumerate(values)
        if is_email(value)
        and not is_http_url(value)
        and not any(separator in value for separator in separators)
    ]
    if len(email_indexes) != 1:
        return None
    email_index = email_indexes[0]
    email = values[email_index].lower()
    values.pop(email_index)
    values = [value for value in values if value]
    if not values:
        return None
    urls = [value for value in values if is_http_url(value)]
    non_urls = [value for value in values if not is_http_url(value)]
    record: dict = {"email": email}
    if not urls:
        totps = [value for value in non_urls if looks_like_totp(value)]
        if len(totps) > 1:
            return None
        if not totps:
            # Four opaque Outlook fields and an untyped two-field line are
            # handled by the legacy format fallback in the caller.
            return None
        record["totp_secret"] = totps[0]
        non_urls.remove(totps[0])
        if len(non_urls) > 1:
            return None
        if non_urls:
            record["chatgpt_password"] = non_urls[0]
        return record
    if len(urls) == 1:
        phone = next((value for value in non_urls if looks_like_phone(value)), "")
        if phone:
            record["sms_code_url"] = urls[0]
            record["phone"] = phone
            non_urls.remove(phone)
        else:
            record["email_code_url"] = urls[0]
    elif len(urls) == 2:
        phone = next((value for value in non_urls if looks_like_phone(value)), "")
        if not phone:
            return None
        record["email_code_url"], record["sms_code_url"] = urls
        record["phone"] = phone
        non_urls.remove(phone)
    else:
        return None
    totps = [value for value in non_urls if looks_like_totp(value)]
    if len(totps) > 1:
        return None
    if totps:
        record["totp_secret"] = totps[0]
        non_urls.remove(totps[0])
    if len(non_urls) > 1:
        return None
    if non_urls:
        record["chatgpt_password"] = non_urls[0]
    return record


def parse_account_material_line(line: object) -> dict | None:
    """Classify an auto-import account line by content semantics."""
    text = clean_import_value(line)
    simple = split_import_line(text, max_fields=2)
    simple_record = _candidate_material(tuple(simple)) if len(simple) == 2 else None
    simple_is_ambiguous = False
    if simple_record:
        simple_values = [clean_import_value(value) for value in simple]
        for value in simple_values:
            if _EMAIL_RE.fullmatch(value.lower()):
                continue
            if any(value.endswith(separator) for separator in configured_import_separators()):
                simple_record = None
                simple_is_ambiguous = True
                break
            if len(re.findall(r"https?://", value, re.IGNORECASE)) > 1:
                simple_record = None
                simple_is_ambiguous = True
                break
            if any(separator in value for separator in configured_import_separators()):
                fragments = [value]
                for separator in configured_import_separators():
                    fragments = [part for fragment in fragments for part in fragment.split(separator)]
                if sum(looks_like_totp(fragment) for fragment in fragments) > 1:
                    simple_record = None
                    simple_is_ambiguous = True
                    break
    if simple_is_ambiguous:
        return None
    candidates: list[dict] = []
    for field_count in range(2, 7):
        for fields in iter_import_splits(text, field_count):
            candidate = _candidate_material(fields)
            if candidate:
                candidates.append(candidate)
    if not candidates:
        return simple_record

    # A separator can be part of an opaque code URL.  A clearly identified
    # TOTP/password field wins over that interpretation; otherwise retain the
    # complete URL instead of silently truncating its token.
    totp_candidates = [item for item in candidates if item.get("totp_secret")]
    if totp_candidates:
        return max(totp_candidates, key=lambda item: (len(item), bool(item.get("chatgpt_password"))))
    if simple_record and simple_record.get("email_code_url"):
        url = simple_record["email_code_url"]
        if any(separator in url for separator in configured_import_separators()) and len(
            re.findall(r"https?://", url, re.IGNORECASE)
        ) == 1:
            password_candidates = [
                item
                for item in candidates
                if item.get("chatgpt_password")
                and _looks_like_password_candidate(item["chatgpt_password"])
            ]
            if password_candidates:
                return max(password_candidates, key=lambda item: (len(item), bool(item.get("totp_secret"))))
            return simple_record

    return max(
        candidates,
        key=lambda item: (
            bool(item.get("chatgpt_password")),
            bool(item.get("sms_code_url")),
            len(item),
        ),
    )


def parse_email_code_url_line(line: object) -> tuple[str, str] | None:
    """Parse the common ``email<separator>code URL`` format."""
    text = clean_import_value(line)
    record = parse_account_material_line(text)
    legacy_parts = split_import_line(text, max_fields=4)
    if (
        3 <= len(legacy_parts) <= 4
        and is_email(legacy_parts[0])
        and is_http_url(legacy_parts[1])
        and looks_like_access_token(legacy_parts[2])
        and (len(legacy_parts) == 3 or looks_like_totp(legacy_parts[3]))
    ):
        # Keep compatibility with the old fixed-position shape only when the
        # optional token has a strong signature.  Otherwise the complete URL
        # (including any embedded separator) remains authoritative.
        return clean_import_value(legacy_parts[0]).lower(), clean_import_value(legacy_parts[1])
    if record and record.get("email_code_url"):
        return record["email"], record["email_code_url"]
    if (
        2 <= len(legacy_parts)
        and is_email(legacy_parts[0])
        and is_http_url(legacy_parts[1])
    ):
        return clean_import_value(legacy_parts[0]).lower(), clean_import_value(legacy_parts[1])
    return None
