from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from registration.application.models import RegistrationRequest
from registration.drivers.registry import get_driver, normalize_driver_name, register_driver
from registration.ports.stop_signal import (
    CallbackStopSignal,
    StopRequested,
    bind_stop_signal,
    check_stop_requested,
)


def test_registration_request_is_driver_neutral(tmp_path: Path):
    request = RegistrationRequest("a@example.com", "Alice", batch_dir=tmp_path)
    assert request.as_kwargs() == {
        "email": "a@example.com",
        "name": "Alice",
        "birthday": None,
        "proxy": None,
        "otp_code": None,
        "batch_dir": tmp_path,
    }


def test_driver_aliases_and_plugin_registration():
    assert normalize_driver_name("browser-use") == "browser_use"
    assert get_driver("api").name == "protocol"

    class StubDriver:
        name = "stub"

        def run(self, request):
            return {"success": True, "email": request.email}

    register_driver("stub", StubDriver(), aliases=("stub-browser",))
    assert get_driver("stub-browser").run(RegistrationRequest("a@b.co", "A"))["success"]


def test_stop_signal_is_framework_neutral():
    with bind_stop_signal(CallbackStopSignal(lambda: True)):
        with pytest.raises(StopRequested):
            check_stop_requested()

    # No Web service is required for a standalone CLI/driver invocation.
    check_stop_requested()


def test_registry_import_does_not_load_optional_browser_implementations():
    root = Path(__file__).resolve().parents[1]
    code = (
        "from registration.drivers.registry import get_driver\n"
        "import sys\n"
        "get_driver('protocol')\n"
        "mods = ('registration.drivers.roxy.implementation', "
        "'registration.drivers.cloak.implementation', "
        "'registration.drivers.browser_use.implementation', "
        "'registration.drivers.skyvern.implementation')\n"
        "assert not any(name in sys.modules for name in mods)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

