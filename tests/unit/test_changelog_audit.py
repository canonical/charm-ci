# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest

from opcli.core.changelog_audit import AuditStore
from opcli.core.exceptions import ValidationError


def test_snapshot_race_replays_first_complete_record():
    files = {}

    def create(path, content):
        if path in files:
            return False
        files[path] = content
        return True

    store = AuditStore(files.get, create)
    key = "a" * 64
    assert store.freeze(key, b'{"title":"initial"}') == b'{"title":"initial"}'
    assert store.freeze(key, b'{"title":"edited"}') == b'{"title":"initial"}'
    assert store.snapshot(key) == b'{"title":"initial"}'


def test_corrupt_content_is_not_replayed():
    store = AuditStore(lambda _: b"tampered", lambda _path, _value: False)
    with pytest.raises(ValidationError, match="hash"):
        store.read("range", "a" * 64)
