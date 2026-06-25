# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Core logic for ``opcli install`` sub-commands.

These functions install tool dependencies needed for local charm development
and the spread test environment.  They are designed to be idempotent and
work correctly whether invoked as root (e.g. inside a spread ``prepare:``
script) or as a regular user (developer workstation).
"""

import grp
import os
import shutil
from pathlib import Path

from opcli.core.exceptions import ConfigurationError
from opcli.core.progress import status, step
from opcli.core.subprocess import run_command


def install_bootstrap() -> None:
    """Install all tools needed for local charm development.

    Installs gh, spread, concierge, tox, and LXD.  Idempotent — each tool
    is skipped if already present.  Requires passwordless sudo (standard on
    developer workstations) for snap-based installs when not running as root.
    """
    install_gh()
    install_spread()
    install_concierge()
    install_tox()
    install_lxd()
    _warn_if_local_bin_not_on_path()


def install_check() -> dict[str, str | None]:
    """Return a mapping of tool name to resolved PATH location (or None).

    Checks gh, spread, concierge, tox, lxd, and uv.
    """
    tools = ["gh", "spread", "concierge", "tox", "lxd", "uv"]
    return {tool: shutil.which(tool) for tool in tools}


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

    Builds spread from source, installing the Go snap as a build dependency.
    When running as root the spread binary is symlinked to /usr/local/bin;
    when running as a regular user it is symlinked to ~/.local/bin.
    """
    if shutil.which("spread"):
        status("spread already installed")
        return
    root = os.getuid() == 0
    snap_cmd = [] if root else ["sudo"]
    with step("Installing spread (Go snap + build from source)"):
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
    if not shutil.which("uv"):
        msg = "uv not found — install with: sudo snap install astral-uv --classic"
        raise ConfigurationError(msg)
    if shutil.which("tox"):
        status("tox already installed")
        return
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


def install_lxd() -> None:
    """Install and initialise LXD, and add the current user to the lxd group.

    LXD is required for the spread local backend.  Initialisation
    (``lxd init --auto``) is skipped if LXD is already present to avoid
    overwriting an existing configuration.  The user is added to the
    ``lxd`` group only if not already a member; a new login session is
    needed for the membership to take effect.
    """
    root = os.getuid() == 0
    snap_cmd = [] if root else ["sudo"]

    if not shutil.which("lxd"):
        with step("Installing LXD"):
            run_command([*snap_cmd, "snap", "install", "lxd"])
        with step("Initialising LXD"):
            run_command([*snap_cmd, "lxd", "init", "--auto"])
    else:
        status("lxd already installed")

    user = os.environ.get("SUDO_USER") or os.environ.get("USER") or ""
    if user:
        try:
            lxd_group = grp.getgrnam("lxd")
            if user not in lxd_group.gr_mem:
                with step(f"Adding {user} to lxd group"):
                    run_command(["sudo", "usermod", "-aG", "lxd", user])
        except KeyError:
            pass  # lxd group not yet created; snap post-install hook will create it


def _warn_if_local_bin_not_on_path() -> None:
    """Print a warning when ~/.local/bin is missing from PATH."""
    local_bin = str(Path.home() / ".local" / "bin")
    path_dirs = os.environ.get("PATH", "").split(":")
    if local_bin not in path_dirs:
        print(
            f"\nWarning: {local_bin} is not on PATH.\n"
            f"Add this line to your ~/.bashrc or ~/.zshrc:\n"
            f'  export PATH="{local_bin}:$PATH"'
        )
