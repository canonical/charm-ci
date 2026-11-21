# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from opcli.core.changelog_collect import ChangeSource, PullRequest, Snapshot
from opcli.core.changelog_git import Commit
from opcli.core.changelog_normalize import normalize
from opcli.core.changelog_render import render
from opcli.models.changelog import Boundary, ReleaseRange


def _range() -> ReleaseRange:
    return ReleaseRange(
        repository="canonical/example",
        repository_id=1,
        previous=Boundary(ref="refs/tags/v1", object_id="a" * 40, commit="a" * 40),
        current=Boundary(ref="refs/tags/v2", object_id="b" * 40, commit="b" * 40),
        source_tip=Boundary(ref="refs/heads/main", object_id="b" * 40, commit="b" * 40),
        release="v2",
        release_date="2026-11-19",
        generator="c" * 40,
        config_hash="d" * 64,
    )


def _source(subject, sha="b", *, body="", pr=True):
    commit = Commit(
        sha=sha * 40,
        parents=("a" * 40,),
        subject=subject,
        body=body,
        author="example",
        authored_at="2026-11-19",
        committed_at="2026-11-19",
        paths=("src/charm.py",),
    )
    return ChangeSource(
        commit=commit,
        prs=(
            PullRequest(
                number=42,
                title=subject,
                author="example",
                labels=(),
                merge_sha=commit.sha,
                base="main",
                merged_at="2026-11-19",
            ),
        )
        if pr
        else (),
    )


def test_breaking_fix_stays_in_base_section_and_security_is_not_a_keyword():
    snapshot = Snapshot(
        retrieval="compare",
        sources=(_source("fix!: security wording"),),
        warnings=(),
    )
    entries, _ = normalize(_range(), snapshot)
    page = render(_range(), entries, "md")
    assert "### Fixed" in page
    assert "### Breaking changes" in page
    assert "### Security" not in page
    expected_links = 2
    assert page.count("https://github.com/canonical/example/pull/42") == expected_links


def test_direct_commit_is_visible_and_rst_cannot_inject_a_directive():
    snapshot = Snapshot(
        retrieval="targeted-git",
        sources=(_source(".. raw:: html\n\n<script>", pr=False),),
        warnings=(),
    )
    entries, _ = normalize(_range(), snapshot)
    page = render(_range(), entries, "rst")
    assert entries[0].review
    assert "Unclassified" in page
    assert "\n.. raw::" not in page
    assert "https://github.com/canonical/example/commit/" in page


def test_in_range_revert_preserves_auditable_exclusion():
    snapshot = Snapshot(
        retrieval="targeted-git",
        sources=(
            _source("Revert feature", "c", body=f"This reverts commit {'b' * 40}.", pr=False),
            _source("feat: introduce X"),
        ),
        warnings=(),
    )
    entries, exclusions = normalize(_range(), snapshot)
    assert not entries
    assert "b" * 40 in exclusions[0]
    assert "c" * 40 in exclusions[0]
