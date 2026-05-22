# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""CLI commands for installing tool dependencies."""

import typer

from opcli.core.install import install_concierge, install_spread, install_tox

app = typer.Typer(
    help="Install tool dependencies for spread test environments.",
    no_args_is_help=True,
)


@app.command()
def spread() -> None:
    """Install the spread test runner (no-op if already present)."""
    install_spread()
    typer.echo("spread is available.")


@app.command()
def tox() -> None:
    """Install tox with tox-uv for running integration tests."""
    install_tox()
    typer.echo("tox is available.")


@app.command()
def concierge() -> None:
    """Install the concierge snap (no-op if already present)."""
    install_concierge()
    typer.echo("concierge is available.")
