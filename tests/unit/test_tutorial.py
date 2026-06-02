# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for ``opcli tutorial expand``."""

from pathlib import Path

import pytest

from opcli.core.exceptions import ValidationError
from opcli.core.tutorial import expand_tutorial


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# ---------------------------------------------------------------------------
# Markdown extraction
# ---------------------------------------------------------------------------


class TestMarkdownExtraction:
    """Tests for Markdown command extraction."""

    def test_extracts_simple_code_block(self, tmp_path: Path) -> None:
        doc = tmp_path / "tutorial.md"
        _write(doc, "Some text\n```\necho hello\n```\n")
        result = expand_tutorial(doc)
        assert "echo hello" in result

    def test_extracts_multiple_blocks_in_order(self, tmp_path: Path) -> None:
        doc = tmp_path / "tutorial.md"
        _write(doc, "```\nfirst\n```\n\nSome prose\n\n```\nsecond\n```\n")
        result = expand_tutorial(doc)
        lines = result.splitlines()
        assert lines.index("first") < lines.index("second")

    def test_excludes_note_blocks(self, tmp_path: Path) -> None:
        doc = tmp_path / "tutorial.md"
        _write(doc, "```{note}\nThis is a note.\n```\n\n```\nreal command\n```\n")
        result = expand_tutorial(doc)
        assert "This is a note." not in result
        assert "real command" in result

    def test_excludes_tip_blocks(self, tmp_path: Path) -> None:
        doc = tmp_path / "tutorial.md"
        _write(doc, "```{tip}\nA tip.\n```\n\n```\nmy command\n```\n")
        result = expand_tutorial(doc)
        assert "A tip." not in result
        assert "my command" in result

    def test_includes_spread_comment_block(self, tmp_path: Path) -> None:
        doc = tmp_path / "tutorial.md"
        _write(doc, "<!-- SPREAD\necho from spread\n-->\n")
        result = expand_tutorial(doc)
        assert "echo from spread" in result

    def test_spread_skip_excludes_range(self, tmp_path: Path) -> None:
        doc = tmp_path / "tutorial.md"
        _write(
            doc,
            "```\nbefore skip\n```\n"
            "<!-- SPREAD SKIP -->\n"
            "```\nskipped command\n```\n"
            "<!-- SPREAD SKIP END -->\n"
            "```\nafter skip\n```\n",
        )
        result = expand_tutorial(doc)
        assert "before skip" in result
        assert "skipped command" not in result
        assert "after skip" in result

    def test_spread_skip_overlap_excluded(self, tmp_path: Path) -> None:
        """A code block that overlaps a SPREAD SKIP range is excluded."""
        doc = tmp_path / "tutorial.md"
        _write(
            doc,
            "<!-- SPREAD SKIP -->\n```\noverlapping block\n```\n<!-- SPREAD SKIP END -->\n",
        )
        result = expand_tutorial(doc)
        assert "overlapping block" not in result

    def test_four_backtick_fence_excluded(self, tmp_path: Path) -> None:
        doc = tmp_path / "tutorial.md"
        _write(doc, "````\n```\nnested\n```\n````\n\n```\nreal\n```\n")
        result = expand_tutorial(doc)
        assert "nested" not in result
        assert "real" in result

    def test_language_identifier_preserved_command(self, tmp_path: Path) -> None:
        """Code blocks with a language hint (not starting with {) are included."""
        doc = tmp_path / "tutorial.md"
        _write(doc, "```bash\necho bash\n```\n")
        result = expand_tutorial(doc)
        assert "echo bash" in result

    def test_spread_skip_on_spread_comment(self, tmp_path: Path) -> None:
        """SPREAD blocks inside SPREAD SKIP are also excluded."""
        doc = tmp_path / "tutorial.md"
        _write(
            doc,
            "<!-- SPREAD SKIP -->\n"
            "<!-- SPREAD\nskipped spread\n-->\n"
            "<!-- SPREAD SKIP END -->\n"
            "<!-- SPREAD\nkept spread\n-->\n",
        )
        result = expand_tutorial(doc)
        assert "skipped spread" not in result
        assert "kept spread" in result

    def test_unclosed_spread_skip_raises(self, tmp_path: Path) -> None:
        doc = tmp_path / "tutorial.md"
        _write(doc, "<!-- SPREAD SKIP -->\n```\nnot closed\n```\n")
        with pytest.raises(ValidationError):
            expand_tutorial(doc)

    def test_empty_file_returns_empty(self, tmp_path: Path) -> None:
        doc = tmp_path / "tutorial.md"
        _write(doc, "")
        assert expand_tutorial(doc) == ""


# ---------------------------------------------------------------------------
# RST extraction
# ---------------------------------------------------------------------------


class TestRstExtraction:
    """Tests for reStructuredText command extraction."""

    def test_extracts_code_block(self, tmp_path: Path) -> None:
        doc = tmp_path / "tutorial.rst"
        _write(doc, "Some text\n\n.. code-block:: bash\n\n   echo hello\n\n")
        result = expand_tutorial(doc)
        assert "echo hello" in result

    def test_extracts_multiple_blocks_in_order(self, tmp_path: Path) -> None:
        doc = tmp_path / "tutorial.rst"
        _write(
            doc,
            ".. code-block::\n\n   first\n\nProse\n\n.. code-block::\n\n   second\n\n",
        )
        result = expand_tutorial(doc)
        lines = result.splitlines()
        assert lines.index("first") < lines.index("second")

    def test_includes_spread_block(self, tmp_path: Path) -> None:
        doc = tmp_path / "tutorial.rst"
        _write(doc, ".. SPREAD\n.. echo from spread\n.. SPREAD END\n")
        result = expand_tutorial(doc)
        assert "echo from spread" in result

    def test_spread_skip_excludes_range(self, tmp_path: Path) -> None:
        doc = tmp_path / "tutorial.rst"
        _write(
            doc,
            ".. code-block::\n\n   before\n\n"
            ".. SPREAD SKIP\n"
            ".. code-block::\n\n   skipped\n\n"
            ".. SPREAD SKIP END\n"
            ".. code-block::\n\n   after\n\n",
        )
        result = expand_tutorial(doc)
        assert "before" in result
        assert "skipped" not in result
        assert "after" in result

    def test_unclosed_spread_marker_raises(self, tmp_path: Path) -> None:
        doc = tmp_path / "tutorial.rst"
        _write(doc, ".. SPREAD\n.. echo unclosed\n")
        with pytest.raises(ValidationError):
            expand_tutorial(doc)

    def test_dedents_code_block_content(self, tmp_path: Path) -> None:
        doc = tmp_path / "tutorial.rst"
        _write(
            doc,
            ".. code-block::\n\n   sudo apt-get install foo\n   sudo apt-get install bar\n\n",
        )
        result = expand_tutorial(doc)
        assert "sudo apt-get install foo" in result
        assert "sudo apt-get install bar" in result
        for line in result.splitlines():
            if line.strip():
                assert not line.startswith(" ")

    def test_rest_extension_supported(self, tmp_path: Path) -> None:
        doc = tmp_path / "tutorial.rest"
        _write(doc, ".. code-block::\n\n   echo rest\n\n")
        result = expand_tutorial(doc)
        assert "echo rest" in result


# ---------------------------------------------------------------------------
# File type detection
# ---------------------------------------------------------------------------


class TestFileTypeDetection:
    """Tests for file type dispatch."""

    def test_unsupported_extension_raises(self, tmp_path: Path) -> None:
        doc = tmp_path / "tutorial.txt"
        _write(doc, "echo hello\n")
        with pytest.raises(ValidationError, match="Unsupported file type"):
            expand_tutorial(doc)

    def test_markdown_extension(self, tmp_path: Path) -> None:
        doc = tmp_path / "tutorial.markdown"
        _write(doc, "```\necho ok\n```\n")
        result = expand_tutorial(doc)
        assert "echo ok" in result
