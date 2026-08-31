"""Compatibility CLI entrypoint.

The implementation lives in :mod:`apps.cli.main`; this file keeps the historic
``uv run --locked python main.py`` command and ``import main`` path stable.
"""
from __future__ import annotations

import importlib
import sys

if __name__ == "__main__":
    from apps.cli.main import main

    main()
else:
    _implementation = importlib.import_module("apps.cli.main")
    sys.modules[__name__] = _implementation
