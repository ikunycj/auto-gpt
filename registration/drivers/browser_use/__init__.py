"""Browser Use Cloud driver adapter."""
from registration.application.models import RegistrationResult


def run(**kwargs) -> RegistrationResult:
    from registration.drivers.browser_use.implementation import run_browser_use_registration

    return run_browser_use_registration(**kwargs)


__all__ = ["run"]
