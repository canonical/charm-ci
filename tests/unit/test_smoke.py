# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Smoke test to verify the test infrastructure works."""

from __future__ import annotations

import opcli


def test_import_opcli() -> None:
    """Verify opcli package can be imported."""
    assert opcli.__version__
