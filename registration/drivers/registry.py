"""Lazy registry for protocol and optional browser drivers.

The registry is deliberately free of imports from Selenium, Playwright, Roxy,
Browser Use or Skyvern.  Those dependencies are loaded only after a caller
selects the corresponding driver, making each integration detachable.
"""
from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from registration.application.models import RegistrationRequest, RegistrationResult
from .base import DriverUnavailableError, RegistrationDriver


@dataclass(frozen=True, slots=True)
class LazyFunctionDriver:
    name: str
    module_name: str
    function_name: str

    def run(self, request: RegistrationRequest) -> RegistrationResult:
        try:
            module = import_module(self.module_name)
            function = getattr(module, self.function_name)
        except (ImportError, AttributeError) as exc:
            raise DriverUnavailableError(
                f"注册驱动 {self.name!r} 不可用，请安装其可选依赖或检查配置: {exc}"
            ) from exc
        try:
            return function(**request.as_kwargs())
        except ImportError as exc:
            raise DriverUnavailableError(
                f"注册驱动 {self.name!r} 缺少可选依赖: {exc}"
            ) from exc


_ALIASES: dict[str, str] = {
    "api": "protocol",
    "http": "protocol",
    "roxybrowser": "roxy",
    "fingerprint": "roxy",
    "browser": "roxy",
    "cloakbrowser": "cloak",
    "browseruse": "browser_use",
    "browser-use": "browser_use",
    "bu": "browser_use",
    "sv": "skyvern",
    "chrome-cdp": "chrome_cdp",
    "chrome": "chrome_cdp",
    "local_chrome": "chrome_cdp",
    "system_chrome": "chrome_cdp",
}

_DRIVERS: dict[str, RegistrationDriver] = {
    "protocol": LazyFunctionDriver("protocol", "registration.drivers.protocol", "run"),
    "roxy": LazyFunctionDriver("roxy", "registration.drivers.roxy", "run"),
    "cloak": LazyFunctionDriver("cloak", "registration.drivers.cloak", "run"),
    "browser_use": LazyFunctionDriver(
        "browser_use", "registration.drivers.browser_use", "run"
    ),
    "skyvern": LazyFunctionDriver("skyvern", "registration.drivers.skyvern", "run"),
    "chrome_cdp": LazyFunctionDriver(
        "chrome_cdp", "registration.drivers.chrome_cdp", "run"
    ),
}


def normalize_driver_name(name: str | None) -> str:
    value = str(name or "protocol").strip().lower()
    return _ALIASES.get(value, value)


def register_driver(name: str, driver: RegistrationDriver, *, aliases: tuple[str, ...] = ()) -> None:
    """Register an adapter for an application/plugin supplied driver."""
    canonical = normalize_driver_name(name)
    if not canonical or not hasattr(driver, "run"):
        raise ValueError("driver 必须提供非空名称和 run(request) 方法")
    _DRIVERS[canonical] = driver
    for alias in aliases:
        _ALIASES[str(alias).strip().lower()] = canonical


def available_drivers() -> tuple[str, ...]:
    return tuple(_DRIVERS)


def get_driver(name: str | None = None) -> RegistrationDriver:
    canonical = normalize_driver_name(name)
    driver = _DRIVERS.get(canonical)
    if driver is None:
        choices = ", ".join(available_drivers())
        raise ValueError(f"不支持的 REGISTRATION_DRIVER={name!r}，可选 {choices}")
    return driver
