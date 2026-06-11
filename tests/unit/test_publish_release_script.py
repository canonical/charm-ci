# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for the publish workflow GitHub release script."""

import json
import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
SCRIPT = REPO_ROOT / ".github" / "scripts" / "create-publish-release.sh"


def test_missing_publish_results_fails(tmp_path: Path) -> None:
    result = _run_script(tmp_path)

    assert result.returncode == 1
    assert "::error::publish-results.json not found" in result.stdout


def test_invalid_publish_results_fails(tmp_path: Path) -> None:
    (tmp_path / "publish-results.json").write_text("not json\n", encoding="utf-8")

    result = _run_script(tmp_path)

    assert result.returncode == 1
    assert "::error::publish-results.json is not valid JSON" in result.stdout


def test_empty_publish_results_skips_release_creation(tmp_path: Path) -> None:
    (tmp_path / "publish-results.json").write_text("[]\n", encoding="utf-8")

    result = _run_script(tmp_path)

    assert result.returncode == 0
    assert "No charms published" in result.stdout
    assert not (tmp_path / "commands.log").exists()


def test_existing_release_is_skipped(tmp_path: Path) -> None:
    _write_publish_results(tmp_path, charm_name="traefik-k8s", revision=308)

    result = _run_script(tmp_path, existing_releases="traefik-k8s-rev308")

    assert result.returncode == 0
    assert "Release traefik-k8s-rev308 already exists" in result.stdout
    assert "release create" not in _read_log(tmp_path)


def test_release_create_uses_generated_notes_and_previous_release_tag(tmp_path: Path) -> None:
    _write_publish_results(tmp_path, charm_name="traefik-k8s", revision=308)

    result = _run_script(
        tmp_path,
        existing_releases="traefik-k8s-rev307",
        remote_tags="traefik-k8s-rev302\ntraefik-k8s-rev307\nother-charm-rev999",
    )

    assert result.returncode == 0, result.stderr
    log = _read_log(tmp_path)
    assert "git tag traefik-k8s-rev308 abc123" in log
    assert "git push origin refs/tags/traefik-k8s-rev308" in log
    assert "gh release create traefik-k8s-rev308" in log
    assert "--generate-notes" in log
    assert "--notes-file" in log
    assert "--notes-start-tag traefik-k8s-rev307" in log


def test_release_create_skips_previous_tag_without_release(tmp_path: Path) -> None:
    _write_publish_results(tmp_path, charm_name="traefik-k8s", revision=308)

    result = _run_script(
        tmp_path,
        existing_releases="traefik-k8s-rev302",
        remote_tags="traefik-k8s-rev302\ntraefik-k8s-rev307",
    )

    assert result.returncode == 0, result.stderr
    log = _read_log(tmp_path)
    assert "--notes-start-tag traefik-k8s-rev302" in log
    assert "--notes-start-tag traefik-k8s-rev307" not in log


def test_release_create_omits_previous_tag_when_none_exists(tmp_path: Path) -> None:
    _write_publish_results(tmp_path, charm_name="traefik-k8s", revision=1)

    result = _run_script(tmp_path, remote_tags="other-charm-rev999")

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


def _run_script(
    tmp_path: Path,
    *,
    existing_releases: str = "",
    remote_tags: str = "",
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
    *) exit 1 ;;
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
    printf 'abc123\\trefs/tags/%s\\n' "$tag"
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
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "REMOTE_TAGS": remote_tags,
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
