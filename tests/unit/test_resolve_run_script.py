# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Regression tests for publish run resolution using a fake GitHub API."""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / ".github" / "scripts" / "resolve-run.sh"
ATTEMPTS = 5
MAX_PAGES = 10


def test_selects_newest_success_with_matching_tree(tmp_path: Path) -> None:
    result = _run_script(
        tmp_path,
        [_page(_run(4, tree="other-tree"), _run(3), _run(2))],
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "output").read_text() == "run-id=3\n"
    requests = (tmp_path / "requests").read_text()
    assert "status=success" not in requests
    assert "per_page=100&page=1" in requests
    assert requests.count("/git/commits/") == 1
    assert "returned=3" in result.stdout
    assert "total=3" in result.stdout
    assert "newest=2026-10-07T03:00:00Z" in result.stdout
    assert "oldest=2026-10-07T03:00:00Z" in result.stdout
    assert "Candidate run: id=3" in result.stdout
    assert not (tmp_path / "sleeps").exists()


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped", None])
def test_rejects_non_successful_runs(tmp_path: Path, conclusion: str | None) -> None:
    result = _run_script(
        tmp_path,
        [_page(_run(1, conclusion=conclusion))] * ATTEMPTS,
    )

    assert result.returncode == 1
    assert "No successful integration-test run found" in result.stdout
    assert not (tmp_path / "output").exists()


def test_retries_missing_run_and_restarts_at_first_page(tmp_path: Path) -> None:
    result = _run_script(tmp_path, [_page(), _page(_run(1))])

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "sleeps").read_text() == "15\n"
    assert (tmp_path / "requests").read_text().count("&page=1\n") == 2  # noqa: PLR2004
    assert "attempt=2/5" in result.stdout


def test_missing_run_exhausts_bounded_backoff(tmp_path: Path) -> None:
    result = _run_script(tmp_path, [_page()] * ATTEMPTS)

    assert result.returncode == 1
    assert (tmp_path / "sleeps").read_text() == "15\n30\n60\n120\n"
    assert (tmp_path / "requests").read_text().count("/runs?") == ATTEMPTS
    assert "after 5 attempts" in result.stdout
    assert not (tmp_path / "output").exists()


def test_finds_matching_success_on_second_page(tmp_path: Path) -> None:
    failed_runs = [_run(index, conclusion="failure") for index in range(100)]
    result = _run_script(
        tmp_path,
        [_page(*failed_runs, total=101), _page(_run(101), total=101)],
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "output").read_text() == "run-id=101\n"
    assert "per_page=100&page=2" in (tmp_path / "requests").read_text()
    assert not (tmp_path / "sleeps").exists()


def test_pagination_and_retries_are_bounded(tmp_path: Path) -> None:
    full_page = _page(*[_run(index, tree="other-tree") for index in range(100)], total=2000)
    result = _run_script(tmp_path, [full_page] * (ATTEMPTS * MAX_PAGES))

    assert result.returncode == 1
    requests = (tmp_path / "requests").read_text()
    assert requests.count("/runs?") == ATTEMPTS * MAX_PAGES
    assert "page=11" not in requests
    assert "Search limit reached: 1000 runs" in result.stdout
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("page_number", [1, 2])
def test_listing_failure_is_not_reported_as_missing_run(tmp_path: Path, page_number: int) -> None:
    pages: list[dict[str, object] | str] = []
    if page_number > 1:
        pages.append(_page(*[_run(index, tree="other-tree") for index in range(100)], total=101))
    pages.append("API_ERROR")
    result = _run_script(tmp_path, pages)

    assert result.returncode == 1
    assert "HTTP 500" in result.stderr
    assert f"Could not list integration-test runs (attempt 1, page {page_number})" in result.stdout
    assert "No successful integration-test run found" not in result.stdout
    assert not (tmp_path / "output").exists()
    assert not (tmp_path / "sleeps").exists()


@pytest.mark.parametrize("response", ["not JSON", "{}", '{"workflow_runs":null,"total_count":1}'])
def test_malformed_listing_fails_without_publishing(tmp_path: Path, response: str) -> None:
    result = _run_script(tmp_path, [response])

    assert result.returncode != 0
    assert "Invalid integration-test run listing" in result.stdout
    assert not (tmp_path / "output").exists()
    assert not (tmp_path / "sleeps").exists()


@pytest.mark.parametrize("workflows", [[], [42, 43]])
def test_missing_or_ambiguous_workflow_fails(tmp_path: Path, workflows: list[int]) -> None:
    result = _run_script(tmp_path, [], workflows=workflows)

    assert result.returncode == 1
    assert "workflow" in result.stdout
    assert "/runs?" not in (tmp_path / "requests").read_text()


@pytest.mark.parametrize("fail_match", ["/git/commits/", "/actions/workflows"])
def test_initial_api_failure_is_preserved(tmp_path: Path, fail_match: str) -> None:
    result = _run_script(tmp_path, [], fail_match=fail_match)

    assert result.returncode != 0
    assert "HTTP 403" in result.stderr
    assert "No successful integration-test run found" not in result.stdout
    assert not (tmp_path / "output").exists()


def _run(
    run_id: int,
    *,
    tree: str = "target-tree",
    conclusion: str | None = "success",
) -> dict[str, object]:
    return {
        "id": run_id,
        "head_sha": f"pr-head-{run_id}",
        "head_commit": {"tree_id": tree},
        "conclusion": conclusion,
        "run_attempt": 2,
        "created_at": "2026-10-07T03:00:00Z",
    }


def _page(*runs: dict[str, object], total: int | None = None) -> dict[str, object]:
    return {"workflow_runs": list(runs), "total_count": len(runs) if total is None else total}


def _run_script(
    tmp_path: Path,
    pages: list[dict[str, object] | str],
    *,
    workflows: list[int] | None = None,
    fail_match: str = "",
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "workflows.json").write_text(
        json.dumps(
            {
                "workflows": [
                    {"id": workflow_id, "path": ".github/workflows/integration-test.yml"}
                    for workflow_id in ([42] if workflows is None else workflows)
                ]
            }
        )
    )
    for index, page in enumerate(pages, start=1):
        (tmp_path / f"page-{index}").write_text(
            page if isinstance(page, str) else json.dumps(page)
        )
    _write_executable(
        bin_dir / "gh",
        """#!/usr/bin/env bash
set -euo pipefail
endpoint="$2"
echo "$endpoint" >> requests
if [ -n "$FAIL_MATCH" ] && [[ "$endpoint" == *"$FAIL_MATCH"* ]]; then
  echo "gh: Forbidden (HTTP 403)" >&2
  exit 1
fi
case "$endpoint" in
  */git/commits/*) response='{"tree":{"sha":"target-tree"}}' ;;
  */actions/workflows) response=$(cat workflows.json) ;;
  */runs\\?*)
    count=0
    if [ -f counter ]; then count=$(cat counter); fi
    count=$((count + 1))
    echo "$count" > counter
    response=$(cat "page-$count")
    if [ "$response" = API_ERROR ]; then
      echo "gh: Internal Server Error (HTTP 500)" >&2
      exit 1
    fi
    ;;
  *) echo "Unexpected endpoint: $endpoint" >&2; exit 1 ;;
esac
if [ "${3:-}" = --jq ]; then
  printf '%s' "$response" | jq -r "$4"
else
  printf '%s' "$response"
fi
""",
    )
    _write_executable(bin_dir / "sleep", '#!/usr/bin/env bash\necho "$1" >> sleeps\n')
    return subprocess.run(
        [str(SCRIPT)],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GH_TOKEN": "test-token",
            "GITHUB_REPOSITORY": "canonical/charm-ci",
            "GITHUB_SHA": "publish-sha",
            "GITHUB_OUTPUT": str(tmp_path / "output"),
            "INPUT_WORKFLOW_FILE": "integration-test.yml",
            "FAIL_MATCH": fail_match,
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
