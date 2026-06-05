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
        "Assemble pytest flags from build output and run integration tests.\n\n"
        "By default generates --charm-file and rock image flags. Customize via\n"
        "pytest-arguments-template or pytest-environment-template in spread.yaml."
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
    By default generates --charm-file and rock image flags. When
    pytest-environment-template is set, outputs env var prefixes.
    Customize invocation via templates in spread.yaml integration-suites.
    """
    root = Path.cwd()
    suite_cfg = get_suite_config(root, suite=suite)
    cwd = root / str(suite_cfg["working-dir"])
    argv = assemble_tox_argv(
        root, tox_env=tox_env, extra_args=ctx.args or None, suite_config=suite_cfg, cwd=cwd
    )

    cd_prefix = _cd_prefix(root, cwd)

    env_template = suite_cfg.get("pytest-environment-template")
    if isinstance(env_template, str):
        rendered_env = render_environment_template(root, env_template)
        prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in rendered_env.items())
        cmd = shlex.join(argv)
        if prefix:
            cmd = f"{prefix} {cmd}"
        typer.echo(f"{cd_prefix}{cmd}" if cd_prefix else cmd)
    else:
        cmd = shlex.join(argv)
        typer.echo(f"{cd_prefix}{cmd}" if cd_prefix else cmd)


def _cd_prefix(root: Path, cwd: Path) -> str:
    """Return ``'cd <dir> && '`` when *cwd* differs from *root*, else ``''``.

    Uses a path relative to *root* when *cwd* is a descendant; falls back to
    the absolute path otherwise.
    """
    if cwd.resolve() == root.resolve():
        return ""
    try:
        rel = cwd.relative_to(root)
        return f"cd {rel} && "
    except ValueError:
        return f"cd {cwd} && "
