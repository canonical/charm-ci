# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Environment detection utilities."""

import os


def is_ci() -> bool:
    """Return True when running inside CI (truthy ``CI`` env var)."""
    return bool(os.environ.get("CI"))
