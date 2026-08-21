"""Driver contract and common adapter errors."""
from __future__ import annotations

from typing import Protocol

from registration.application.models import RegistrationRequest, RegistrationResult


class DriverUnavailableError(RuntimeError):
    """Raised when a selected optional driver cannot be loaded."""


class RegistrationDriver(Protocol):
    """Minimal contract implemented by every registration backend."""

    name: str

    def run(self, request: RegistrationRequest) -> RegistrationResult:
        """Execute one registration attempt and return a result mapping."""

