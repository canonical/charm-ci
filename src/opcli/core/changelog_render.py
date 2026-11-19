# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Render inert Markdown or RST from normalized release entries."""

import re
import string
from typing import TYPE_CHECKING, Literal

from opcli.core.exceptions import ValidationError

if TYPE_CHECKING:
    from opcli.core.changelog_normalize import Entry
    from opcli.models.changelog import ReleaseRange

Format = Literal["md", "rst"]
_ORDER = (
    "Added",
    "Changed",
    "Deprecated",
    "Removed",
    "Fixed",
    "Security",
    "Breaking changes",
    "Documentation",
    "Dependencies",
    "Reverted",
    "Unclassified",
)
_SECRET = re.compile(
    r"(?i)(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"AKIA[A-Z0-9]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|(?:https?|ssh|ftp)://)"
)
_MARKUP = re.compile(f"[{re.escape(string.punctuation)}]")


def render(
    release: "ReleaseRange",
    entries: tuple["Entry", ...],
    output_format: Format,
    *,
    use_date: bool = False,
) -> str:
    """Render base sections and flag sections using the same stable link."""
    heading = release.release_date if use_date else f"{release.release} - {release.release_date}"
    lines = [_heading(heading, output_format, "=", "##"), ""]
    for category in _ORDER:
        members = [entry for entry in entries if _in_section(entry, category)]
        if not members:
            continue
        lines.extend([_heading(category, output_format, "-", "###"), ""])
        lines.extend(_list_item(release.repository, entry, output_format) for entry in members)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _in_section(entry: "Entry", category: str) -> bool:
    return entry.category == category or (
        (category == "Breaking changes" and entry.breaking)
        or (category == "Security" and entry.security)
        or (category == "Deprecated" and entry.deprecation)
    )


def _heading(title: str, output_format: Format, underline: str, md: str) -> str:
    return f"{md} {title}" if output_format == "md" else f"{title}\n{underline * len(title)}"


def _list_item(repository: str, entry: "Entry", output_format: Format) -> str:
    label = " ".join(entry.summary.split())
    if _SECRET.search(label):
        raise ValidationError("unsafe or potentially private text in changelog summary")
    if not re.fullmatch(
        rf"https://github\.com/{re.escape(repository)}/(?:pull/[1-9][0-9]*|commit/[a-f0-9]{{40}})",
        entry.url,
    ):
        raise ValidationError("unsafe changelog source URL")
    label = _MARKUP.sub(lambda match: f"\\{match[0]}", label[:300]) or "Untitled change"
    if output_format == "rst":
        return f"- {label} (`source <{entry.url}>`__)"
    return f"- {label} ([source]({entry.url}))"
