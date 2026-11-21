# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Collect and freeze metadata without changing the Git inclusion set."""

from typing import TYPE_CHECKING, Literal

from opcli.core.changelog_git import Commit
from opcli.core.changelog_github import APIError, mapping, sequence, text, verify_comparison
from opcli.models.changelog import ChangelogRecord, GitID

if TYPE_CHECKING:
    from opcli.core.changelog_git import GitHistory
    from opcli.core.changelog_github import GitHub
    from opcli.models.changelog import ReleaseRange

_COMMIT_PATH_LIMIT = 3000


class PullRequest(ChangelogRecord):
    """Observed PR metadata, not necessarily its state at merge time."""

    number: int
    title: str
    author: str
    labels: tuple[str, ...]
    merge_sha: GitID
    base: str
    merged_at: str


class ChangeSource(ChangelogRecord):
    """A commit plus its candidate PRs."""

    commit: Commit
    prs: tuple[PullRequest, ...] = ()


class Snapshot(ChangelogRecord):
    """Complete Git coverage and its frozen enrichment."""

    retrieval: Literal["compare", "targeted-git", "full-git"]
    sources: tuple[ChangeSource, ...]
    warnings: tuple[str, ...]


def collect(
    release: "ReleaseRange", github: "GitHub", git: "GitHistory", *, allow_full: bool = False
) -> Snapshot:
    """Try metadata-only collection, otherwise use targeted Git."""
    warnings: list[str] = []
    prefix = f"/repos/{release.repository}"
    try:
        commits = _compare(github, prefix, release.from_sha, release.to_sha)
        retrieval: Literal["compare", "targeted-git", "full-git"] = "compare"
    except APIError as exc:
        warnings.append(f"Compare fallback: {exc}")
        commits, full = git.fetch(
            release.repository, release.from_sha, release.to_sha, allow_full=allow_full
        )
        retrieval = "full-git" if full else "targeted-git"
    sources: list[ChangeSource] = []
    for commit in commits:
        try:
            prs = _pulls(github, prefix, commit.sha)
        except APIError:
            warnings.append(
                f"PR metadata could not be collected for {commit.sha}; review required"
            )
            prs = ()
        sources.append(ChangeSource(commit=commit, prs=prs))
    if any(source.prs for source in sources):
        warnings.append("PR metadata is an observation, not an archived merge-time snapshot")
    return Snapshot(retrieval=retrieval, sources=tuple(sources), warnings=tuple(warnings))


def _compare(github: "GitHub", prefix: str, base: str, head: str) -> tuple[Commit, ...]:
    path = f"{prefix}/compare/{base}...{head}"
    pages = [mapping(page) for page in github.page_records(path, key="commits")]
    rows = [mapping(row) for page in pages for row in sequence(page.get("commits"))]
    shas = tuple(text(row.get("sha")) for row in rows)
    for page in pages:
        verify_comparison(page, base, head, shas)
    # Compare's aggregate path list is capped and cannot provide per-commit paths.
    # The per-commit endpoint has its own documented 3,000 path limit.
    return tuple(_compare_commit(github, prefix, sha) for sha in reversed(shas))


def _compare_commit(github: "GitHub", prefix: str, sha: str) -> Commit:
    path = f"{prefix}/commits/{sha}"
    pages = [mapping(page) for page in github.page_records(path, key="files")]
    files = [mapping(item) for page in pages for item in sequence(page.get("files"))]
    if len(files) >= _COMMIT_PATH_LIMIT or any(page.get("sha") != sha for page in pages):
        raise APIError("commit changed paths may be truncated")
    paths = {text(item.get("filename")) for item in files}
    if len(paths) != len(files):
        raise APIError("commit changed paths repeat across pages")
    paths.update(text(item["previous_filename"]) for item in files if "previous_filename" in item)
    record = mapping(pages[0].get("commit"))
    author = mapping(record.get("author"))
    subject, _, body = text(record.get("message")).partition("\n")
    return Commit(
        sha=sha,
        parents=tuple(
            text(mapping(item).get("sha")) for item in sequence(pages[0].get("parents"))
        ),
        subject=subject,
        body=body.lstrip("\n"),
        author=text(author.get("name")),
        authored_at=text(author.get("date")),
        committed_at=text(mapping(record.get("committer")).get("date")),
        paths=tuple(sorted(paths)),
    )


def _pulls(github: "GitHub", prefix: str, sha: str) -> tuple[PullRequest, ...]:
    result: dict[int, PullRequest] = {}
    for item in github.pages(f"{prefix}/commits/{sha}/pulls"):
        row = mapping(item)
        if row.get("merged_at") is None:
            continue
        number = row.get("number")
        if type(number) is not int or number < 1:
            raise APIError("invalid PR number")
        user = row.get("user")
        result[number] = PullRequest(
            number=number,
            title=text(row.get("title")),
            author=text(mapping(user).get("login")) if user else "",
            labels=tuple(
                sorted(text(mapping(label).get("name")) for label in sequence(row.get("labels")))
            ),
            merge_sha=text(row.get("merge_commit_sha")),
            base=text(mapping(row.get("base")).get("ref")),
            merged_at=text(row.get("merged_at")),
        )
    return tuple(result[number] for number in sorted(result))
