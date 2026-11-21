# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Content-addressed records on a create-only audit backend."""

import re
from collections.abc import Callable
from typing import Literal

from opcli.core.changelog_manifest import digest
from opcli.core.exceptions import ValidationError

Kind = Literal["range", "snapshot", "run", "receipt"]


class AuditStore:
    """Persist immutable content and freeze one snapshot per key."""

    def __init__(
        self, read: Callable[[str], bytes | None], create: Callable[[str, bytes], bool]
    ) -> None:
        self._get = read
        self._create = create

    def read(self, kind: Kind, key: str) -> bytes | None:
        """Read a record and validate its address."""
        content = self._get(f"records/{kind}/{_hash(key)}.json")
        if content is not None and digest(content) != key:
            raise ValidationError(f"audit {kind} hash does not match")
        return content

    def write(self, kind: Kind, content: bytes) -> str:
        """Write only when absent; verify the winner on conflict."""
        key = digest(content)
        if not self._create(f"records/{kind}/{key}.json", content) and (
            self.read(kind, key) != content
        ):
            raise ValidationError(f"audit {kind} could not be persisted")
        return key

    def snapshot(self, key: str) -> bytes | None:
        """Retrieve the frozen snapshot, if one has been recorded."""
        pointer = self._get(f"snapshots/{_hash(key)}")
        if pointer is None:
            return None
        try:
            content = self.read("snapshot", pointer.decode("ascii"))
        except UnicodeDecodeError as exc:
            raise ValidationError("invalid audit snapshot pointer") from exc
        if content is None:
            raise ValidationError("audit snapshot pointer has no record")
        return content

    def freeze(self, key: str, content: bytes) -> bytes:
        """Persist a snapshot and return the first complete version."""
        if (existing := self.snapshot(key)) is not None:
            return existing
        snapshot_hash = self.write("snapshot", content)
        if self._create(f"snapshots/{key}", snapshot_hash.encode("ascii")):
            return content
        result = self.snapshot(key)
        if result is None:
            raise ValidationError("audit snapshot could not be frozen")
        return result


def _hash(value: str) -> str:
    if re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValidationError("invalid audit hash")
    return value
