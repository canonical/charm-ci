# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""CLI commands for installing tool dependencies."""

import typer

from opcli.core.install import (
    install_all,
    install_concierge,
    install_doctor,
    install_gh,
    install_lxd,
    install_spread,
    install_tox,
)

app = typer.Typer(
    help=(
        "Bootstrap local dev environments and install individual tool dependencies.\n\n"
        "Most users want: opcli install all\n\n"
        "Individual installers (gh, spread, tox, concierge, lxd) are grouped below "
        "for use in CI prepare scripts."
    ),
    no_args_is_help=True,
)

_PANEL = "Per-tool installers (for CI / prepare scripts)"


@app.command(name="all")
def all_tools() -> None:
    """Install all tools for local charm development in one shot.

    Installs gh, spread (built from source via Go), concierge, tox, and LXD.
    Idempotent — skips any tool already present.
    Requires passwordless sudo for snap-based installs when not running as root.
    """
    install_all()
    typer.echo("\n✓ All tools installed: gh, spread, concierge, tox, lxd")
    typer.echo("\nNext steps:")
    typer.echo("  opcli install doctor   # verify your environment")
    typer.echo("  opcli --help           # see what you can do")


@app.command()
def doctor() -> None:
    """Check which tools are installed — prints a status table with versions."""
    results = install_doctor()
    all_ok = True
    typer.echo(f"  {'Tool':<12} {'Status':<8} {'Version':<30} Path")
    typer.echo("  " + "-" * 70)
    for tool, (path, version) in results.items():
        if path:
            ver_str = version or "(unknown)"
            typer.echo(f"  \u2713 {tool:<10} {'ok':<8} {ver_str:<30} {path}")
        else:
            typer.echo(f"  \u2717 {tool:<10} {'missing':<8} {'—':<30} —")
            all_ok = False
    if not all_ok:
        typer.echo("\nRun 'opcli install all' to install missing tools.")
        raise typer.Exit(code=1)


@app.command(rich_help_panel=_PANEL)
def gh() -> None:
    """Install the GitHub CLI (gh) snap (no-op if already present)."""
    install_gh()


@app.command(rich_help_panel=_PANEL)
def spread() -> None:
    """Install spread — builds from source using the Go snap (~30s)."""
    install_spread()


@app.command(rich_help_panel=_PANEL)
def tox() -> None:
    """Install tox with tox-uv for running integration tests."""
    install_tox()


@app.command(rich_help_panel=_PANEL)
def concierge() -> None:
    """Install the concierge snap (no-op if already present)."""
    install_concierge()


@app.command(rich_help_panel=_PANEL)
def lxd() -> None:
    """Install and initialise LXD (no-op if already present)."""
    install_lxd()
