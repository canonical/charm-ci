# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Normalize release changes without losing their inclusion provenance."""

import re
from typing import TYPE_CHECKING, Literal

from opcli.models.changelog import ChangelogRecord, GitID

if TYPE_CHECKING:
    from opcli.core.changelog_collect import ChangeSource, PullRequest, Snapshot
    from opcli.models.changelog import ReleaseRange

_TITLE = re.compile(r"^([a-z]+)(?:\([^()\r\n]+\))?(!)?:\s+(.+)$")
_REVERT = re.compile(r"(?m)^This reverts commit ([a-f0-9]{40})\.$")
_CHERRY = re.compile(r"(?m)^\(cherry picked from commit ([a-f0-9]{40})\)$")
_LABELS = {
    f"changelog:{name.lower()}": name
    for name in ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security", "Skip")
}
_BASE = {
    "feat": "Added",
    "perf": "Changed",
    "fix": "Fixed",
    "docs": "Documentation",
    "build": "Dependencies",
    "refactor": "Changed",
    "test": "Changed",
    "ci": "Changed",
    "chore": "Changed",
    "revert": "Reverted",
}


class Entry(ChangelogRecord):
    """One user-facing change with stable evidence."""

    entry_id: str
    commits: tuple[GitID, ...]
    pr: int | None
    summary: str
    url: str
    category: str
    signal: str
    breaking: bool = False
    security: bool = False
    deprecation: bool = False
    author: str
    timestamp: str
    paths: tuple[str, ...]
    review: bool
    confidence: Literal["high", "low"]
    warnings: tuple[str, ...] = ()


def normalize(
    release: "ReleaseRange", snapshot: "Snapshot"
) -> tuple[tuple[Entry, ...], tuple[str, ...]]:
    """Group proven related commits and keep explicit exclusions."""
    excluded = _reverts(snapshot)
    removed = {sha for record in excluded for sha in re.findall(r"[a-f0-9]{40}", record)}
    included = {
        item.commit.sha: item for item in snapshot.sources if item.commit.sha not in removed
    }
    prs = {
        pr.number: pr
        for source in included.values()
        for pr in source.prs
        if pr.merge_sha in included
        and pr.base == release.source_tip.ref.removeprefix("refs/heads/")
    }
    group: dict[str, list[ChangeSource]] = {}
    for source in included.values():
        key = _group(source, included, prs)
        group.setdefault(key, []).append(source)
    entries: list[Entry] = []
    for key, sources in group.items():
        pr = prs.get(int(key.removeprefix("pr-"))) if key.startswith("pr-") else None
        entry = _entry(release.repository, key, sources, pr)
        if entry.category == "Skip":
            excluded.append(f"Explicit changelog:skip: {', '.join(entry.commits)}")
        else:
            entries.append(entry)
    return tuple(entries), tuple(excluded)


def _reverts(snapshot: "Snapshot") -> list[str]:
    sources = {item.commit.sha: item for item in snapshot.sources}
    edges = {
        item.commit.sha: match[1]
        for item in snapshot.sources
        if item.commit.subject.startswith("Revert ")
        and (match := _REVERT.search(item.commit.body))
        and match[1] in sources
    }
    return [
        f"Introduced and reverted in range: {target}, {revert}"
        for revert, target in edges.items()
        if target not in edges
        and revert not in edges.values()
        and list(edges.values()).count(target) == 1
    ]


def _group(
    source: "ChangeSource",
    included: dict[str, "ChangeSource"],
    prs: dict[int, "PullRequest"],
) -> str:
    related = [pr.number for pr in prs.values() if pr.merge_sha == source.commit.sha]
    if not related:
        related = [pr.number for pr in source.prs if pr.number in prs]
    if len(related) == 1:
        return f"pr-{related[0]}"
    cherry = _CHERRY.search(source.commit.body)
    if cherry and cherry[1] in included and not _CHERRY.search(included[cherry[1]].commit.body):
        return _group(included[cherry[1]], included, prs)
    return source.commit.sha


def _entry(
    repository: str, key: str, sources: list["ChangeSource"], pr: "PullRequest | None"
) -> Entry:
    preferred = pr.merge_sha if pr else key
    commit = next(
        (source.commit for source in sources if source.commit.sha == preferred), sources[0].commit
    )
    immutable = _TITLE.match(commit.subject) if pr and pr.merge_sha == commit.sha else None
    current = _TITLE.match(pr.title) if pr else None
    category, summary, signal = _classify(commit.subject, commit.body, pr, immutable, current)
    labels = set(pr.labels) if pr else set()
    title = immutable or current
    warnings = ("Merge-time PR metadata unavailable",) if pr else ()
    review = bool(warnings) or category == "Unclassified"
    link = f"pull/{pr.number}" if pr else f"commit/{commit.sha}"
    return Entry(
        entry_id=key,
        commits=tuple(source.commit.sha for source in sources),
        pr=pr.number if pr else None,
        summary=summary,
        url=f"https://github.com/{repository}/{link}",
        category=category,
        signal=signal,
        breaking=bool(title and title[2])
        or "breaking-change" in labels
        or bool(re.search(r"(?m)^BREAKING CHANGE: \S", commit.body)),
        security="changelog:security" in labels
        or bool(re.search(r"(?m)^Changelog-Security: true$", commit.body)),
        deprecation="changelog:deprecated" in labels
        or bool(re.search(r"(?m)^Changelog-Deprecated: true$", commit.body)),
        author=pr.author if pr else commit.author,
        timestamp=pr.merged_at if pr else commit.committed_at,
        paths=tuple(sorted({path for source in sources for path in source.commit.paths})),
        review=review,
        confidence="low" if review else "high",
        warnings=warnings,
    )


def _classify(  # noqa: PLR0911
    subject: str,
    body: str,
    pr: "PullRequest | None",
    immutable: re.Match[str] | None,
    current: re.Match[str] | None,
) -> tuple[str, str, str]:
    labels = set(pr.labels) if pr else set()
    overrides = {_LABELS[label] for label in labels if label in _LABELS} - {
        "Security",
        "Deprecated",
    }
    unknown = {
        label for label in labels if label.startswith("changelog:") and label not in _LABELS
    }
    if unknown or len(overrides) > 1:
        return "Unclassified", subject, "conflicting or unknown labels"
    if "Skip" in overrides:
        # Live labels cannot silently remove a change from the first review.
        if re.search(r"(?m)^Changelog: skip$", body):
            return "Skip", subject, "commit trailer"
        return "Unclassified", subject, "unverified skip label"
    if _REVERT.search(body) and subject.startswith("Revert "):
        return "Reverted", subject, "revert trailer"
    if immutable:
        base = _BASE.get(immutable[1], "Unclassified")
        if overrides and base not in overrides:
            return "Unclassified", immutable[3], "conflicting classification evidence"
        return base, immutable[3], "merge commit title"
    if overrides and pr:
        return next(iter(overrides)), pr.title, "observed PR label"
    if current:
        return _BASE.get(current[1], "Unclassified"), current[3], "observed PR title"
    return "Unclassified", subject, "no verified classification"
