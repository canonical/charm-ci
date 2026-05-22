"""Unit test fixtures for opcli.

Unit tests exercise local (non-CI) behaviour by default.  The autouse
fixture below removes GITHUB_ACTIONS from the environment so that tests
never accidentally trigger GitHub-Actions-specific code paths (e.g.
pushing rocks to GHCR) when the test suite runs inside GitHub Actions.

Tests that explicitly cover CI mode (e.g. TestArtifactsBuildCIMode) set
GITHUB_ACTIONS=true themselves via ``patch.dict(os.environ, ...)``.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def clear_github_actions_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure GITHUB_ACTIONS is unset for every unit test."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
