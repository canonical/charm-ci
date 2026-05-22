# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Timestamped progress output for long-running operations.

All messages go to stderr so that stdout remains clean for
machine-parseable output (JSON, shell commands, YAML).
"""

import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager


def _timestamp() -> str:
    """Return a compact HH:MM:SS timestamp."""
    return time.strftime("%H:%M:%S")


def status(message: str) -> None:
    """Print a timestamped status line to stderr."""
    sys.stderr.write(f"[{_timestamp()}] {message}\n")
    sys.stderr.flush()


@contextmanager
def step(description: str) -> Iterator[None]:
    """Context manager that prints start/done with elapsed time."""
    status(description)
    t0 = time.monotonic()
    yield
    elapsed = time.monotonic() - t0
    if elapsed < 60:  # noqa: PLR2004
        elapsed_str = f"{elapsed:.1f}s"
    else:
        minutes = int(elapsed // 60)
        seconds = elapsed % 60
        elapsed_str = f"{minutes}m {seconds:.0f}s"
    status(f"done: {description} ({elapsed_str})")
