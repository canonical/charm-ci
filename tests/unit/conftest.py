# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit test fixtures for opcli.

Unit tests exercise local (non-CI) behaviour by default.  The autouse
fixture below removes GITHUB_ACTIONS from the environment so that tests
never accidentally trigger GitHub-Actions-specific code paths (e.g.
pushing rocks to GHCR) when the test suite runs inside GitHub Actions.

Tests that explicitly cover CI mode (e.g. TestArtifactsBuildCIMode) set
GITHUB_ACTIONS=true themselves via ``patch.dict(os.environ, ...)``.
"""

import logging

import pytest


@pytest.fixture(autouse=True)
def clear_github_actions_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure GITHUB_ACTIONS is unset for every unit test."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


@pytest.fixture(autouse=True)
def reset_opcli_logger() -> None:
    """Reset the "opcli" logger's handlers/level/propagate after every test.

    Any test that invokes the CLI (even indirectly, e.g. via CliRunner)
    runs opcli.app's ``--verbose``/``--version`` Typer callback, which
    configures the "opcli" logger (handlers, level, ``propagate=False``)
    as a side effect. Without resetting this, that configuration leaks
    across tests and files, breaking pytest's ``caplog`` fixture for any
    later test that logs through an ``opcli.*`` logger (``caplog``
    depends on propagation to the root logger to capture records).
    """
    yield
    opcli_logger = logging.getLogger("opcli")
    opcli_logger.handlers.clear()
    opcli_logger.setLevel(logging.NOTSET)
    opcli_logger.propagate = True
