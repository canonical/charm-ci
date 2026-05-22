# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Top-level Typer application for opcli."""

import typer

app = typer.Typer(name="opcli", no_args_is_help=True)


@app.callback()
def main() -> None:
    """CLI tool for operator development workflows (Charms, Rocks, Snaps)."""
