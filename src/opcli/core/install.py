# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Core logic for ``opcli install`` sub-commands.

These functions install tool dependencies needed for local charm development
and the spread test environment.  They are designed to be idempotent and
work correctly whether invoked as root (e.g. inside a spread ``prepare:``
script) or as a regular user (developer workstation).
"""

import os
import shutil
from pathlib import Path

from opcli.core.progress import status, step
from opcli.core.subprocess import run_command


def install_local() -> None:
    """Install all tools needed for local charm development.

    Installs gh, spread, concierge, and tox.  Idempotent — each tool is
    skipped if already present.  Requires passwordless sudo (standard on
    developer workstations) for snap-based installs when not running as root.
    """
    install_gh()
    install_spread()
    install_concierge()
    install_tox()


def install_gh() -> None:
    """Install the GitHub CLI (gh) snap if not already on PATH."""
    if shutil.which("gh"):
        status("gh already installed")
        return
    snap_cmd = [] if os.getuid() == 0 else ["sudo"]
    with step("Installing gh"):
        run_command([*snap_cmd, "snap", "install", "gh", "--classic"])


def install_spread() -> None:
    """Install spread if not already on PATH.

    Installs Go via snap and builds spread from source.  When running as
    root the spread binary is symlinked to /usr/local/bin; when running as
    a regular user it is symlinked to ~/.local/bin so it stays on PATH.
    """
    if shutil.which("spread"):
        status("spread already installed")
        return
    root = os.getuid() == 0
    snap_cmd = [] if root else ["sudo"]
    with step("Installing spread (Go + build from source)"):
        run_command([*snap_cmd, "snap", "install", "go", "--classic"])
        run_command(
            ["go", "install", "github.com/canonical/spread/cmd/spread@latest"],
        )
        if root:
            src = "/root/go/bin/spread"
            dst = "/usr/local/bin/spread"
        else:
            home = Path.home()
            src = str(home / "go" / "bin" / "spread")
            dst = str(home / ".local" / "bin" / "spread")
        run_command(["ln", "-sf", src, dst])


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
    snap_cmd = [] if os.getuid() == 0 else ["sudo"]
    with step("Installing concierge"):
        run_command([*snap_cmd, "snap", "install", "concierge", "--classic"])
