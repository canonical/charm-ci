# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for the GitHub Actions workflows."""

from pathlib import Path

import pytest
from ruamel.yaml import YAML

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


def test_integration_build_status_query_uses_small_pages() -> None:
    """Build status queries avoid GitHub timeouts on workflows with many jobs."""
    workflow = (_ROOT / ".github/workflows/integration-test.yml").read_text()

    assert "gh api --paginate" in workflow
    assert "jobs?per_page=50" in workflow
    assert "jq --slurp" in workflow
    assert 'gh run view "${{ github.run_id }}"' not in workflow


def test_integration_gates_build_and_tests_on_changes() -> None:
    """Every expensive job waits for the change check and honours its result."""
    jobs = YAML(typ="safe").load(_ROOT / ".github/workflows/integration-test.yml")["jobs"]

    assert jobs["find-changes"]["steps"][-1]["run"] == ".charm-ci/.github/scripts/find-changes.sh"
    for name in ("build", "test-matrix", "test"):
        assert "find-changes" in jobs[name]["needs"]
        assert jobs[name]["if"] == "needs.find-changes.outputs.has-code-changes == 'true'"
