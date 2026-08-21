"""Stable input/output models shared by CLI, Web and driver adapters."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class RegistrationRequest:
    """Parameters for one registration attempt.

    ``birthday`` may be omitted by callers that want the application layer to
    generate one.  ``batch_dir`` is intentionally opaque to drivers so each
    adapter can decide how to archive its artifacts.
    """

    email: str
    name: str
    birthday: str | None = None
    proxy: str | None = None
    otp_code: str | None = None
    batch_dir: Path | None = None

    def as_kwargs(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "name": self.name,
            "birthday": self.birthday,
            "proxy": self.proxy,
            "otp_code": self.otp_code,
            "batch_dir": self.batch_dir,
        }


RegistrationResult = Mapping[str, Any]

