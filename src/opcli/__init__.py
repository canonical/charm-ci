# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""opcli — CLI tool for operator development workflows."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("opcli")
except PackageNotFoundError:
    # Running from a raw source checkout without an installed distribution
    # (e.g. PYTHONPATH=src without `uv sync`/`pip install`) — package
    # metadata isn't available, so fall back rather than crash on import.
    __version__ = "0.0.0+unknown"
