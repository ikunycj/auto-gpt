"""CloakBrowser driver adapter."""
from registration.application.models import RegistrationResult


def run(**kwargs) -> RegistrationResult:
    from registration.drivers.cloak.implementation import run_cloak_registration

    return run_cloak_registration(**kwargs)


__all__ = ["run"]
