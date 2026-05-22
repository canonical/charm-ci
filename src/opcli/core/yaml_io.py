# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""YAML read/write helpers backed by ruamel.yaml.

These thin wrappers centralise YAML I/O so the rest of the codebase never
imports ruamel.yaml directly.
"""

from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from opcli.models.artifacts import ArtifactsPlan
from opcli.models.artifacts_build import ArtifactsGenerated

_yaml = YAML()
_yaml.default_flow_style = False


def load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file and return its contents as a plain dict."""
    with path.open() as fh:
        data = _yaml.load(fh)
    if not isinstance(data, dict):
        msg = f"{path} does not contain a YAML mapping"
        raise ValueError(msg)
    return dict(data)


def dump_yaml(data: dict[str, Any], path: Path) -> None:
    """Write *data* to a YAML file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        _yaml.dump(data, fh)


def load_artifacts_plan(path: Path) -> ArtifactsPlan:
    """Load and validate ``artifacts.yaml``."""
    raw = load_yaml(path)
    return ArtifactsPlan.model_validate(raw)


def dump_artifacts_plan(plan: ArtifactsPlan, path: Path) -> None:
    """Serialize an :class:`ArtifactsPlan` to YAML."""
    dump_yaml(plan.model_dump(exclude_none=True, by_alias=True), path)


def load_artifacts_build(path: Path) -> ArtifactsGenerated:
    """Load and validate ``artifacts.build.yaml``."""
    raw = load_yaml(path)
    return ArtifactsGenerated.model_validate(raw)


def dump_artifacts_build(gen: ArtifactsGenerated, path: Path) -> None:
    """Serialize an :class:`ArtifactsGenerated` to YAML."""
    dump_yaml(gen.model_dump(exclude_none=True, by_alias=True), path)
