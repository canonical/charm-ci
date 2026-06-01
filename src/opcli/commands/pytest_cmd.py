# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""CLI commands for pytest/tox integration test execution."""

import shlex
from pathlib import Path

import typer

from opcli.core.pytest_args import assemble_tox_argv, pytest_run
from opcli.core.spread import get_suite_config
from opcli.core.template import render_environment_template

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
    cwd = root / str(suite_cfg["cwd"])
    pytest_run(
        root,
        tox_env=tox_env,
        extra_args=ctx.args or None,
        suite_config=suite_cfg,
        cwd=cwd,
    )


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def expand(
    ctx: typer.Context,
    *,
    tox_env: str = typer.Option("integration", "-e", help="Tox environment name."),
    suite: str | None = typer.Option(
        None,
        "--suite",
        help="Suite key from integration-suites (trailing slash optional). "
        "Auto-detected when only one integration-suite exists.",
    ),
) -> None:
    """Print the full tox command assembled from artifacts.build.yaml.

    Extra args after -- are forwarded into the printed command.
    When a pytest-environment-template is set, outputs env var prefixes.
    """
    root = Path.cwd()
    suite_cfg = get_suite_config(root, suite=suite)
    cwd = root / str(suite_cfg["cwd"])
    argv = assemble_tox_argv(
        root, tox_env=tox_env, extra_args=ctx.args or None, suite_config=suite_cfg, cwd=cwd
    )

    env_template = suite_cfg.get("pytest-environment-template")
    if isinstance(env_template, str):
        rendered_env = render_environment_template(root, env_template)
        prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in rendered_env.items())
        if prefix:
            typer.echo(f"{prefix} {shlex.join(argv)}")
        else:
            typer.echo(shlex.join(argv))
    else:
        typer.echo(shlex.join(argv))
