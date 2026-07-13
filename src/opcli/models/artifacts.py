# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Pydantic models for artifacts.yaml.

This schema declares the charms, rocks, and snaps in a repository, and
the links between charms and their OCI-image resources (rocks).

Schema version: 1
- Each artifact carries an explicit path to its craft YAML file
  (``rockcraft-yaml``, ``charmcraft-yaml``, ``snapcraft-yaml``) rather than
  a source directory.
- An optional ``pack-dir`` field controls the working directory for the build
  tool (e.g. run ``rockcraft pack`` from the repo root when ``go.mod`` lives
  there but ``rockcraft.yaml`` is in a subdirectory).
- An optional ``platforms`` list declares the target architectures and GitHub
  runner labels for each artifact.  Defaults to a single amd64 platform.
"""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Arch values must be hyphen-free tokens.  All standard Linux architectures
# (amd64, arm64, s390x, ppc64el, riscv64, armhf) satisfy this constraint.
# Hyphens in arch strings would break _artifact_name_to_build_job_name in
# core/artifacts.py which parses "artifacts-build-{type}-{name}-{arch}" by
# splitting on "-" and taking the last token as arch.
_ARCH_RE = re.compile(r"^[a-z0-9]+$")


class ArtifactResource(BaseModel):
    """A resource declared by a charm (e.g. an OCI image backed by a rock)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["oci-image"]
    rock: str | None = None


class BuildTarget(BaseModel):
    """A single build target: an architecture and optional runner label(s).

    ``arch`` must be a hyphen-free alphanumeric token (e.g. ``amd64``,
    ``arm64``, ``s390x``).  Hyphens are reserved as delimiters in the
    ``artifacts-build-{type}-{name}-{arch}`` artifact naming scheme and
    would break the artifact↔job-name conversion in
    :func:`~opcli.core.artifacts._artifact_name_to_build_job_name`.
    """

    model_config = ConfigDict(extra="forbid")

    arch: str
    runner: list[str] | None = None

    @field_validator("arch")
    @classmethod
    def _arch_no_hyphens(cls, v: str) -> str:
        if not _ARCH_RE.match(v):
            msg = (
                f"arch {v!r} is invalid: must be a hyphen-free alphanumeric token "
                "(e.g. 'amd64', 'arm64', 's390x')."
            )
            raise ValueError(msg)
        return v


def _default_platforms() -> list[BuildTarget]:
    return [BuildTarget(arch="amd64")]


class CharmArtifact(BaseModel):
    """A charm declared in artifacts.yaml."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    charmcraft_yaml: str = Field(alias="charmcraft-yaml")
    pack_dir: str | None = Field(default=None, alias="pack-dir")
    resources: dict[str, ArtifactResource] = {}
    platforms: list[BuildTarget] = Field(default_factory=_default_platforms, alias="platforms")
    channel: str | None = None


class RockArtifact(BaseModel):
    """A rock declared in artifacts.yaml."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    rockcraft_yaml: str = Field(alias="rockcraft-yaml")
    pack_dir: str | None = Field(default=None, alias="pack-dir")
    platforms: list[BuildTarget] = Field(default_factory=_default_platforms, alias="platforms")


class SnapArtifact(BaseModel):
    """A snap declared in artifacts.yaml."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    snapcraft_yaml: str = Field(alias="snapcraft-yaml")
    pack_dir: str | None = Field(default=None, alias="pack-dir")
    platforms: list[BuildTarget] = Field(default_factory=_default_platforms, alias="platforms")


class ArtifactsPlan(BaseModel):
    """Top-level schema for ``artifacts.yaml`` (schema version 1)."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    rocks: list[RockArtifact] = []
    charms: list[CharmArtifact] = []
    snaps: list[SnapArtifact] = []

    @model_validator(mode="after")
    def _unique_names(self) -> "ArtifactsPlan":
        """Ensure no duplicate names within each artifact kind."""
        checks: list[tuple[str, list[str]]] = [
            ("rock", [r.name for r in self.rocks]),
            ("charm", [c.name for c in self.charms]),
            ("snap", [s.name for s in self.snaps]),
        ]
        for kind, names in checks:
            dupes = {n for n in names if names.count(n) > 1}
            if dupes:
                msg = f"Duplicate {kind} name(s): {', '.join(sorted(dupes))}"
                raise ValueError(msg)
        return self
