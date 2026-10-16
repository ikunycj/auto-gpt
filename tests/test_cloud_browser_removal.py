"""Regression coverage for removal of the two cloud browser integrations."""
from pathlib import Path
import subprocess
import sys

import pytest

from apps.web import config_editor
from config import codex, env_loader, roxybrowser
from core import codex_oauth
from registration.drivers.registry import get_driver


REMOVED_DRIVERS = ("browser_use", "browseruse", "browser-use", "bu", "skyvern", "sv")


@pytest.mark.parametrize("name", REMOVED_DRIVERS)
def test_removed_registration_drivers_are_rejected(name):
    with pytest.raises(ValueError, match="不支持的 REGISTRATION_DRIVER"):
        get_driver(name)


@pytest.mark.parametrize("name", REMOVED_DRIVERS)
def test_removed_oauth_drivers_are_rejected_without_protocol_fallback(monkeypatch, name):
    monkeypatch.setattr(codex, "CODEX_OAUTH_DRIVER", name)
    monkeypatch.setattr(codex_oauth, "BrowserSession", lambda **_kw: pytest.fail("must not start protocol flow"))
    with pytest.raises(RuntimeError, match="不支持的 CODEX_OAUTH_DRIVER"):
        codex_oauth.run_codex_oauth("test@example.com", force=True)


@pytest.mark.parametrize("name", ("browser_use", "skyvern"))
def test_follow_registration_rejects_removed_driver(monkeypatch, name):
    monkeypatch.setattr(codex, "CODEX_OAUTH_DRIVER", "same_as_registration")
    monkeypatch.setattr(roxybrowser, "REGISTRATION_DRIVER", name)
    with pytest.raises(RuntimeError, match="不支持的 CODEX_OAUTH_DRIVER"):
        codex_oauth.run_codex_oauth("test@example.com", force=True)


def test_removed_config_fields_and_secrets_are_not_exposed():
    for field in config_editor.get_config():
        assert not field["key"].startswith(("BROWSER_USE_", "SKYVERN_"))
        assert field.get("browser_module") not in ("browser_use", "skyvern")
    assert not any(key.startswith(("BROWSER_USE_", "SKYVERN_")) for key in env_loader.SECRET_ENV_KEYS)


def test_fresh_registry_and_config_reload_support_only_remaining_drivers():
    code = """
import config
from registration.drivers.registry import available_drivers, get_driver
assert set(available_drivers()) == {'protocol', 'roxy', 'cloak', 'chrome_cdp'}
for name in available_drivers():
    assert get_driver(name).name == name
reloaded = config.reload_all()
assert 'config.browser_use' not in reloaded
assert 'config.skyvern' not in reloaded
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_frontend_no_longer_offers_cloud_modules_or_driver_options():
    source = (Path(__file__).resolve().parents[1] / "web/src/pages/ConfigPage.jsx").read_text()
    for name in ("browser_use", "skyvern"):
        assert f"id: '{name}'" not in source
        assert f'<option value="{name}">' not in source
    workspace = (Path(__file__).resolve().parents[1] / "web/src/components/WorkspaceBrowser.jsx").read_text()
    for name in ("protocol", "roxy", "cloak", "chrome_cdp"):
        assert f"value: '{name}'" in workspace
