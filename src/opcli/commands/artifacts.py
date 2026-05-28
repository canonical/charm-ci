# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""CLI commands for artifact discovery and building."""

import json
from pathlib import Path
from typing import Annotated

import typer

from opcli.core.artifacts import (
    artifacts_build,
    artifacts_collect,
    artifacts_fetch,
    artifacts_init,
    artifacts_localize,
    artifacts_matrix,
    artifacts_path,
)
from opcli.core.exceptions import ConfigurationError
from opcli.core.provision import provision_load
from opcli.core.publish import artifacts_publish, publish_results_to_dicts

app = typer.Typer(
    help="Discover and build charms, rocks, and snaps.",
    no_args_is_help=True,
)


@app.command()
def init(
    *,
    force: bool = typer.Option(False, "--force", help="Overwrite existing artifacts.yaml."),
) -> None:
    """Discover artifacts and generate artifacts.yaml."""
    path = artifacts_init(Path.cwd(), force=force)
    typer.echo(f"Wrote {path}")


@app.command()
def build(
    *,
    charm: list[str] = typer.Option([], "--charm", help="Build only this charm. Repeatable."),
    rock: list[str] = typer.Option([], "--rock", help="Build only this rock. Repeatable."),
    snap: list[str] = typer.Option([], "--snap", help="Build only this snap. Repeatable."),
) -> None:
    """Build artifacts and produce artifacts.build.yaml."""
    path = artifacts_build(
        Path.cwd(),
        charm_names=charm or None,
        rock_names=rock or None,
        snap_names=snap or None,
    )
    typer.echo(f"Wrote {path}")


@app.command()
def matrix() -> None:
    """Print the GitHub Actions build matrix as JSON.

    Reads artifacts.yaml and outputs a JSON object with an ``include`` key
    suitable for use as a GitHub Actions ``strategy.matrix`` value.
    """
    result = artifacts_matrix(Path.cwd())
    typer.echo(json.dumps(result))


@app.command()
def collect(
    partials: Annotated[
        list[Path],
        typer.Argument(help="Partial artifacts.build.yaml files to merge."),
    ],
) -> None:
    """Merge partial artifacts.build.yaml files into one.

    Downloads from each parallel CI build job produce a partial
    artifacts.build.yaml.  This command merges them and re-fills charm
    resource references from the merged rock outputs.
    """
    path = artifacts_collect(Path.cwd(), partials)
    typer.echo(f"Wrote {path}")


@app.command()
def fetch(
    *,
    run_id: Annotated[
        str,
        typer.Option("--run-id", help="GitHub Actions workflow run ID."),
    ],
    repo: Annotated[
        str | None,
        typer.Option(
            "--repo",
            help="GitHub repository in 'owner/name' format. Defaults to the current git remote.",
        ),
    ] = None,
    wait: Annotated[
        bool,
        typer.Option(
            "--wait/--no-wait",
            help="Retry until artifacts-build appears (use when the build "
            "job may still be running).",
        ),
    ] = False,
) -> None:
    """Download artifacts from a CI run and prepare for local testing.

    Downloads artifacts.build.yaml, then downloads all charm/snap artifact
    archives. Rock artifacts are GHCR images and require no download.
    Finally rewrites artifacts.build.yaml with local file paths so that
    ``opcli pytest run`` and ``opcli spread run`` work without a local build.
    """
    path = artifacts_fetch(Path.cwd(), run_id=run_id, repo=repo, wait=wait)
    typer.echo(f"Fetched artifacts and updated {path}")


@app.command()
def localize() -> None:
    """Update artifacts.build.yaml with downloaded local charm file paths.

    In CI, charm outputs are CI artifact references (artifact + run-id).
    After the workflow downloads the built charm files, run this command to
    rewrite artifacts.build.yaml so each charm points to the local
    ``.charm`` file instead of the CI reference.
    """
    updated = artifacts_localize(Path.cwd())
    if updated:
        typer.echo(f"Localised {updated} charm(s) in artifacts.build.yaml.")
    else:
        typer.echo("No CI artifact references found; artifacts.build.yaml unchanged.")


@app.command("push-images")
def push_images(
    *,
    registry: str = typer.Option(
        "localhost:32000", "-r", "--registry", help="Target image registry."
    ),
    missing_registry: str = typer.Option(
        "skip",
        "--missing-registry",
        help="Policy when registry is unreachable: skip, deploy, or fail.",
    ),
) -> None:
    """Load OCI image artifacts (rocks) into a local image registry."""
    valid_policies = ("skip", "deploy", "fail")
    if missing_registry not in valid_policies:
        typer.echo(f"Error: --missing-registry must be one of {valid_policies!r}", err=True)
        raise typer.Exit(code=1)
    pushed = provision_load(Path.cwd(), registry=registry, missing_registry=missing_registry)
    if pushed:
        for ref in pushed:
            typer.echo(f"Pushed {ref}")
    else:
        typer.echo("No rock images to push.")


_VALID_ARTIFACT_TYPES = ("charm", "rock", "snap")


@app.command()
def path(
    name: Annotated[
        str | None,
        typer.Argument(help="Artifact name to filter by."),
    ] = None,
    *,
    artifact_type: Annotated[
        str | None,
        typer.Option("--type", "-t", help="Filter by type: charm, rock, or snap."),
    ] = None,
    arch: Annotated[
        str | None,
        typer.Option("--arch", help="Architecture override (default: current machine)."),
    ] = None,
) -> None:
    """Resolve and display built artifact locations.

    Reads artifacts.build.yaml and outputs one resolved absolute file
    location per line. Useful for scripting or passing artifact locations
    via environment variables.
    """
    if artifact_type is not None and artifact_type not in _VALID_ARTIFACT_TYPES:
        msg = f"Invalid --type '{artifact_type}'. Valid values: {', '.join(_VALID_ARTIFACT_TYPES)}"
        raise ConfigurationError(msg)
    paths = artifacts_path(Path.cwd(), name=name, artifact_type=artifact_type, arch=arch)
    for p in paths:
        typer.echo(str(p))


@app.command()
def publish(
    *,
    channel: Annotated[
        str,
        typer.Option("--channel", help="CharmHub channel (e.g. latest/edge, 1.0/stable)."),
    ],
    charm: Annotated[
        list[str] | None,
        typer.Option("--charm", help="Publish only this charm. Repeatable."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what would be published without executing."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output structured JSON results to stdout."),
    ] = False,
) -> None:
    """Upload charms and resources to CharmHub.

    Reads artifacts.yaml and artifacts.build.yaml to determine what to
    publish.  For each charm, uploads OCI-image resources (rocks) first,
    then uploads and releases the .charm file(s) with resource bindings.
    """
    if charm is None:
        charm = []
    results = artifacts_publish(
        Path.cwd(), channel=channel, charm_names=charm or None, dry_run=dry_run
    )
    if json_output:
        typer.echo(json.dumps(publish_results_to_dicts(results)))
    else:
        for r in results:
            typer.echo(f"Published {r.charm_name} to {r.channel}:")
            for entry in r.releases:
                base_str = f"{entry.base} " if entry.base else ""
                res_str = ""
                if r.resources:
                    bindings = ", ".join(f"{k}:{v}" for k, v in r.resources.items())
                    res_str = f" — resources: {bindings}"
                typer.echo(f"  rev {entry.revision} ({base_str}{entry.arch}){res_str}")
