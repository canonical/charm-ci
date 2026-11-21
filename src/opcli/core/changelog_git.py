# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Exact release history from an isolated, checkout-free Git repository."""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import TYPE_CHECKING

from opcli.core.exceptions import ValidationError
from opcli.core.subprocess import run_command
from opcli.models.changelog import ChangelogRecord, GitID

if TYPE_CHECKING:
    from opcli.core.subprocess import SubprocessResult

_DEPTHS = (32, 64, 128, 256, 512)


class Commit(ChangelogRecord):
    """Git metadata and paths for one exact commit."""

    sha: GitID
    parents: tuple[GitID, ...]
    subject: str
    body: str
    author: str
    authored_at: str
    committed_at: str
    paths: tuple[str, ...]


class IncompleteHistoryError(ValidationError):
    """The available graph cannot yet prove range completeness."""


class GitHistory:
    """Read Git objects without checking out or running repository code."""

    def __init__(self, path: Path, token: str = "") -> None:
        self.path = path
        self._env = {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_CONFIG_COUNT": "0",
        }
        if token:
            credential = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
            self._env.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credential}",
                }
            )

    def fetch(
        self, repository: str, base: str, head: str, *, allow_full: bool = False
    ) -> tuple[tuple[Commit, ...], bool]:
        """Fetch targeted objects, deepening when completeness is not proven."""
        _check_ids(base, head)
        if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repository):
            raise ValidationError("invalid GitHub repository")
        self._git("init", "--bare", "--template=")
        self._git("remote", "add", "origin", f"https://github.com/{repository}.git")
        for depth in _DEPTHS:
            self._git(
                "fetch", "--no-tags", "--filter=blob:none", f"--depth={depth}", "origin", head
            )
            try:
                return self.collect(base, head), False
            except IncompleteHistoryError:
                continue
        if not allow_full:
            raise IncompleteHistoryError("release history is incomplete after bounded deepening")
        shallow = self._git("rev-parse", "--is-shallow-repository").stdout.strip() == "true"
        flags = ["--unshallow"] if shallow else []
        self._git("fetch", "--no-tags", "--filter=blob:none", *flags, "origin", head)
        return self.collect(base, head), True

    def collect(self, base: str, head: str) -> tuple[Commit, ...]:
        """Enumerate all parents, allowing shallow history only with a proof."""
        _check_ids(base, head)
        shallow_file = Path(self._git("rev-parse", "--git-path", "shallow").stdout.strip())
        if not shallow_file.is_absolute():
            shallow_file = self.path / shallow_file
        shallow = set(shallow_file.read_text().splitlines()) if shallow_file.exists() else set()
        parent_check = self._git("merge-base", "--is-ancestor", base, head, check=False)
        if parent_check.returncode != 0:
            if shallow:
                raise IncompleteHistoryError(
                    "release ancestry cannot be proven from incomplete history"
                )
            raise ValidationError("from_sha is not an ancestor of to_sha")
        span = f"{base}..{head}"
        commits = self._git("rev-list", "--topo-order", span).stdout.splitlines()
        if shallow:
            descendants = self._git("rev-list", "--ancestry-path", span).stdout.splitlines()
            # With a shallow base, an alternate path can expose pre-base commits
            # that Git cannot yet subtract. Only the descendant-only cut is safe.
            if shallow.intersection(commits) or set(commits) != set(descendants):
                reachable = set(self._git("rev-list", head).stdout.splitlines())
                if shallow.intersection(reachable):
                    raise IncompleteHistoryError("release range has incomplete side history")
        first_parent = set(self._git("rev-list", "--first-parent", span).stdout.splitlines())
        ordered = sorted(commits, key=lambda sha: sha not in first_parent)
        return tuple(self._commit(sha) for sha in ordered)

    def _commit(self, sha: str) -> Commit:
        fields = self._git("show", "-s", "--format=%P%x00%s%x00%b%x00%an%x00%aI%x00%cI", sha)
        parents, subject, body, author, authored, committed = fields.stdout.rstrip("\n").split(
            "\0"
        )
        parent_ids = tuple(parents.split())
        diff = [parent_ids[0], sha] if parent_ids else ["--root", sha]
        paths = self._git("diff-tree", "-r", "--no-commit-id", "--name-only", "-z", *diff)
        return Commit(
            sha=sha,
            parents=parent_ids,
            subject=subject,
            body=body,
            author=author,
            authored_at=authored,
            committed_at=committed,
            paths=tuple(sorted(set(paths.stdout.split("\0")) - {""})),
        )

    def _git(self, *args: str, check: bool = True) -> SubprocessResult:
        return run_command(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "protocol.allow=never",
                "-c",
                "protocol.https.allow=always",
                "-c",
                "http.followRedirects=false",
                *args,
            ],
            cwd=str(self.path),
            check=check,
            timeout=90,
            stream=False,
            quiet=True,
            env=self._env,
        )


def _check_ids(*values: str) -> None:
    if not all(re.fullmatch(r"[a-f0-9]{40}", value) for value in values):
        raise ValidationError("Git history requires exact commit IDs")
