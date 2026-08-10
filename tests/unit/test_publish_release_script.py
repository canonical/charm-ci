# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for publish workflow scripts."""

import json
import os
import stat
import subprocess
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
SCRIPT = REPO_ROOT / ".github" / "scripts" / "create-publish-release.sh"
INJECT_SCRIPT = REPO_ROOT / ".github" / "scripts" / "inject-charm-version.sh"


def test_inject_charm_version_adds_short_sha(tmp_path: Path) -> None:
    charm_path = tmp_path / "test.charm"
    _write_charm(charm_path)

    result = _run_inject_script(tmp_path, "1234567890abcdef", charm_path)

    assert result.returncode == 0, result.stderr
    assert _read_charm_file(charm_path, "version") == "12345678\n"


def test_inject_charm_version_preserves_existing_version(tmp_path: Path) -> None:
    charm_path = tmp_path / "test.charm"
    _write_charm(charm_path, version="custom-version\n")

    result = _run_inject_script(tmp_path, "1234567890abcdef", charm_path)

    assert result.returncode == 0, result.stderr
    assert "Preserving existing version file" in result.stdout
    assert _read_charm_file(charm_path, "version") == "custom-version\n"


def test_inject_charm_version_handles_multiple_charms(tmp_path: Path) -> None:
    first_charm = tmp_path / "first.charm"
    second_charm = tmp_path / "second.charm"
    _write_charm(first_charm)
    _write_charm(second_charm)

    result = _run_inject_script(tmp_path, "abcdef1234567890", first_charm, second_charm)

    assert result.returncode == 0, result.stderr
    assert _read_charm_file(first_charm, "version") == "abcdef12\n"
    assert _read_charm_file(second_charm, "version") == "abcdef12\n"


def test_inject_charm_version_requires_charm_paths(tmp_path: Path) -> None:
    result = subprocess.run(
        [str(INJECT_SCRIPT), "1234567890abcdef"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "usage: inject-charm-version.sh" in result.stderr


def test_inject_charm_version_fails_when_charm_missing(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.charm"

    result = _run_inject_script(tmp_path, "1234567890abcdef", missing_path)

    assert result.returncode == 1
    assert f"charm file not found: {missing_path}" in result.stderr


def test_inject_charm_version_fails_when_charm_is_not_zip(tmp_path: Path) -> None:
    charm_path = tmp_path / "invalid.charm"
    charm_path.write_text("not a zip archive\n", encoding="utf-8")

    result = _run_inject_script(tmp_path, "1234567890abcdef", charm_path)

    assert result.returncode == 1
    assert f"not a valid charm archive: {charm_path}" in result.stderr


def test_missing_publish_results_fails(tmp_path: Path) -> None:
    result = _run_script(tmp_path)

    assert result.returncode == 1
    assert "::error::publish-results.json not found" in result.stdout


def test_invalid_publish_results_fails(tmp_path: Path) -> None:
    (tmp_path / "publish-results.json").write_text("not json\n", encoding="utf-8")

    result = _run_script(tmp_path)

    assert result.returncode == 1
    assert "::error::publish-results.json is not valid JSON" in result.stdout


def test_empty_publish_results_skips_tag_and_release_creation(tmp_path: Path) -> None:
    (tmp_path / "publish-results.json").write_text("[]\n", encoding="utf-8")

    result = _run_script(tmp_path)

    assert result.returncode == 0
    assert "No charms published" in result.stdout
    assert not (tmp_path / "commands.log").exists()


def test_both_toggles_false_is_a_noop(tmp_path: Path) -> None:
    _write_publish_results(tmp_path, charm_name="traefik-k8s", revision=308)

    result = _run_script(tmp_path, create_tags="false", create_release="false")

    assert result.returncode == 0
    assert "create-tags and create-release are both false" in result.stdout
    assert not (tmp_path / "commands.log").exists()


def test_default_toggles_create_tag_and_combined_release(tmp_path: Path) -> None:
    _write_publish_results(tmp_path, charm_name="traefik-k8s", revision=308)

    result = _run_script(tmp_path, github_run_id="42")

    assert result.returncode == 0, result.stderr
    log = _read_log(tmp_path)
    assert "git tag traefik-k8s-rev308 abc123" in log
    assert "git push origin refs/tags/traefik-k8s-rev308" in log
    assert "gh release create publish-42" in log
    assert "--target abc123" in log
    assert "--generate-notes" in log
    notes = _read_notes_file(log)
    assert "traefik-k8s-rev308" in notes
    assert "(unknown, amd64)" in notes
    assert "traefik-image rev165" in notes
    assert "github.com/canonical/charm-ci/tree/traefik-k8s-rev308" in notes


def test_create_release_false_still_tags_but_skips_release(tmp_path: Path) -> None:
    _write_publish_results(tmp_path, charm_name="traefik-k8s", revision=308)

    result = _run_script(tmp_path, create_release="false", github_run_id="42")

    assert result.returncode == 0, result.stderr
    log = _read_log(tmp_path)
    assert "git tag traefik-k8s-rev308 abc123" in log
    assert "git push origin refs/tags/traefik-k8s-rev308" in log
    assert "gh release create" not in log
    assert "create-release is false" in result.stdout


def test_create_tags_false_skips_tags_but_still_releases(tmp_path: Path) -> None:
    _write_publish_results(tmp_path, charm_name="traefik-k8s", revision=308)

    result = _run_script(tmp_path, create_tags="false", github_run_id="42")

    assert result.returncode == 0, result.stderr
    log = _read_log(tmp_path)
    assert "git tag" not in log
    assert "git push" not in log
    assert "gh release create publish-42" in log
    notes = _read_notes_file(log)
    assert "traefik-k8s-rev308 (unknown, amd64)" in notes
    assert "[traefik-k8s-rev308]" not in notes
    assert "/tree/" not in notes


def test_combined_release_aggregates_multiple_charms_bases_arches(tmp_path: Path) -> None:
    payload = [
        {
            "charm_name": "traefik-k8s",
            "channel": "latest/edge",
            "releases": [
                {"revision": 7, "base": "ubuntu@22.04", "arch": "amd64"},
                {"revision": 8, "base": "ubuntu@24.04", "arch": "amd64"},
            ],
            "resources": {"traefik-image": 165},
        },
        {
            "charm_name": "haproxy",
            "channel": "latest/stable",
            "releases": [{"revision": 3, "base": None, "arch": "arm64"}],
            "resources": {},
        },
    ]
    (tmp_path / "publish-results.json").write_text(json.dumps(payload), encoding="utf-8")

    result = _run_script(tmp_path, github_run_id="99")

    assert result.returncode == 0, result.stderr
    log = _read_log(tmp_path)
    # Exactly one combined release, covering every charm/revision.
    assert log.count("gh release create") == 1
    assert "gh release create publish-99" in log
    assert "git tag traefik-k8s-rev7 abc123" in log
    assert "git tag traefik-k8s-rev8 abc123" in log
    assert "git tag haproxy-rev3 abc123" in log
    notes = _read_notes_file(log)
    assert "traefik-k8s-rev7" in notes
    assert "traefik-k8s-rev8" in notes
    assert "haproxy-rev3" in notes
    assert "traefik-image rev165" in notes


def test_existing_tag_is_skipped_but_still_linked(tmp_path: Path) -> None:
    _write_publish_results(tmp_path, charm_name="traefik-k8s", revision=308)

    result = _run_script(
        tmp_path,
        remote_tags="traefik-k8s-rev308",
        github_run_id="42",
    )

    assert result.returncode == 0, result.stderr
    assert "Tag traefik-k8s-rev308 already exists" in result.stdout
    log = _read_log(tmp_path)
    assert "git tag traefik-k8s-rev308" not in log
    assert "git push origin refs/tags/traefik-k8s-rev308" not in log
    notes = _read_notes_file(log)
    assert "[traefik-k8s-rev308 (unknown, amd64)" in notes
    assert "tree/traefik-k8s-rev308" in notes


def test_idempotent_rerun_skips_existing_combined_release(tmp_path: Path) -> None:
    _write_publish_results(tmp_path, charm_name="traefik-k8s", revision=308)

    result = _run_script(
        tmp_path,
        existing_releases="publish-42",
        github_run_id="42",
    )

    assert result.returncode == 0, result.stderr
    assert "Release publish-42 already exists" in result.stdout
    assert "gh release create" not in _read_log(tmp_path)


def test_notes_start_tag_uses_previous_combined_release(tmp_path: Path) -> None:
    _write_publish_results(tmp_path, charm_name="traefik-k8s", revision=308)

    result = _run_script(
        tmp_path,
        existing_releases="publish-41",
        remote_tags="publish-41\npublish-40",
        github_run_id="42",
    )

    assert result.returncode == 0, result.stderr
    log = _read_log(tmp_path)
    assert "--notes-start-tag publish-41" in log


def test_notes_start_tag_uses_previous_release_even_at_same_commit(tmp_path: Path) -> None:
    """A previous combined release at the same commit SHA is still a valid
    --notes-start-tag candidate (e.g. retrying a workflow_dispatch on the
    same commit after a transient failure) — it must not be excluded.
    """
    _write_publish_results(tmp_path, charm_name="traefik-k8s", revision=308)

    result = _run_script(
        tmp_path,
        existing_releases="publish-41",
        remote_tags="publish-41\npublish-40",
        same_sha_tags="publish-41",  # publish-41 points at the same commit as this run
        github_run_id="42",
    )

    assert result.returncode == 0, result.stderr
    log = _read_log(tmp_path)
    assert "--notes-start-tag publish-41" in log


def test_transient_release_view_error_aborts_instead_of_recreating(tmp_path: Path) -> None:
    """A non-404 `gh release view` failure (e.g. a transient API error) must
    abort the script, not be silently treated as "release doesn't exist" —
    otherwise a flaky API call could cause `gh release create` to fail on an
    already-existing release, or (for the previous-tag scan) silently widen
    release notes past a release that does in fact exist.
    """
    _write_publish_results(tmp_path, charm_name="traefik-k8s", revision=308)

    result = _run_script(
        tmp_path,
        existing_releases="ERROR:publish-42",
        github_run_id="42",
    )

    assert result.returncode == 1
    assert "failed unexpectedly" in result.stderr
    assert "gh release create" not in _read_log(tmp_path)


def test_notes_start_tag_omitted_when_no_previous_release(tmp_path: Path) -> None:
    _write_publish_results(tmp_path, charm_name="traefik-k8s", revision=1)

    result = _run_script(tmp_path, remote_tags="other-charm-rev999", github_run_id="1")

    assert result.returncode == 0, result.stderr
    log = _read_log(tmp_path)
    assert "--generate-notes" in log
    assert "--notes-start-tag" not in log


def _write_publish_results(tmp_path: Path, *, charm_name: str, revision: int) -> None:
    payload = [
        {
            "charm_name": charm_name,
            "channel": "latest/edge",
            "releases": [{"revision": revision, "base": None, "arch": "amd64"}],
            "resources": {"traefik-image": 165},
        }
    ]
    (tmp_path / "publish-results.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_charm(path: Path, *, version: str | None = None) -> None:
    with zipfile.ZipFile(path, "w") as charm:
        charm.writestr("metadata.yaml", "name: test\n")
        if version is not None:
            charm.writestr("version", version)


def _read_charm_file(path: Path, name: str) -> str:
    with zipfile.ZipFile(path) as charm:
        return charm.read(name).decode()


def _run_inject_script(
    tmp_path: Path,
    commit_sha: str,
    *charm_paths: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(INJECT_SCRIPT), commit_sha, *(str(path) for path in charm_paths)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )


def _run_script(  # noqa: PLR0913
    tmp_path: Path,
    *,
    existing_releases: str = "",
    remote_tags: str = "",
    create_tags: str = "true",
    create_release: str = "true",
    github_run_id: str = "1",
    same_sha_tags: str = "",
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "commands.log"
    _write_executable(
        bin_dir / "gh",
        """#!/usr/bin/env bash
set -euo pipefail
if [ "$1 $2" = "release view" ]; then
  case " ${EXISTING_RELEASES:-} " in
    *" $3 "*) exit 0 ;;
    *" ERROR:$3 "*)
      echo "HTTP 500: Internal Server Error" >&2
      exit 1
      ;;
    *)
      echo "release not found" >&2
      exit 1
      ;;
  esac
fi
printf 'gh' >> "${COMMAND_LOG}"
printf ' %q' "$@" >> "${COMMAND_LOG}"
printf '\\n' >> "${COMMAND_LOG}"
""",
    )
    _write_executable(
        bin_dir / "git",
        """#!/usr/bin/env bash
set -euo pipefail
if [ "$1" = "ls-remote" ]; then
  while IFS= read -r tag; do
    [ -n "$tag" ] || continue
    case " ${SAME_SHA_TAGS:-} " in
      *" ${tag} "*) sha="${GITHUB_SHA:-abc123}" ;;
      *) sha="old123" ;;
    esac
    printf '%s\\trefs/tags/%s\\n' "$sha" "$tag"
  done <<< "${REMOTE_TAGS:-}"
  exit 0
fi
printf 'git' >> "${COMMAND_LOG}"
printf ' %q' "$@" >> "${COMMAND_LOG}"
printf '\\n' >> "${COMMAND_LOG}"
""",
    )

    env = {
        **os.environ,
        "COMMAND_LOG": str(log_path),
        "EXISTING_RELEASES": existing_releases,
        "GH_TOKEN": "test-token",
        "GITHUB_SHA": "abc123",
        "GITHUB_REPOSITORY": "canonical/charm-ci",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_RUN_ID": github_run_id,
        "CREATE_TAGS": create_tags,
        "CREATE_RELEASE": create_release,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "REMOTE_TAGS": remote_tags,
        "SAME_SHA_TAGS": same_sha_tags,
    }
    return subprocess.run(
        [str(SCRIPT)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _read_log(tmp_path: Path) -> str:
    log_path = tmp_path / "commands.log"
    if not log_path.exists():
        return ""
    return log_path.read_text(encoding="utf-8")


def _read_notes_file(log: str) -> str:
    """Extract and read the --notes-file path referenced in a logged gh command."""
    marker = "--notes-file "
    idx = log.index(marker) + len(marker)
    rest = log[idx:]
    notes_path = rest.split()[0]
    return Path(notes_path).read_text(encoding="utf-8")
