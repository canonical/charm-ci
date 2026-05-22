# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared test fixtures for opcli."""

from pathlib import Path


def write_file(path: Path, content: str) -> None:
    """Create parent dirs and write *content* to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
