"""Skyvern Browser Session driver adapter."""
from registration.application.models import RegistrationResult


def run(**kwargs) -> RegistrationResult:
    from registration.drivers.skyvern.implementation import run_skyvern_registration

    return run_skyvern_registration(**kwargs)


__all__ = ["run"]
