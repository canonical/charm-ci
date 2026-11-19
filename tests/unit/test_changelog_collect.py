# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest

from opcli.core.changelog_git import GitHistory
from opcli.core.exceptions import ValidationError
from opcli.core.subprocess import run_command


@pytest.fixture
def history(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()

    def git(*args):
        return run_command(["git", *args], cwd=str(repo), stream=False, quiet=True).stdout.strip()

    git("init", "--initial-branch=main")
    git("config", "user.name", "Release Fixture")
    git("config", "user.email", "fixture@example.com")
    (repo / "base").write_text("base")
    git("add", "base")
    git("commit", "-m", "base")
    base = git("rev-parse", "HEAD")
    git("switch", "-c", "side")
    (repo / "side").write_text("side")
    git("add", "side")
    git("commit", "-m", "feat: add a side change")
    side = git("rev-parse", "HEAD")
    git("switch", "main")
    (repo / "main").write_text("main")
    git("add", "main")
    git("commit", "-m", "fix: add a main change")
    main = git("rev-parse", "HEAD")
    git("merge", "--no-ff", "side", "-m", "merge")
    head = git("rev-parse", "HEAD")
    return GitHistory(repo), base, side, main, head


def test_collect_includes_commits_off_first_parent(history):
    git, base, side, main, head = history
    commits = git.collect(base, head)
    assert {item.sha for item in commits} == {side, main, head}
    assert next(item for item in commits if item.sha == side).paths == ("side",)


def test_collect_rejects_non_ancestor_range(history):
    git, _, side, main, _ = history
    with pytest.raises(ValidationError, match="ancestor"):
        git.collect(side, main)


def test_visible_shallow_frontier_cannot_hide_a_side_ancestor(history):
    git, base, side, _, head = history
    (git.path / ".git" / "shallow").write_text(f"{side}\n")
    with pytest.raises(ValidationError, match="incomplete"):
        git.collect(base, head)
