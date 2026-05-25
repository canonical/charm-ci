# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Core logic for ``opcli install spread``, ``opcli install tox``,
and ``opcli install concierge``.

These commands install tool dependencies needed by the spread test
environment.  They are designed to be called from the spread ``prepare:``
script after opcli itself has been bootstrapped.
"""

import os
import shutil

from opcli.core.progress import status, step
from opcli.core.subprocess import run_command


def install_spread() -> None:
    """Install spread if not already on PATH.

    Installs Go via snap and builds spread from source.
    """
    if shutil.which("spread"):
        status("spread already installed")
        return
    with step("Installing spread (Go + build from source)"):
        run_command(["snap", "install", "go", "--classic"])
        run_command(
            ["go", "install", "github.com/canonical/spread/cmd/spread@latest"],
        )
        run_command(["ln", "-sf", "/root/go/bin/spread", "/usr/local/bin/spread"])


def install_tox() -> None:
    """Install tox with tox-uv via uv tool.

    When running as root (e.g. inside spread prepare scripts), installs to
    /usr/local/bin so tox is available system-wide.  When running as a
    normal user (local testing without spread), uses the default user-local
    paths (~/.local/bin).
    """
    env: dict[str, str] | None = None
    if os.getuid() == 0:
        env = {
            "UV_TOOL_BIN_DIR": "/usr/local/bin",
            "UV_TOOL_DIR": "/usr/local/share/uv-tools",
        }
    with step("Installing tox"):
        run_command(
            ["uv", "tool", "install", "tox", "--with", "tox-uv", "--quiet"],
            env=env,
        )


def install_concierge() -> None:
    """Install the concierge snap if not already on PATH."""
    if shutil.which("concierge"):
        status("concierge already installed")
        return
    with step("Installing concierge"):
        run_command(["snap", "install", "concierge", "--classic"])
