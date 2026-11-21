# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import re
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from opcli.changelog_job import generate, main
from opcli.core.changelog_collect import Snapshot
from opcli.core.changelog_github import GitHub
from opcli.core.changelog_manifest import canonical, digest
from opcli.core.exceptions import ValidationError
from opcli.models.changelog import Boundary, ReleaseRange


def _boundaries():
    return {
        "refs/tags/v1": Boundary(ref="refs/tags/v1", object_id="a" * 40, commit="a" * 40),
        "refs/tags/v2": Boundary(ref="refs/tags/v2", object_id="b" * 40, commit="b" * 40),
        "refs/heads/main": Boundary(ref="refs/heads/main", object_id="b" * 40, commit="b" * 40),
    }


def test_job_persists_range_before_collecting_and_emits_verifiable_output(tmp_path, mocker):
    source = _boundaries()
    release = ReleaseRange(
        repository="canonical/example",
        repository_id=1,
        previous=source["refs/tags/v1"],
        current=source["refs/tags/v2"],
        source_tip=source["refs/heads/main"],
        release="v2",
        release_date="2026-11-19",
        generator="c" * 40,
        config_hash=digest({"format": "md", "date_heading": False}),
    )
    api = GitHub("")
    mocker.patch.object(api, "boundary", side_effect=lambda _, ref: source[ref])
    out = tmp_path / "output"

    def collect_saved_range(*_, **_options):
        assert (out / "range.json").read_bytes() == canonical(release)
        return Snapshot(retrieval="compare", sources=(), warnings=())

    mocker.patch("opcli.changelog_job.collect", side_effect=collect_saved_range)
    generate(release, api, out, "md", "https://github.com/canonical/example/actions/runs/1")

    run = json.loads((out / "run.json").read_bytes())
    assert run["output_hash"] == digest((out / "release.md").read_bytes())
    assert run["range_hash"] == digest(release)


def test_workflow_is_pinned_and_defaults_to_no_writes():
    root = Path(__file__).resolve().parents[2]
    workflow = YAML(typ="safe").load(
        (root / ".github/workflows/generate-changelog.yml").read_text()
    )
    assert set(workflow["on"]) == {"workflow_call"}
    call_inputs = workflow["on"]["workflow_call"]["inputs"]
    assert call_inputs["open_pr"]["default"] is False
    dispatch = YAML(typ="safe").load(
        (root / ".github/workflows/dispatch-changelog.yml").read_text()
    )
    assert set(dispatch["on"]) == {"workflow_dispatch"}
    dispatch_inputs = dispatch["on"]["workflow_dispatch"]["inputs"]
    assert dispatch_inputs["open_pr"]["default"] is False
    assert dispatch["permissions"] == workflow["permissions"]
    (caller,) = dispatch["jobs"].values()
    assert caller["uses"] == "./.github/workflows/generate-changelog.yml"
    assert "secrets" not in caller
    assert caller["with"] == {field: f"${{{{ inputs.{field} }}}}" for field in dispatch_inputs}
    dispatch_refs = ("from_ref", "to_ref", "source_branch", "release_version", "release_date")
    for field in dispatch_refs:
        assert dispatch_inputs[field]["required"]
    for field in (*dispatch_refs, "range_manifest", "range_manifest_hash"):
        assert call_inputs[field]["type"] == "string"
        assert not call_inputs[field].get("required")
    for job in workflow["jobs"].values():
        if job.get("permissions", {}).get("contents") == "write":
            assert "open_pr" in job["if"]
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[a-f0-9]{40}", step["uses"].rsplit("@", 1)[1])


def test_dry_run_rejects_repointed_tag_before_generation(tmp_path, mocker):
    source = _boundaries()
    release = ReleaseRange(
        repository="canonical/example",
        repository_id=1,
        previous=source["refs/tags/v1"],
        current=source["refs/tags/v2"],
        source_tip=source["refs/heads/main"],
        release="v2",
        release_date="2026-11-19",
        generator="c" * 40,
        config_hash=digest({"format": "md", "date_heading": False}),
    )
    source["refs/tags/v2"] = Boundary(ref="refs/tags/v2", object_id="c" * 40, commit="b" * 40)
    api = GitHub("")
    mocker.patch.object(api, "boundary", side_effect=lambda _, ref: source[ref])
    out = tmp_path / "output"
    with pytest.raises(ValidationError, match="boundary moved"):
        generate(release, api, out, "md", "https://github.com/canonical/example/actions/runs/1")
    assert not (out / "release.md").exists()


@pytest.fixture
def api_draft(tmp_path, monkeypatch, mocker):
    variables = {
        "GITHUB_REPOSITORY": "canonical/example",
        "GITHUB_REPOSITORY_ID": "1",
        "GITHUB_RUN_ID": "1",
        "GENERATOR_SHA": "c" * 40,
        "FROM_REF": "refs/tags/v1",
        "TO_REF": "refs/tags/v2",
        "SOURCE_BRANCH": "refs/heads/main",
        "RELEASE_VERSION": "v2",
        "RELEASE_DATE": "2026-11-19",
        "OUTPUT_FORMAT": "rst",
        "CHANGELOG_OUTPUT_DIR": str(tmp_path),
    }
    for key, value in variables.items():
        monkeypatch.setenv(key, value)
    base = "a" * 40
    head = "b" * 40
    prefix = "/repos/canonical/example"
    stamp = {"name": "Example", "date": "2026-11-19"}
    records = {
        **{
            f"{prefix}/git/ref/{ref.removeprefix('refs/')}": {
                "ref": ref,
                "object": {"sha": item.object_id, "type": "commit"},
            }
            for ref, item in _boundaries().items()
        },
        f"{prefix}/compare/{base}...{head}": {
            "status": "ahead",
            "merge_base_commit": {"sha": base},
            "total_commits": 1,
            "ahead_by": 1,
            "commits": [{"sha": head}],
        },
        f"{prefix}/commits/{head}": {
            "sha": head,
            "files": [{"filename": "new", "previous_filename": "old"}],
            "commit": {"message": "fix: correct an error", "author": stamp, "committer": stamp},
            "parents": [{"sha": base}],
        },
        f"{prefix}/commits/{head}/pulls": [
            {
                "number": 42,
                "title": "fix: edited wording",
                "merged_at": "2026-11-19",
                "merge_commit_sha": head,
                "user": {"login": "example"},
                "base": {"ref": "main"},
                "labels": [{"name": "changelog:security"}],
            }
        ],
    }

    def request(method, path, data=None):
        assert method == "GET"
        assert data is None
        return 200, records[path.split("?")[0]], {}

    return mocker.patch.object(GitHub, "request", side_effect=request)


def test_fixture_api_generates_rst_using_immutable_merge_title(api_draft, tmp_path):
    assert main() == 0
    page = (tmp_path / "release.rst").read_text()
    assert "correct an error" in page
    assert "edited wording" not in page
    assert "Security" in page
    run = json.loads((tmp_path / "run.json").read_bytes())
    assert run["retrieval"] == "compare"
    assert run["pull_requests"] == 1
    assert run["warnings"]
    assert run["output_hash"] == digest(page.encode())
    assert api_draft.called


def test_write_request_fails_before_network_access(api_draft, tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_PR", "true")
    assert main() == 1
    assert not api_draft.called
    assert json.loads((tmp_path / "run.json").read_bytes())["output_hash"] is None
    assert not (tmp_path / "release.rst").exists()


_REF_ENV = ("FROM_REF", "TO_REF", "SOURCE_BRANCH", "RELEASE_VERSION", "RELEASE_DATE")


@pytest.mark.parametrize("extra_ref", ["", "refs/tags/v1"])
def test_manifest_mode_requires_no_ref_inputs(api_draft, tmp_path, monkeypatch, extra_ref):
    source = _boundaries()
    manifest = ReleaseRange(
        repository="canonical/example",
        repository_id=1,
        previous=source["refs/tags/v1"],
        current=source["refs/tags/v2"],
        source_tip=source["refs/heads/main"],
        release="v2",
        release_date="2026-11-19",
        generator="c" * 40,
        config_hash=digest({"format": "rst", "date_heading": False}),
    )
    for key in _REF_ENV:
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("FROM_REF", extra_ref)
    monkeypatch.setenv("RANGE_MANIFEST", canonical(manifest).decode())
    monkeypatch.setenv("RANGE_MANIFEST_HASH", digest(manifest))
    if extra_ref:
        assert main() == 1
        assert not api_draft.called
        assert not (tmp_path / "range.json").exists()
        return
    assert main() == 0
    assert (tmp_path / "range.json").read_bytes() == canonical(manifest)
    assert (tmp_path / "release.rst").exists()
    assert api_draft.called


@pytest.mark.parametrize(
    "overrides",
    [
        {"RANGE_MANIFEST": "{}"},
        {"RANGE_MANIFEST_HASH": "a" * 64},
        {"RANGE_MANIFEST": "{}", "RANGE_MANIFEST_HASH": "a" * 64},
        {"TO_REF": ""},
        dict.fromkeys(_REF_ENV, ""),
    ],
)
def test_mixed_or_incomplete_modes_fail_before_network(
    api_draft, tmp_path, monkeypatch, overrides
):
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    assert main() == 1
    assert not api_draft.called
    assert not (tmp_path / "range.json").exists()
