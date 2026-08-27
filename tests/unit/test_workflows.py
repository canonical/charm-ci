# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for GitHub Actions workflow security."""

from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[2]
_VERBOSE_SPREAD_WORKFLOWS = (
    ".github/workflows/doc-test.yml",
    ".github/workflows/integration-test.yml",
    ".github/workflows/test-integration.yml",
)


@pytest.mark.parametrize("workflow_path", _VERBOSE_SPREAD_WORKFLOWS)
def test_verbose_spread_password_is_masked(workflow_path: str) -> None:
    """Verbose Spread runs use a password registered with GitHub log masking."""
    workflow = (_ROOT / workflow_path).read_text()

    mask_index = workflow.index('echo "::add-mask::$password"')
    export_index = workflow.index('echo "SPREAD_CI_PASSWORD=$password" >> "$GITHUB_ENV"')
    run_index = workflow.index("opcli spread run -- -vv")

    assert mask_index < export_index < run_index
    assert 'opcli spread run -- -vv -pass "$SPREAD_CI_PASSWORD"' in workflow
