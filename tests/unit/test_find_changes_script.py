# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Regression tests for skipping integration tests on pull requests that change only ignored files."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / ".github" / "scripts" / "find-changes.sh"
_DOCS = r"\.md$|^LICENSE$"


@pytest.mark.parametrize(
    ("pr", "patterns", "has_code_changes"),
    [
        ("touch LICENSE naïve.md", _DOCS, "false"),
        ("touch README.md && echo x >> src/charm.py", _DOCS, "true"),
        ("git mv src/charm.py docs.md", _DOCS, "true"),
        ("touch a-x", r"-x$|\.md$", "false"),
        ("touch README.md", "", "true"),
    ],
)
def test_skips_pull_requests_that_only_change_ignored_files(
    tmp_path: Path, pr: str, patterns: str, has_code_changes: str
) -> None:
    """Build and tests are skipped only when every changed file is ignored."""
    result = _run_script(tmp_path, pr, patterns)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "output").read_text() == f"has-code-changes={has_code_changes}\n"


@pytest.mark.parametrize("patterns", ["(", "\\.md$\n"])
def test_rejects_patterns_that_would_ignore_everything(tmp_path: Path, patterns: str) -> None:
    """An invalid pattern or one matching every path fails instead of skipping the tests."""
    result = _run_script(tmp_path, "touch src/x.py", patterns)

    assert result.returncode != 0
    assert "::error::ignored-file-patterns" in result.stdout
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize(
    ("event", "merge"), [("pull_request", False), ("workflow_dispatch", True)]
)
def test_runs_everything_when_it_cannot_diff(tmp_path: Path, event: str, merge: bool) -> None:
    """Without a PR merge commit to diff, nothing is skipped."""
    result = _run_script(tmp_path, "touch README.md", _DOCS, event=event, merge=merge)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "output").read_text() == "has-code-changes=true\n"


def _run_script(
    tmp_path: Path, pr: str, patterns: str, event: str = "pull_request", merge: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run the script in a repo where the `pr` command makes the PR commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env |= {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    setup = (
        "git init -q -b main && git config user.name t && git config user.email t@example.com"
        " && mkdir src && seq 100 > src/charm.py && git add -A && git commit -q -m base"
        f" && git checkout -q -b pr && {pr} && git add -A && git commit -q -m pr"
    )
    if merge:
        setup += " && git checkout -q main && git merge -q --no-ff -m merge pr"
    subprocess.run(setup, shell=True, cwd=repo, env=env, check=True, capture_output=True)

    env |= {
        "EVENT_NAME": event,
        "IGNORED_FILE_PATTERNS": patterns,
        "GITHUB_OUTPUT": str(tmp_path / "output"),
    }
    return subprocess.run(
        [str(SCRIPT)], cwd=repo, env=env, check=False, capture_output=True, text=True
    )
