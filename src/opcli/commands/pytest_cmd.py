# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""CLI commands for pytest/tox integration test execution."""

import shlex
from pathlib import Path

import typer

from opcli.core.pytest_args import assemble_tox_argv, pytest_run

app = typer.Typer(
    help="Assemble pytest flags from build output and run integration tests.",
    no_args_is_help=True,
)


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def run(
    ctx: typer.Context,
    *,
    tox_env: str = typer.Option("integration", "-e", help="Tox environment name."),
) -> None:
    """Assemble and execute the tox integration test command.

    Extra args after -- are forwarded to tox/pytest.
    """
    pytest_run(Path.cwd(), tox_env=tox_env, extra_args=ctx.args or None)


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def expand(
    ctx: typer.Context,
    *,
    tox_env: str = typer.Option("integration", "-e", help="Tox environment name."),
) -> None:
    """Print the full tox command assembled from artifacts.build.yaml.

    Extra args after -- are forwarded into the printed command.
    """
    argv = assemble_tox_argv(Path.cwd(), tox_env=tox_env, extra_args=ctx.args or None)
    typer.echo(shlex.join(argv))
