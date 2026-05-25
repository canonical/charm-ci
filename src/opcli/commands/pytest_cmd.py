# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""CLI commands for pytest/tox integration test execution."""

import shlex
from pathlib import Path

import typer

from opcli.core.artifacts import artifacts_path
from opcli.core.pytest_args import assemble_tox_argv, pytest_run
from opcli.core.spread import get_pytest_invocation_mode

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
    In observability mode, outputs the CHARM_PATH env var prefix.
    """
    root = Path.cwd()
    mode = get_pytest_invocation_mode(root)
    argv = assemble_tox_argv(root, tox_env=tox_env, extra_args=ctx.args or None, mode=mode)

    if mode == "observability":
        paths = artifacts_path(root, artifact_type="charm")
        prefix = f"CHARM_PATH={shlex.quote(str(paths[0]))} "
        typer.echo(prefix + shlex.join(argv))
    else:
        typer.echo(shlex.join(argv))
