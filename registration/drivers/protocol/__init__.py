"""Pure HTTP protocol registration adapter."""

from registration.application.models import RegistrationResult


def run(**kwargs) -> RegistrationResult:
    """Run the configured protocol flow without loading browser drivers."""
    from .attachment_implementation import run_attachment_protocol_registration

    return run_attachment_protocol_registration(**kwargs)


__all__ = ["run"]

