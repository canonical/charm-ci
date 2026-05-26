# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Core logic for ``opcli artifacts publish``.

Uploads charms and their OCI-image resources to CharmHub using charmcraft.
Resolves resource→rock→image references from opcli's own manifests, avoiding
``charmcraft expand-extensions``.
"""

import json
import logging
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

from opcli.core.exceptions import ConfigurationError, DiscoveryError
from opcli.core.progress import status, step
from opcli.core.subprocess import run_command
from opcli.core.yaml_io import load_artifacts_build, load_artifacts_plan, load_yaml
from opcli.models.artifacts_build import ArtifactsGenerated, GeneratedCharm, GeneratedRock

logger = logging.getLogger(__name__)

_ARTIFACTS_YAML = "artifacts.yaml"
_ARTIFACTS_GENERATED_YAML = "artifacts.build.yaml"


# ---------------------------------------------------------------------------
#  Public API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReleaseEntry:
    """A single charm revision that was uploaded and released."""

    revision: int
    base: str | None
    arch: str


@dataclass(frozen=True)
class PublishResult:
    """Result of publishing one charm."""

    charm_name: str
    channel: str
    releases: list[ReleaseEntry]
    resources: dict[str, int] = field(default_factory=dict)


def artifacts_publish(
    root: Path,
    *,
    channel: str,
    charm_names: list[str] | None = None,
    dry_run: bool = False,
) -> list[PublishResult]:
    """Publish charms and their resources to CharmHub.

    Args:
        root: Project root directory.
        channel: CharmHub channel (e.g. ``latest/edge``, ``1.0/stable``).
        charm_names: Publish only these charms. ``None`` means all.
        dry_run: Print what would be uploaded without executing.

    Returns:
        List of publish results, one per charm.

    Raises:
        ConfigurationError: If manifest files are missing or charms have
            un-fetched CI artifacts.
        DiscoveryError: If a resource references a rock not in the build manifest.
    """
    plan_path = root / _ARTIFACTS_YAML
    gen_path = root / _ARTIFACTS_GENERATED_YAML

    if not plan_path.exists():
        msg = f"{_ARTIFACTS_YAML} not found in {root}. Run 'opcli artifacts init' first."
        raise ConfigurationError(msg)
    if not gen_path.exists():
        msg = (
            f"{_ARTIFACTS_GENERATED_YAML} not found in {root}. "
            "Run 'opcli artifacts build' or 'opcli artifacts fetch' first."
        )
        raise ConfigurationError(msg)

    plan = load_artifacts_plan(plan_path)
    generated = load_artifacts_build(gen_path)

    charms = _select_charms(generated, charm_names)
    rocks_by_name = {r.name: r for r in generated.rocks}

    # Build a map of resource declarations from artifacts.yaml (has rock: links)
    plan_resources: dict[str, dict[str, str | None]] = {}
    for charm_plan in plan.charms:
        plan_resources[charm_plan.name] = {
            res_name: res.rock for res_name, res in charm_plan.resources.items()
        }

    if dry_run:
        _print_dry_run(charms, rocks_by_name, plan_resources, channel, root)
        return []

    results: list[PublishResult] = []
    for charm in charms:
        result = _publish_charm(charm, rocks_by_name, plan_resources, channel, root)
        results.append(result)

    return results


# ---------------------------------------------------------------------------
#  Private helpers (call order)
# ---------------------------------------------------------------------------


def _select_charms(
    generated: ArtifactsGenerated, charm_names: list[str] | None
) -> list[GeneratedCharm]:
    """Filter charms from the build manifest."""
    if charm_names is None:
        return list(generated.charms)

    available = {c.name for c in generated.charms}
    for name in charm_names:
        if name not in available:
            msg = (
                f"Charm '{name}' not found in {_ARTIFACTS_GENERATED_YAML}. "
                f"Available: {', '.join(sorted(available))}"
            )
            raise ConfigurationError(msg)

    return [c for c in generated.charms if c.name in set(charm_names)]


def _publish_charm(
    charm: GeneratedCharm,
    rocks_by_name: dict[str, GeneratedRock],
    plan_resources: dict[str, dict[str, str | None]],
    channel: str,
    root: Path,
) -> PublishResult:
    """Publish a single charm: upload resources, then upload+release charm files."""
    resource_flags = _upload_resources(charm, rocks_by_name, plan_resources, root)

    releases: list[ReleaseEntry] = []
    for build in charm.builds:
        if not build.path:
            if build.artifact and build.run_id:
                msg = (
                    f"Charm '{charm.name}' has un-fetched CI artifacts "
                    f"(artifact: {build.artifact}, run-id: {build.run_id}). "
                    f"Run 'opcli artifacts fetch --run-id {build.run_id}' first."
                )
                raise ConfigurationError(msg)
            msg = f"Charm '{charm.name}' build has no path."
            raise ConfigurationError(msg)

        entry = _upload_charm_file(charm.name, build.path, channel, resource_flags, root)
        releases.append(ReleaseEntry(revision=entry, base=build.base, arch=build.arch))

    resources_map = dict(resource_flags)
    return PublishResult(
        charm_name=charm.name,
        channel=channel,
        releases=releases,
        resources=resources_map,
    )


def _upload_resources(
    charm: GeneratedCharm,
    rocks_by_name: dict[str, GeneratedRock],
    plan_resources: dict[str, dict[str, str | None]],
    root: Path,
) -> list[tuple[str, int]]:
    """Upload OCI resources for a charm, returning (name, revision) pairs."""
    resource_map = plan_resources.get(charm.name, {})
    if not resource_map:
        return []

    results: list[tuple[str, int]] = []
    for resource_name, rock_name in resource_map.items():
        image_ref = _resolve_image_ref(charm, resource_name, rock_name, rocks_by_name, root)
        with step(f"Uploading resource '{resource_name}' for charm '{charm.name}'"):
            revision = _do_upload_resource(charm.name, resource_name, image_ref, root)
        results.append((resource_name, revision))

    return results


def _resolve_image_ref(
    charm: GeneratedCharm,
    resource_name: str,
    rock_name: str | None,
    rocks_by_name: dict[str, GeneratedRock],
    root: Path,
) -> str:
    """Determine the image reference for a resource.

    If ``rock_name`` is set, uses the built rock from the build manifest.
    If ``rock_name`` is None, reads ``upstream-source`` from the charm's
    charmcraft metadata.
    """
    if rock_name is not None:
        return _resolve_rock_image_ref(rock_name, rocks_by_name, root)
    return _resolve_upstream_source(charm, resource_name, root)


def _resolve_rock_image_ref(
    rock_name: str, rocks_by_name: dict[str, GeneratedRock], root: Path
) -> str:
    """Get the image ref for a rock from the build manifest."""
    rock = rocks_by_name.get(rock_name)
    if rock is None:
        msg = (
            f"Resource references rock '{rock_name}' but it was not found in "
            f"{_ARTIFACTS_GENERATED_YAML}. Available rocks: "
            f"{', '.join(sorted(rocks_by_name.keys())) or '(none)'}"
        )
        raise DiscoveryError(msg)

    if not rock.builds:
        msg = f"Rock '{rock_name}' has no builds in {_ARTIFACTS_GENERATED_YAML}."
        raise DiscoveryError(msg)

    # Prefer image: (registry ref) over file: (local archive)
    build = rock.builds[0]
    if build.image:
        return f"docker://{build.image}"
    if build.file:
        return str((root / build.file).resolve())

    msg = f"Rock '{rock_name}' build has neither image nor file."
    raise DiscoveryError(msg)


def _resolve_upstream_source(charm: GeneratedCharm, resource_name: str, root: Path) -> str:
    """Read upstream-source from the charm's charmcraft/metadata YAML."""
    charmcraft_path = root / charm.charmcraft_yaml
    metadata_path = charmcraft_path.parent / "metadata.yaml"

    upstream_source = _read_upstream_source(charmcraft_path, resource_name)
    if upstream_source is None and metadata_path.exists():
        upstream_source = _read_upstream_source(metadata_path, resource_name)

    if upstream_source is None:
        msg = (
            f"Resource '{resource_name}' for charm '{charm.name}' has no 'rock:' in "
            f"artifacts.yaml and no 'upstream-source' in {charmcraft_path} or "
            f"{metadata_path}. Cannot determine image reference for upload."
        )
        raise ConfigurationError(msg)

    return f"docker://{upstream_source}"


def _read_upstream_source(yaml_path: Path, resource_name: str) -> str | None:
    """Extract upstream-source for a resource from a YAML file."""
    if not yaml_path.exists():
        return None
    try:
        data = load_yaml(yaml_path)
    except (ValueError, OSError):
        return None

    resources = data.get("resources", {})
    if not isinstance(resources, dict):
        return None

    resource = resources.get(resource_name, {})
    if not isinstance(resource, dict):
        return None

    upstream = resource.get("upstream-source")
    return str(upstream) if upstream else None


def _do_upload_resource(charm_name: str, resource_name: str, image_ref: str, root: Path) -> int:
    """Call charmcraft upload-resource and return the revision number."""
    cmd = [
        "charmcraft",
        "upload-resource",
        charm_name,
        resource_name,
        f"--image={image_ref}",
        "--format=json",
    ]
    result = run_command(cmd, cwd=str(root), stream=False)
    parsed = json.loads(result.stdout)
    revision: int = parsed["revision"]
    status(f"Uploaded resource '{resource_name}' → revision {revision}")
    return revision


def _upload_charm_file(
    charm_name: str,
    charm_path: str,
    channel: str,
    resource_flags: list[tuple[str, int]],
    root: Path,
) -> int:
    """Upload a .charm file and release it to the channel. Returns revision."""
    cmd = [
        "charmcraft",
        "upload",
        charm_path,
        f"--release={channel}",
        "--format=json",
    ]
    for res_name, rev in resource_flags:
        cmd.append(f"--resource={res_name}:{rev}")

    with step(f"Uploading '{charm_path}' to {channel}"):
        result = run_command(cmd, cwd=str(root), stream=False)

    parsed = json.loads(result.stdout)
    revision: int = parsed["revision"]
    status(f"Uploaded charm '{charm_name}' → revision {revision}")
    return revision


def _print_dry_run(
    charms: list[GeneratedCharm],
    rocks_by_name: dict[str, GeneratedRock],
    plan_resources: dict[str, dict[str, str | None]],
    channel: str,
    root: Path,
) -> None:
    """Print what would be published without executing."""
    out = sys.stderr

    out.write("Dry run — the following would be published:\n\n")

    for charm in charms:
        out.write(f"{charm.name} → {channel}:\n")

        resource_map = plan_resources.get(charm.name, {})
        if resource_map:
            out.write("  Resources:\n")
            for resource_name, rock_name in resource_map.items():
                image_ref = _resolve_image_ref(
                    charm, resource_name, rock_name, rocks_by_name, root
                )
                source_desc = f"rock: {rock_name}" if rock_name else "upstream-source"
                out.write(f"    {resource_name} ({source_desc}) → {image_ref}\n")
                cmd = [
                    "charmcraft",
                    "upload-resource",
                    charm.name,
                    resource_name,
                    f"--image={image_ref}",
                    "--format=json",
                ]
                out.write(f"    $ {shlex.join(cmd)}\n")
        else:
            out.write("  Resources: (none)\n")

        out.write("  Charm files:\n")
        for build in charm.builds:
            if not build.path and build.artifact and build.run_id:
                out.write(f"    {build.artifact} — NOT FETCHED (run-id: {build.run_id})\n")
                continue
            base_str = f"{build.base} " if build.base else ""
            out.write(f"    {build.path} ({base_str}{build.arch})\n")
            cmd = ["charmcraft", "upload", build.path or "<unknown>", f"--release={channel}"]
            if resource_map:
                cmd.append("--resource=<name>:<rev>")
            cmd.append("--format=json")
            out.write(f"    $ {shlex.join(cmd)}\n")

        out.write("\n")
