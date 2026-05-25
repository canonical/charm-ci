# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Pydantic models for artifacts.build.yaml.

Extends the build plan with paths/references of the built artifacts.

Schema version: 1
- Each artifact carries an explicit path to its craft YAML file
  (``rockcraft-yaml``, ``charmcraft-yaml``, ``snapcraft-yaml``).
- Charm entries include a ``resources`` mapping with resolved output paths
  (image reference), making ``artifacts.build.yaml`` self-contained for
  pytest flag assembly without needing to also read ``artifacts.yaml``.
- ``builds`` is always a **flat list of per-file build entries** (:class:`RockOutput`,
  :class:`CharmOutput`, or :class:`SnapOutput`).
  Rocks and snaps have one entry per built arch.
  Charms have one entry per produced ``.charm`` file (one per base per arch for local
  builds, one per arch for CI builds).
  Local builds produce ``file`` / ``path`` entries; CI builds produce ``image``
  (rocks) or ``artifact`` + ``run-id`` (charms / snaps) entries.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RockOutput(BaseModel):
    """Rock build output entry.

    Local builds populate ``file``; CI builds populate ``image``.
    Fork-PR builds in CI populate ``file`` + ``artifact`` + ``run-id``
    (the rock is uploaded as a GitHub artifact instead of pushed to GHCR).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    arch: str
    file: str | None = None
    image: str | None = None
    artifact: str | None = None
    run_id: str | None = Field(default=None, alias="run-id")

    @model_validator(mode="after")
    def _at_least_one_output(self) -> "RockOutput":
        if not self.file and not self.image:
            msg = "RockOutput must specify file or image"
            raise ValueError(msg)
        if self.artifact and not self.run_id:
            msg = "run-id is required when artifact is set"
            raise ValueError(msg)
        return self


class CharmOutput(BaseModel):
    """Charm build output entry.

    Local builds: one entry per produced ``.charm`` file, with ``arch``,
    ``path``, and optional ``base``.
    CI builds: one entry per arch with ``artifact`` and ``run-id`` (covers
    all bases for that arch).
    Localised CI builds (after ``artifacts localize``) carry all fields.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    arch: str
    path: str | None = None
    base: str | None = None
    artifact: str | None = None
    run_id: str | None = Field(default=None, alias="run-id")

    @model_validator(mode="after")
    def _at_least_one_output(self) -> "CharmOutput":
        if not self.path and not self.artifact:
            msg = "CharmOutput must specify path or artifact"
            raise ValueError(msg)
        if self.artifact and not self.run_id:
            msg = "run-id is required when artifact is set"
            raise ValueError(msg)
        return self


class SnapOutput(BaseModel):
    """Snap build output entry.

    Local builds populate ``file``; CI builds populate ``artifact`` + ``run-id``.
    Localised CI builds (after ``artifacts localize``) populate both.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    arch: str
    file: str | None = None
    artifact: str | None = None
    run_id: str | None = Field(default=None, alias="run-id")

    @model_validator(mode="after")
    def _at_least_one_output(self) -> "SnapOutput":
        if not self.file and not self.artifact:
            msg = "SnapOutput must specify file or artifact"
            raise ValueError(msg)
        if self.artifact and not self.run_id:
            msg = "run-id is required when artifact is set"
            raise ValueError(msg)
        return self


class GeneratedResource(BaseModel):
    """A charm OCI-image resource with a reference to its backing rock.

    The actual image location is resolved by looking up the linked rock in
    ``ArtifactsGenerated.rocks`` — it is not duplicated here.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["oci-image"]
    rock: str | None = None


class GeneratedRock(BaseModel):
    """A rock with its per-architecture build output."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    rockcraft_yaml: str = Field(alias="rockcraft-yaml")
    builds: list[RockOutput]


class GeneratedCharm(BaseModel):
    """A charm with its flat build output list and resolved resource paths."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    charmcraft_yaml: str = Field(alias="charmcraft-yaml")
    builds: list[CharmOutput]
    resources: dict[str, GeneratedResource] | None = None


class GeneratedSnap(BaseModel):
    """A snap with its per-architecture build output."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    snapcraft_yaml: str = Field(alias="snapcraft-yaml")
    builds: list[SnapOutput]


class ArtifactsGenerated(BaseModel):
    """Top-level schema for ``artifacts.build.yaml`` (schema version 1)."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    rocks: list[GeneratedRock] = []
    charms: list[GeneratedCharm] = []
    snaps: list[GeneratedSnap] = []
