# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Top-level Typer application — registers all command groups."""

import sys
from collections.abc import Sequence
from typing import Any

import click
import typer
from typer.core import TyperGroup

from opcli.commands import (
    artifacts,
    env,
    install,
    pytest_cmd,
    spread,
)
from opcli.core.exceptions import OpcliError


class _ErrorHandlingGroup(TyperGroup):
    """Click group that catches OpcliError and prints user-friendly messages."""

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except OpcliError as exc:
            click.echo(f"error: {exc}", err=True)
            if hint := getattr(exc, "hint", None):
                click.echo(f"hint: {hint}", err=True)
            ctx.exit(1)
            return None


typer_app = typer.Typer(
    name="opcli",
    help="CLI tool for operator development workflows (Charms, Rocks, Snaps).",
    no_args_is_help=True,
    cls=_ErrorHandlingGroup,
)

typer_app.add_typer(artifacts.app, name="artifacts")
typer_app.add_typer(env.app, name="env")
typer_app.add_typer(install.app, name="install")
typer_app.add_typer(spread.app, name="spread")
typer_app.add_typer(pytest_cmd.app, name="pytest")


def app(args: Sequence[str] | None = None) -> None:
    """Entry point for console_scripts."""
    try:
        typer_app(args)
    except SystemExit as exc:
        if exc.code:
            sys.exit(exc.code)
    except OpcliError as exc:
        # Fallback in case the Click group handler doesn't catch it
        # (e.g. errors raised during Typer parameter processing).
        click.echo(f"error: {exc}", err=True)
        if hint := getattr(exc, "hint", None):
            click.echo(f"hint: {hint}", err=True)
        sys.exit(1)
