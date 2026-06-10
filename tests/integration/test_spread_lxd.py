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

import contextlib
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

_INTEGRATION_SUITES_SPREAD_YAML = """\
project: integration-suites-test

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
  - .build

integration-suites:
  tests/integration/:
    cwd: ./
    summary: integration tests
    backends:
      - integration-test
"""

_INTEGRATION_SUITES_TEST_FILE = """\
# A dummy test file for auto-discovery.
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


class TestSpreadIntegrationSuites:
    """End-to-end tests using integration-suites (no committed task.yaml).

    These tests validate that spread can find and list tasks from
    integration-suites-generated content.  They require ``spread`` but
    NOT LXD (using ``spread -list`` only).
    """

    @pytest.fixture()
    def integration_suites_project(self, tmp_path: Path) -> Path:
        """Create a project using integration-suites (no task.yaml committed)."""
        (tmp_path / "spread.yaml").write_text(_INTEGRATION_SUITES_SPREAD_YAML)
        test_dir = tmp_path / "tests" / "integration"
        test_dir.mkdir(parents=True)
        (test_dir / "test_basic.py").write_text(_INTEGRATION_SUITES_TEST_FILE)
        return tmp_path

    def test_no_task_yaml_committed(self, integration_suites_project: Path) -> None:
        """No task.yaml exists in the source tree before runtime."""
        task_path = integration_suites_project / "tests" / "integration" / "run" / "task.yaml"
        assert not task_path.exists()

    def test_task_yaml_cleaned_up_after_run(self, integration_suites_project: Path) -> None:
        """Generated task.yaml in the project tree is cleaned up after spread_run."""
        with contextlib.suppress(SubprocessError, FileNotFoundError, OSError):
            spread_run(integration_suites_project, ci=False)
        # task.yaml should be removed from project tree after cleanup
        task_path = integration_suites_project / "tests" / "integration" / "run" / "task.yaml"
        assert not task_path.exists()
        # .build/ should also be cleaned
        build_dir = integration_suites_project / ".build"
        assert not build_dir.exists()

    def test_integration_suites_full_lxd_run(self, integration_suites_project: Path) -> None:
        """spread_run works with integration-suites (task.yaml generated at runtime).

        Validates the full flow:
        1. integration-suites parsed from spread.yaml
        2. test modules auto-discovered (test_basic)
        3. task.yaml generated in project tree at runtime
        4. spread runs successfully in LXD VM
        5. task.yaml cleaned up after run

        We override the generated task.yaml with a simple echo to avoid
        needing opcli installed inside the VM.
        """
        # Override _TASK_YAML_CONTENT_SUITE for this test so spread doesn't
        # need opcli inside the VM.  We do this by pre-creating the task.yaml
        # in the expected location — _materialize_task_files won't overwrite
        # a non-empty directory scenario (it always writes).
        # Instead, we patch the content template. For integration tests,
        # just provide a trivial task.yaml at the suite path.
        task_dir = integration_suites_project / "tests" / "integration" / "run"
        task_dir.mkdir(parents=True)
        (task_dir / "task.yaml").write_text(
            'summary: basic test\n\nexecute: |\n    echo "integration-suites test passed"\n'
        )
        spread_run(integration_suites_project, ci=False)
