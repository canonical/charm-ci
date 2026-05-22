# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for spread local backend with real LXD VMs.

These tests require ``spread`` and ``lxc`` to be available on the host.
They launch real VMs, so they are slow (~30-60s) and marked with
``@pytest.mark.integration``.

The prepare script is conditional — it skips concierge and provision load
when the corresponding files are absent.  This lets us exercise the full
allocate → SSH → execute → discard flow without needing concierge or
opcli installed inside the VM.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from opcli.core.exceptions import SubprocessError
from opcli.core.spread import spread_run

_HAVE_SPREAD = shutil.which("spread") is not None
_HAVE_LXC = shutil.which("lxc") is not None

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (_HAVE_SPREAD and _HAVE_LXC),
        reason="spread and/or lxc not available",
    ),
]


_SPREAD_YAML = """\
project: integration-test

path: /home/ubuntu/proj

kill-timeout: 30m

backends:
  integration-test:
    systems:
      - ubuntu-24.04

environment:
  CONCIERGE: concierge.yaml

exclude:
  - .git

suites:
  tests/integration/:
    summary: integration tests
    environment:
      MODULE/test_basic: test_basic
"""

_TASK_YAML = """\
summary: basic spread integration test

execute: |
    echo "spread integration test passed"
    whoami
    pwd
"""

_FAILING_TASK_YAML = """\
summary: task that exits non-zero

execute: |
    echo "about to fail"
    exit 1
"""

_SECRETS_SPREAD_YAML = """\
project: secrets-test

path: /home/ubuntu/proj

kill-timeout: 30m

backends:
  integration-test:
    systems:
      - ubuntu-24.04

environment:
  CONCIERGE: concierge.yaml
  TEST_SECRET: '$(HOST: echo "${TEST_SECRET:-}")'

exclude:
  - .git

suites:
  tests/integration/:
    summary: integration tests
    environment:
      MODULE/test_basic: test_basic
"""

_SECRETS_TASK_YAML = """\
summary: verify secrets are forwarded

execute: |
    test -n "$TEST_SECRET" || { echo "TEST_SECRET empty"; exit 1; }
    test "$TEST_SECRET" = "s3cr3t-value" || { echo "wrong value"; exit 1; }
    echo "secret correctly forwarded"
"""


@pytest.fixture()
def spread_project(tmp_path: Path) -> Path:
    """Create a minimal spread project with a passing task."""
    (tmp_path / "spread.yaml").write_text(_SPREAD_YAML)
    task_dir = tmp_path / "tests" / "integration" / "run"
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(_TASK_YAML)
    return tmp_path


class TestSpreadLxdLocal:
    """End-to-end tests using the local (LXD VM) backend."""

    def test_basic_spread_run(self, spread_project: Path) -> None:
        """A trivial task runs successfully inside an LXD VM."""
        spread_run(spread_project, ci=False)

    def test_no_leftover_vms(self, spread_project: Path) -> None:
        """VMs are cleaned up after a successful spread run."""
        before = _count_spread_vms()
        spread_run(spread_project, ci=False)
        after = _count_spread_vms()
        assert after == before

    def test_failing_task_raises_subprocess_error(self, tmp_path: Path) -> None:
        """A task that exits non-zero causes spread_run to raise SubprocessError."""
        (tmp_path / "spread.yaml").write_text(_SPREAD_YAML)
        task_dir = tmp_path / "tests" / "integration" / "run"
        task_dir.mkdir(parents=True)
        (task_dir / "task.yaml").write_text(_FAILING_TASK_YAML)

        with pytest.raises(SubprocessError) as exc_info:
            spread_run(tmp_path, ci=False)

        assert exc_info.value.returncode != 0
        assert "spread" in exc_info.value.cmd[0]

    def test_secrets_env_forwarded_to_vm(self, tmp_path: Path) -> None:
        """Secrets from .secrets.env are available inside the spread VM."""
        (tmp_path / "spread.yaml").write_text(_SECRETS_SPREAD_YAML)
        task_dir = tmp_path / "tests" / "integration" / "run"
        task_dir.mkdir(parents=True)
        (task_dir / "task.yaml").write_text(_SECRETS_TASK_YAML)
        (tmp_path / ".secrets.env").write_text("TEST_SECRET=s3cr3t-value\n")

        spread_run(tmp_path, ci=False)


def _count_spread_vms() -> int:
    """Count LXD instances whose names start with ``spread-``.

    Uses raw subprocess since this is test infrastructure inspecting the
    host, not opcli business logic.
    """
    result = subprocess.run(
        ["lxc", "ls", "--format", "csv", "--columns", "n"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return 0
    return sum(1 for line in result.stdout.splitlines() if line.startswith("spread-"))
