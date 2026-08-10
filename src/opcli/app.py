# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Top-level Typer application — registers all command groups."""

import logging
import sys
from collections.abc import Sequence
from typing import Any

try:
    import typer
    from typer.core import TyperGroup
except ImportError:
    print(
        "error: the 'cli' extra is required to run opcli.\n"
        "       Install with: pip install 'opcli[cli]'",
        file=sys.stderr,
    )
    sys.exit(1)

from opcli import __version__
from opcli.commands import (
    artifacts,
    env,
    install,
    pytest_cmd,
    spread,
    tutorial_cmd,
)
from opcli.core.exceptions import OpcliError


def app(args: Sequence[str] | None = None) -> None:
    """Entry point for console_scripts."""
    try:
        typer_app(args)
    except SystemExit as exc:
        if exc.code:
            sys.exit(exc.code)
    except OpcliError as exc:
        # Fallback in case the Typer group handler doesn't catch it
        # (e.g. errors raised during Typer parameter processing).
        typer.echo(f"error: {exc}", err=True)
        sys.exit(1)


class _ErrorHandlingGroup(TyperGroup):
    """Typer group that catches OpcliError and prints user-friendly messages."""

    def invoke(self, ctx: Any) -> Any:
        try:
            return super().invoke(ctx)
        except OpcliError as exc:
            typer.echo(f"error: {exc}", err=True)
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
typer_app.add_typer(tutorial_cmd.app, name="tutorial")


def _version_callback(show_version: bool) -> None:
    """Print opcli's version and exit, if --version was passed."""
    if show_version:
        typer.echo(f"opcli {__version__}")
        raise typer.Exit()


@typer_app.callback()
def _main(
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help=(
            "Show INFO-level detail from opcli's internal operations "
            "(e.g. per-artifact download/localize/publish results). "
            "Warnings and errors are always shown."
        ),
    ),
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show opcli's version and exit.",
    ),
) -> None:
    _configure_logging(verbose=verbose)


def _configure_logging(*, verbose: bool) -> None:
    """Configure opcli's own logger namespace so its logger.* calls are visible.

    Without this, ``logger.info(...)`` calls scattered across ``core/`` are
    silently dropped (the root logger defaults to WARNING with no handler),
    and any ``logger.warning``/``logger.error`` that does fire uses Python's
    unformatted "handler of last resort". ``--verbose`` raises the level to
    INFO for users debugging artifact discovery/build/publish behavior.

    This deliberately configures the ``opcli`` logger (every ``core/``
    module logs via ``logging.getLogger(__name__)``, i.e. under the
    ``opcli.*`` namespace) rather than the root logger, and disables
    propagation to it. ``opcli.app.app()`` is a CLI entrypoint, not an
    embeddable library API, but a caller who does import and invoke it
    programmatically should not have their own root-logger handlers or
    unrelated third-party loggers clobbered or have their level bumped to
    INFO as a side effect of running opcli's CLI logic.
    """
    opcli_logger = logging.getLogger("opcli")
    opcli_logger.handlers.clear()
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    opcli_logger.addHandler(handler)
    opcli_logger.setLevel(logging.INFO if verbose else logging.WARNING)
    opcli_logger.propagate = False
