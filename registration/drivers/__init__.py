"""Pluggable registration drivers.

The package itself is import-light; concrete adapters and optional browser
dependencies are loaded by :mod:`registration.drivers.registry` on demand.
"""

__all__ = [
    "DriverUnavailableError",
    "RegistrationDriver",
    "available_drivers",
    "get_driver",
    "register_driver",
]


def __getattr__(name: str):
    if name in {"DriverUnavailableError", "RegistrationDriver"}:
        from .base import DriverUnavailableError, RegistrationDriver

        return {"DriverUnavailableError": DriverUnavailableError, "RegistrationDriver": RegistrationDriver}[name]
    if name in {"available_drivers", "get_driver", "register_driver"}:
        from .registry import available_drivers, get_driver, register_driver

        return {"available_drivers": available_drivers, "get_driver": get_driver, "register_driver": register_driver}[name]
    raise AttributeError(name)
