# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""CLI commands for spread-based test execution."""

import json
from pathlib import Path

import typer

from opcli.core.spread import spread_expand, spread_init, spread_jobs, spread_run

app = typer.Typer(
    help="Generate, expand, and run spread-based integration tests.",
    no_args_is_help=True,
)


@app.command()
def init(
    *,
    force: bool = typer.Option(
        False, "--force", help="Overwrite existing spread.yaml and task.yaml."
    ),
) -> None:
    """Generate spread.yaml and tests/integration/run/task.yaml."""
    spread_path, task_path = spread_init(Path.cwd(), force=force)
    typer.echo(f"Wrote {spread_path}")
    typer.echo(f"Wrote {task_path}")


@app.command(
    context_settings={
        "allow_extra_args": True,
        "ignore_unknown_options": True,
    },
)
def run(ctx: typer.Context) -> None:
    """Expand virtual backend and run spread.

    Extra args after -- are forwarded to spread.
    """
    spread_run(Path.cwd(), extra_args=ctx.args or None)


@app.command()
def expand() -> None:
    """Print the fully expanded spread.yaml to stdout."""
    content = spread_expand(Path.cwd())
    typer.echo(content, nl=False)


@app.command()
def jobs() -> None:
    """Print CI test job selectors as a JSON array for GitHub Actions matrix."""
    entries = spread_jobs(Path.cwd())
    typer.echo(json.dumps({"include": entries}))
