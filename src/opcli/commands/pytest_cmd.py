# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""CLI commands for pytest/tox integration test execution."""

import shlex
from pathlib import Path

import typer

from opcli.core.artifacts import artifacts_path
from opcli.core.exceptions import ConfigurationError
from opcli.core.pytest_args import assemble_tox_argv, pytest_run
from opcli.core.spread import get_pytest_invocation_mode, get_suite_config

_VALID_MODES = ("pfe", "observability")

app = typer.Typer(
    help="Assemble pytest flags from build output and run integration tests.",
    no_args_is_help=True,
)


def _resolve_mode(invocation_mode: str | None) -> str:
    """Resolve the effective invocation mode from CLI flag or spread.yaml."""
    if invocation_mode is not None:
        if invocation_mode not in _VALID_MODES:
            msg = (
                f"Invalid --invocation-mode '{invocation_mode}'. "
                f"Valid values: {', '.join(_VALID_MODES)}"
            )
            raise ConfigurationError(msg)
        return invocation_mode
    return get_pytest_invocation_mode(Path.cwd())


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def run(
    ctx: typer.Context,
    *,
    tox_env: str = typer.Option("integration", "-e", help="Tox environment name."),
    invocation_mode: str | None = typer.Option(
        None,
        "--invocation-mode",
        "-m",
        help="Override pytest invocation mode (pfe, observability). "
        "Defaults to the value in spread.yaml, or 'pfe' if absent.",
    ),
    suite: str | None = typer.Option(
        None,
        "--suite",
        help="Suite key from integration-suites (trailing slash optional). "
        "Auto-detected when only one integration-suite exists.",
    ),
) -> None:
    """Assemble and execute the tox integration test command.

    Extra args after -- are forwarded to tox/pytest.
    """
    root = Path.cwd()
    suite_cfg = get_suite_config(root, suite=suite)
    cwd = root / suite_cfg["cwd"]
    mode = _resolve_mode(invocation_mode)
    pytest_run(root, tox_env=tox_env, extra_args=ctx.args or None, mode=mode, cwd=cwd)


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def expand(
    ctx: typer.Context,
    *,
    tox_env: str = typer.Option("integration", "-e", help="Tox environment name."),
    invocation_mode: str | None = typer.Option(
        None,
        "--invocation-mode",
        "-m",
        help="Override pytest invocation mode (pfe, observability). "
        "Defaults to the value in spread.yaml, or 'pfe' if absent.",
    ),
    suite: str | None = typer.Option(
        None,
        "--suite",
        help="Suite key from integration-suites (trailing slash optional). "
        "Auto-detected when only one integration-suite exists.",
    ),
) -> None:
    """Print the full tox command assembled from artifacts.build.yaml.

    Extra args after -- are forwarded into the printed command.
    In observability mode, outputs the CHARM_PATH env var prefix.
    """
    root = Path.cwd()
    suite_cfg = get_suite_config(root, suite=suite)
    cwd = root / suite_cfg["cwd"]
    mode = _resolve_mode(invocation_mode)
    argv = assemble_tox_argv(
        root, tox_env=tox_env, extra_args=ctx.args or None, mode=mode, cwd=cwd
    )

    if mode == "observability":
        paths = artifacts_path(root, artifact_type="charm")
        prefix = f"CHARM_PATH={shlex.quote(str(paths[0]))} "
        typer.echo(prefix + shlex.join(argv))
    else:
        typer.echo(shlex.join(argv))
