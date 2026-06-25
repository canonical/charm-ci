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
import platform
import shutil
import subprocess
from pathlib import Path

from opcli.core.exceptions import ConfigurationError
from opcli.core.progress import status, step
from opcli.core.subprocess import run_command


def install_all() -> None:
    """Install all tools needed for local charm development.

    Installs gh, spread, concierge, tox, and LXD.  Idempotent — each tool
    is skipped if already present.  Requires passwordless sudo for snap-based
    installs when not running as root.
    """
    _check_os_prerequisites()
    root = os.getuid() == 0
    if root:
        status("Installing as root (system-wide: /usr/local/bin)")
    else:
        _warn_if_local_bin_not_on_path(prefix=True)
        home = Path.home()
        status(f"Installing as user (user-local: {home / '.local' / 'bin'}, snap via sudo)")

    install_gh()
    install_spread()
    install_concierge()
    install_tox()
    install_lxd()

    if not root:
        _warn_if_local_bin_not_on_path(prefix=False)


def install_doctor() -> dict[str, tuple[str | None, str | None]]:
    """Return a mapping of tool name to (path, version) for each required tool.

    Checks gh, spread, concierge, tox, lxd, and uv.
    Version is the first line of ``--version`` output, or None if not found.
    """
    tools = ["gh", "spread", "concierge", "tox", "lxd", "uv"]
    result: dict[str, tuple[str | None, str | None]] = {}
    for tool in tools:
        path = shutil.which(tool)
        version: str | None = None
        if path:
            version = _get_version(tool, path)
        result[tool] = (path, version)
    return result


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
    When running as root the spread binary is copied to /usr/local/bin so
    all users can execute it; when running as a regular user it is symlinked
    to ~/.local/bin.
    """
    if shutil.which("spread"):
        status("spread already installed")
        return
    root = os.getuid() == 0
    snap_cmd = [] if root else ["sudo"]
    with step("Installing spread (Go snap + build from source, ~30s)"):
        run_command([*snap_cmd, "snap", "install", "go", "--classic"])
        run_command(
            ["go", "install", "github.com/canonical/spread/cmd/spread@latest"],
        )
        if root:
            src = "/root/go/bin/spread"
            dst = "/usr/local/bin/spread"
            # Copy (not symlink) so non-root users can execute the binary.
            run_command(["cp", src, dst])
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

    Uses ``--upgrade`` to ensure the entry-point symlink always lands in
    the correct bin directory even if tox was previously installed elsewhere.
    """
    if not shutil.which("uv"):
        msg = "uv not found — install with: sudo snap install astral-uv --classic"
        raise ConfigurationError(msg)
    env: dict[str, str] | None = None
    if os.getuid() == 0:
        env = {
            "UV_TOOL_BIN_DIR": "/usr/local/bin",
            "UV_TOOL_DIR": "/usr/local/share/uv-tools",
        }
    with step("Installing tox"):
        run_command(
            ["uv", "tool", "install", "tox", "--with", "tox-uv", "--upgrade", "--quiet"],
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
    ``lxd`` group only if not already a member.

    Note: membership in the ``lxd`` group is equivalent to root access on
    the host.  A new login session is needed for membership to take effect.
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
                print(
                    f"  ⚠ {user} added to lxd group (root-equivalent access).\n"
                    f"    Log out and back in for this to take effect."
                )
        except KeyError:
            pass  # lxd group not yet created; snap post-install hook will create it


def _check_os_prerequisites() -> None:
    """Raise ConfigurationError if the OS or snap is not available."""
    if platform.system() != "Linux":
        msg = (
            f"opcli install requires Linux with snapd. "
            f"Detected: {platform.system()}. "
            f"Install on Ubuntu 22.04+ or another snap-supported distribution."
        )
        raise ConfigurationError(msg)
    if not shutil.which("snap"):
        msg = "snapd not found. Install snapd first: https://snapcraft.io/docs/installing-snapd"
        raise ConfigurationError(msg)


def _get_version(tool: str, path: str) -> str | None:
    """Return a short version string for *tool*, or None on failure."""
    version_flags: dict[str, list[str]] = {
        "lxd": ["version"],
        "spread": [],  # spread exposes no version flag
    }
    flags = version_flags.get(tool, ["--version"])
    if not flags:
        return None
    try:
        result = subprocess.run(
            [path, *flags],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        output = (result.stdout or result.stderr).strip()
        if not output:
            return None
        # Some tools (e.g. tox) emit preamble lines before the version.
        # Use the first line that starts with a digit.
        for line in output.splitlines():
            stripped = line.strip()
            if stripped and stripped[0].isdigit():
                # Truncate at " from " (tox: "4.56.1 from /path/...")
                return stripped.split(" from ")[0]
        return output.splitlines()[0]
    except Exception:
        return None


def _warn_if_local_bin_not_on_path(*, prefix: bool) -> None:
    """Print a warning when ~/.local/bin is missing from PATH.

    Args:
        prefix: If True print a pre-install warning; if False a post-install reminder.
    """
    local_bin = str(Path.home() / ".local" / "bin")
    if local_bin in os.environ.get("PATH", "").split(":"):
        return
    shell = os.environ.get("SHELL", "")
    rc_file = ".zshrc" if "zsh" in shell else ".bashrc"
    verb = "Warning" if prefix else "Reminder"
    print(
        f"\n⚠ {verb}: {local_bin} is not on PATH.\n"
        f'  Add to ~/{rc_file}:  export PATH="{local_bin}:$PATH"\n'
        f"  Then run:           source ~/{rc_file}"
    )
