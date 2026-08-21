"""Application services for account registration.

Imports are lazy so loading one optional driver never imports the full
application graph (and its protocol dependencies).
"""

__all__ = ["RegistrationRequest", "run_registration"]


def __getattr__(name: str):
    if name == "RegistrationRequest":
        from .models import RegistrationRequest

        return RegistrationRequest
    if name == "run_registration":
        from .use_case import run_registration

        return run_registration
    raise AttributeError(name)
