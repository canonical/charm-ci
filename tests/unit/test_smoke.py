# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Smoke test to verify the test infrastructure works."""

import opcli


def test_import_opcli() -> None:
    """Verify opcli package can be imported."""
    assert opcli.__version__
