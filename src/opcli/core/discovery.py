# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Artifact discovery — walk a repository to find charms, rocks, and snaps.

Discovery looks for ``charmcraft.yaml``, ``rockcraft.yaml``, and
``snapcraft.yaml`` marker files, extracts names, and links charm OCI-image
resources to discovered rocks when the match is unambiguous.
"""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from opcli.core.exceptions import DiscoveryError
from opcli.models.artifacts import (
    ArtifactResource,
    ArtifactsPlan,
    CharmArtifact,
    RockArtifact,
    SnapArtifact,
)

logger = logging.getLogger(__name__)

_yaml = YAML()

_PRUNE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        ".tox",
        "build",
        "dist",
        "tests",
        "__pycache__",
        "node_modules",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
    }
)

_MARKER_FILES: dict[str, str] = {
    "charmcraft.yaml": "charm",
    "rockcraft.yaml": "rock",
    "snapcraft.yaml": "snap",
}


def _read_yaml_name(path: Path) -> str:
    """Read the ``name`` field from a craft YAML file.

    For charmcraft.yaml, falls back to ``metadata.yaml`` in the same
    directory when ``name`` is absent (legacy split format where build
    config lives in ``charmcraft.yaml`` and charm identity lives in
    ``metadata.yaml``).
    """
    name = _read_yaml_field_with_metadata_fallback(
        path,
        "name",
        should_fallback=_missing_name,
        require_mapping=True,
    )
    if not isinstance(name, str) or not name:
        msg = f"{path} is missing a valid 'name' field"
        raise DiscoveryError(msg)
    return name


def _read_charm_resources(path: Path) -> dict[str, dict[str, Any]]:
    """Read the ``resources`` section from a charmcraft.yaml file.

    Falls back to ``metadata.yaml`` in the same directory when the
    ``resources`` section is absent from ``charmcraft.yaml`` (legacy
    split format).
    """
    resources = _read_yaml_field_with_metadata_fallback(
        path,
        "resources",
        should_fallback=_missing_mapping,
    )
    if not isinstance(resources, dict):
        return {}

    normalized: dict[str, dict[str, Any]] = {}
    for resource_name, resource_data in resources.items():
        if isinstance(resource_name, str) and isinstance(resource_data, dict):
            normalized[resource_name] = dict(resource_data)
    return normalized


def _yaml_relative(marker_path: Path, root: Path) -> str:
    """Return the marker file path relative to *root*."""
    return str(marker_path.relative_to(root))


def _read_yaml_field_with_metadata_fallback(
    path: Path,
    field: str,
    *,
    should_fallback: Callable[[object | None], bool],
    require_mapping: bool = False,
) -> object | None:
    """Read a YAML field, falling back to metadata.yaml for charmcraft files."""
    data = _load_yaml_mapping(path, require_mapping=require_mapping)
    if data is None:
        return None

    value = data.get(field)
    if not should_fallback(value):
        return value

    metadata = _load_metadata_mapping(path)
    if metadata is None:
        return value

    metadata_value = metadata.get(field)
    return metadata_value if not should_fallback(metadata_value) else value


def _load_metadata_mapping(path: Path) -> dict[str, object] | None:
    """Load metadata.yaml for charmcraft files when present and well-formed."""
    if path.name != "charmcraft.yaml":
        return None

    metadata_path = path.parent / "metadata.yaml"
    if not metadata_path.exists():
        return None
    return _load_yaml_mapping(metadata_path, require_mapping=False)


def _load_yaml_mapping(path: Path, *, require_mapping: bool) -> dict[str, object] | None:
    """Load a YAML mapping from *path*."""
    with path.open() as fh:
        data = _yaml.load(fh)

    if not isinstance(data, dict):
        if require_mapping:
            msg = f"{path} does not contain a YAML mapping"
            raise DiscoveryError(msg)
        return None
    return dict(data)


def _missing_name(value: object | None) -> bool:
    """Return whether *value* is missing a valid non-empty name."""
    return not isinstance(value, str) or not value


def _missing_mapping(value: object | None) -> bool:
    """Return whether *value* is missing a mapping."""
    return not isinstance(value, dict)


def _snap_fields(marker_path: Path, root: Path) -> tuple[str, str | None]:
    """Return ``(snapcraft_yaml, pack_dir)`` for a snapcraft.yaml marker.

    When the marker lives at ``*/snap/snapcraft.yaml`` the build tool should
    run from the parent of ``snap/``, so ``pack_dir`` is set to that parent
    directory.  Otherwise ``pack_dir`` is ``None`` (defaults to the yaml dir).
    """
    yaml_rel = str(marker_path.relative_to(root))
    if marker_path.parent.name == "snap":
        parent = marker_path.parent.parent
        pack_dir_rel = str(parent.relative_to(root)) if parent != root else "."
        return yaml_rel, pack_dir_rel
    return yaml_rel, None


def _process_marker(
    path: Path,
    root: Path,
    rocks: list[RockArtifact],
    snaps: list[SnapArtifact],
    charm_raw: list[tuple[str, str, dict[str, dict[str, Any]]]],
) -> None:
    """Process a single marker file found during discovery."""
    kind = _MARKER_FILES[path.name]

    if kind == "rock":
        name = _read_yaml_name(path)
        rocks.append(
            RockArtifact.model_validate(
                {"rockcraft-yaml": _yaml_relative(path, root), "name": name}
            )
        )
    elif kind == "snap":
        name = _read_yaml_name(path)
        snap_yaml, pack_dir = _snap_fields(path, root)
        snap = SnapArtifact.model_validate(
            {"snapcraft-yaml": snap_yaml, "name": name, "pack-dir": pack_dir}
        )
        snaps.append(snap)
    elif kind == "charm":
        name = _read_yaml_name(path)
        raw_resources = _read_charm_resources(path)
        charm_raw.append((name, _yaml_relative(path, root), raw_resources))


def _link_charm_resources(
    charm_name: str,
    raw_resources: dict[str, dict[str, Any]],
    rock_names: set[str],
) -> dict[str, ArtifactResource]:
    """Build charm resources, auto-linking OCI images to rocks when unambiguous."""
    resources: dict[str, ArtifactResource] = {}
    for res_name, res_data in raw_resources.items():
        if not isinstance(res_data, dict):
            continue
        if res_data.get("type") != "oci-image":
            continue
        upstream = res_data.get("upstream-source", "")
        candidates = []
        if isinstance(upstream, str) and upstream in rock_names:
            candidates.append(upstream)
        if res_name in rock_names and res_name not in candidates:
            candidates.append(res_name)
        rock_ref = candidates[0] if len(candidates) == 1 else None
        if len(candidates) > 1:
            logger.warning(
                "Ambiguous rock match for resource '%s' in charm '%s'; "
                "skipping auto-link (candidates: %s)",
                res_name,
                charm_name,
                ", ".join(candidates),
            )
        resources[res_name] = ArtifactResource(type="oci-image", rock=rock_ref)
    return resources


def discover_artifacts(root: Path) -> ArtifactsPlan:
    """Walk *root* and discover charms, rocks, and snaps.

    The walk prunes common non-source directories and does not follow
    symlinked directories.  Charm resources of ``type: oci-image`` are
    linked to discovered rocks when the match is unambiguous.

    Raises:
        DiscoveryError: If a marker file cannot be read or is malformed.
    """
    root = root.resolve()
    rocks: list[RockArtifact] = []
    charms: list[CharmArtifact] = []
    snaps: list[SnapArtifact] = []

    charm_raw: list[tuple[str, str, dict[str, dict[str, Any]]]] = []

    for path in sorted(root.rglob("*")):
        if path.is_dir() and path.is_symlink():
            continue
        if path.is_dir() and path.name in _PRUNE_DIRS:
            continue
        if not path.is_file() or path.name not in _MARKER_FILES:
            continue
        if any(part in _PRUNE_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        _process_marker(path, root, rocks, snaps, charm_raw)

    rock_names = {r.name for r in rocks}
    for charm_name, charm_yaml, raw_resources in charm_raw:
        resources = _link_charm_resources(charm_name, raw_resources, rock_names)
        charms.append(
            CharmArtifact.model_validate(
                {
                    "charmcraft-yaml": charm_yaml,
                    "name": charm_name,
                    "resources": resources,
                }
            )
        )

    return ArtifactsPlan(rocks=rocks, charms=charms, snaps=snaps)
