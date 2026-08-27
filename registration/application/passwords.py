"""Password generation shared by browser-backed registration drivers."""
from __future__ import annotations

import secrets


_UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_LOWER = "abcdefghjkmnpqrstuvwxyz"
_DIGITS = "23456789"
_SYMBOLS = "!@#$%^&*?_-+="


def generate_registration_password(length: int = 14) -> str:
    """Generate a password with all required character groups.

    Ambiguous characters are omitted because account material is often copied
    manually from the WebUI.  ``SystemRandom`` keeps credential generation
    separate from the non-security randomness used for browser timing.
    """
    length = max(12, int(length or 14))
    chooser = secrets.SystemRandom()
    groups = (_UPPER, _LOWER, _DIGITS, _SYMBOLS)
    chars = [chooser.choice(group) for group in groups]
    pool = "".join(groups)
    chars.extend(chooser.choice(pool) for _ in range(length - len(chars)))
    chooser.shuffle(chars)
    return "".join(chars)


def registration_password(length: int = 14) -> str:
    """Use the configured password or generate a unique per-account value."""
    try:
        from config import register as register_cfg

        configured = str(getattr(register_cfg, "REGISTER_PASSWORD", "") or "").strip()
        if configured:
            return configured
    except Exception:
        pass
    return generate_registration_password(length)


__all__ = ["generate_registration_password", "registration_password"]
