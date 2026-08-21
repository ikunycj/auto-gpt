"""RoxyBrowser driver adapter.

The optional Roxy/Selenium implementation is imported only when this adapter is
selected, so the Web service and other drivers do not require it at startup.
"""
from registration.application.models import RegistrationResult


def run(**kwargs) -> RegistrationResult:
    from registration.drivers.roxy.implementation import run_roxy_registration

    return run_roxy_registration(**kwargs)


__all__ = ["run"]
