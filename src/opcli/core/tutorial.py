# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Core logic for ``opcli tutorial expand``.

Extracts shell commands from Markdown and reStructuredText tutorial files
and returns them as a shell script suitable for use with process substitution
in spread ``task.yaml``::

    runuser -l ubuntu -s /bin/bash -c 'set -ex; . <(opcli tutorial expand -- "$1")' _ "${SPREAD_PATH}${TUTORIAL}"
"""

import re
from pathlib import Path

from opcli.core.exceptions import ValidationError

# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------


def _line_number(content: str, pos: int) -> int:
    """Return the 1-based line number for a byte offset in *content*."""
    return content.count("\n", 0, pos) + 1


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
                    f"opening marker at line {_line_number(content, pos)}"
                )
                raise ValidationError(msg)
            start_pos = stack.pop()
            pairs.append((start_pos, pos))

    if stack:
        msg = f"Unclosed {marker_name} marker found at line {_line_number(content, stack[0])}"
        raise ValidationError(msg)

    return pairs


# ---------------------------------------------------------------------------
# Markdown extraction
# ---------------------------------------------------------------------------


def _extract_markdown_spread_comments(content: str) -> list[tuple[int, str]]:
    """Return ``(position, command_string)`` tuples for ``<!-- SPREAD -->`` blocks."""
    result: list[tuple[int, str]] = []
    spread_starts = [m.start() for m in re.finditer(r"<!-- SPREAD(?! SKIP)\s*", content)]
    for start_pos in spread_starts:
        remaining = content[start_pos:]
        if "-->" not in remaining:
            msg = f"Unclosed SPREAD comment block found at line {_line_number(content, start_pos)}"
            raise ValidationError(msg)
        next_spread = remaining.find("<!-- SPREAD", 1)
        closing_pos = remaining.find("-->")
        if next_spread != -1 and closing_pos > next_spread:
            msg = f"Unclosed SPREAD comment block found at line {_line_number(content, start_pos)}"
            raise ValidationError(msg)
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

        if lang.strip().startswith("{") and not lang.strip().startswith("{code-cell}"):
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


def _parse_rst_code_block_body(
    lines: list[str],
    start_idx: int,
) -> tuple[list[str], int]:
    """Parse the body of a ``.. code-block::`` directive starting at *start_idx*.

    Skips directive option lines (``:option: value``), the blank-line separator,
    then collects all indented lines (including internal blank lines) until the
    first non-blank, non-indented line.

    Returns:
        A tuple of ``(body_lines, new_index)`` where *body_lines* is the raw
        (still-indented) content and *new_index* is the line index after the block.
    """
    i = start_idx

    # Skip directive option lines (:option: value, indented)
    while i < len(lines) and re.match(r"^[ \t]+:[a-zA-Z]", lines[i]):
        i += 1

    # Skip blank-line separator(s)
    while i < len(lines) and not lines[i].strip():
        i += 1

    # Collect indented body including internal blank lines
    body: list[str] = []
    while i < len(lines):
        bl = lines[i]
        if not bl.strip():
            body.append("")
        elif bl.startswith((" ", "\t")):
            body.append(bl)
        else:
            break
        i += 1

    # Strip trailing blank lines
    while body and not body[-1].strip():
        body.pop()

    return body, i


def _extract_commands_from_rst(file_path: Path) -> list[str]:
    """Extract shell commands from a reStructuredText tutorial file."""
    content = file_path.read_text(encoding="utf-8")

    spread_blocks = _extract_rst_spread_comments(content)
    skip_ranges = _extract_rst_skip_ranges(content)

    # Parse .. code-block:: directives line by line to correctly handle:
    #   - directive option lines (:caption:, :linenos:, etc.)
    #   - blank lines within the code body
    code_blocks: list[tuple[int, str]] = []
    lines = content.split("\n")
    byte_pos = 0  # running byte offset for lines[i] in content

    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"^\.\. code-block::", line):
            block_start = byte_pos
            byte_pos += len(line) + 1
            i += 1
            pre_body_i = i

            body, i = _parse_rst_code_block_body(lines, i)
            byte_pos += sum(len(lines[j]) + 1 for j in range(pre_body_i, i))

            if not body or any(s <= block_start < e for s, e in skip_ranges):
                continue

            non_empty = [ln for ln in body if ln.strip()]
            min_indent = min(len(ln) - len(ln.lstrip()) for ln in non_empty)
            dedented = [ln[min_indent:] if ln.strip() else "" for ln in body]
            text = "\n".join(dedented)
            if text:
                code_blocks.append((block_start, text))
        else:
            byte_pos += len(line) + 1
            i += 1

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
