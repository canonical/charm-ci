# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Core logic for ``opcli artifacts publish``.

Uploads charms and their OCI-image resources to CharmHub using charmcraft.
Resolves resource→rock→image references from opcli's own manifests, avoiding
``charmcraft expand-extensions``.
"""

import json
import logging
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ruamel.yaml.error import YAMLError

from opcli.core.exceptions import ConfigurationError, DiscoveryError
from opcli.core.progress import status, step
from opcli.core.subprocess import run_command
from opcli.core.yaml_io import load_artifacts_build, load_yaml
from opcli.models.artifacts_build import ArtifactsGenerated, GeneratedCharm, GeneratedRock

logger = logging.getLogger(__name__)

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
    gen_path = root / _ARTIFACTS_GENERATED_YAML

    if not gen_path.exists():
        msg = (
            f"{_ARTIFACTS_GENERATED_YAML} not found in {root}. "
            "Run 'opcli artifacts build' or 'opcli artifacts fetch' first."
        )
        raise ConfigurationError(msg)

    generated = load_artifacts_build(gen_path)

    charms = _select_charms(generated, charm_names)
    rocks_by_name = {r.name: r for r in generated.rocks}

    # Build resource→rock mapping from the build manifest
    plan_resources: dict[str, dict[str, str | None]] = {}
    for charm in charms:
        if charm.resources:
            plan_resources[charm.name] = {
                res_name: res.rock for res_name, res in charm.resources.items()
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


def _print_dry_run(
    charms: list[GeneratedCharm],
    rocks_by_name: dict[str, GeneratedRock],
    plan_resources: dict[str, dict[str, str | None]],
    channel: str,
    root: Path,
) -> None:
    """Print what would be published without executing.

    Runs through the same validation and resolution logic as the real
    publish path to ensure consistency and surface errors early.
    """
    out = sys.stderr
    out.write("Dry run — the following would be published:\n\n")

    for charm in charms:
        out.write(f"{charm.name} → {channel}:\n")

        # Validate builds (same check as _publish_charm)
        if not charm.builds:
            out.write(f"  ⚠ ERROR: no builds in {_ARTIFACTS_GENERATED_YAML}\n\n")
            continue

        # Resolve resources (same logic as _upload_resources)
        resource_map = plan_resources.get(charm.name, {})
        if resource_map:
            out.write("  Resources:\n")
            for resource_name, rock_name in resource_map.items():
                image_ref = _resolve_image_ref(
                    charm, resource_name, rock_name, rocks_by_name, root
                )
                source_desc = f"rock: {rock_name}" if rock_name else "upstream-source"
                out.write(f"    {resource_name} ({source_desc}) → {image_ref}\n")
                cmd = _build_upload_resource_cmd(charm.name, resource_name, image_ref)
                out.write(f"    $ {shlex.join(cmd)}\n")
        else:
            out.write("  Resources: (none)\n")

        # Validate and show charm files (same logic as _publish_charm loop)
        out.write("  Charm files:\n")
        for build in charm.builds:
            if not build.path:
                if build.artifact and build.run_id:
                    out.write(f"    ⚠ {build.artifact} — NOT FETCHED (run-id: {build.run_id})\n")
                else:
                    out.write("    ⚠ ERROR: build has no path\n")
                continue
            charm_file = root / build.path
            exists_marker = "" if charm_file.exists() else " ⚠ MISSING"
            base_str = f"{build.base} " if build.base else ""
            out.write(f"    {build.path} ({base_str}{build.arch}){exists_marker}\n")
            cmd = _build_upload_charm_cmd(build.path, channel, resource_map)
            out.write(f"    $ {shlex.join(cmd)}\n")

        out.write("\n")


def _publish_charm(
    charm: GeneratedCharm,
    rocks_by_name: dict[str, GeneratedRock],
    plan_resources: dict[str, dict[str, str | None]],
    channel: str,
    root: Path,
) -> PublishResult:
    """Publish a single charm: upload resources, then upload+release charm files."""
    if not charm.builds:
        msg = f"Charm '{charm.name}' has no builds in {_ARTIFACTS_GENERATED_YAML}."
        raise ConfigurationError(msg)

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
    """Get the image ref for a rock from the build manifest.

    Prefers a registry ``image`` ref (shared manifest list across all arches)
    over a local ``file``.  If only files exist and the rock has multiple arch
    builds, logs a warning — only the first file is uploaded (single-arch).
    """
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

    # Prefer image: (registry ref) over file: (local archive).
    # In CI, all arch builds share the same registry manifest-list ref.
    for build in rock.builds:
        if build.image:
            return _add_transport_prefix(build.image)

    # Fall back to local file — only the first arch's file is uploaded.
    for build in rock.builds:
        if build.file:
            if len(rock.builds) > 1:
                logger.warning(
                    "Rock '%s' has %d arch builds but only local files. "
                    "Uploading only '%s' (%s). Use a registry for multi-arch.",
                    rock_name,
                    len(rock.builds),
                    build.file,
                    build.arch,
                )
            file_path = root / build.file
            if not file_path.exists():
                msg = f"Rock file '{build.file}' for rock '{rock_name}' not found at {file_path}."
                raise DiscoveryError(msg)
            return str(file_path.resolve())

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

    return _add_transport_prefix(upstream_source)


def _read_upstream_source(yaml_path: Path, resource_name: str) -> str | None:
    """Extract upstream-source for a resource from a YAML file.

    Returns ``None`` only when the file doesn't exist or the resource/field
    is simply absent.  Raises ``ConfigurationError`` on malformed YAML.
    """
    if not yaml_path.exists():
        return None
    try:
        data = load_yaml(yaml_path)
    except (YAMLError, ValueError, OSError) as exc:
        msg = f"Failed to parse {yaml_path}: {exc}"
        raise ConfigurationError(msg) from exc

    if not isinstance(data, dict):
        msg = f"Expected a YAML mapping in {yaml_path}, got {type(data).__name__}."
        raise ConfigurationError(msg)

    resources = data.get("resources")
    if resources is None:
        return None
    if not isinstance(resources, dict):
        msg = f"'resources' in {yaml_path} must be a mapping, got {type(resources).__name__}."
        raise ConfigurationError(msg)

    resource = resources.get(resource_name)
    if resource is None:
        return None
    if not isinstance(resource, dict):
        msg = (
            f"Resource '{resource_name}' in {yaml_path} must be a mapping, "
            f"got {type(resource).__name__}."
        )
        raise ConfigurationError(msg)

    upstream = resource.get("upstream-source")
    return str(upstream) if upstream else None


def _do_upload_resource(charm_name: str, resource_name: str, image_ref: str, root: Path) -> int:
    """Call charmcraft upload-resource and return the revision number."""
    cmd = _build_upload_resource_cmd(charm_name, resource_name, image_ref)
    result = run_command(cmd, cwd=str(root), stream=False)
    revision = _parse_revision(result.stdout, cmd)
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
    full_path = root / charm_path
    if not full_path.exists():
        msg = f"Charm file '{charm_path}' not found at {full_path}."
        raise DiscoveryError(msg)

    cmd = ["charmcraft", "upload", charm_path, f"--release={channel}", "--format=json"]
    for res_name, rev in resource_flags:
        cmd.append(f"--resource={res_name}:{rev}")

    with step(f"Uploading '{charm_path}' to {channel}"):
        result = run_command(cmd, cwd=str(root), stream=False)

    revision = _parse_revision(result.stdout, cmd)
    status(f"Uploaded charm '{charm_name}' → revision {revision}")
    return revision


# ---------------------------------------------------------------------------
#  Lowest-level utilities
# ---------------------------------------------------------------------------

_TRANSPORT_PREFIX_RE = "^[a-z][a-z0-9+.-]*:"


def _build_upload_resource_cmd(charm_name: str, resource_name: str, image_ref: str) -> list[str]:
    """Build the charmcraft upload-resource command."""
    return [
        "charmcraft",
        "upload-resource",
        charm_name,
        resource_name,
        f"--image={image_ref}",
        "--format=json",
    ]


def _build_upload_charm_cmd(
    charm_path: str, channel: str, resource_names: dict[str, str | None]
) -> list[str]:
    """Build the charmcraft upload command (with placeholder resource revisions)."""
    cmd = ["charmcraft", "upload", charm_path, f"--release={channel}", "--format=json"]
    for res_name in resource_names:
        cmd.append(f"--resource={res_name}:<rev>")
    return cmd


def _add_transport_prefix(ref: str) -> str:
    """Ensure a registry ref has a skopeo transport prefix.

    If the ref already contains a transport (e.g. ``docker://``, ``oci-archive:``),
    returns it unchanged.  Otherwise prepends ``docker://``.
    """
    if re.match(_TRANSPORT_PREFIX_RE, ref):
        return ref
    return f"docker://{ref}"


def _parse_revision(stdout: str, cmd: list[str]) -> int:
    """Parse the revision number from charmcraft's JSON output.

    Raises ``ConfigurationError`` with context if output is malformed.
    """
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        msg = f"Failed to parse JSON output from {shlex.join(cmd)}: {exc}\nOutput was: {stdout!r}"
        raise ConfigurationError(msg) from exc

    if not isinstance(parsed, dict) or "revision" not in parsed:
        msg = (
            f"Unexpected output from {shlex.join(cmd)}: "
            f"expected {{'revision': N}}, got: {stdout!r}"
        )
        raise ConfigurationError(msg)

    return int(parsed["revision"])
