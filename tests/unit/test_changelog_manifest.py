# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json

import pytest
from pydantic import ValidationError as ModelError

from opcli.core.changelog_manifest import canonical, digest, verify_range
from opcli.core.exceptions import ValidationError
from opcli.models.changelog import Boundary, ReleaseRange


@pytest.fixture
def release_range() -> ReleaseRange:
    return ReleaseRange(
        repository="canonical/example-operator",
        repository_id=123,
        previous=Boundary(ref="refs/tags/v1", object_id="1" * 40, commit="a" * 40),
        current=Boundary(ref="refs/tags/v2", object_id="2" * 40, commit="b" * 40),
        source_tip=Boundary(ref="refs/heads/track/1.0", object_id="b" * 40, commit="b" * 40),
        release="v2",
        release_date="2026-11-19",
        generator="c" * 40,
        config_hash="d" * 64,
    )


def test_range_round_trips_with_same_identity(release_range):
    encoded = canonical(release_range)
    reordered = json.dumps(dict(reversed(list(json.loads(encoded).items()))))
    assert digest(ReleaseRange.model_validate_json(reordered)) == digest(release_range)
    assert json.loads(encoded)["from_sha"] == "a" * 40
    assert json.loads(encoded)["to_sha"] == "b" * 40


@pytest.mark.parametrize("ref", ["--output=x", "main~1", "refs/heads/a..b", "refs/tags/x^{}"])
def test_rejects_revision_syntax(ref):
    with pytest.raises(ModelError, match="ref"):
        Boundary(ref=ref, object_id="a" * 40, commit="a" * 40)


def test_rejects_a_repointed_tag_even_if_it_peels_to_same_commit(release_range):
    observed = {
        item.ref: item
        for item in (release_range.previous, release_range.current, release_range.source_tip)
    }
    observed[release_range.current.ref] = release_range.current.model_copy(
        update={"object_id": "3" * 40}
    )
    with pytest.raises(ValidationError, match="refs/tags/v2"):
        verify_range(release_range, digest(release_range), observed.__getitem__)


def test_rejects_a_moved_source_tip(release_range):
    observed = {
        item.ref: item
        for item in (release_range.previous, release_range.current, release_range.source_tip)
    }
    observed[release_range.source_tip.ref] = Boundary(
        ref=release_range.source_tip.ref, object_id="e" * 40, commit="e" * 40
    )
    with pytest.raises(ValidationError, match=r"refs/heads/track/1\.0"):
        verify_range(release_range, digest(release_range), observed.__getitem__)


def test_invalid_hash_does_not_consult_remote(release_range):
    with pytest.raises(ValidationError, match="hash"):
        verify_range(release_range, "0" * 64, {}.__getitem__)


def test_rejects_unrecognized_schema(release_range):
    record = json.loads(canonical(release_range))
    record["schema_version"] = 99
    with pytest.raises(ModelError, match="schema_version"):
        ReleaseRange.model_validate(record)
