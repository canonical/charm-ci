# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Core logic for ``opcli tutorial expand``.

Extracts shell commands from Markdown and reStructuredText tutorial files
and returns them as a shell script suitable for use with process substitution
in spread ``task.yaml``::

    runuser -l ubuntu -s /bin/bash -c 'set -ex; . <(opcli tutorial expand -- "$1")' _ "${SPREAD_PATH}${TUTORIAL}"

Ported and adapted from
https://github.com/javierdelapuente/operator-ci-poc/blob/main/src/opcli/core/tutorial.py
"""

import re
from pathlib import Path

from opcli.core.exceptions import ValidationError

# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------


def _validate_paired_markers(
    content: str,
    start_pattern: str,
    end_pattern: str,
    marker_name: str,
    flags: int = 0,
) -> list[tuple[int, int]]:
    """Validate that markers are properly paired using a stack-based approach.

    Returns:
        List of ``(start_pos, end_pos)`` for valid marker pairs.

    Raises:
        ValidationError: If markers are not properly paired or ordered.
    """
    starts = [(m.start(), "start") for m in re.finditer(start_pattern, content, flags)]
    ends = [(m.start(), "end") for m in re.finditer(end_pattern, content, flags)]

    all_markers = sorted(starts + ends, key=lambda x: x[0])
    stack: list[int] = []
    pairs: list[tuple[int, int]] = []

    for pos, marker_type in all_markers:
        if marker_type == "start":
            stack.append(pos)
        else:
            if not stack:
                msg = (
                    f"Found closing {marker_name} marker without corresponding "
                    f"opening marker at position {pos}"
                )
                raise ValidationError(msg)
            start_pos = stack.pop()
            pairs.append((start_pos, pos))

    if stack:
        msg = f"Unclosed {marker_name} marker found at position {stack[0]}"
        raise ValidationError(msg)

    return pairs


# ---------------------------------------------------------------------------
# Markdown extraction
# ---------------------------------------------------------------------------


def _extract_markdown_spread_comments(content: str) -> list[tuple[int, str]]:
    """Return ``(position, command_string)`` tuples for ``<!-- SPREAD -->`` blocks."""
    spread_starts = [m.start() for m in re.finditer(r"<!-- SPREAD(?! SKIP)\s*", content)]
    for start_pos in spread_starts:
        remaining = content[start_pos:]
        if "-->" not in remaining:
            msg = f"Unclosed SPREAD comment block found at position {start_pos}"
            raise ValidationError(msg)
        next_spread = remaining.find("<!-- SPREAD", 1)
        closing_pos = remaining.find("-->")
        if next_spread != -1 and closing_pos > next_spread:
            msg = f"Unclosed SPREAD comment block found at position {start_pos}"
            raise ValidationError(msg)

    result: list[tuple[int, str]] = []
    pattern = r"<!-- SPREAD(?! SKIP)\s*\n(.*?)-->"
    for match in re.finditer(pattern, content, re.DOTALL):
        text = match.group(1).strip()
        if text:
            result.append((match.start(), text))
    return result


def _extract_markdown_skip_ranges(content: str) -> list[tuple[int, int]]:
    """Return ``(start, end)`` byte ranges for ``<!-- SPREAD SKIP -->`` blocks."""
    pairs = _validate_paired_markers(
        content,
        r"<!-- SPREAD SKIP -->",
        r"<!-- SPREAD SKIP END -->",
        "SPREAD SKIP",
    )
    end_marker_re = re.compile(r"<!-- SPREAD SKIP END -->")
    ranges: list[tuple[int, int]] = []
    for start_pos, end_pos in pairs:
        m = end_marker_re.search(content, end_pos)
        if m:
            ranges.append((start_pos, m.end()))
    return ranges


def _extract_commands_from_markdown(file_path: Path) -> list[str]:
    """Extract shell commands from a Markdown tutorial file."""
    content = file_path.read_text(encoding="utf-8")

    spread_blocks = _extract_markdown_spread_comments(content)
    skip_ranges = _extract_markdown_skip_ranges(content)

    # 4+ backtick fences are excluded (they contain meta-docs, not commands)
    excluded: list[tuple[int, int]] = []
    for m in re.finditer(r"````+[^\n]*\n(.*?)````+", content, re.DOTALL):
        excluded.append((m.start(), m.end()))
    excluded.extend(skip_ranges)

    # Exactly 3 backticks (not more, not fewer)
    code_blocks: list[tuple[int, str]] = []
    for m in re.finditer(r"(?<!`)```(?!`)([^\n]*)\n(.*?)(?<!`)```(?!`)", content, re.DOTALL):
        lang = m.group(1)
        code = m.group(2)
        start = m.start()
        end = m.end()

        if lang.strip().startswith("{"):
            continue

        is_excluded = any(start < e2 and end > s for s, e2 in excluded)
        if is_excluded:
            continue

        stripped = code.strip()
        if stripped:
            code_blocks.append((start, stripped))

    filtered_spread: list[tuple[int, str]] = [
        (pos, cmd) for pos, cmd in spread_blocks if not any(s <= pos < e for s, e in skip_ranges)
    ]

    all_blocks = sorted(code_blocks + filtered_spread, key=lambda x: x[0])
    return [cmd for _, cmd in all_blocks]


# ---------------------------------------------------------------------------
# RST extraction
# ---------------------------------------------------------------------------


def _extract_rst_spread_comments(content: str) -> list[tuple[int, str]]:
    """Return ``(position, command_string)`` tuples for ``.. SPREAD`` blocks."""
    _validate_paired_markers(
        content,
        r"^\.\. SPREAD\s*$",
        r"^\.\. SPREAD END\s*$",
        "SPREAD",
        re.MULTILINE,
    )

    result: list[tuple[int, str]] = []
    pattern = r"^\.\. SPREAD\s*\n(.*?)^\.\. SPREAD END\s*$"
    for m in re.finditer(pattern, content, re.MULTILINE | re.DOTALL):
        raw = m.group(1)
        lines = raw.split("\n")
        stripped = []
        for line in lines:
            if line.startswith(".. "):
                stripped.append(line[3:])
            elif line.startswith(".."):
                stripped.append(line[2:])
            else:
                stripped.append(line)

        non_empty = [ln for ln in stripped if ln.strip()]
        if not non_empty:
            continue

        min_indent = min(len(ln) - len(ln.lstrip()) for ln in non_empty)
        dedented = [ln[min_indent:] if ln.strip() else "" for ln in stripped]
        text = "\n".join(dedented).strip()
        if text:
            result.append((m.start(), text))
    return result


def _extract_rst_skip_ranges(content: str) -> list[tuple[int, int]]:
    """Return ``(start, end)`` byte ranges for ``.. SPREAD SKIP`` blocks."""
    _validate_paired_markers(
        content,
        r"^\.\. SPREAD SKIP\s*$",
        r"^\.\. SPREAD SKIP END\s*$",
        "SPREAD SKIP",
        re.MULTILINE,
    )

    ranges: list[tuple[int, int]] = []
    pattern = r"^\.\. SPREAD SKIP\s*\n(.*?)^\.\. SPREAD SKIP END\s*$"
    for m in re.finditer(pattern, content, re.MULTILINE | re.DOTALL):
        ranges.append((m.start(), m.end()))
    return ranges


def _extract_commands_from_rst(file_path: Path) -> list[str]:
    """Extract shell commands from a reStructuredText tutorial file."""
    content = file_path.read_text(encoding="utf-8")

    spread_blocks = _extract_rst_spread_comments(content)
    skip_ranges = _extract_rst_skip_ranges(content)

    # .. code-block:: directive; allow one optional blank line after directive
    pattern = r"^\.\. code-block::[^\n]*\n(?:\n)?((?:[ \t]+.+(?:\n|$))+)"
    code_blocks: list[tuple[int, str]] = []
    for m in re.finditer(pattern, content, re.MULTILINE):
        start = m.start()
        if any(s <= start < e for s, e in skip_ranges):
            continue

        indented = m.group(1)
        lines = indented.split("\n")
        non_empty = [ln for ln in lines if ln.strip()]
        if not non_empty:
            continue

        min_indent = min(len(ln) - len(ln.lstrip()) for ln in non_empty)
        dedented = [ln[min_indent:] if ln.strip() else "" for ln in lines]
        text = "\n".join(dedented).strip()
        if text:
            code_blocks.append((start, text))

    filtered_spread: list[tuple[int, str]] = [
        (pos, cmd) for pos, cmd in spread_blocks if not any(s <= pos < e for s, e in skip_ranges)
    ]

    all_blocks = sorted(code_blocks + filtered_spread, key=lambda x: x[0])
    return [cmd for _, cmd in all_blocks]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def expand_tutorial(file_path: Path) -> str:
    """Extract shell commands from *file_path* and return them as a shell script.

    The returned string is suitable for sourcing in a spread task via bash
    process substitution::

        runuser -l ubuntu -s /bin/bash -c 'set -ex; . <(opcli tutorial expand -- "$1")' _ "${SPREAD_PATH}${TUTORIAL}"

    Using ``bash -ex`` ensures commands are traced (``-x``) and the session
    exits on the first failure (``-e``).

    Supports ``.md``/``.markdown`` (Markdown) and ``.rst``/``.rest``
    (reStructuredText) files.

    Raises:
        ValidationError: If the file type is unsupported or markers are malformed.
    """
    ext = file_path.suffix.lower()
    if ext in (".rst", ".rest"):
        commands = _extract_commands_from_rst(file_path)
    elif ext in (".md", ".markdown"):
        commands = _extract_commands_from_markdown(file_path)
    else:
        msg = f"Unsupported file type '{ext}'. Supported: .md, .markdown, .rst, .rest"
        raise ValidationError(msg)

    return "\n".join(commands)
