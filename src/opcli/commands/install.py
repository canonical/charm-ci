# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""CLI commands for installing tool dependencies."""

import typer

from opcli.core.install import (
    install_bootstrap,
    install_check,
    install_concierge,
    install_gh,
    install_lxd,
    install_spread,
    install_tox,
)

app = typer.Typer(
    help=(
        "Bootstrap local dev environments and install individual tool dependencies.\n\n"
        "Most users want: opcli install bootstrap\n\n"
        "Individual commands (gh, spread, tox, concierge, lxd) are also available "
        "for scripting and CI prepare scripts."
    ),
    no_args_is_help=True,
)


@app.command()
def bootstrap() -> None:
    """Install all tools for local charm development in one shot.

    Installs gh, spread (built from source via Go), concierge, tox, and LXD.
    Idempotent — skips any tool already present.
    Requires passwordless sudo for snap-based installs when not running as root.
    """
    install_bootstrap()
    typer.echo("Bootstrap complete: gh, spread, concierge, tox, lxd.")


@app.command()
def check() -> None:
    """Check which tools are installed and print a status table."""
    results = install_check()
    all_ok = True
    for tool, path in results.items():
        if path:
            typer.echo(f"  \u2713 {tool}: {path}")
        else:
            typer.echo(f"  \u2717 {tool}: not found")
            all_ok = False
    if not all_ok:
        typer.echo("\nRun 'opcli install bootstrap' to install missing tools.")
        raise typer.Exit(code=1)


@app.command(hidden=True)
def gh() -> None:
    """Install the GitHub CLI (gh) snap (no-op if already present)."""
    install_gh()


@app.command(hidden=True)
def spread() -> None:
    """Install the spread test runner (builds from source via Go snap)."""
    install_spread()


@app.command(hidden=True)
def tox() -> None:
    """Install tox with tox-uv for running integration tests."""
    install_tox()


@app.command(hidden=True)
def concierge() -> None:
    """Install the concierge snap (no-op if already present)."""
    install_concierge()


@app.command(hidden=True)
def lxd() -> None:
    """Install and initialise LXD (no-op if already present)."""
    install_lxd()
