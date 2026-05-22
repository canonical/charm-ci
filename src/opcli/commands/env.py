# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""CLI commands for test environment management."""

from pathlib import Path

import typer

from opcli.core.provision import provision_prepare, provision_registry

app = typer.Typer(
    help="Manage test environments (provisioning and registry).",
    no_args_is_help=True,
)


@app.command()
def provision(
    *,
    concierge_file: str = typer.Option(
        "concierge.yaml",
        "-c",
        "--concierge",
        help="Path to concierge.yaml (relative to the project root).",
    ),
) -> None:
    """Run concierge prepare to provision the test environment."""
    provision_prepare(Path.cwd(), concierge_file=concierge_file)
    typer.echo("Provisioning complete.")


@app.command("deploy-registry")
def deploy_registry() -> None:
    """Deploy a local OCI registry at localhost:32000.

    Auto-detects the active k8s provider and deploys the registry.
    No-op if the registry is already running or no k8s tooling is found.
    """
    result = provision_registry(Path.cwd())
    match result:
        case "deployed":
            typer.echo("Registry deployed at localhost:32000.")
        case "already_running":
            typer.echo("Registry already running at localhost:32000.")
        case _:
            typer.echo("No k8s provider found — skipping registry setup.")
