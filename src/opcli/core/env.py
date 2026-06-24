# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Environment detection utilities."""

import os
import platform


def is_ci() -> bool:
    """Return True when running inside CI (truthy ``CI`` env var)."""
    return bool(os.environ.get("CI"))


def current_arch() -> str:
    """Return the normalised architecture of the current machine.

    Maps ``x86_64`` → ``amd64``, ``aarch64`` → ``arm64``, and
    ``ppc64le`` → ``ppc64el`` (Linux kernel name → Debian/charmcraft name).
    All other values are returned as-is (lower-cased).
    """
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "amd64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    if machine == "ppc64le":
        return "ppc64el"
    return machine
