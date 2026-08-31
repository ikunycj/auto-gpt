import sys

import pytest

import web


def test_webui_uses_fixed_frontend_and_backend_ports():
    assert web.WEBUI_HOST == "127.0.0.1"
    assert web.FRONTEND_PORT == 5555
    assert web.BACKEND_PORT == 6666
    assert web.FRONTEND_URL == "http://127.0.0.1:5555"
    assert web.BACKEND_URL == "http://127.0.0.1:6666"


@pytest.mark.parametrize(
    "arguments",
    [
        ["web.py", "--host", "0.0.0.0"],
        ["web.py", "--port", "5000"],
        ["web.py", "--port", "5555"],
    ],
)
def test_webui_rejects_non_backend_listener(monkeypatch, arguments):
    monkeypatch.setattr(sys, "argv", arguments)

    with pytest.raises(SystemExit, match="2"):
        web.main()
