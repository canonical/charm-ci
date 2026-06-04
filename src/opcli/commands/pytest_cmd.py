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
    help=(
        "Run integration tests via tox.\n\n"
        "Artifacts are injected into tests via the pytest-opcli plugin fixtures.\n"
        "Custom pytest flags can be added via pytest-arguments-template or\n"
        "pytest-environment-template in spread.yaml integration-suites."
    ),
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
    module: str | None = typer.Option(
        None,
        "--module",
        help="Specific test file to run (relative to the working directory). "
        "Passed as the first pytest argument so that conftest.py in the test "
        "directory is loaded before option parsing.",
    ),
) -> None:
    """Assemble and execute the tox integration test command.

    Extra args after -- are forwarded to tox/pytest.
    """
    root = Path.cwd()
    suite_cfg = get_suite_config(root, suite=suite)
    cwd = root / str(suite_cfg["working-dir"])
    pytest_run(
        root,
        tox_env=tox_env,
        extra_args=ctx.args or None,
        suite_config=suite_cfg,
        cwd=cwd,
        module_path=module,
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
    module: str | None = typer.Option(
        None,
        "--module",
        help="Specific test file to run (relative to the working directory). "
        "Passed as the first pytest argument so that conftest.py in the test "
        "directory is loaded before option parsing.",
    ),
) -> None:
    """Print the full tox command that would be executed.

    Extra args after -- are forwarded into the printed command.
    When pytest-environment-template is set in spread.yaml, the env var
    prefix is also printed.
    """
    root = Path.cwd()
    suite_cfg = get_suite_config(root, suite=suite)
    argv = assemble_tox_argv(
        root,
        tox_env=tox_env,
        extra_args=ctx.args or None,
        suite_config=suite_cfg,
        module_path=module,
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
