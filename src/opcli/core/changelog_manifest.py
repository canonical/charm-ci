# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Canonical changelog record serialization and boundary validation."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from pydantic import BaseModel

from opcli.core.exceptions import ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable

    from opcli.models.changelog import Boundary, ReleaseRange


def canonical(record: BaseModel | dict[str, object]) -> bytes:
    """Serialize a record for storage and hashing."""
    data = record.model_dump(mode="json") if isinstance(record, BaseModel) else record
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def digest(record: BaseModel | dict[str, object] | bytes) -> str:
    """Return a SHA-256 digest of canonical bytes."""
    return hashlib.sha256(record if isinstance(record, bytes) else canonical(record)).hexdigest()


def verify_range(
    release: ReleaseRange, expected_hash: str, resolve: Callable[[str], Boundary]
) -> None:
    """Reject changed labels or a moved source tip before collecting history."""
    if digest(release) != expected_hash:
        raise ValidationError("range manifest hash does not match")
    for boundary in (release.previous, release.current, release.source_tip):
        if resolve(boundary.ref) != boundary:
            raise ValidationError(f"release boundary moved: {boundary.ref}")


def idempotency_key(release: ReleaseRange) -> str:
    """Bind the repository, immutable range and implementation versions."""
    return digest(
        {
            "repository_id": release.repository_id,
            "range_hash": digest(release),
            "generator": release.generator,
            "renderer_version": release.renderer_version,
        }
    )
