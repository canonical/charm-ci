# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Immutable records for changelog generation."""

import re
from datetime import date
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

GitID = Annotated[str, Field(pattern=r"\A[0-9a-f]{40}\z")]
ContentHash = Annotated[str, Field(pattern=r"\A[0-9a-f]{64}\z")]
Repository = Annotated[str, Field(pattern=r"\A[A-Za-z0-9-]+/[A-Za-z0-9_.-]+\z")]
PositiveID = Annotated[int, Field(gt=0)]
Count = Annotated[int, Field(ge=0)]
SCHEMA_VERSION: Literal[1] = 1
RENDERER_VERSION: Literal[1] = 1


class ChangelogRecord(BaseModel):
    """Strict, versioned data exchanged between the workflow jobs."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Boundary(ChangelogRecord):
    """A ref label with its original and peeled object IDs."""

    ref: str
    object_id: GitID
    commit: GitID

    @field_validator("ref")
    @classmethod
    def check_ref(cls, value: str) -> str:
        """Accept only literal, qualified branch and tag references."""
        match = re.fullmatch(r"refs/(?:heads|tags)/([A-Za-z0-9._/-]+)", value)
        if (
            not match
            or ".." in value
            or any(
                not part or part.startswith(".") or part.endswith((".", ".lock"))
                for part in match[1].split("/")
            )
        ):
            raise ValueError("ref must be a fully qualified literal branch or tag")
        return value

    @model_validator(mode="after")
    def check_branch(self) -> Self:
        """Branches must refer directly to commits."""
        if self.ref.startswith("refs/heads/") and self.object_id != self.commit:
            raise ValueError("branch ref must point directly to a commit")
        return self


class ReleaseRange(ChangelogRecord):
    """The input authority for a single generation."""

    repository: Repository
    repository_id: PositiveID
    previous: Boundary
    current: Boundary
    source_tip: Boundary
    release: Annotated[str, Field(pattern=r"\A[A-Za-z0-9][A-Za-z0-9._ -]{0,79}\z")]
    release_date: str
    generator: GitID
    config_hash: ContentHash
    schema_version: Literal[1] = SCHEMA_VERSION
    renderer_version: Literal[1] = RENDERER_VERSION
    semantics: Literal["from-exclusive-to-inclusive"] = "from-exclusive-to-inclusive"
    history_policy: Literal["all-parents-first-parent-presentation"] = (
        "all-parents-first-parent-presentation"
    )

    @field_validator("release_date")
    @classmethod
    def check_date(cls, value: str) -> str:
        """Require a reproducible ISO date for headings."""
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError("release date must use YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def check_source(self) -> Self:
        """Require a branch to verify the live source tip."""
        if not self.source_tip.ref.startswith("refs/heads/"):
            raise ValueError("source tip must be a branch")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def from_sha(self) -> str:
        """Exclusive base commit."""
        return self.previous.commit

    @computed_field  # type: ignore[prop-decorator]
    @property
    def to_sha(self) -> str:
        """Inclusive head commit."""
        return self.current.commit

    @model_validator(mode="before")
    @classmethod
    def verify_serialized_shas(cls, value: object) -> object:
        """Check redundant on-disk SHAs against the boundary records."""
        if not isinstance(value, dict):
            return value
        data = dict(value)
        for key, boundary in (("from_sha", "previous"), ("to_sha", "current")):
            if key in data:
                item = data.get(boundary)
                if not isinstance(item, dict) or data.pop(key) != item.get("commit"):
                    raise ValueError(f"{key} does not match its release boundary")
        return data


class RunManifest(ChangelogRecord):
    """A reproducible successful generation or a closed failure."""

    range_hash: ContentHash
    idempotency_key: ContentHash
    generator: GitID
    schema_version: Literal[1] = SCHEMA_VERSION
    renderer_version: Literal[1] = RENDERER_VERSION
    retrieval: Literal["snapshot", "compare", "targeted-git", "full-git", "failed"]
    commits: Count
    pull_requests: Count
    unclassified: Count
    warnings: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    metadata_hash: ContentHash | None = None
    entries_hash: ContentHash | None = None
    output_hash: ContentHash | None = None
    result_url: str

    @model_validator(mode="after")
    def check_completion(self) -> Self:
        """Disallow a complete-looking output without its input hashes."""
        if self.retrieval == "failed":
            if self.output_hash is not None:
                raise ValueError("a failed run cannot have an output hash")
        elif not all((self.metadata_hash, self.entries_hash, self.output_hash)):
            raise ValueError("a complete run must record all three content hashes")
        return self


class GenerationReceipt(ChangelogRecord):
    """Audit record for a generated documentation commit."""

    docs_commit: GitID
    output_paths: tuple[str, ...]
    timestamp: str
    manifest_hash: ContentHash
