"""Pure HTTP protocol registration adapter."""

from registration.application.models import RegistrationRequest, RegistrationResult


def run(**kwargs) -> RegistrationResult:
    """Delegate to the application-owned protocol flow lazily."""
    from registration.application.use_case import _run_protocol_registration

    return _run_protocol_registration(**kwargs)


__all__ = ["run"]

