# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""YAML read/write helpers backed by ruamel.yaml.

These thin wrappers centralise YAML I/O so the rest of the codebase never
imports ruamel.yaml directly.
"""

from io import StringIO
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import LiteralScalarString

from opcli.core.exceptions import ValidationError
from opcli.models.artifacts import ArtifactsPlan
from opcli.models.artifacts_build import ArtifactsGenerated

_yaml = YAML()
_yaml.default_flow_style = False


# ---------------------------------------------------------------------------
#  Generic helpers
# ---------------------------------------------------------------------------


def load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file and return its contents as a plain dict."""
    with path.open() as fh:
        data = _yaml.load(fh)
    if not isinstance(data, dict):
        msg = f"{path} does not contain a YAML mapping"
        raise ValidationError(msg)
    return dict(data)


def load_yaml_optional(path: Path) -> dict[str, Any] | None:
    """Load a YAML file, returning ``None`` if the content is not a mapping."""
    with path.open() as fh:
        data = _yaml.load(fh)
    if not isinstance(data, dict):
        return None
    return dict(data)


def loads_yaml(text: str) -> dict[str, Any]:
    """Parse a YAML string and return its contents as a dict."""
    data = _yaml.load(StringIO(text))
    if not isinstance(data, dict):
        msg = "YAML text does not contain a mapping"
        raise ValidationError(msg)
    return dict(data)


def dump_yaml(data: dict[str, Any], path: Path) -> None:
    """Write *data* to a YAML file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        _yaml.dump(data, fh)


def dumps_yaml(data: dict[str, Any]) -> str:
    """Serialize *data* to a YAML string."""
    buf = StringIO()
    _yaml.dump(data, buf)
    return buf.getvalue()


def literal_str(text: str) -> Any:
    """Wrap *text* so it serialises as a YAML literal block scalar (``|``).

    Use this for multi-line strings (e.g. shell scripts) that should be
    emitted with the ``|`` indicator rather than quoted or folded.
    """
    return LiteralScalarString(text)


def literalize(obj: Any) -> Any:
    """Recursively convert multiline strings to literal block scalars.

    This ensures shell scripts and other multi-line values are serialised
    with the ``|`` style.
    """
    if isinstance(obj, str) and "\n" in obj:
        return LiteralScalarString(obj)
    if isinstance(obj, dict):
        return {k: literalize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [literalize(item) for item in obj]
    return obj


def load_artifacts_plan(path: Path) -> ArtifactsPlan:
    """Load and validate ``artifacts.yaml``."""
    raw = load_yaml(path)
    try:
        return ArtifactsPlan.model_validate(raw)
    except PydanticValidationError as exc:
        msg = f"{path}: {exc}"
        raise ValidationError(msg) from exc


def dump_artifacts_plan(plan: ArtifactsPlan, path: Path) -> None:
    """Serialize an :class:`ArtifactsPlan` to YAML."""
    dump_yaml(plan.model_dump(exclude_none=True, by_alias=True), path)


def load_artifacts_build(path: Path) -> ArtifactsGenerated:
    """Load and validate ``artifacts.build.yaml``."""
    raw = load_yaml(path)
    try:
        return ArtifactsGenerated.model_validate(raw)
    except PydanticValidationError as exc:
        msg = f"{path}: {exc}"
        raise ValidationError(msg) from exc


def dump_artifacts_build(gen: ArtifactsGenerated, path: Path) -> None:
    """Serialize an :class:`ArtifactsGenerated` to YAML."""
    dump_yaml(gen.model_dump(exclude_none=True, by_alias=True), path)
