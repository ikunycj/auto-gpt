"""Stock Chrome CDP registration driver adapter."""
from registration.application.models import RegistrationResult


def run(**kwargs) -> RegistrationResult:
    from registration.drivers.chrome_cdp.implementation import run_chrome_cdp_registration

    return run_chrome_cdp_registration(**kwargs)


__all__ = ["run"]
