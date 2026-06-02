# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""``opcli tutorial`` command group."""

from pathlib import Path
from typing import Annotated

import typer

from opcli.core.tutorial import expand_tutorial

app = typer.Typer(
    help="Extract shell commands from tutorial documents (.md/.rst) as a runnable script.",
    no_args_is_help=True,
)


@app.command()
def expand(
    tutorial_file: Annotated[
        Path,
        typer.Argument(
            help="Path to the tutorial file (.md or .rst).",
            exists=True,
            readable=True,
        ),
    ],
) -> None:
    """Extract shell commands from a tutorial file and print them to stdout.

    The output is a shell script suitable for sourcing in a spread task.yaml::

        runuser -l ubuntu -s /bin/bash -c 'set -ex; . <(opcli tutorial expand -- "$1")' _ "${SPREAD_PATH}${TUTORIAL}"

    Supports Markdown (.md) and reStructuredText (.rst) files.

    **Markdown:** 3-backtick code fences are included (except language tags
    starting with ``{``, e.g. ``{terminal}``). Blocks delimited by
    ``<!-- SPREAD`` ... ``-->`` are always included regardless of
    fence rules. Use ``<!-- SPREAD SKIP -->`` ... ``<!-- SPREAD SKIP END -->``
    to mark regions that should be skipped entirely (e.g. local-only setup).

    **RST:** ``.. code-block::`` directives (directive options like
    ``:caption:`` are skipped). ``.. SPREAD`` / ``.. SPREAD END`` blocks
    are always included. Use ``.. SPREAD SKIP`` / ``.. SPREAD SKIP END``
    to skip regions.
    """
    script = expand_tutorial(tutorial_file)
    typer.echo(script)
