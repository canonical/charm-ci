# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Core logic for ``opcli artifacts`` commands.

``init`` discovers artifacts and writes ``artifacts.yaml``.
``build`` reads the plan, invokes pack tools, and writes
``artifacts.build.yaml``.
``fetch`` downloads a completed CI run's artifacts so tests can run locally.
"""

import glob as globmod
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from opcli.core.constants import ARTIFACTS_BUILD_YAML, ARTIFACTS_YAML
from opcli.core.discovery import discover_artifacts
from opcli.core.env import current_arch
from opcli.core.exceptions import (
    ConfigurationError,
    DiscoveryError,
    OpcliError,
    SubprocessError,
)
from opcli.core.pack_utils import resolve_pack_dir, with_pack_yaml_symlink
from opcli.core.progress import status, step
from opcli.core.subprocess import run_command
from opcli.core.yaml_io import (
    dump_artifacts_build,
    dump_artifacts_plan,
    load_artifacts_build,
    load_artifacts_plan,
)
from opcli.models.artifacts import (
    ArtifactsPlan,
    CharmArtifact,
    RockArtifact,
    SnapArtifact,
)
from opcli.models.artifacts_build import (
    ArtifactsGenerated,
    CharmOutput,
    GeneratedCharm,
    GeneratedResource,
    GeneratedRock,
    GeneratedSnap,
    RockOutput,
    SnapOutput,
)

logger = logging.getLogger(__name__)


_PACK_COMMANDS: dict[str, list[str]] = {
    "charm": ["charmcraft", "pack", "--verbose"],
    "rock": ["rockcraft", "pack", "--verbose"],
    "snap": ["snapcraft", "pack", "--verbose"],
}

_ROCKCRAFT_ENV = {"ROCKCRAFT_ENABLE_EXPERIMENTAL_EXTENSIONS": "1"}

_OUTPUT_GLOBS: dict[str, str] = {
    "charm": "*.charm",
    "rock": "*.rock",
    "snap": "*.snap",
}


# Pattern: {name}_{distro}{sep}{version}-{arch}.charm  (sep is - or @)
# e.g. aproxy_ubuntu-22.04-amd64.charm or traefik-k8s_ubuntu@22.04-amd64.charm
_CHARM_FILENAME_RE = re.compile(
    r"^.+_(?P<distro>[a-z]+)[-@](?P<version>\d+\.\d+)-(?P<arch>[^.]+)\.charm$"
)


# Pattern: {name}_{version}_{arch}.snap  e.g. my-snap_1.0_amd64.snap
_SNAP_FILENAME_RE = re.compile(r"^.+_[^_]+_(?P<arch>[^.]+)\.snap$")
_ROCK_FILENAME_RE = re.compile(r"^.+_[^_]+_(?P<arch>[^.]+)\.rock$")


_GITHUB_URL_RE = re.compile(r"github\.com[:/](.+?)(?:\.git)?/?$")


_WAIT_MAX_ATTEMPTS = 60
_WAIT_SLEEP_SECONDS = 30
_DEFAULT_WAIT_TIMEOUT_SECONDS = _WAIT_MAX_ATTEMPTS * _WAIT_SLEEP_SECONDS
# Keywords in gh CLI stderr that indicate a hard auth/permission failure
# rather than "artifact not yet available" — retrying is pointless for these.
_AUTH_ERROR_KEYWORDS = (
    "authentication",
    "credentials",
    "unauthorized",
    "token",
    "403",
    "401",
)

# Keywords that indicate the destination file already exists; we delete it and retry.
_FILE_EXISTS_KEYWORDS = ("file exists",)

# Conclusions that mean the artifact will never arrive — bail immediately.
_BAIL_CONCLUSIONS: frozenset[str] = frozenset({"failure", "cancelled"})

# Special arch value meaning "all architectures".
_ARCH_ALL = "all"


def artifacts_init(root: Path, *, force: bool = False) -> Path:
    """Discover artifacts and write ``artifacts.yaml``.

    Returns:
        The path to the written file.

    Raises:
        ConfigurationError: If the file already exists and *force* is False.
    """
    dest = root / ARTIFACTS_YAML
    if dest.exists() and not force:
        msg = f"{ARTIFACTS_YAML} already exists. Use --force to overwrite."
        raise ConfigurationError(msg)

    plan = discover_artifacts(root)
    dump_artifacts_plan(plan, dest)
    logger.info(
        "Wrote %s (%d charms, %d rocks, %d snaps)",
        dest,
        len(plan.charms),
        len(plan.rocks),
        len(plan.snaps),
    )
    return dest


def artifacts_path(
    root: Path,
    *,
    name: str | None = None,
    artifact_type: str | None = None,
    arch: str | None = None,
) -> list[Path]:
    """Return absolute paths to built artifacts from ``artifacts.build.yaml``.

    Args:
        root: Project root directory.
        name: Optional artifact name filter.
        artifact_type: Optional type filter (``charm``, ``rock``, ``snap``).
        arch: Architecture filter. Defaults to the current machine's arch.

    Returns:
        List of resolved absolute paths.

    Raises:
        ConfigurationError: If ``artifacts.build.yaml`` does not exist.
        DiscoveryError: If no artifacts match, or if multiple artifacts match
            without a *name* filter.
    """
    gen_path = root / ARTIFACTS_BUILD_YAML
    if not gen_path.exists():
        msg = f"{ARTIFACTS_BUILD_YAML} not found. Run 'opcli artifacts build' first."
        raise ConfigurationError(msg)

    generated = load_artifacts_build(gen_path)
    target_arch = arch or current_arch()
    paths: list[Path] = []

    if artifact_type is None or artifact_type == "charm":
        paths.extend(_collect_charm_paths(generated, target_arch, name))

    if artifact_type is None or artifact_type == "rock":
        paths.extend(_collect_rock_paths(generated, target_arch, name))

    if artifact_type is None or artifact_type == "snap":
        paths.extend(_collect_snap_paths(generated, target_arch, name))

    if not paths:
        if name:
            msg = f"No built artifact named '{name}' found in {ARTIFACTS_BUILD_YAML}."
        else:
            msg = f"No built artifacts with local paths found in {ARTIFACTS_BUILD_YAML}."
        raise DiscoveryError(msg)

    if not name and len(paths) > 1 and artifact_type == "charm":
        names = [c.name for c in generated.charms]
        if len(generated.charms) > 1:
            msg = f"Multiple charms found: {', '.join(names)}. Specify a name to disambiguate."
            raise DiscoveryError(msg)

    return [(root / p).resolve() for p in paths]


def _collect_charm_paths(generated: ArtifactsGenerated, arch: str, name: str | None) -> list[Path]:
    """Extract local charm paths matching filters."""
    paths: list[Path] = []
    for charm in generated.charms:
        if name and charm.name != name:
            continue
        for build in charm.builds:
            if build.arch == arch and build.path:
                paths.append(Path(build.path))
    # Graceful degradation: if no arch match, try all builds
    if not paths:
        for charm in generated.charms:
            if name and charm.name != name:
                continue
            for build in charm.builds:
                if build.path:
                    paths.append(Path(build.path))
    return paths


def _collect_rock_paths(generated: ArtifactsGenerated, arch: str, name: str | None) -> list[Path]:
    """Extract local rock paths matching filters."""
    paths: list[Path] = []
    for rock in generated.rocks:
        if name and rock.name != name:
            continue
        for build in rock.builds:
            if build.arch == arch and build.file:
                paths.append(Path(build.file))
    if not paths:
        for rock in generated.rocks:
            if name and rock.name != name:
                continue
            for build in rock.builds:
                if build.file:
                    paths.append(Path(build.file))
    return paths


def _collect_snap_paths(generated: ArtifactsGenerated, arch: str, name: str | None) -> list[Path]:
    """Extract local snap paths matching filters."""
    paths: list[Path] = []
    for snap in generated.snaps:
        if name and snap.name != name:
            continue
        for build in snap.builds:
            if build.arch == arch and build.file:
                paths.append(Path(build.file))
    if not paths:
        for snap in generated.snaps:
            if name and snap.name != name:
                continue
            for build in snap.builds:
                if build.file:
                    paths.append(Path(build.file))
    return paths


def artifacts_build(
    root: Path,
    *,
    charm_names: list[str] | None = None,
    rock_names: list[str] | None = None,
    snap_names: list[str] | None = None,
    build_timeout: int = 3600,
) -> Path:
    """Build artifacts and write ``artifacts.build.yaml``.

    If *charm_names*, *rock_names*, or *snap_names* are given, only
    those artifacts are built.  Otherwise all declared artifacts are built.

    Args:
        root: Working directory containing ``artifacts.yaml``.
        charm_names: Names of charms to build. ``None`` builds all.
        rock_names: Names of rocks to build. ``None`` builds all.
        snap_names: Names of snaps to build. ``None`` builds all.
        build_timeout: Maximum wall-clock seconds allowed for each individual
            pack invocation (charmcraft/rockcraft/snapcraft). Defaults to
            3600 (1 hour).

    Returns:
        The path to the written file.

    Raises:
        ConfigurationError: If ``artifacts.yaml`` does not exist.
        OpcliError: If a build fails or no output file is found.
    """
    plan_path = root / ARTIFACTS_YAML
    if not plan_path.exists():
        msg = f"{ARTIFACTS_YAML} not found. Run 'opcli artifacts init' first."
        raise ConfigurationError(msg)

    plan = load_artifacts_plan(plan_path)

    # If any type filter is provided, unspecified types default to empty so
    # that `--charm foo` builds only the charm, not all rocks/snaps too.
    any_filter = charm_names is not None or rock_names is not None or snap_names is not None
    if any_filter:
        rock_names = rock_names if rock_names is not None else []
        charm_names = charm_names if charm_names is not None else []
        snap_names = snap_names if snap_names is not None else []

    rocks_to_build = _filter_by_name(plan.rocks, rock_names, "rock")
    charms_to_build = _filter_by_name(plan.charms, charm_names, "charm")
    snaps_to_build = _filter_by_name(plan.snaps, snap_names, "snap")

    total = len(rocks_to_build) + len(charms_to_build) + len(snaps_to_build)
    parts = []
    if rocks_to_build:
        parts.append(f"{len(rocks_to_build)} rock(s)")
    if charms_to_build:
        parts.append(f"{len(charms_to_build)} charm(s)")
    if snaps_to_build:
        parts.append(f"{len(snaps_to_build)} snap(s)")
    status(f"Building {total} artifact(s): {', '.join(parts)}")

    # Track absolute output paths attributed to each artifact so far, to detect
    # collisions when multiple artifacts share a pack-dir.
    attributed: set[str] = set()
    gen_rocks = [_build_rock(r, root, attributed, build_timeout) for r in rocks_to_build]
    gen_charms = [_build_charm(c, root, attributed, build_timeout) for c in charms_to_build]
    gen_snaps = [_build_snap(s, root, attributed, build_timeout) for s in snaps_to_build]

    # In GitHub Actions, rewrite outputs to CI-format references.
    ci = _get_ci_context()
    if ci is not None:
        upload_mode = _get_upload_mode()
        if upload_mode == "registry":
            gen_rocks = [_push_rock_to_ghcr(r, ci, root) for r in gen_rocks]
        else:
            # artifact mode: keep file: reference, add artifact + run-id metadata
            gen_rocks = [_to_ci_rock_artifact(r, ci) for r in gen_rocks]
        gen_charms = [_to_ci_charm(c, ci) for c in gen_charms]
        gen_snaps = [_to_ci_snap(s, ci) for s in gen_snaps]

    generated = ArtifactsGenerated(
        rocks=gen_rocks,
        charms=gen_charms,
        snaps=gen_snaps,
    )

    dest = root / ARTIFACTS_BUILD_YAML

    # When a filter is active and an existing build file exists, merge new
    # entries into the previous results so unrelated artifacts are preserved.
    if any_filter and dest.exists():
        existing = load_artifacts_build(dest)
        generated = _merge_generated(existing, generated)

    dump_artifacts_build(generated, dest)
    logger.info("Wrote %s", dest)
    return dest


def artifacts_matrix(root: Path) -> dict[str, list[dict[str, object]]]:
    """Read ``artifacts.yaml`` and return a GitHub Actions matrix dict.

    The returned dict has a single key ``"include"`` whose value is a list of
    entries — one per (artifact, arch) combination.  Each entry has
    ``"name"``, ``"type"``, ``"arch"``, and ``"runner"`` keys; rock entries
    additionally include ``"rockcraft-yaml"`` and ``"pack-dir"`` so the
    calling workflow can locate the source directory for cache-key computation.
    ``runner`` is a list of GitHub runner label strings.

    Rocks come first, then charms, then snaps.  Within each kind, entries are
    ordered by artifact declaration order, then by ``platforms`` order.

    The result is JSON-serialisable and suitable for use as a GitHub Actions
    ``strategy.matrix`` value via ``$GITHUB_OUTPUT``.

    Raises:
        ConfigurationError: If ``artifacts.yaml`` does not exist.
    """
    plan_path = root / ARTIFACTS_YAML
    if not plan_path.exists():
        msg = f"{ARTIFACTS_YAML} not found. Run 'opcli artifacts init' first."
        raise ConfigurationError(msg)

    plan = load_artifacts_plan(plan_path)
    include: list[dict[str, object]] = []
    for rock in plan.rocks:
        for build in rock.platforms:
            include.append(
                {
                    "name": rock.name,
                    "type": "rock",
                    "arch": build.arch,
                    "runner": json.dumps(build.runner or ["ubuntu-latest"]),
                    "rockcraft-yaml": rock.rockcraft_yaml,
                    "pack-dir": rock.pack_dir or "",
                }
            )
    for charm in plan.charms:
        for build in charm.platforms:
            include.append(
                {
                    "name": charm.name,
                    "type": "charm",
                    "arch": build.arch,
                    "runner": json.dumps(build.runner or ["ubuntu-latest"]),
                }
            )
    for snap in plan.snaps:
        for build in snap.platforms:
            include.append(
                {
                    "name": snap.name,
                    "type": "snap",
                    "arch": build.arch,
                    "runner": json.dumps(build.runner or ["ubuntu-latest"]),
                }
            )

    return {"include": include}


def artifacts_collect(root: Path, partial_paths: list[Path]) -> Path:
    """Merge partial ``artifacts.build.yaml`` files into one.

    In CI, each matrix build job produces a partial file containing only the
    artifact it built.  This function merges all partials into a single
    ``artifacts.build.yaml`` by concatenating per-arch build entries for each
    artifact type, then validates that every charm resource's referenced rock
    is present in the merged output.  Image refs on rocks are not resolved
    here — they are resolved lazily at publish time (``opcli artifacts publish``).

    Args:
        root: Repository root; the merged file is written here.
        partial_paths: Paths to the partial ``artifacts.build.yaml`` files.

    Returns:
        The path to the written merged file.

    Raises:
        ConfigurationError: If *partial_paths* is empty or a path does not exist.
    """
    if not partial_paths:
        msg = "No partial artifacts.build.yaml files provided to collect."
        raise ConfigurationError(msg)

    for p in partial_paths:
        if not p.exists():
            msg = f"Partial artifacts.build.yaml not found: {p}"
            raise ConfigurationError(msg)

    all_rocks: list[GeneratedRock] = []
    all_charms: list[GeneratedCharm] = []
    all_snaps: list[GeneratedSnap] = []

    for p in partial_paths:
        partial = load_artifacts_build(p)
        all_rocks.extend(partial.rocks)
        all_charms.extend(partial.charms)
        all_snaps.extend(partial.snaps)

    # Merge same-named artifacts: combine their output lists.
    # Reject duplicate (name, arch) combinations across partials.
    merged_rocks = _merge_artifact_outputs(all_rocks, "rock")
    merged_charms = _merge_artifact_outputs(all_charms, "charm")
    merged_snaps = _merge_artifact_outputs(all_snaps, "snap")

    # Validate that every rock referenced by a charm resource is present.
    rocks_by_name: dict[str, GeneratedRock] = {r.name: r for r in merged_rocks}
    for charm in merged_charms:
        for res_name, res in (charm.resources or {}).items():
            if res.rock and res.rock not in rocks_by_name:
                msg = (
                    f"Charm '{charm.name}' resource '{res_name}' references rock "
                    f"'{res.rock}' which was not found in the collected partials. "
                    f"Ensure the rock build job partial is included."
                )
                raise ConfigurationError(msg)

    generated = ArtifactsGenerated(
        rocks=merged_rocks,
        charms=merged_charms,
        snaps=merged_snaps,
    )
    dest = root / ARTIFACTS_BUILD_YAML
    dump_artifacts_build(generated, dest)
    logger.info("Wrote merged %s", dest)
    return dest


def artifacts_localize(root: Path) -> int:
    """Update ``artifacts.build.yaml`` with local artifact file paths.

    In CI, charm, snap, and rock outputs are recorded as ``artifact + run-id``
    references.  Before running integration tests, the workflow downloads
    the artifacts to the working directory.  This command scans the project
    tree for ``.charm`` / ``.snap`` / ``.rock`` files and rewrites
    ``artifacts.build.yaml`` so that each output with only a CI artifact
    reference gets a local path entry (``path`` for charms, ``file`` for
    snaps and rocks).

    Returns the total number of arch-builds that were localised.

    Raises:
        ConfigurationError: If ``artifacts.build.yaml`` is not found or
            if any artifact with a CI reference has no matching local file.
    """
    gen_path = root / ARTIFACTS_BUILD_YAML
    if not gen_path.exists():
        msg = f"{ARTIFACTS_BUILD_YAML} not found."
        raise ConfigurationError(msg)

    generated = load_artifacts_build(gen_path)

    updated = 0
    missing: list[str] = []

    for charm in generated.charms:
        updated += _localize_charm(charm, root, missing)

    for snap in generated.snaps:
        updated += _localize_snap(snap, root, missing)

    for rock in generated.rocks:
        updated += _localize_rock(rock, root, missing)

    if missing:
        msg = (
            f"Could not find downloaded artifact files for: {', '.join(missing)}. "
            "Ensure artifacts were downloaded before running localize."
        )
        raise ConfigurationError(msg)

    if updated:
        dump_artifacts_build(generated, gen_path)
        logger.info("Updated %s with %d localised artifact(s).", gen_path, updated)

    return updated


def artifacts_fetch(  # noqa: PLR0913
    root: Path,
    run_id: str,
    repo: str | None = None,
    *,
    wait: bool = False,
    wait_timeout: int | None = None,
    arch: str | None = None,
) -> Path:
    """Download a CI run's artifacts and prepare for local testing.

    The *arch* parameter selects what to download:

    - ``arch=None`` (default) — auto-detects the current machine's
      architecture via :func:`~opcli.core.env.current_arch`.
    - ``arch="amd64"`` (or any specific arch) — download only that arch.
    - ``arch="all"`` — download artifacts for every architecture.

    For single-arch modes, per-arch partial build manifests are downloaded
    directly and merged locally.  For ``arch="all"``, all partial build
    manifests matching ``artifacts-build-*`` are downloaded and merged.

    Rock artifacts with ``image:`` references (GHCR) are never downloaded.
    Rock artifacts with ``artifact:`` references (fork-PR mode) are
    downloaded alongside charms and snaps.

    Steps:
    1. Infer ``owner/repo`` from the local git remote if *repo* is not given.
    2. Resolve *arch* (auto-detect if ``None``).
    3. Download build manifest partial(s) for the resolved arch.
    4. For every charm/snap/rock that carries a CI artifact reference,
       download the archive (filtered to *arch* unless ``arch="all"``).
    5. Call :func:`artifacts_localize` to rewrite ``artifacts.build.yaml``
       with local ``.charm`` / ``.snap`` file paths.

    Args:
        root: Working directory; all artifacts are downloaded here.
        run_id: GitHub Actions workflow run ID.
        repo: GitHub repository in ``owner/name`` format.  Inferred from the
            local git remote when ``None``.
        wait: When ``True``, retry the manifest download(s) until they
            appear.  Specifying *wait_timeout* also enables waiting.
            Fails immediately on authentication/permission errors.
        wait_timeout: Maximum seconds to wait for artifacts to appear.
            When ``None`` (default), uses :data:`_DEFAULT_WAIT_TIMEOUT_SECONDS`
            (1800 s / 30 min). Providing any value enables waiting even when
            *wait* is ``False``.
        arch: Architecture to fetch.  ``None`` auto-detects the current
            machine.  ``"all"`` fetches every architecture.

    Returns:
        Path to the updated ``artifacts.build.yaml``.

    Raises:
        ConfigurationError: If the repo cannot be inferred, ``artifacts.yaml``
            is missing (required for partial-manifest discovery), no artifacts
            are defined for the requested arch, or the wait timeout is exceeded.
        SubprocessError: If ``gh run download`` fails non-transiently.
    """
    if repo is None:
        repo = _infer_repo_from_git(root)

    effective_timeout = wait_timeout if wait_timeout is not None else _DEFAULT_WAIT_TIMEOUT_SECONDS
    # Providing wait_timeout also enables waiting.
    enable_wait = wait or wait_timeout is not None

    if arch == _ARCH_ALL:
        return _artifacts_fetch_all_arches(
            root, run_id, repo, wait=enable_wait, wait_timeout=effective_timeout
        )

    effective_arch = arch if arch is not None else current_arch()
    autodetected = arch is None
    if autodetected:
        status(f"Fetching artifacts for arch: {effective_arch} (auto-detected)")
    else:
        status(f"Fetching artifacts for arch: {effective_arch}")
    return _artifacts_fetch_by_arch(
        root, run_id, repo, arch=effective_arch, wait=enable_wait, wait_timeout=effective_timeout
    )


def _artifacts_fetch_all_arches(
    root: Path,
    run_id: str,
    repo: str,
    *,
    wait: bool,
    wait_timeout: int = _DEFAULT_WAIT_TIMEOUT_SECONDS,
) -> Path:
    """Fetch artifacts for all architectures by downloading all partial manifests.

    Downloads every ``artifacts-build-*`` partial using a glob pattern,
    merges them locally, then downloads all artifact archives.  This
    replaces the old ``Collect artifacts`` CI job — no merged
    ``artifacts-build`` artifact is required.

    With *wait*, retries until all expected partial manifests from
    ``artifacts.yaml`` are present; bails early if the run itself fails.
    """
    status("Fetching artifacts for all architectures")

    plan_path = root / ARTIFACTS_YAML
    if not plan_path.exists():
        msg = (
            f"{ARTIFACTS_YAML} not found.  Fetching all arches requires "
            "artifacts.yaml to discover the expected partial manifests."
        )
        raise ConfigurationError(msg)
    plan = load_artifacts_plan(plan_path)

    all_partial_names = _all_partial_manifest_names(plan)
    if not all_partial_names:
        msg = f"No artifacts are defined in {ARTIFACTS_YAML}."
        raise ConfigurationError(msg)

    partial_dir = root / "partial-artifacts-fetch"
    # Always start clean so stale partials from a previous fetch (e.g. a
    # different run_id or an earlier single-arch fetch) can never corrupt
    # the merge.  The wait path also clears before each retry; doing it here
    # guarantees both branches are safe.
    if partial_dir.exists():
        shutil.rmtree(partial_dir)
    partial_dir.mkdir(parents=True)

    pattern_cmd = [
        "gh",
        "run",
        "download",
        run_id,
        "--repo",
        repo,
        "--pattern",
        "artifacts-build-*",
        "--dir",
        str(partial_dir),
    ]

    if wait:
        _gh_download_all_partials_with_wait(
            pattern_cmd,
            str(root),
            run_id,
            repo,
            partial_dir,
            all_partial_names,
            wait_timeout=wait_timeout,
        )
    else:
        run_command(pattern_cmd, cwd=str(root))
        _normalize_partial_dir_layout(partial_dir)
        missing = _missing_partial_names(partial_dir, all_partial_names)
        if missing:
            msg = (
                f"{len(missing)} partial build manifest(s) were not available: "
                f"{', '.join(missing[:5])}. "
                "The build may still be running — use --wait to retry until all "
                "partials are present."
            )
            raise ConfigurationError(msg)

    # Collect all downloaded partial files.
    partial_paths = sorted(partial_dir.rglob(ARTIFACTS_BUILD_YAML))
    if not partial_paths:
        msg = (
            f"No {ARTIFACTS_BUILD_YAML} files found under {partial_dir} after download. "
            "Ensure the CI run has completed its build phase."
        )
        raise ConfigurationError(msg)

    gen_path = artifacts_collect(root, partial_paths)

    # Download all artifact archives (all arches).
    generated = load_artifacts_build(gen_path)
    seen_artifacts = _all_artifact_archives(generated)
    _download_artifact_archives(root, run_id, repo, seen_artifacts)

    artifacts_localize(root)
    return gen_path


def _artifacts_fetch_by_arch(  # noqa: PLR0913
    root: Path,
    run_id: str,
    repo: str,
    *,
    arch: str,
    wait: bool,
    wait_timeout: int = _DEFAULT_WAIT_TIMEOUT_SECONDS,
) -> Path:
    """Fetch artifacts for a single architecture.

    Downloads per-arch partial build manifests, merges them locally, then
    downloads the arch-specific artifact archives.  Does not depend on the
    ``Collect artifacts`` CI job.
    """
    plan_path = root / ARTIFACTS_YAML
    if not plan_path.exists():
        msg = (
            f"{ARTIFACTS_YAML} not found.  Arch-filtered fetch requires "
            "artifacts.yaml to discover the expected partial manifests."
        )
        raise ConfigurationError(msg)
    plan = load_artifacts_plan(plan_path)

    partial_names = _partial_manifest_names_for_arch(plan, arch)
    if not partial_names:
        available = _all_defined_arches(plan)
        available_str = ", ".join(sorted(available)) if available else "(none)"
        msg = (
            f"No artifacts are defined for arch '{arch}' in {ARTIFACTS_YAML}. "
            f"Available: {available_str}."
        )
        raise ConfigurationError(msg)

    partial_paths = _download_partial_manifests(
        partial_names, root, run_id, repo, wait=wait, wait_timeout=wait_timeout
    )
    gen_path = artifacts_collect(root, partial_paths)

    generated = load_artifacts_build(gen_path)
    seen_artifacts = _arch_artifact_archives(generated, arch)
    _download_artifact_archives(root, run_id, repo, seen_artifacts)

    artifacts_localize(root)
    return gen_path


def _all_defined_arches(plan: ArtifactsPlan) -> set[str]:
    """Return the set of all architectures declared in an artifacts plan."""
    arches: set[str] = set()
    for rock in plan.rocks:
        arches.update(b.arch for b in rock.platforms)
    for charm in plan.charms:
        arches.update(b.arch for b in charm.platforms)
    for snap in plan.snaps:
        arches.update(b.arch for b in snap.platforms)
    return arches


def _all_partial_manifest_names(plan: ArtifactsPlan) -> list[str]:
    """Return all expected partial manifest artifact names across every arch."""
    names: list[str] = []
    for rock in plan.rocks:
        for b in rock.platforms:
            names.append(f"artifacts-build-rock-{rock.name}-{b.arch}")
    for charm in plan.charms:
        for b in charm.platforms:
            names.append(f"artifacts-build-charm-{charm.name}-{b.arch}")
    for snap in plan.snaps:
        for b in snap.platforms:
            names.append(f"artifacts-build-snap-{snap.name}-{b.arch}")
    return names


def _all_artifact_archives(generated: ArtifactsGenerated) -> set[str]:
    """Return all GitHub artifact archive names across every architecture."""
    seen: set[str] = set()
    for rock in generated.rocks:
        for rb in rock.builds:
            if rb.artifact:
                seen.add(rb.artifact)
    for charm in generated.charms:
        for cb in charm.builds:
            if cb.artifact:
                seen.add(cb.artifact)
    for snap in generated.snaps:
        for sb in snap.builds:
            if sb.artifact:
                seen.add(sb.artifact)
    return seen


def _partial_manifest_names_for_arch(
    plan: ArtifactsPlan,
    arch: str,
) -> list[str]:
    """Return the GitHub artifact names for per-arch partial build manifests.

    The naming convention ``artifacts-build-{type}-{name}-{arch}`` mirrors
    what ``build-artifacts.yml`` uploads in the ``Upload partial build manifest``
    step.
    """
    names: list[str] = []
    for rock in plan.rocks:
        if any(b.arch == arch for b in rock.platforms):
            names.append(f"artifacts-build-rock-{rock.name}-{arch}")
    for charm in plan.charms:
        if any(b.arch == arch for b in charm.platforms):
            names.append(f"artifacts-build-charm-{charm.name}-{arch}")
    for snap in plan.snaps:
        if any(b.arch == arch for b in snap.platforms):
            names.append(f"artifacts-build-snap-{snap.name}-{arch}")
    return names


def _download_partial_manifests(  # noqa: PLR0913
    partial_names: list[str],
    root: Path,
    run_id: str,
    repo: str,
    *,
    wait: bool,
    wait_timeout: int = _DEFAULT_WAIT_TIMEOUT_SECONDS,
) -> list[Path]:
    """Download per-arch partial ``artifacts.build.yaml`` files from a CI run.

    Each partial is downloaded into
    ``root/partial-artifacts-fetch/{name}/artifacts.build.yaml``.
    Returns the list of downloaded file paths (one per name).

    The *wait_timeout* budget is shared across all partials: each call to
    :func:`_gh_download_with_wait` receives only the *remaining* budget so
    the total wait is bounded close to *wait_timeout*.  A floor of
    :data:`_WAIT_SLEEP_SECONDS` guarantees at least one download attempt per
    partial even when the budget is already spent; the total may therefore
    marginally exceed *wait_timeout* by one API round-trip per remaining
    partial.
    """
    partial_dir = root / "partial-artifacts-fetch"
    partial_paths: list[Path] = []
    deadline = time.monotonic() + wait_timeout
    for name in partial_names:
        dest_dir = partial_dir / name
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / ARTIFACTS_BUILD_YAML
        cmd = [
            "gh",
            "run",
            "download",
            run_id,
            "--repo",
            repo,
            "--name",
            name,
            "--dir",
            str(dest_dir),
        ]
        status(f"Downloading build manifest '{name}'")
        if wait:
            remaining = max(_WAIT_SLEEP_SECONDS, int(deadline - time.monotonic()))
            _gh_download_with_wait(
                cmd, str(root), run_id=run_id, repo=repo, dest=dest, wait_timeout=remaining
            )
        else:
            _gh_download(cmd, str(root), dest=dest)
        partial_paths.append(dest)
    return partial_paths


def _arch_artifact_archives(generated: ArtifactsGenerated, arch: str) -> set[str]:
    """Return the set of GitHub artifact archive names for a given architecture."""
    seen: set[str] = set()
    for rock in generated.rocks:
        for rock_build in rock.builds:
            if rock_build.artifact and rock_build.arch == arch:
                seen.add(rock_build.artifact)
    for charm in generated.charms:
        for charm_build in charm.builds:
            if charm_build.artifact and charm_build.arch == arch:
                seen.add(charm_build.artifact)
    for snap in generated.snaps:
        for snap_build in snap.builds:
            if snap_build.artifact and snap_build.arch == arch:
                seen.add(snap_build.artifact)
    return seen


def _download_artifact_archives(
    root: Path,
    run_id: str,
    repo: str,
    artifact_names: set[str],
) -> None:
    """Download a set of GitHub Actions artifact archives to *root*."""
    for name in sorted(artifact_names):
        artifact_dir = _safe_artifact_dir(root, name)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        status(f"Downloading artifact '{name}'")
        run_command(
            [
                "gh",
                "run",
                "download",
                run_id,
                "--repo",
                repo,
                "--name",
                name,
                "--dir",
                str(artifact_dir),
            ],
            cwd=str(root),
        )
        logger.info("Downloaded artifact '%s'.", name)


def _filter_by_name[T: (RockArtifact, CharmArtifact, SnapArtifact)](
    items: list[T],
    names: list[str] | None,
    kind: str,
) -> list[T]:
    """Return items filtered by *names*, or all if *names* is None.

    ``None`` means "no filter — build all".  An empty list means "build none".
    """
    if names is None:
        return items
    name_set = set(names)
    available = {item.name for item in items}
    unknown = name_set - available
    if unknown:
        msg = f"Unknown {kind}(s): {', '.join(sorted(unknown))}"
        raise ConfigurationError(msg)
    return [item for item in items if item.name in name_set]


def _build_rock(
    rock: RockArtifact, root: Path, attributed: set[str], build_timeout: int = 3600
) -> GeneratedRock:
    """Build a single rock artifact."""
    yaml_path = (root / rock.rockcraft_yaml).resolve()
    if not yaml_path.is_file():
        msg = f"rockcraft-yaml not found: {rock.rockcraft_yaml}"
        raise ConfigurationError(msg)
    pack_dir = resolve_pack_dir(yaml_path, rock.pack_dir, root)
    if not pack_dir.is_dir():
        msg = f"pack-dir not found: {rock.pack_dir}"
        raise ConfigurationError(msg)

    before = _snapshot_outputs(pack_dir, "rock")

    with (
        with_pack_yaml_symlink("rockcraft.yaml", yaml_path, pack_dir),
        step(f"Building rock '{rock.name}' (rockcraft pack)"),
    ):
        run_command(
            [*_PACK_COMMANDS["rock"]], cwd=str(pack_dir), env=_ROCKCRAFT_ENV, timeout=build_timeout
        )

    after = _snapshot_outputs(pack_dir, "rock")
    try:
        new_output = _pick_new_output(before, after, "rock", pack_dir, attributed)
    except OpcliError:
        # Overwrite-in-place with multiple pre-existing files: fall back to a
        # name-prefix filter.  Rockcraft always names output as {name}_*.rock,
        # so filtering by prefix unambiguously identifies the file we just built.
        prefix = str(pack_dir / f"{rock.name}_")
        candidates = sorted(p for p in after if p.startswith(prefix))
        if len(candidates) != 1:
            raise
        new_output = candidates[0]
        if attributed and new_output in attributed:
            msg = (
                f"Output file {new_output} was already produced by another artifact. "
                "Two artifacts cannot share the same output filename in the same "
                "pack-dir. Use separate pack-dirs or ensure each artifact produces "
                "a unique filename."
            )
            raise OpcliError(msg)
    output_file = _relative_to_root(new_output, root)
    attributed.add(new_output)
    return GeneratedRock(
        name=rock.name,
        **{"rockcraft-yaml": rock.rockcraft_yaml},
        builds=[RockOutput(arch=current_arch(), file=output_file)],
    )


def _snapshot_outputs(pack_dir: Path, kind: str) -> set[str]:
    """Return the set of existing output files in *pack_dir* for *kind*."""
    return set(globmod.glob(str(pack_dir / _OUTPUT_GLOBS[kind])))


def _pick_new_output(
    before: set[str],
    after: set[str],
    kind: str,
    pack_dir: Path,
    attributed: set[str] | None = None,
) -> str:
    """Return the new output file produced by pack, relative to repo root.

    *attributed* is the set of paths already claimed by previous artifact builds
    in this session.  If the overwrite-in-place fallback would return a path that
    is already attributed, a collision is detected and an error is raised.

    Three cases:
    1. New files appeared (``after - before`` non-empty) — use those.
    2. No files at all — raise error.
    3. Same files before and after — the build overwrote an existing file in
       place.  Unambiguous only when there is exactly one file; otherwise we
       cannot determine which file was just produced.
    """
    new_files = sorted(after - before)
    if new_files:
        if len(new_files) > 1:
            logger.warning(
                "Multiple new %s files in %s; using %s",
                _OUTPUT_GLOBS[kind],
                pack_dir,
                new_files[0],
            )
        return new_files[0]

    # No new files — check overwrite-in-place case.
    if not after:
        msg = f"No {_OUTPUT_GLOBS[kind]} found in {pack_dir} after pack"
        raise OpcliError(msg)

    if len(after) == 1:
        # Exactly one pre-existing file; the build overwrote it in place.
        path = next(iter(after))
        if attributed and path in attributed:
            msg = (
                f"Output file {path} was already produced by another artifact. "
                "Two artifacts cannot share the same output filename in the same "
                "pack-dir. Use separate pack-dirs or ensure each artifact produces "
                "a unique filename."
            )
            raise OpcliError(msg)
        return path

    # Multiple pre-existing files, none added — cannot determine which was built.
    msg = (
        f"Cannot determine which {_OUTPUT_GLOBS[kind]} in {pack_dir} was just "
        "built: the pack tool overwrote an existing file but multiple "
        f"{_OUTPUT_GLOBS[kind]} files already exist. "
        "Use a dedicated pack-dir that does not contain pre-existing output files."
    )
    raise OpcliError(msg)


def _relative_to_root(path_str: str, root: Path) -> str:
    """Return *path_str* as a ``./``-prefixed relative path from *root*.

    The ``./`` prefix makes the path unambiguously local (required by Juju
    when distinguishing a local charm/rock from a CharmHub reference).
    """
    resolved = Path(path_str).resolve()
    try:
        rel = str(resolved.relative_to(root.resolve()))
        return f"./{rel}"
    except ValueError as exc:
        msg = f"Built artifact {resolved} is outside repository root {root}"
        raise OpcliError(msg) from exc


def _build_charm(
    charm: CharmArtifact,
    root: Path,
    attributed: set[str],
    build_timeout: int = 3600,
) -> GeneratedCharm:
    """Build a single charm artifact."""
    yaml_path = (root / charm.charmcraft_yaml).resolve()
    if not yaml_path.is_file():
        msg = f"charmcraft-yaml not found: {charm.charmcraft_yaml}"
        raise ConfigurationError(msg)
    pack_dir = resolve_pack_dir(yaml_path, charm.pack_dir, root)
    if not pack_dir.is_dir():
        msg = f"pack-dir not found: {charm.pack_dir}"
        raise ConfigurationError(msg)

    with (
        with_pack_yaml_symlink("charmcraft.yaml", yaml_path, pack_dir),
        step(f"Building charm '{charm.name}' (charmcraft pack)"),
    ):
        run_command([*_PACK_COMMANDS["charm"]], cwd=str(pack_dir), timeout=build_timeout)
    after = _snapshot_outputs(pack_dir, "charm")
    new_outputs = _pick_new_charm_outputs(after, pack_dir, charm.name, attributed)
    attributed.update(new_outputs)
    arch = current_arch()
    charm_outputs = [
        CharmOutput(
            arch=arch,
            path=_relative_to_root(p, root),
            base=_parse_base_from_charm_path(p),
        )
        for p in new_outputs
    ]

    resources: dict[str, GeneratedResource] = {}
    for res_name, res_def in charm.resources.items():
        resources[res_name] = GeneratedResource(
            type=res_def.type,
            rock=res_def.rock,
        )

    return GeneratedCharm(
        name=charm.name,
        **{"charmcraft-yaml": charm.charmcraft_yaml},
        **{"pack-dir": os.path.relpath(str(pack_dir), str(root.resolve()))},
        builds=charm_outputs,
        resources=resources if resources else None,
    )


def _pick_new_charm_outputs(
    after: set[str],
    pack_dir: Path,
    charm_name: str,
    attributed: set[str] | None = None,
) -> list[str]:
    """Return the charm files for *charm_name* from the pack output.

    Charmcraft names consist of lowercase letters, digits, and hyphens only
    (no underscores).  Packed filenames follow ``{name}_{suffix}.charm`` so
    filtering by the prefix ``{charm_name}_`` gives an exact name match —
    no other charm name can be a prefix of ``{charm_name}_``.

    After name-filtering, already-attributed files are removed.  If all
    matches are already attributed, two artifacts share the same internal
    charmcraft name (collision) and an error is raised.
    """
    if not after:
        msg = f"No *.charm found in {pack_dir} after pack"
        raise OpcliError(msg)

    prefix = f"{charm_name}_"
    matching = {p for p in after if Path(p).name.startswith(prefix)}
    if not matching:
        msg = (
            f"No *.charm files for charm '{charm_name}' found in {pack_dir}. "
            "Ensure the 'name' in artifacts.yaml matches the charm name produced "
            "by charmcraft (from charmcraft.yaml or metadata.yaml for split format)."
        )
        raise OpcliError(msg)

    unclaimed = matching - (attributed or set())
    if not unclaimed:
        msg = (
            f"Output files {sorted(matching)} were already produced by another "
            "artifact. Two artifacts cannot share the same output filenames in "
            "the same pack-dir. Use separate pack-dirs or ensure each charm "
            "produces unique filenames."
        )
        raise OpcliError(msg)

    return sorted(unclaimed)


def _parse_base_from_charm_path(path: str) -> str | None:
    """Return the base string (e.g. ``ubuntu@22.04``) parsed from a charm filename.

    Returns ``None`` if the filename does not follow the expected
    ``{name}_{distro}-{version}-{arch}.charm`` convention.
    """
    filename = Path(path).name
    m = _CHARM_FILENAME_RE.match(filename)
    if not m:
        return None
    return f"{m.group('distro')}@{m.group('version')}"


def _build_snap(
    snap: SnapArtifact, root: Path, attributed: set[str], build_timeout: int = 3600
) -> GeneratedSnap:
    """Build a single snap artifact."""
    yaml_path = (root / snap.snapcraft_yaml).resolve()
    if not yaml_path.is_file():
        msg = f"snapcraft-yaml not found: {snap.snapcraft_yaml}"
        raise ConfigurationError(msg)
    pack_dir = resolve_pack_dir(yaml_path, snap.pack_dir, root)
    if not pack_dir.is_dir():
        msg = f"pack-dir not found: {snap.pack_dir}"
        raise ConfigurationError(msg)

    before = _snapshot_outputs(pack_dir, "snap")
    with step(f"Building snap '{snap.name}' (snapcraft pack)"):
        run_command([*_PACK_COMMANDS["snap"]], cwd=str(pack_dir), timeout=build_timeout)
    after = _snapshot_outputs(pack_dir, "snap")
    new_output = _pick_new_output(before, after, "snap", pack_dir, attributed)
    output_file = _relative_to_root(new_output, root)
    attributed.add(new_output)
    return GeneratedSnap(
        name=snap.name,
        **{"snapcraft-yaml": snap.snapcraft_yaml},
        builds=[SnapOutput(arch=current_arch(), file=output_file)],
    )


@dataclass
class _CIContext:
    """GitHub Actions environment variables needed to produce CI-format outputs."""

    run_id: str
    owner: str  # lowercased GITHUB_REPOSITORY_OWNER
    repo: str  # repository name only (not org/repo)
    sha: str  # GITHUB_SHA[:7]


def _get_ci_context() -> _CIContext | None:
    """Return GitHub Actions context if running inside GitHub Actions, else ``None``.

    Reads ``GITHUB_ACTIONS``, ``GITHUB_RUN_ID``, ``GITHUB_REPOSITORY_OWNER``,
    ``GITHUB_REPOSITORY``, and ``GITHUB_SHA`` from the environment.

    Raises:
        ConfigurationError: If ``GITHUB_ACTIONS=true`` but required variables
            are missing or empty.
    """
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return None

    run_id = os.environ.get("GITHUB_RUN_ID", "")
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "").lower()
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    sha = os.environ.get("GITHUB_SHA", "")

    missing = [
        name
        for name, val in [
            ("GITHUB_RUN_ID", run_id),
            ("GITHUB_REPOSITORY_OWNER", owner),
            ("GITHUB_REPOSITORY", repository),
            ("GITHUB_SHA", sha),
        ]
        if not val.strip()
    ]
    if missing:
        msg = f"GITHUB_ACTIONS=true but required variables are missing: {', '.join(missing)}"
        raise ConfigurationError(msg)

    repo = repository.split("/", 1)[-1]
    if not repo.strip():
        msg = f"GITHUB_REPOSITORY must be in 'owner/repo' format, got: {repository!r}"
        raise ConfigurationError(msg)
    return _CIContext(run_id=run_id, owner=owner, repo=repo, sha=sha[:7])


def _get_upload_mode() -> str:
    """Return the rock upload mode from ``OPCLI_ROCK_UPLOAD``.

    Returns:
        ``"registry"`` (default) or ``"artifact"``.

    Raises:
        ConfigurationError: If the env var contains an unrecognised value.
    """
    raw = os.environ.get("OPCLI_ROCK_UPLOAD", "registry").strip().lower()
    valid = ("registry", "artifact")
    if raw not in valid:
        msg = (
            f"OPCLI_ROCK_UPLOAD must be one of {valid!r}, got: {raw!r}. "
            "Set to 'artifact' for fork PRs or 'registry' (default) to push to GHCR."
        )
        raise ConfigurationError(msg)
    return raw


def _push_rock_to_ghcr(rock: GeneratedRock, ci: _CIContext, root: Path) -> GeneratedRock:
    """Push a locally-built rock to GHCR and return an updated ``GeneratedRock``.

    The rock ``.rock`` file is pushed to
    ``ghcr.io/<owner>/<repo>/<name>:<sha7>-<arch>`` using ``skopeo copy``.
    The returned object has its ``builds`` rewritten to a single
    :class:`RockArchBuild` with ``image`` set and no ``file``.

    Raises:
        OpcliError: If the rock output list is empty or has no local file.
        SubprocessError: If the skopeo push fails.
    """
    if not rock.builds:
        msg = f"Rock '{rock.name}' has no build output to push to GHCR."
        raise OpcliError(msg)
    if len(rock.builds) > 1:
        msg = (
            f"Rock '{rock.name}' has {len(rock.builds)} builds — "
            "multi-arch GHCR push is not yet implemented."
        )
        raise OpcliError(msg)
    build = rock.builds[0]
    if not build.file:
        msg = f"Rock '{rock.name}' has no local file to push to GHCR."
        raise OpcliError(msg)

    rock_path = Path(build.file)
    if not rock_path.is_absolute():
        rock_path = (root / rock_path).resolve()
    if not rock_path.exists():
        msg = f"Rock file not found: {rock_path}"
        raise OpcliError(msg)

    image_ref = f"ghcr.io/{ci.owner}/{ci.repo}/{rock.name}:{ci.sha}-{build.arch}"
    with step(f"Pushing rock '{rock.name}' to GHCR"):
        run_command(
            [
                "skopeo",
                "--insecure-policy",
                "copy",
                f"oci-archive:{rock_path}",
                f"docker://{image_ref}",
            ],
            cwd=str(root),
        )
    logger.info("Pushed rock '%s' to %s", rock.name, image_ref)
    return GeneratedRock(
        name=rock.name,
        **{"rockcraft-yaml": rock.rockcraft_yaml},
        builds=[RockOutput(arch=build.arch, image=image_ref)],
    )


def _to_ci_rock_artifact(rock: GeneratedRock, ci: _CIContext) -> GeneratedRock:
    """Return a copy of *rock* with artifact-mode output (no GHCR push).

    Keeps the local ``file`` reference and adds ``artifact`` + ``run-id``
    metadata so the downstream fetch/collect steps know how to retrieve
    the ``.rock`` file from GitHub Artifacts.
    """
    artifact_name = f"built-rock-{rock.name}-{rock.builds[0].arch}" if rock.builds else ""
    new_builds = [
        RockOutput(
            arch=b.arch,
            file=b.file,
            artifact=f"built-rock-{rock.name}-{b.arch}",
            **{"run-id": ci.run_id},
        )
        for b in rock.builds
        if b.file
    ]
    if not new_builds:
        msg = f"Rock '{rock.name}' has no local file for artifact-mode output."
        raise OpcliError(msg)
    logger.info(
        "Rock '%s' in artifact mode — skipping GHCR push (artifact=%s)",
        rock.name,
        artifact_name,
    )
    return GeneratedRock(
        name=rock.name,
        **{"rockcraft-yaml": rock.rockcraft_yaml},
        builds=new_builds,
    )


def _to_ci_charm(charm: GeneratedCharm, ci: _CIContext) -> GeneratedCharm:
    """Return a copy of *charm* with CI artifact-reference output."""
    return GeneratedCharm(
        name=charm.name,
        **{"charmcraft-yaml": charm.charmcraft_yaml},
        builds=[_to_ci_file_output(charm.name, charm.builds, "charm", ci, CharmOutput)],
        resources=charm.resources,
    )


def _to_ci_file_output[T: (CharmOutput, SnapOutput)](
    name: str,
    builds: list[T],
    kind: str,
    ci: _CIContext,
    output_type: type[T],
) -> T:
    """Build a CI artifact-reference output entry for a charm or snap.

    The artifact name includes the architecture so parallel multi-arch builds
    produce distinct artifact names (for example
    ``built-charm-my-charm-amd64``).
    """
    arch = builds[0].arch if builds else current_arch()
    return output_type.model_validate(
        {
            "arch": arch,
            "artifact": f"built-{kind}-{name}-{arch}",
            "run-id": ci.run_id,
        }
    )


def _to_ci_snap(snap: GeneratedSnap, ci: _CIContext) -> GeneratedSnap:
    """Return a copy of *snap* with CI artifact-reference output."""
    return GeneratedSnap(
        name=snap.name,
        **{"snapcraft-yaml": snap.snapcraft_yaml},
        builds=[_to_ci_file_output(snap.name, snap.builds, "snap", ci, SnapOutput)],
    )


def _merge_generated(existing: ArtifactsGenerated, new: ArtifactsGenerated) -> ArtifactsGenerated:
    """Merge *new* build entries into *existing*, replacing by name."""

    def _merge_by_name[T: (GeneratedRock, GeneratedCharm, GeneratedSnap)](
        old_list: list[T], new_list: list[T]
    ) -> list[T]:
        new_names = {item.name for item in new_list}
        # Keep existing entries whose name was NOT rebuilt, then append new.
        merged = [item for item in old_list if item.name not in new_names]
        merged.extend(new_list)
        return merged

    return ArtifactsGenerated(
        rocks=_merge_by_name(existing.rocks, new.rocks),
        charms=_merge_by_name(existing.charms, new.charms),
        snaps=_merge_by_name(existing.snaps, new.snaps),
    )


def _merge_artifact_outputs[T: (GeneratedRock, GeneratedCharm, GeneratedSnap)](
    items: list[T],
    kind: str,
) -> list[T]:
    """Merge artifacts with the same name by combining their builds lists.

    In a multi-arch CI build, each arch produces a separate partial file for
    the same artifact but with a different arch entry in ``builds``.  This
    function groups them by name and concatenates the ``builds`` lists so that
    the collected file holds all arches for each artifact.

    For rocks and snaps, raises :class:`ConfigurationError` if the same
    ``(name, arch)`` pair appears in more than one partial (genuine conflict).
    For charms (flat format), raises if the same ``(arch, path, artifact)``
    tuple appears in more than one partial.
    """
    merged: dict[str, T] = {}
    for item in items:
        if item.name not in merged:
            merged[item.name] = item
        else:
            existing = merged[item.name]
            existing_keys = {_output_key(b) for b in existing.builds}
            for build in item.builds:
                key = _output_key(build)
                if key in existing_keys:
                    msg = (
                        f"Duplicate {kind} '{item.name}' output {key!r} across "
                        "collected partials. Each (artifact, arch) must appear in "
                        "exactly one partial file."
                    )
                    raise ConfigurationError(msg)
            existing.builds.extend(item.builds)
    return list(merged.values())


def _output_key(
    build: RockOutput | CharmOutput | SnapOutput,
) -> tuple[object, ...]:
    """Return a hashable key that uniquely identifies a build output entry.

    For :class:`CharmOutput` (flat format), multiple entries per arch are valid
    (different bases/paths), so the key includes ``path`` and ``artifact``.
    For :class:`RockOutput` and :class:`SnapOutput`, ``arch`` alone is the
    unique key since there is one entry per arch.
    """
    if isinstance(build, CharmOutput):
        return (build.arch, build.path, build.artifact)
    return (build.arch,)


def _localize_snap(
    snap: GeneratedSnap,
    root: Path,
    missing: list[str],
) -> int:
    """Localize CI-only snap entries by resolving local ``.snap`` file paths.

    Returns the number of builds that were localized.
    """
    localized = 0
    for snap_build in snap.builds:
        if snap_build.file or not snap_build.artifact:
            continue
        artifact_dir = _safe_artifact_dir(root, snap_build.artifact)
        if artifact_dir.is_dir():
            rel = _find_snap_file_in_dir(artifact_dir, root, snap_build.arch)
        else:
            rel = _find_local_file(root, snap.name, "snap", snap_build.arch)
        if rel is None:
            missing.append(f"{snap.name} ({snap_build.arch})")
            logger.error(
                "No .snap file found for snap '%s' arch '%s'.",
                snap.name,
                snap_build.arch,
            )
            continue
        snap_build.file = rel
        logger.info("Localised snap '%s' (%s) → %s", snap.name, snap_build.arch, rel)
        localized += 1
    return localized


def _localize_rock(
    rock: GeneratedRock,
    root: Path,
    missing: list[str],
) -> int:
    """Localize artifact-mode rock entries by resolving local ``.rock`` file paths.

    Returns the number of builds that were localized.
    """
    localized = 0
    for rock_build in rock.builds:
        if not rock_build.artifact:
            continue
        # Unlike snaps, we do NOT skip when file is set — artifact-mode rocks
        # retain the build-runner file path which won't exist on the test runner.
        # Localization rewrites it to the actual download location.
        artifact_dir = _safe_artifact_dir(root, rock_build.artifact)
        if artifact_dir.is_dir():
            rel = _find_rock_file_in_dir(artifact_dir, root, rock.name, rock_build.arch)
            if rel is None:
                rel = _find_local_file(root, rock.name, "rock", rock_build.arch)
        else:
            rel = _find_local_file(root, rock.name, "rock", rock_build.arch)
        if rel is None:
            missing.append(f"{rock.name} ({rock_build.arch})")
            logger.error(
                "No .rock file found for rock '%s' arch '%s'.",
                rock.name,
                rock_build.arch,
            )
            continue
        rock_build.file = rel
        logger.info("Localised rock '%s' (%s) → %s", rock.name, rock_build.arch, rel)
        localized += 1
    return localized


def _localize_charm(
    charm: GeneratedCharm,
    root: Path,
    missing: list[str],
) -> int:
    """Localize CI-only charm entries by replacing them with local file entries.

    Returns the number of arch-groups that were localized.
    """
    indices_to_replace: list[int] = []
    new_entries: list[CharmOutput] = []
    localized = 0

    for idx, build in enumerate(charm.builds):
        if build.path or not build.artifact:
            continue
        artifact_dir = _safe_artifact_dir(root, build.artifact)
        if artifact_dir.is_dir():
            charm_files = _find_charm_files_in_dir(artifact_dir, root, build.arch)
        else:
            charm_files = _find_local_charm_files(root, charm.name, build.arch)
        if not charm_files:
            missing.append(f"{charm.name} ({build.arch})")
            logger.error(
                "No .charm file found for charm '%s' arch '%s'.",
                charm.name,
                build.arch,
            )
            continue
        indices_to_replace.append(idx)
        for path, base in charm_files:
            new_entries.append(
                CharmOutput.model_validate(
                    {
                        "arch": build.arch,
                        "path": path,
                        "base": base,
                        "artifact": build.artifact,
                        "run-id": build.run_id,
                    }
                )
            )
        logger.info(
            "Localised charm '%s' (%s) → %d file(s).",
            charm.name,
            build.arch,
            len(charm_files),
        )
        localized += 1

    if indices_to_replace:
        replace_set = set(indices_to_replace)
        charm.builds = [
            b for i, b in enumerate(charm.builds) if i not in replace_set
        ] + new_entries

    return localized


def _safe_artifact_dir(root: Path, name: str) -> Path:
    """Resolve an artifact name to a directory under root, preventing traversal.

    Raises ConfigurationError if the resolved path escapes the root directory.
    """
    artifact_dir = (root / name).resolve()
    root_resolved = root.resolve()
    if not (
        artifact_dir == root_resolved or str(artifact_dir).startswith(str(root_resolved) + os.sep)
    ):
        msg = (
            f"Artifact name {name!r} resolves outside the project root "
            f"({root_resolved}). This may indicate a malicious artifacts.build.yaml."
        )
        raise ConfigurationError(msg)
    return artifact_dir


def _find_charm_files_in_dir(
    search_dir: Path, root: Path, arch: str | None = None
) -> list[tuple[str, str | None]]:
    """Find all ``.charm`` files under *search_dir*.

    Returns (root-relative path, base) pairs.  Unlike
    :func:`_find_local_charm_files`, this function does not filter by charm
    name — it returns every ``.charm`` file found.  This is the correct
    approach when searching an artifact-specific subdirectory
    (e.g. ``root/built-charm-my-charm-amd64/``) because the internal charm
    name baked into the filename may differ from the opcli artifact name.

    When *arch* is given, only files whose parsed arch matches (or whose arch
    cannot be determined) are returned.  Paths are returned relative to *root*.
    """
    pattern = str(search_dir / "**" / "*.charm")
    matches = sorted(globmod.glob(pattern, recursive=True))
    if arch is not None:
        matches = [m for m in matches if _parse_arch_from_charm_path(m) in (arch, None)]
    return [
        (
            "./" + str(Path(m).relative_to(root)),
            _parse_base_from_charm_path(m),
        )
        for m in matches
    ]


def _parse_arch_from_charm_path(path: str) -> str | None:
    """Return the arch (e.g. ``amd64``) parsed from a charm filename.

    Returns ``None`` if the filename does not follow the expected convention.
    """
    filename = Path(path).name
    m = _CHARM_FILENAME_RE.match(filename)
    return m.group("arch") if m else None


def _find_local_charm_files(
    root: Path, name: str, arch: str | None = None
) -> list[tuple[str, str | None]]:
    """Find all ``name_*.charm`` files under *root* and return (path, base) pairs.

    When *arch* is given, only files whose parsed arch matches (or whose arch
    cannot be determined) are returned.  Returns an empty list when no files
    are found.  Multiple matches are expected for multi-base charms — each
    file gets its base parsed from the filename.
    """
    pattern = str(root / "**" / f"{name}_*.charm")
    matches = sorted(globmod.glob(pattern, recursive=True))
    if arch is not None:
        matches = [m for m in matches if _parse_arch_from_charm_path(m) in (arch, None)]
    return [
        (
            "./" + str(Path(m).relative_to(root)),
            _parse_base_from_charm_path(m),
        )
        for m in matches
    ]


def _find_snap_file_in_dir(search_dir: Path, root: Path, arch: str | None = None) -> str | None:
    """Find a single ``.snap`` file under *search_dir*.

    Like :func:`_find_charm_files_in_dir`, this searches by extension only —
    not by snap name — so it works correctly when the artifact directory name
    differs from the internal snap name.  Returns a path relative to *root*.
    """
    pattern = str(search_dir / "**" / "*.snap")
    matches = sorted(globmod.glob(pattern, recursive=True))
    if arch is not None:
        matches = [m for m in matches if _parse_arch_from_snap_path(m) in (arch, None)]
    if not matches:
        return None
    if len(matches) > 1:
        logger.warning(
            "Multiple .snap files found in '%s'; using %s.",
            search_dir,
            matches[0],
        )
    return "./" + str(Path(matches[0]).relative_to(root))


def _find_rock_file_in_dir(
    search_dir: Path, root: Path, name: str, arch: str | None = None
) -> str | None:
    """Find a ``.rock`` file under *search_dir*, returning a path relative to *root*.

    Searches for ``{name}_*.rock`` files.  Returns the first match or ``None``.
    """
    pattern = str(search_dir / "**" / f"{name}_*.rock")
    matches = sorted(globmod.glob(pattern, recursive=True))
    if not matches:
        # Also try without name prefix (in case the artifact contains a differently-named file)
        pattern = str(search_dir / "**" / "*.rock")
        matches = sorted(globmod.glob(pattern, recursive=True))
    if arch is not None:
        matches = [m for m in matches if _parse_arch_from_rock_path(m) in (arch, None)]
    if not matches:
        return None
    if len(matches) > 1:
        logger.warning(
            "Multiple .rock files found in '%s'; using %s.",
            search_dir,
            matches[0],
        )
    return "./" + str(Path(matches[0]).relative_to(root))


def _parse_arch_from_rock_path(path: str) -> str | None:
    """Return the arch (e.g. ``amd64``) parsed from a rock filename.

    Returns ``None`` if the filename does not follow the expected convention.
    """
    filename = Path(path).name
    m = _ROCK_FILENAME_RE.match(filename)
    return m.group("arch") if m else None


def _parse_arch_from_snap_path(path: str) -> str | None:
    """Return the arch (e.g. ``amd64``) parsed from a snap filename.

    Returns ``None`` if the filename does not follow the expected convention.
    """
    filename = Path(path).name
    m = _SNAP_FILENAME_RE.match(filename)
    return m.group("arch") if m else None


def _find_local_file(root: Path, name: str, extension: str, arch: str | None = None) -> str | None:
    """Find a single ``name_*.{extension}`` file under *root*.

    When *arch* is given and the filename follows the expected naming
    convention, only files whose parsed arch matches are considered.

    Returns the relative path (``./...``) on success, or ``None`` if no file
    is found.  Logs a warning when multiple matches exist and picks the first.
    """
    pattern = str(root / "**" / f"{name}_*.{extension}")
    matches = sorted(globmod.glob(pattern, recursive=True))
    if arch is not None and extension == "snap":
        matches = [m for m in matches if _parse_arch_from_snap_path(m) in (arch, None)]
    if not matches:
        return None
    if len(matches) > 1:
        logger.warning(
            "Multiple .%s files found for '%s'; using %s.",
            extension,
            name,
            matches[0],
        )
    return "./" + str(Path(matches[0]).relative_to(root))


# ---------------------------------------------------------------------------
# artifacts fetch
# ---------------------------------------------------------------------------


def _infer_repo_from_git(root: Path) -> str:
    """Return ``owner/repo`` inferred from the git remote of *root*.

    Raises:
        ConfigurationError: If the git remote URL cannot be parsed.
    """
    try:
        result = run_command(["git", "remote", "get-url", "origin"], cwd=str(root))
    except Exception as exc:
        msg = "Could not read git remote 'origin'. Use --repo to specify the repository."
        raise ConfigurationError(msg) from exc

    url = result.stdout.strip()
    m = _GITHUB_URL_RE.search(url)
    if not m:
        msg = (
            f"Could not parse a GitHub 'owner/repo' from remote URL {url!r}. "
            "Use --repo to specify the repository."
        )
        raise ConfigurationError(msg)
    return m.group(1)


def _gh_download_with_wait(  # noqa: PLR0913
    cmd: list[str],
    cwd: str,
    run_id: str | None = None,
    repo: str | None = None,
    dest: Path | None = None,
    wait_timeout: int = _DEFAULT_WAIT_TIMEOUT_SECONDS,
) -> None:
    """Run ``gh run download``, retrying until the artifact appears.

    Retries every :data:`_WAIT_SLEEP_SECONDS` seconds until *wait_timeout*
    seconds have elapsed.  Fails immediately if the error looks like an
    authentication/permission problem.  When *run_id* and *repo* are provided,
    bails early if the CI run itself has a terminal conclusion (failure,
    cancelled, or success-with-missing-artifact).

    Args:
        cmd: Full ``gh run download ...`` command list.
        cwd: Working directory for the subprocess.
        run_id: GitHub Actions run ID (used for early-bail run conclusion check).
        repo: GitHub repository in ``owner/name`` format.
        dest: Path of the expected output file.  When provided and ``gh``
            reports "file exists", the file is deleted and the download is
            retried once before giving up.
        wait_timeout: Maximum seconds to keep retrying. Defaults to
            :data:`_DEFAULT_WAIT_TIMEOUT_SECONDS`.

    Raises:
        ConfigurationError: On auth/permission errors or timeout.
        SubprocessError: Propagated for non-retryable failures.
    """
    max_attempts = max(1, wait_timeout // _WAIT_SLEEP_SECONDS)
    last_exc: SubprocessError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            _run_gh_download(cmd, cwd, dest)
            return
        except SubprocessError as exc:
            stderr_lower = exc.stderr.lower()
            if any(kw in stderr_lower for kw in _AUTH_ERROR_KEYWORDS):
                msg = (
                    f"Authentication/permission error downloading artifact. "
                    f"Check GH_TOKEN and repository permissions.\n{exc.stderr.strip()}"
                )
                raise ConfigurationError(msg) from exc
            last_exc = exc
            logger.info(
                "Artifact not yet available (attempt %d/%d): %s — retrying in %ds...",
                attempt,
                max_attempts,
                exc.stderr.strip(),
                _WAIT_SLEEP_SECONDS,
            )

        if run_id and repo:
            conclusion = _check_run_conclusion(run_id, repo)
            if conclusion in _BAIL_CONCLUSIONS:
                msg = (
                    f"CI run {run_id!r} {conclusion} before the artifact was uploaded. "
                    "Check the build job logs."
                )
                raise ConfigurationError(msg)
            if conclusion == "success":
                msg = (
                    f"CI run {run_id!r} completed successfully but the artifact was not found. "
                    "Some matrix jobs may have been skipped."
                )
                raise ConfigurationError(msg)

        if attempt < max_attempts:
            time.sleep(_WAIT_SLEEP_SECONDS)

    last_msg = last_exc.stderr.strip() if last_exc else ""
    msg = f"Timed out waiting for artifact after {wait_timeout}s. Last error: {last_msg}"
    raise ConfigurationError(msg)


def _gh_download_all_partials_with_wait(  # noqa: PLR0913, C901
    cmd: list[str],
    cwd: str,
    run_id: str,
    repo: str,
    partial_dir: Path,
    expected_names: list[str],
    wait_timeout: int = _DEFAULT_WAIT_TIMEOUT_SECONDS,
) -> None:
    """Run ``gh run download --pattern artifacts-build-*``, retrying until all expected partials appear.

    Downloads all ``artifacts-build-*`` partial manifests.  Retries when not
    all expected partials are present yet (some builds still running).  Bails
    early when the overall CI run itself has a terminal failure conclusion.

    Args:
        cmd: Full ``gh run download --pattern artifacts-build-* ...`` command.
        cwd: Working directory for the subprocess.
        run_id: GitHub Actions run ID (used to check run conclusion).
        repo: GitHub repository in ``owner/name`` format.
        partial_dir: Directory where partials are downloaded (``--dir`` value).
        expected_names: Artifact names expected from ``artifacts.yaml``.
        wait_timeout: Maximum seconds to keep retrying. Defaults to
            :data:`_DEFAULT_WAIT_TIMEOUT_SECONDS`.
    """
    # Use a real wall-clock deadline so that slow API calls (run_command,
    # _check_run_conclusion) don't silently extend the effective wait beyond
    # wait_timeout.  max_attempts caps the loop as a safety valve.
    deadline = time.monotonic() + wait_timeout
    max_attempts = max(1, wait_timeout // _WAIT_SLEEP_SECONDS)
    last_exc: SubprocessError | None = None
    for attempt in range(1, max_attempts + 1):
        # Short-circuit once the wall-clock budget is spent (only after at
        # least one attempt so wait_timeout=0 still makes one try).
        if attempt > 1 and time.monotonic() >= deadline:
            break
        # Clear stale partials before each attempt so "file exists" never
        # blocks progress and stale files from prior runs cannot corrupt merges.
        if partial_dir.exists():
            shutil.rmtree(partial_dir)
        partial_dir.mkdir(parents=True, exist_ok=True)

        download_succeeded = False
        try:
            run_command(cmd, cwd=cwd)
            download_succeeded = True
        except SubprocessError as exc:
            stderr_lower = exc.stderr.lower()
            if any(kw in stderr_lower for kw in _AUTH_ERROR_KEYWORDS):
                msg = (
                    "Authentication/permission error downloading artifacts. "
                    f"Check GH_TOKEN and repository permissions.\n{exc.stderr.strip()}"
                )
                raise ConfigurationError(msg) from exc
            last_exc = exc
        else:
            # Normalize in case gh used flat extraction (single matching artifact).
            _normalize_partial_dir_layout(partial_dir)
            # Download succeeded; check if all expected partial files are present.
            missing = _missing_partial_names(partial_dir, expected_names)
            if not missing:
                return
            logger.info(
                "Waiting for %d partial manifest(s) (attempt %d/%d): %s",
                len(missing),
                attempt,
                max_attempts,
                ", ".join(missing[:5]),
            )

        # Check the run's overall conclusion to bail early on permanent failures.
        # NOTE: in the integration-test spread path, run_id is GITHUB_RUN_ID of
        # the *current* workflow run, which contains both build and test jobs.
        # The overall conclusion stays null while the test job itself is running,
        # so the bail branches below are effectively inactive there.  They work
        # correctly in the publish workflow path (where run_id is a completed
        # build-only run) and for post-run diagnostics.  This is a known
        # limitation; the _check_run_conclusion approach is still useful for the
        # publish path and does no harm in the test path.
        conclusion = _check_run_conclusion(run_id, repo)
        if conclusion in _BAIL_CONCLUSIONS:
            msg = (
                f"CI run {run_id!r} {conclusion} before all build artifacts were uploaded. "
                "Check the build job logs."
            )
            raise ConfigurationError(msg)
        # Only diagnose "jobs were skipped" when the download itself succeeded on
        # this attempt; if the download failed transiently we cannot distinguish
        # "artifact never uploaded" from "artifact exists but download failed",
        # so we retry rather than raising a misleading error.
        if conclusion == "success" and download_succeeded:
            missing_now = _missing_partial_names(partial_dir, expected_names)
            if missing_now:
                msg = (
                    f"CI run {run_id!r} completed successfully but "
                    f"{len(missing_now)} partial manifest(s) were never uploaded: "
                    f"{', '.join(missing_now[:5])}. "
                    "Some matrix jobs may have been skipped."
                )
                raise ConfigurationError(msg)
            return

        logger.info(
            "Retrying in %ds (attempt %d/%d)...",
            _WAIT_SLEEP_SECONDS,
            attempt,
            max_attempts,
        )
        remaining = deadline - time.monotonic()
        if attempt < max_attempts and remaining > 0:
            time.sleep(min(_WAIT_SLEEP_SECONDS, remaining))

    last_msg = last_exc.stderr.strip() if last_exc else "Some partial manifests still missing."
    msg = (
        f"Timed out waiting for all partial build manifests from run "
        f"{run_id!r} after {wait_timeout}s. "
        f"Last error: {last_msg}"
    )
    raise ConfigurationError(msg)


def _missing_partial_names(partial_dir: Path, expected_names: list[str]) -> list[str]:
    """Return expected partial manifest names whose files have not been downloaded yet."""
    return [
        name for name in expected_names if not (partial_dir / name / ARTIFACTS_BUILD_YAML).exists()
    ]


def _partial_manifest_artifact_name(generated: ArtifactsGenerated) -> str | None:
    """Infer the partial manifest artifact name from an :class:`ArtifactsGenerated` object.

    A partial manifest produced by a single CI build job contains builds for
    exactly one artifact (one type, one name, one arch).  Returns the
    expected artifact name (``artifacts-build-{type}-{name}-{arch}``) for the
    first non-empty build entry found, or ``None`` if the manifest is empty.
    """
    for rock in generated.rocks:
        for rock_build in rock.builds:
            return f"artifacts-build-rock-{rock.name}-{rock_build.arch}"
    for charm in generated.charms:
        for charm_build in charm.builds:
            return f"artifacts-build-charm-{charm.name}-{charm_build.arch}"
    for snap in generated.snaps:
        for snap_build in snap.builds:
            return f"artifacts-build-snap-{snap.name}-{snap_build.arch}"
    return None


def _normalize_partial_dir_layout(partial_dir: Path) -> None:
    """Move a flat-extracted partial manifest into the expected per-name subdir.

    ``gh run download --pattern`` extracts artifacts into
    ``<dir>/<artifact-name>/`` subdirectories **unless exactly one artifact
    matches**, in which case it extracts flat into ``<dir>/`` directly.
    This function detects the flat layout and moves the file into the correct
    ``partial_dir/<name>/artifacts.build.yaml`` location so that
    :func:`_missing_partial_names` can find it.

    If the file cannot be parsed or the artifact name cannot be inferred, the
    function logs a warning and leaves the file in place; the subsequent
    :func:`_missing_partial_names` check will report it missing and the caller
    will retry.
    """
    flat_yaml = partial_dir / ARTIFACTS_BUILD_YAML
    if not flat_yaml.exists():
        return
    try:
        generated = load_artifacts_build(flat_yaml)
        name = _partial_manifest_artifact_name(generated)
    except Exception as exc:
        logger.warning(
            "Could not parse flat-extracted partial manifest %s (%s); will retry on next attempt.",
            flat_yaml,
            exc,
        )
        return
    if name is None:
        logger.warning(
            "Flat-extracted partial manifest %s contains no builds; ignoring.",
            flat_yaml,
        )
        return
    dest_dir = partial_dir / name
    dest_dir.mkdir(parents=True, exist_ok=True)
    flat_yaml.rename(dest_dir / ARTIFACTS_BUILD_YAML)
    logger.debug(
        "Normalized flat-extracted partial %r into %s/",
        name,
        dest_dir,
    )


def _run_gh_download(cmd: list[str], cwd: str, dest: Path | None = None) -> None:
    """Run ``gh run download`` once, deleting an existing output file if needed."""
    try:
        run_command(cmd, cwd=cwd)
    except SubprocessError as exc:
        if dest is None or not any(kw in exc.stderr.lower() for kw in _FILE_EXISTS_KEYWORDS):
            raise
        dest.unlink(missing_ok=True)
        run_command(cmd, cwd=cwd)


def _check_run_conclusion(run_id: str, repo: str) -> str | None:
    """Return the overall conclusion of a GitHub Actions run, or ``None``.

    Returns the conclusion string (e.g. ``"success"``, ``"failure"``,
    ``"cancelled"``) when the run has finished, or ``None`` when the run is
    still in progress or the API call fails.  Callers should treat ``None``
    as "don't know yet — keep retrying".
    """
    try:
        result = run_command(
            ["gh", "run", "view", run_id, "--repo", repo, "--json", "conclusion"],
            stream=False,
        )
    except Exception:
        return None

    try:
        data: object = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None
    conclusion = data.get("conclusion")
    if not isinstance(conclusion, str):
        return None
    return conclusion or None


def _gh_download(cmd: list[str], cwd: str, dest: Path | None = None) -> None:
    """Run ``gh run download``, raising :class:`SubprocessError` on failure."""
    _run_gh_download(cmd, cwd, dest)
