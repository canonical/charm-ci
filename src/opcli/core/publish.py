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
from typing import TYPE_CHECKING, Any, cast

from ruamel.yaml.error import YAMLError

if TYPE_CHECKING:
    from opcli.models.artifacts import CharmArtifact

from opcli.core.constants import ARTIFACTS_BUILD_YAML, ARTIFACTS_YAML
from opcli.core.exceptions import ConfigurationError, DiscoveryError
from opcli.core.progress import status, step
from opcli.core.subprocess import run_command
from opcli.core.yaml_io import load_artifacts_build, load_artifacts_plan, load_yaml
from opcli.models.artifacts_build import (
    ArtifactsGenerated,
    CharmOutput,
    GeneratedCharm,
    GeneratedRock,
)

logger = logging.getLogger(__name__)


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


def publish_results_to_dicts(results: list[PublishResult]) -> list[dict[str, object]]:
    """Serialize publish results to a JSON-compatible list of dicts."""
    return [
        {
            "charm_name": r.charm_name,
            "channel": r.channel,
            "releases": [
                {"revision": e.revision, "base": e.base, "arch": e.arch} for e in r.releases
            ],
            "resources": r.resources,
        }
        for r in results
    ]


def artifacts_publish(
    root: Path,
    *,
    channel: str | None = None,
    charm_names: list[str] | None = None,
    dry_run: bool = False,
) -> list[PublishResult]:
    """Publish charms and their resources to CharmHub.

    Args:
        root: Project root directory.
        channel: CharmHub channel (e.g. ``latest/edge``, ``1.0/stable``).
            Acts as a global default/fallback.  A per-charm ``channel`` set
            in ``artifacts.yaml`` takes precedence over this value.
            Either this or a per-charm channel must be set for every charm.
        charm_names: Publish only these charms. ``None`` means all.
        dry_run: Print what would be uploaded without executing.

    Returns:
        List of publish results, one per charm.

    Raises:
        ConfigurationError: If manifest files are missing, charms have
            un-fetched CI artifacts, or no channel is resolvable for a charm.
        DiscoveryError: If a resource references a rock not in the build manifest.
    """
    gen_path = root / ARTIFACTS_BUILD_YAML

    if not gen_path.exists():
        msg = (
            f"{ARTIFACTS_BUILD_YAML} not found in {root}. "
            "Run 'opcli artifacts build' or 'opcli artifacts fetch' first."
        )
        raise ConfigurationError(msg)

    generated = load_artifacts_build(gen_path)

    # Load per-charm channel overrides from artifacts.yaml (optional).
    plan_charms: dict[str, CharmArtifact] = {}
    plan_path = root / ARTIFACTS_YAML
    if plan_path.exists():
        plan = load_artifacts_plan(plan_path)
        plan_charms = {c.name: c for c in plan.charms}

    charms = _select_charms(generated, charm_names)
    rocks_by_name = {r.name: r for r in generated.rocks}

    # Build resource→rock mapping from the build manifest
    plan_resources: dict[str, dict[str, str | None]] = {}
    for charm in charms:
        if charm.resources:
            plan_resources[charm.name] = {
                res_name: res.rock for res_name, res in charm.resources.items()
            }

    # Resolve and validate channels before doing any work.
    charm_channels: dict[str, str] = {}
    for charm in charms:
        charm_channels[charm.name] = _resolve_channel(
            charm.name,
            plan_charms.get(charm.name),
            channel,
        )

    if dry_run:
        _print_dry_run(charms, rocks_by_name, plan_resources, charm_channels, root)
        return []

    results: list[PublishResult] = []
    for charm in charms:
        result = _publish_charm(
            charm, rocks_by_name, plan_resources, charm_channels[charm.name], root
        )
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
                f"Charm '{name}' not found in {ARTIFACTS_BUILD_YAML}. "
                f"Available: {', '.join(sorted(available))}"
            )
            raise ConfigurationError(msg)

    return [c for c in generated.charms if c.name in set(charm_names)]


def _resolve_channel(
    charm_name: str,
    plan_charm: "CharmArtifact | None",
    global_channel: str | None,
) -> str:
    """Return the effective channel for a charm.

    Resolution order:
    1. Per-charm ``channel`` in ``artifacts.yaml`` (highest priority)
    2. Global ``--channel`` CLI flag (fallback)
    3. ``ConfigurationError`` if neither is set.
    """
    resolved = (plan_charm.channel if plan_charm is not None else None) or global_channel
    if not resolved:
        msg = (
            f"No channel specified for charm '{charm_name}'. "
            "Set 'channel' in artifacts.yaml or pass --channel."
        )
        raise ConfigurationError(msg)
    return resolved


def _print_dry_run(
    charms: list[GeneratedCharm],
    rocks_by_name: dict[str, GeneratedRock],
    plan_resources: dict[str, dict[str, str | None]],
    charm_channels: dict[str, str],
    root: Path,
) -> None:
    """Print what would be published without executing.

    Runs through the same validation and resolution logic as the real
    publish path.  Raises the same exceptions on invalid state — if
    dry-run succeeds, the real run should too (modulo network errors).
    """
    out = sys.stderr
    out.write("Dry run — the following would be published:\n\n")

    for charm in charms:
        channel = charm_channels[charm.name]
        out.write(f"{charm.name} → {channel}:\n")

        # Same validation as _publish_charm
        if not charm.builds:
            msg = f"Charm '{charm.name}' has no builds in {ARTIFACTS_BUILD_YAML}."
            raise ConfigurationError(msg)

        resource_map = plan_resources.get(charm.name, {})
        _print_dry_run_charm(charm, resource_map, rocks_by_name, channel, root, out)

        out.write("\n")


def _print_dry_run_charm(  # noqa: PLR0913
    charm: GeneratedCharm,
    resource_map: dict[str, str | None],
    rocks_by_name: dict[str, GeneratedRock],
    channel: str,
    root: Path,
    out: Any,
) -> None:
    """Print dry-run output for a single charm."""
    if resource_map:
        # Stage 1: Upload all charm builds (no release)
        out.write("  Stage 1: Upload charm builds (registers resources)\n")
        for build in charm.builds:
            _validate_build_path(charm.name, build, root)
            path = cast(str, build.path)
            base_str = f"{build.base} " if build.base else ""
            out.write(f"    {path} ({base_str}{build.arch})\n")
            cmd_upload: list[str] = ["charmcraft", "upload", path, "--format=json"]
            out.write(f"    $ {shlex.join(cmd_upload)}\n")

        # Stage 2: Upload resources
        out.write("  Stage 2: Upload resources\n")
        for resource_name, rock_name in resource_map.items():
            image_ref = _resolve_image_ref(charm, resource_name, rock_name, rocks_by_name, root)
            source_desc = f"rock: {rock_name}" if rock_name else "upstream-source"
            out.write(f"    {resource_name} ({source_desc}) → {image_ref}\n")
            cmd = _build_upload_resource_cmd(charm.name, resource_name, image_ref)
            out.write(f"    $ {shlex.join(cmd)}\n")

        # Stage 3: Release each revision to channel
        out.write("  Stage 3: Release to channel with resource bindings\n")
        for build in charm.builds:
            path = cast(str, build.path)
            base_str = f"{build.base} " if build.base else ""
            out.write(f"    {path} ({base_str}{build.arch})\n")
            cmd_release = _build_release_cmd(charm.name, channel, resource_map)
            out.write(f"    $ {shlex.join(cmd_release)}\n")
    else:
        out.write("  Resources: (none)\n")
        out.write("  Charm files:\n")
        for build in charm.builds:
            _validate_build_path(charm.name, build, root)
            path = cast(str, build.path)
            base_str = f"{build.base} " if build.base else ""
            out.write(f"    {path} ({base_str}{build.arch})\n")
            cmd = _build_upload_charm_cmd(path, channel, resource_map)
            out.write(f"    $ {shlex.join(cmd)}\n")


def _validate_build_path(charm_name: str, build: CharmOutput, root: Path) -> None:
    """Validate that a build has a path and the file exists."""
    if not build.path:
        if build.artifact and build.run_id:
            msg = (
                f"Charm '{charm_name}' has un-fetched CI artifacts "
                f"(artifact: {build.artifact}, run-id: {build.run_id}). "
                f"Run 'opcli artifacts fetch --run-id {build.run_id}' first."
            )
            raise ConfigurationError(msg)
        msg = f"Charm '{charm_name}' build has no path."
        raise ConfigurationError(msg)

    charm_file = root / build.path
    if not charm_file.exists():
        msg = f"Charm file '{build.path}' not found at {charm_file}."
        raise DiscoveryError(msg)


def _publish_charm(
    charm: GeneratedCharm,
    rocks_by_name: dict[str, GeneratedRock],
    plan_resources: dict[str, dict[str, str | None]],
    channel: str,
    root: Path,
) -> PublishResult:
    """Publish a single charm: upload, upload resources, then release.

    CharmHub requires a charm revision to be uploaded before resources can be
    uploaded — the charm's metadata defines which resources exist.  The workflow:
    1. Upload all charm builds (no release) → get revision numbers
    2. Upload resources (CharmHub now recognizes resource names from step 1)
    3. Release each revision to channel with resource bindings

    If the charm has no resources, steps are collapsed: upload with --release
    directly (single command per build, matching charming-actions behaviour).
    """
    if not charm.builds:
        msg = f"Charm '{charm.name}' has no builds in {ARTIFACTS_BUILD_YAML}."
        raise ConfigurationError(msg)

    _validate_build_paths(charm)

    resource_map = plan_resources.get(charm.name, {})

    if resource_map:
        return _publish_charm_with_resources(
            charm, rocks_by_name, plan_resources, resource_map, channel, root
        )
    return _publish_charm_without_resources(charm, channel, root)


def _publish_charm_with_resources(  # noqa: PLR0913
    charm: GeneratedCharm,
    rocks_by_name: dict[str, GeneratedRock],
    plan_resources: dict[str, dict[str, str | None]],
    resource_map: dict[str, str | None],
    channel: str,
    root: Path,
) -> PublishResult:
    """Publish a charm that has resources: upload → resources → release."""
    # Stage 1: Upload all charm builds (no release) to register resource definitions
    build_revisions: list[tuple[int, CharmOutput]] = []
    for build in charm.builds:
        path = cast(str, build.path)
        with step(f"Uploading '{path}' (registering resources)"):
            revision = _upload_charm_no_release(charm.name, path, root)
        build_revisions.append((revision, build))

    # Stage 2: Upload resources (CharmHub now knows about them)
    resource_flags = _upload_resources(charm, rocks_by_name, plan_resources, root)

    # Stage 3: Release each charm revision to channel with resource bindings
    releases: list[ReleaseEntry] = []
    for revision, build in build_revisions:
        _release_charm(charm.name, revision, channel, resource_flags, root)
        releases.append(ReleaseEntry(revision=revision, base=build.base, arch=build.arch))

    return PublishResult(
        charm_name=charm.name,
        channel=channel,
        releases=releases,
        resources=dict(resource_flags),
    )


def _publish_charm_without_resources(
    charm: GeneratedCharm,
    channel: str,
    root: Path,
) -> PublishResult:
    """Publish a charm with no resources: upload + release in one step."""
    releases: list[ReleaseEntry] = []
    for build in charm.builds:
        path = cast(str, build.path)
        entry = _upload_and_release_charm(charm.name, path, channel, [], root)
        releases.append(ReleaseEntry(revision=entry, base=build.base, arch=build.arch))

    return PublishResult(
        charm_name=charm.name,
        channel=channel,
        releases=releases,
        resources={},
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
            f"{ARTIFACTS_BUILD_YAML}. Available rocks: "
            f"{', '.join(sorted(rocks_by_name.keys())) or '(none)'}"
        )
        raise DiscoveryError(msg)

    if not rock.builds:
        msg = f"Rock '{rock_name}' has no builds in {ARTIFACTS_BUILD_YAML}."
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
    if not upstream:
        return None
    if not isinstance(upstream, str):
        msg = (
            f"'upstream-source' for resource '{resource_name}' in {yaml_path} "
            f"must be a string, got {type(upstream).__name__}."
        )
        raise ConfigurationError(msg)
    return upstream


def _do_upload_resource(charm_name: str, resource_name: str, image_ref: str, root: Path) -> int:
    """Call charmcraft upload-resource and return the revision number."""
    cmd = _build_upload_resource_cmd(charm_name, resource_name, image_ref)
    result = run_command(cmd, cwd=str(root), stream=False)
    revision = _parse_revision(result.stdout, cmd)
    status(f"Uploaded resource '{resource_name}' → revision {revision}")
    return revision


def _validate_build_paths(charm: GeneratedCharm) -> None:
    """Validate all builds have paths, raising ConfigurationError if not."""
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


def _upload_charm_no_release(charm_name: str, charm_path: str, root: Path) -> int:
    """Upload a .charm file without releasing it. Returns revision.

    Used to register the charm revision (and its resource definitions) before
    uploading resources.  CharmHub requires a charm revision to exist before
    ``upload-resource`` will accept resource uploads.
    """
    full_path = root / charm_path
    if not full_path.exists():
        msg = f"Charm file '{charm_path}' not found at {full_path}."
        raise DiscoveryError(msg)

    cmd = ["charmcraft", "upload", charm_path, "--format=json"]
    result = run_command(cmd, cwd=str(root), stream=False)
    revision = _parse_revision(result.stdout, cmd)
    status(f"Uploaded charm '{charm_name}' → revision {revision}")
    return revision


def _release_charm(
    charm_name: str,
    revision: int,
    channel: str,
    resource_flags: list[tuple[str, int]],
    root: Path,
) -> None:
    """Release an already-uploaded charm revision to a channel with resource bindings."""
    cmd = [
        "charmcraft",
        "release",
        charm_name,
        f"--revision={revision}",
        f"--channel={channel}",
    ]
    for res_name, rev in resource_flags:
        cmd.append(f"--resource={res_name}:{rev}")

    with step(f"Releasing '{charm_name}' revision {revision} to {channel}"):
        run_command(cmd, cwd=str(root), stream=False)


def _upload_and_release_charm(
    charm_name: str,
    charm_path: str,
    channel: str,
    resource_flags: list[tuple[str, int]],
    root: Path,
) -> int:
    """Upload a .charm file and release it to the channel in one step. Returns revision."""
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


def _build_release_cmd(
    charm_name: str, channel: str, resource_names: dict[str, str | None]
) -> list[str]:
    """Build the charmcraft release command (with placeholder revision/resource revisions)."""
    cmd = ["charmcraft", "release", charm_name, "--revision=<rev>", f"--channel={channel}"]
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

    revision = parsed["revision"]
    if not isinstance(revision, int):
        msg = (
            f"Expected integer 'revision' from {shlex.join(cmd)}, "
            f"got {type(revision).__name__}: {revision!r}"
        )
        raise ConfigurationError(msg)

    return revision
