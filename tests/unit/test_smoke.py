# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Smoke test to verify the test infrastructure works."""

import importlib
from importlib.metadata import PackageNotFoundError

import pytest

import opcli


def test_import_opcli() -> None:
    """Verify opcli package can be imported."""
    assert opcli.__version__


def test_version_falls_back_when_package_metadata_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """__version__ degrades gracefully instead of crashing on import.

    This simulates running from a raw source checkout without an installed
    distribution (e.g. PYTHONPATH=src without `uv sync`/`pip install`).
    """

    def _raise(_name: str) -> str:
        raise PackageNotFoundError(_name)

    monkeypatch.setattr("importlib.metadata.version", _raise)
    try:
        importlib.reload(opcli)
        assert opcli.__version__ == "0.0.0+unknown"
    finally:
        importlib.reload(opcli)  # restore the real version for later tests
