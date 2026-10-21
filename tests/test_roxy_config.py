from __future__ import annotations

import runpy
from pathlib import Path
from unittest.mock import Mock

import pytest

from config import env_loader, roxybrowser
from core.roxybrowser_client import RoxyBrowserClient

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "roxybrowser.py"


def load_roxy_config(monkeypatch, workspace_id, project_id):
    # Do not load the user's .env into this isolated configuration evaluation.
    monkeypatch.setattr(env_loader, "_LOADED", True)
    for key, value in (
        ("ROXY_WORKSPACE_ID", workspace_id),
        ("ROXY_PROJECT_ID", project_id),
    ):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    return runpy.run_path(str(CONFIG_PATH))


@pytest.mark.parametrize("workspace_id,project_id", [(None, None), ("", ""), ("144436", "")])
def test_unconfigured_roxy_ids_do_not_fall_back_to_another_team(
    monkeypatch, workspace_id, project_id,
):
    cfg = load_roxy_config(monkeypatch, workspace_id, project_id)

    assert cfg["ROXY_WORKSPACE_ID"] == (workspace_id or "")
    assert cfg["ROXY_PROJECT_ID"] == ""


def test_explicit_roxy_team_and_project_are_preserved(monkeypatch):
    cfg = load_roxy_config(monkeypatch, "144436", "154968")

    assert cfg["ROXY_WORKSPACE_ID"] == "144436"
    assert cfg["ROXY_PROJECT_ID"] == "154968"


def test_blank_project_is_omitted_from_create_request(monkeypatch):
    cfg = load_roxy_config(monkeypatch, "144436", "")
    monkeypatch.setattr(roxybrowser, "ROXY_WORKSPACE_ID", cfg["ROXY_WORKSPACE_ID"])
    monkeypatch.setattr(roxybrowser, "ROXY_PROJECT_ID", cfg["ROXY_PROJECT_ID"])
    monkeypatch.setattr(roxybrowser, "ROXY_CREATE_USE_PROXY_POOL", False)
    monkeypatch.setattr(roxybrowser, "ROXY_PROFILE_CREATE_PAYLOAD", {})
    client = RoxyBrowserClient(api_base="http://127.0.0.1", token="test-token")
    client.request = Mock(return_value={"data": {"dirId": "test-profile"}})
    try:
        assert client.create_profile() == "test-profile"
        payload = client.request.call_args.kwargs["json_body"]
        assert payload["workspaceId"] == 144436
        assert "projectId" not in payload
    finally:
        client.http.close()
