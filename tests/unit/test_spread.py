# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Tests for ``opcli spread init``, ``expand``, and ``run``."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from opcli.core.exceptions import ConfigurationError, SubprocessError, ValidationError
from opcli.core.spread import (
    _arch_from_runner,
    _virtual_runner_map,
    spread_expand,
    spread_init,
    spread_jobs,
    spread_run,
)
from opcli.core.subprocess import SubprocessResult
from opcli.core.yaml_io import load_yaml, loads_yaml
from tests.conftest import write_file

_MINIMAL_SPREAD = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-24.04
environment:
  CONCIERGE: concierge.yaml
suites:
  tests/integration/:
    summary: integration tests
    backends:
      - integration-test
    environment:
      MODULE/test_charm: test_charm
"""


class TestSpreadInit:
    """Tests for spread_init()."""

    def test_generates_files(self, tmp_path: Path) -> None:
        spread_path, task_path = spread_init(tmp_path)

        assert spread_path.exists()
        assert task_path.exists()
        assert task_path == tmp_path / "tests" / "integration" / "run" / "task.yaml"

        content = spread_path.read_text()
        assert "integration-test" in content

        task_content = task_path.read_text()
        assert 'opcli pytest expand -e "${TOX_ENV:-integration}"' in task_content

    def test_generates_required_fields(self, tmp_path: Path) -> None:
        spread_path, _ = spread_init(tmp_path)

        parsed = loads_yaml(spread_path.read_text())
        assert parsed["path"] == "/home/ubuntu/proj"
        assert parsed["kill-timeout"] == "60m"
        assert parsed["warn-timeout"] == "1m"
        assert "summary" in parsed["suites"]["tests/integration/"]

    def test_generates_exclude_list(self, tmp_path: Path) -> None:
        spread_path, _ = spread_init(tmp_path)

        parsed = loads_yaml(spread_path.read_text())
        exclude = parsed["exclude"]
        assert ".git" in exclude
        assert ".tox" in exclude
        assert ".venv" in exclude
        assert ".*_cache" in exclude

    def test_generates_standard_env_vars(self, tmp_path: Path) -> None:
        spread_path, _ = spread_init(tmp_path)

        parsed = loads_yaml(spread_path.read_text())
        env = parsed["environment"]
        assert "SUDO_USER" not in env
        assert "SUDO_UID" not in env
        assert "LANG" not in env
        assert "LANGUAGE" not in env
        assert "CONCIERGE" in env
        # GitHub Actions vars belong only in the expanded CI backend, not root
        assert "GITHUB_TOKEN" not in env
        assert "GITHUB_RUN_ID" not in env
        assert "GITHUB_REPOSITORY" not in env
        # MODULE variants belong in the suite, not the root environment
        assert not any(k.startswith("MODULE") for k in env)
        assert "TOX_ENV" not in env

    def test_module_vars_in_suite_environment(self, tmp_path: Path) -> None:
        test_dir = tmp_path / "tests" / "integration"
        test_dir.mkdir(parents=True)
        (test_dir / "test_charm.py").write_text("")
        (test_dir / "test_actions.py").write_text("")

        spread_path, _ = spread_init(tmp_path)

        parsed = loads_yaml(spread_path.read_text())
        suite_env = parsed["suites"]["tests/integration/"]["environment"]
        assert suite_env["MODULE/test_charm"] == "test_charm"
        assert suite_env["MODULE/test_actions"] == "test_actions"
        assert "TOX_ENV" not in suite_env
        # Also not in root environment
        assert "MODULE/test_charm" not in parsed["environment"]
        assert "TOX_ENV" not in parsed["environment"]

    def test_discovers_test_modules(self, tmp_path: Path) -> None:
        test_dir = tmp_path / "tests" / "integration"
        test_dir.mkdir(parents=True)
        (test_dir / "test_charm.py").write_text("")
        (test_dir / "test_actions.py").write_text("")
        (test_dir / "conftest.py").write_text("")  # not a test module

        spread_path, _ = spread_init(tmp_path)

        parsed = loads_yaml(spread_path.read_text())
        suite_env = parsed["suites"]["tests/integration/"]["environment"]
        assert "MODULE/test_charm" in suite_env
        assert "MODULE/test_actions" in suite_env
        assert not any("conftest" in k for k in suite_env)

    def test_refuses_overwrite_without_force(self, tmp_path: Path) -> None:
        write_file(tmp_path / "spread.yaml", "existing\n")

        with pytest.raises(ConfigurationError, match="already exists"):
            spread_init(tmp_path)

    def test_overwrites_with_force(self, tmp_path: Path) -> None:
        write_file(tmp_path / "spread.yaml", "old\n")
        write_file(tmp_path / "tests" / "integration" / "run" / "task.yaml", "old\n")

        spread_path, task_path = spread_init(tmp_path, force=True)
        assert "integration-test" in spread_path.read_text()
        tox_env_flag = 'opcli pytest expand -e "${TOX_ENV:-integration}"'
        assert tox_env_flag in task_path.read_text()

    def test_project_name_from_directory(self, tmp_path: Path) -> None:
        spread_path, _ = spread_init(tmp_path)
        content = spread_path.read_text()
        assert f"project: {tmp_path.resolve().name}" in content

    def test_generated_suite_has_backends_key(self, tmp_path: Path) -> None:
        """spread_init generates suite with backends: [integration-test] for scoping."""
        write_file(tmp_path / "tests" / "integration" / "test_charm.py", "")
        spread_path, _ = spread_init(tmp_path)

        parsed = loads_yaml(spread_path.read_text())
        suite = parsed["suites"]["tests/integration/"]
        assert "backends" in suite
        assert "integration-test" in suite["backends"]


class TestSpreadExpand:
    """Tests for spread_expand()."""

    def test_missing_spread_yaml_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="not found"):
            spread_expand(tmp_path)

    def test_no_known_virtual_backend_raises(self, tmp_path: Path) -> None:
        """Raises ConfigurationError when no backend with a virtual type is found."""
        spread = """\
project: test-project
backends:
  custom-backend:
    type: adhoc
suites:
  tests/: {}
"""
        write_file(tmp_path / "spread.yaml", spread)

        with pytest.raises(ConfigurationError, match="no backend with a recognised virtual type"):
            spread_expand(tmp_path)

    def test_expand_local(self, tmp_path: Path) -> None:
        write_file(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        result = spread_expand(tmp_path, ci=False)
        parsed = loads_yaml(result)

        assert "integration-test:" not in result
        local = parsed["backends"]["integration-test-local"]
        assert local["type"] == "adhoc"
        assert "lxc launch --vm" in local["allocate"]
        assert "SPREAD_PASSWORD" in local["allocate"]
        assert "lxc delete --force" in local["discard"]
        prepare = local["prepare"]
        assert "opcli install concierge" in prepare
        assert "opcli env provision" in prepare
        assert "opcli env deploy-registry" in prepare
        assert "opcli artifacts push-images" in prepare
        assert "opcli install spread" in prepare
        assert "opcli install tox" in prepare
        # Local uses uv (not pipx) with dev-mode detection, same as CI
        assert "pipx" not in prepare
        assert "uv tool install" in prepare
        assert "UV_TOOL_BIN_DIR=/usr/local/bin" in prepare
        assert "UV_TOOL_DIR=/usr/local/share/uv-tools" in prepare
        assert "pyproject.toml" in prepare
        assert "SPREAD_PATH" in prepare
        assert "loginctl enable-linger ubuntu" in prepare

        # Systems should have username: ubuntu injected
        systems = local["systems"]
        assert len(systems) == 1
        assert "ubuntu-24.04" in systems[0]
        assert systems[0]["ubuntu-24.04"]["username"] == "ubuntu"

    def test_expand_ci(self, tmp_path: Path) -> None:
        write_file(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        result = spread_expand(tmp_path, ci=True)
        parsed = loads_yaml(result)

        assert "integration-test:" not in result
        ci = parsed["backends"]["integration-test-ci"]
        assert ci["type"] == "adhoc"
        assert "ADDRESS localhost" in ci["allocate"]
        assert "chpasswd" in ci["allocate"]
        assert "PasswordAuthentication yes" in ci["allocate"]
        assert "password" not in ci
        assert "concierge" in ci["prepare"]
        # provision prepare runs as root (spread always elevates) with
        # HOME=/home/ubuntu so credentials land in the ubuntu user's home.
        assert "export HOME=/home/ubuntu" in ci["prepare"]
        assert "opcli env provision" in ci["prepare"]
        assert "opcli install tox" in ci["prepare"]
        assert "opcli install spread" in ci["prepare"]
        assert "opcli" in ci["prepare"]
        assert "SPREAD_PATH" in ci["prepare"]
        assert "GITHUB_WORKSPACE" in ci["prepare"]
        assert "chown" in ci["prepare"]
        assert "loginctl enable-linger ubuntu" in ci["prepare"]
        assert "UV_TOOL_BIN_DIR=/usr/local/bin" in ci["prepare"]
        assert "UV_TOOL_DIR=/usr/local/share/uv-tools" in ci["prepare"]
        # CI prepare downloads build artifacts via opcli artifacts fetch
        assert "opcli artifacts fetch" in ci["prepare"]
        assert "artifacts-build" not in ci["prepare"]  # no manual gh download
        assert "built-charm-*" not in ci["prepare"]  # handled by fetch
        assert "GH_TOKEN" in ci["prepare"]
        assert "GITHUB_RUN_ID" in ci["prepare"]
        assert "opcli artifacts localize" not in ci["prepare"]  # fetch does it
        assert "--wait" in ci["prepare"]
        # CI backend has GitHub Actions vars scoped to it for artifact download
        assert "environment" in ci
        ci_env = ci["environment"]
        assert "GITHUB_TOKEN" in ci_env
        assert "GITHUB_RUN_ID" in ci_env
        assert "GITHUB_REPOSITORY" in ci_env
        assert "GITHUB_WORKSPACE" in ci_env
        # CI backend does NOT override SUDO_USER; ubuntu is created in allocate
        assert "SUDO_USER" in ci_env
        assert ci_env["SUDO_USER"] == "ubuntu"
        assert "useradd" in ci["allocate"]
        assert "pipx install" not in ci["prepare"]
        # uv installed in CI prepare (idempotent: already on runner but re-ensures)
        assert "astral-uv" in ci["prepare"]
        # spread installed via opcli install spread
        assert "opcli install spread" in ci["prepare"]
        assert "discard" not in ci
        # CI injects username: root per-system for SSH access
        systems = ci["systems"]
        assert len(systems) == 1
        assert isinstance(systems[0], dict)
        assert systems[0]["ubuntu-24.04"]["username"] == "root"

    def test_preserves_other_sections(self, tmp_path: Path) -> None:
        write_file(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        result = spread_expand(tmp_path, ci=False)

        assert "project: test-project" in result
        assert "MODULE/test_charm" in result
        assert "suites:" in result

    def test_preserves_systems(self, tmp_path: Path) -> None:
        write_file(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        result = spread_expand(tmp_path, ci=False)

        assert "ubuntu-24.04" in result

    def test_preserves_user_defined_backend_fields(self, tmp_path: Path) -> None:
        """User fields in the virtual backend survive expansion."""
        spread_with_extras = """\
project: test-project
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-24.04
    environment:
      EXTRA_VAR: hello
    prepare-each: |
      echo extra setup
    kill-timeout: 30m
environment:
  MODULE/test_charm: test_charm
suites:
  tests/integration/: {}
"""
        write_file(tmp_path / "spread.yaml", spread_with_extras)
        result = spread_expand(tmp_path, ci=False)
        parsed = loads_yaml(result)
        local = parsed["backends"]["integration-test-local"]

        assert local["environment"] == {"SUDO_USER": "ubuntu", "EXTRA_VAR": "hello"}
        assert "extra setup" in local["prepare-each"]
        assert local["kill-timeout"] == "30m"
        # Systems get username injected for local backend
        assert local["systems"] == [{"ubuntu-24.04": {"username": "ubuntu"}}]
        # opcli fields are set
        assert local["type"] == "adhoc"
        assert "lxc launch --vm" in local["allocate"]

    def test_user_prepare_spliced_into_local(self, tmp_path: Path) -> None:
        """User prepare is inserted after provisioning, before final chown."""
        spread_with_prepare = """\
project: test-project
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-24.04
    prepare: |
      echo "user setup step"
      apt-get install -y custom-pkg
environment:
  MODULE/test_charm: test_charm
suites:
  tests/integration/: {}
"""
        write_file(tmp_path / "spread.yaml", spread_with_prepare)
        result = spread_expand(tmp_path, ci=False)
        parsed = loads_yaml(result)
        local = parsed["backends"]["integration-test-local"]
        prepare = local["prepare"]

        # User prepare is present
        assert 'echo "user setup step"' in prepare
        assert "apt-get install -y custom-pkg" in prepare
        # User prepare comes after concierge/provisioning
        assert prepare.index("env provision") < prepare.index("user setup step")
        # User prepare comes before final chown
        assert prepare.index("user setup step") < prepare.index(
            'chown -R ubuntu:ubuntu "${SPREAD_PATH}"'
        )

    def test_user_prepare_spliced_into_ci(self, tmp_path: Path) -> None:
        """User prepare is inserted after concierge, before artifact fetch."""
        spread_with_prepare = """\
project: test-project
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-24.04
    prepare: |
      echo "user ci setup"
environment:
  MODULE/test_charm: test_charm
suites:
  tests/integration/: {}
"""
        write_file(tmp_path / "spread.yaml", spread_with_prepare)
        result = spread_expand(tmp_path, ci=True)
        parsed = loads_yaml(result)
        ci = parsed["backends"]["integration-test-ci"]
        prepare = ci["prepare"]

        # User prepare is present
        assert 'echo "user ci setup"' in prepare
        # User prepare comes after concierge provisioning
        assert prepare.index("env provision") < prepare.index("user ci setup")
        # User prepare comes before artifact fetch
        assert prepare.index("user ci setup") < prepare.index("opcli artifacts fetch")

    def test_no_user_prepare_unchanged(self, tmp_path: Path) -> None:
        """Without a user prepare key, generated prepare is unchanged."""
        write_file(tmp_path / "spread.yaml", _MINIMAL_SPREAD)
        result = spread_expand(tmp_path, ci=False)
        parsed = loads_yaml(result)
        local = parsed["backends"]["integration-test-local"]
        prepare = local["prepare"]

        # Standard parts are present
        assert "opcli env provision" in prepare
        assert 'chown -R ubuntu:ubuntu "${SPREAD_PATH}"' in prepare
        # No doubled newlines from empty user prepare
        assert "\n\n\n" not in prepare

    def test_local_allocate_has_cleanup_trap(self, tmp_path: Path) -> None:
        """The local allocate script must clean up the VM on failure."""
        write_file(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        result = spread_expand(tmp_path, ci=False)
        parsed = loads_yaml(result)
        allocate = parsed["backends"]["integration-test-local"]["allocate"]

        assert "CLEANUP_VM=true" in allocate
        assert "trap cleanup EXIT" in allocate
        assert "CLEANUP_VM=false" in allocate

    def test_local_allocate_waits_for_agent(self, tmp_path: Path) -> None:
        """The local allocate script waits for LXD agent before cloud-init."""
        write_file(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        result = spread_expand(tmp_path, ci=False)
        parsed = loads_yaml(result)
        allocate = parsed["backends"]["integration-test-local"]["allocate"]

        # Agent readiness must come before cloud-init
        agent_pos = allocate.index('lxc exec "${VM_NAME}" -- true')
        cloudinit_pos = allocate.index("cloud-init status --wait")
        assert agent_pos < cloudinit_pos

    def test_auto_detects_ci_env_var(self, tmp_path: Path) -> None:
        """CI env var toggles between ci/local backend expansion."""
        write_file(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        with patch.dict("os.environ", {"CI": "true"}):
            result = spread_expand(tmp_path)
        parsed = loads_yaml(result)
        assert "integration-test-ci" in parsed["backends"]
        ci_backend = parsed["backends"]["integration-test-ci"]
        assert "ADDRESS localhost" in ci_backend["allocate"]

        with patch.dict("os.environ", {"CI": ""}, clear=False):
            result = spread_expand(tmp_path)
        parsed = loads_yaml(result)
        assert "integration-test-local" in parsed["backends"]
        local_backend = parsed["backends"]["integration-test-local"]
        assert "lxc launch --vm" in local_backend["allocate"]

    def test_expanded_is_valid_yaml(self, tmp_path: Path) -> None:
        write_file(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        result = spread_expand(tmp_path, ci=False)

        parsed = loads_yaml(result)
        assert isinstance(parsed, dict)
        assert "backends" in parsed

    def test_local_allocate_uses_ubuntu_user(self, tmp_path: Path) -> None:
        """The allocate script sets up ubuntu user, not root."""
        write_file(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        result = spread_expand(tmp_path, ci=False)
        parsed = loads_yaml(result)
        allocate = parsed["backends"]["integration-test-local"]["allocate"]

        assert "echo ubuntu:${SPREAD_PASSWORD}" in allocate
        assert "PermitRootLogin" not in allocate
        assert "PasswordAuthentication yes" in allocate

    def test_local_prepare_conditional(self, tmp_path: Path) -> None:
        """Prepare script delegates conditionals to opcli subcommands."""
        write_file(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        result = spread_expand(tmp_path, ci=False)
        parsed = loads_yaml(result)
        prepare = parsed["backends"]["integration-test-local"]["prepare"]

        # Conditionals are now internal to the opcli commands
        assert "opcli install concierge" in prepare
        assert "opcli env provision" in prepare
        assert "opcli artifacts push-images" in prepare

    def test_ci_prepare_conditional(self, tmp_path: Path) -> None:
        """CI prepare delegates concierge to opcli subcommand."""
        write_file(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        result = spread_expand(tmp_path, ci=True)
        parsed = loads_yaml(result)
        prepare = parsed["backends"]["integration-test-ci"]["prepare"]

        assert "opcli install concierge" in prepare
        assert "opcli env provision" in prepare
        assert "opcli install tox" in prepare
        assert "SPREAD_PATH" in prepare
        assert "pipx install" not in prepare

    def test_local_username_injection_mapping_systems(self, tmp_path: Path) -> None:
        """Username injection deep-merges; runner is stripped; native fields kept."""
        spread_with_mapping = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-24.04:
          runner: [self-hosted, noble]
          workers: 2
environment:
  MODULE/test_charm: test_charm
suites:
  tests/integration/:
    summary: integration tests
"""
        write_file(tmp_path / "spread.yaml", spread_with_mapping)

        result = spread_expand(tmp_path, ci=False)
        parsed = loads_yaml(result)
        systems = parsed["backends"]["integration-test-local"]["systems"]

        assert len(systems) == 1
        sys_def = systems[0]["ubuntu-24.04"]
        assert sys_def["username"] == "ubuntu"
        # runner is CI-only; stripped from local expansion
        assert "runner" not in sys_def
        # spread-native fields like workers survive
        _EXPECTED_WORKERS = 2  # noqa: N806
        assert sys_def["workers"] == _EXPECTED_WORKERS

    def test_local_username_preserves_user_set_username(self, tmp_path: Path) -> None:
        """If user already set a username, it is not overridden."""
        spread_with_user = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-24.04:
          username: custom-user
environment:
  MODULE/test_charm: test_charm
suites:
  tests/integration/:
    summary: integration tests
"""
        write_file(tmp_path / "spread.yaml", spread_with_user)

        result = spread_expand(tmp_path, ci=False)
        parsed = loads_yaml(result)
        systems = parsed["backends"]["integration-test-local"]["systems"]

        assert systems[0]["ubuntu-24.04"]["username"] == "custom-user"


class TestImplicitBackendType:
    """Implicit type fallback: backend name used as type when type: is absent."""

    def test_name_only_backend_expands_as_virtual(self, tmp_path: Path) -> None:
        """Backend named 'integration-test' with no type: is treated as virtual."""
        spread = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    systems:
      - ubuntu-24.04
suites:
  tests/integration/:
    summary: integration tests
    backends:
      - integration-test
    environment:
      MODULE/test_charm: test_charm
"""
        write_file(tmp_path / "spread.yaml", spread)

        result = spread_expand(tmp_path, ci=False)
        parsed = loads_yaml(result)

        assert "integration-test:" not in result
        assert "integration-test-local" in parsed["backends"]
        assert parsed["backends"]["integration-test-local"]["type"] == "adhoc"

    def test_unknown_name_no_type_is_not_expanded(self, tmp_path: Path) -> None:
        """A backend with an unrecognised name and no type: is not a virtual backend."""
        spread = """\
project: test-project
path: /home/ubuntu/proj
backends:
  my-custom:
    systems:
      - ubuntu-24.04
  integration-test:
    type: integration-test
    systems:
      - ubuntu-24.04
suites:
  tests/integration/:
    summary: integration tests
    backends:
      - integration-test
    environment:
      MODULE/test_charm: test_charm
"""
        write_file(tmp_path / "spread.yaml", spread)

        result = spread_expand(tmp_path, ci=False)
        parsed = loads_yaml(result)

        # my-custom has no type: so its name is the type — not a virtual type
        assert "my-custom" in parsed["backends"]
        assert "my-custom-local" not in parsed["backends"]

    def test_non_string_type_field_falls_back_to_name(self, tmp_path: Path) -> None:
        """A non-string type: value is ignored; backend name used as type fallback."""
        spread = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    type: [integration-test]
    systems:
      - ubuntu-24.04
suites:
  tests/integration/:
    summary: integration tests
    backends:
      - integration-test
    environment:
      MODULE/test_charm: test_charm
"""
        write_file(tmp_path / "spread.yaml", spread)

        # type: is a list (not a string) — falls back to backend name "integration-test"
        result = spread_expand(tmp_path, ci=False)
        parsed = loads_yaml(result)

        assert "integration-test-local" in parsed["backends"]

    def test_concrete_name_collision_raises(self, tmp_path: Path) -> None:
        """Expanding a virtual backend whose concrete name already exists raises."""
        spread = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-24.04
  integration-test-local:
    type: adhoc
    systems:
      - ubuntu-24.04
suites:
  tests/integration/:
    summary: integration tests
    backends:
      - integration-test
    environment:
      MODULE/test_charm: test_charm
"""
        write_file(tmp_path / "spread.yaml", spread)

        with pytest.raises(ConfigurationError, match=r"concrete name.*already exists"):
            spread_expand(tmp_path, ci=False)


class TestSystemResourceFields:
    """Tests for cpu/memory/disk/runner handling in virtual backend system entries."""

    _SPREAD_WITH_RESOURCES = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-24.04:
          cpu: 2
          memory: 4
          disk: 30
environment:
  MODULE/test_charm: test_charm
suites:
  tests/integration/:
    summary: integration tests
    backends:
      - integration-test
"""

    def test_resources_appear_in_local_allocate(self, tmp_path: Path) -> None:
        """cpu/memory/disk from system entry appear as case-arm in local allocate."""
        write_file(tmp_path / "spread.yaml", self._SPREAD_WITH_RESOURCES)

        result = spread_expand(tmp_path, ci=False)
        parsed = loads_yaml(result)
        allocate = parsed["backends"]["integration-test-local"]["allocate"]

        assert "ubuntu-24.04" in allocate
        assert 'CPU="${CPU:-2}"' in allocate
        assert 'MEM="${MEM:-4}"' in allocate
        assert 'DISK="${DISK:-30}"' in allocate

    def test_resources_stripped_from_local_systems(self, tmp_path: Path) -> None:
        """cpu/memory/disk are removed from system entries in local expansion."""
        write_file(tmp_path / "spread.yaml", self._SPREAD_WITH_RESOURCES)

        result = spread_expand(tmp_path, ci=False)
        parsed = loads_yaml(result)
        local_backend = parsed["backends"]["integration-test-local"]
        sys_def = local_backend["systems"][0]["ubuntu-24.04"]

        assert "cpu" not in sys_def
        assert "memory" not in sys_def
        assert "disk" not in sys_def
        assert sys_def["username"] == "ubuntu"

    def test_resources_stripped_from_ci_systems(self, tmp_path: Path) -> None:
        """cpu/memory/disk are removed from system entries in CI expansion."""
        write_file(tmp_path / "spread.yaml", self._SPREAD_WITH_RESOURCES)

        result = spread_expand(tmp_path, ci=True)
        parsed = loads_yaml(result)
        # After stripping resource keys, only username: ubuntu remains
        systems = parsed["backends"]["integration-test-ci"]["systems"]
        assert len(systems) == 1
        assert isinstance(systems[0], dict)
        sys_props = systems[0]["ubuntu-24.04"]
        assert "cpu" not in sys_props
        assert "memory" not in sys_props
        assert "disk" not in sys_props
        assert sys_props.get("username") == "root"

    def test_runner_stripped_from_local_systems(self, tmp_path: Path) -> None:
        """Runner label is stripped from local system entries (CI-only field)."""
        spread = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-24.04:
          runner: [self-hosted, noble]
environment:
  MODULE/test_charm: test_charm
suites:
  tests/integration/:
    summary: integration tests
"""
        write_file(tmp_path / "spread.yaml", spread)

        result = spread_expand(tmp_path, ci=False)
        parsed = loads_yaml(result)
        local_b = parsed["backends"]["integration-test-local"]
        sys_def = local_b["systems"][0]["ubuntu-24.04"]

        assert "runner" not in sys_def
        assert sys_def["username"] == "ubuntu"

    def test_runner_stripped_from_ci_systems(self, tmp_path: Path) -> None:
        """Runner label is stripped from CI system entries (GitHub Actions only)."""
        spread = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-24.04:
          runner: [self-hosted, noble]
environment:
  MODULE/test_charm: test_charm
suites:
  tests/integration/:
    summary: integration tests
"""
        write_file(tmp_path / "spread.yaml", spread)

        result = spread_expand(tmp_path, ci=True)
        parsed = loads_yaml(result)
        systems = parsed["backends"]["integration-test-ci"]["systems"]

        assert len(systems) == 1
        sys_def = systems[0]["ubuntu-24.04"]
        assert "runner" not in sys_def
        assert sys_def.get("username") == "root"

    def test_multiple_systems_with_different_resources(self, tmp_path: Path) -> None:
        """Each system gets its own case arm in the allocate preamble."""
        spread = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-22.04:
          cpu: 2
          memory: 4
          disk: 20
      - ubuntu-24.04:
          cpu: 8
          memory: 16
          disk: 50
environment:
  MODULE/test_charm: test_charm
suites:
  tests/integration/:
    summary: integration tests
"""
        write_file(tmp_path / "spread.yaml", spread)

        result = spread_expand(tmp_path, ci=False)
        parsed = loads_yaml(result)
        allocate = parsed["backends"]["integration-test-local"]["allocate"]

        assert "ubuntu-22.04" in allocate
        assert "ubuntu-24.04" in allocate
        assert 'CPU="${CPU:-2}"' in allocate
        assert 'CPU="${CPU:-8}"' in allocate

    def test_env_var_overrides_system_resource(self, tmp_path: Path) -> None:
        """Per-system case arms use :- so explicit env vars still win."""
        write_file(tmp_path / "spread.yaml", self._SPREAD_WITH_RESOURCES)

        result = spread_expand(tmp_path, ci=False)
        parsed = loads_yaml(result)
        allocate = parsed["backends"]["integration-test-local"]["allocate"]

        # Each arm must use ${VAR:-value} not bare assignment
        assert 'CPU="${CPU:-' in allocate
        assert 'MEM="${MEM:-' in allocate
        assert 'DISK="${DISK:-' in allocate

    def test_invalid_resource_value_raises(self, tmp_path: Path) -> None:
        """Non-positive-integer resource value raises ValidationError at expand time."""
        spread = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-24.04:
          cpu: -1
environment:
  MODULE/test_charm: test_charm
suites:
  tests/integration/:
    summary: integration tests
"""
        write_file(tmp_path / "spread.yaml", spread)

        with pytest.raises(ValidationError, match="positive integer"):
            spread_expand(tmp_path, ci=False)

    def test_no_resources_no_preamble(self, tmp_path: Path) -> None:
        """When no resources are declared, no case statement is prepended."""
        write_file(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        result = spread_expand(tmp_path, ci=False)
        parsed = loads_yaml(result)
        allocate = parsed["backends"]["integration-test-local"]["allocate"]

        assert "case" not in allocate
        # Fallback defaults still present
        assert 'DISK="${DISK:-20}"' in allocate

    def test_boolean_resource_value_raises(self, tmp_path: Path) -> None:
        """Boolean values must be rejected (bool is a subclass of int in Python)."""
        spread = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-24.04:
          cpu: true
environment:
  MODULE/test_charm: test_charm
suites:
  tests/integration/:
    summary: integration tests
"""
        write_file(tmp_path / "spread.yaml", spread)

        with pytest.raises(ValidationError, match="positive integer"):
            spread_expand(tmp_path, ci=False)

    def test_case_pattern_is_quoted(self, tmp_path: Path) -> None:
        """Case arm patterns must be quoted to prevent shell glob expansion."""
        write_file(tmp_path / "spread.yaml", self._SPREAD_WITH_RESOURCES)

        result = spread_expand(tmp_path, ci=False)
        parsed = loads_yaml(result)
        allocate = parsed["backends"]["integration-test-local"]["allocate"]

        # Pattern must be quoted: "ubuntu-24.04") not ubuntu-24.04)
        assert '"ubuntu-24.04")' in allocate


class TestSpreadRun:
    def test_runs_spread_from_temp_subdir(self, tmp_path: Path) -> None:
        """Spread is invoked from a temp subdirectory inside the project root."""
        write_file(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        captured_cwd: list[str] = []

        def capture_cmd(cmd: list[str], **kwargs: object) -> None:
            captured_cwd.append(str(kwargs.get("cwd", "")))

        with patch("opcli.core.spread.run_command", side_effect=capture_cmd):
            spread_run(tmp_path, ci=False)

        assert len(captured_cwd) == 1
        cwd = Path(captured_cwd[0])
        # Must be inside the project root, not the root itself
        assert cwd.parent == tmp_path
        assert cwd != tmp_path

    def test_uses_spread_binary(self, tmp_path: Path) -> None:
        write_file(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        with patch("opcli.core.spread.run_command") as mock_run:
            spread_run(tmp_path, ci=False)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "spread"
        assert not any(arg.startswith("-spread=") for arg in cmd)

    def test_spread_yaml_in_temp_subdir_has_reroot(self, tmp_path: Path) -> None:
        """The temp spread.yaml must contain reroot pointing to the project root."""
        write_file(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        captured_yaml: list[dict[str, object]] = []

        def capture_cmd(cmd: list[str], **kwargs: object) -> None:
            cwd = kwargs.get("cwd", "")
            tmp_yaml = Path(str(cwd)) / "spread.yaml"
            captured_yaml.append(load_yaml(tmp_yaml))

        with patch("opcli.core.spread.run_command", side_effect=capture_cmd):
            spread_run(tmp_path, ci=False)

        assert len(captured_yaml) == 1
        written = captured_yaml[0]
        assert "integration-test-local" in written["backends"]
        assert "integration-test" not in written["backends"]
        assert written.get("reroot") == ".."

    def test_original_spread_yaml_never_modified(self, tmp_path: Path) -> None:
        write_file(tmp_path / "spread.yaml", _MINIMAL_SPREAD)
        original_content = (tmp_path / "spread.yaml").read_text()

        with patch("opcli.core.spread.run_command"):
            spread_run(tmp_path, ci=False)

        assert (tmp_path / "spread.yaml").read_text() == original_content

    def test_original_spread_yaml_not_modified_on_failure(self, tmp_path: Path) -> None:
        write_file(tmp_path / "spread.yaml", _MINIMAL_SPREAD)
        original_content = (tmp_path / "spread.yaml").read_text()

        def failing_cmd(cmd: list[str], **kwargs: object) -> None:
            raise SubprocessError(cmd=cmd, returncode=1, stderr="spread failed")

        with (
            patch("opcli.core.spread.run_command", side_effect=failing_cmd),
            pytest.raises(SubprocessError),
        ):
            spread_run(tmp_path, ci=False)

        assert (tmp_path / "spread.yaml").read_text() == original_content

    def test_temp_dir_cleaned_up_on_success(self, tmp_path: Path) -> None:
        write_file(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        with patch("opcli.core.spread.run_command"):
            spread_run(tmp_path, ci=False)

        leftover = list(tmp_path.glob(".spread-run-*"))
        assert leftover == []

    def test_extra_args_forwarded(self, tmp_path: Path) -> None:
        write_file(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        selector = "integration-test-local:ubuntu-24.04:tests/integration/run:test_charm"
        with patch("opcli.core.spread.run_command") as mock_run:
            spread_run(
                tmp_path,
                extra_args=["-v", selector],
                ci=False,
            )

        cmd = mock_run.call_args[0][0]
        assert cmd == ["spread", "-v", selector]

    def test_expand_output_has_no_reroot(self, tmp_path: Path) -> None:
        """spread_expand() for display should not include reroot."""
        write_file(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        result = spread_expand(tmp_path, ci=False)
        parsed = loads_yaml(result)
        assert "reroot" not in parsed

    def test_missing_spread_yaml_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="not found"):
            spread_run(tmp_path)


class TestSpreadRunSecrets:
    """Tests for secrets env integration in spread_run()."""

    def test_secrets_env_passed_in_local_mode(self, tmp_path: Path) -> None:
        """In local mode, .secrets.env vars are passed to run_command."""
        write_file(tmp_path / "spread.yaml", _MINIMAL_SPREAD)
        (tmp_path / ".secrets.env").write_text("MY_SECRET=hunter2\n")

        with patch("opcli.core.spread.run_command") as mock_run:
            spread_run(tmp_path, ci=False)

        kwargs = mock_run.call_args[1]
        assert kwargs["env"] == {"MY_SECRET": "hunter2"}

    def test_secrets_env_not_loaded_in_ci_mode(self, tmp_path: Path) -> None:
        """In CI mode, .secrets.env is not loaded (vars come from environment)."""
        write_file(tmp_path / "spread.yaml", _MINIMAL_SPREAD)
        (tmp_path / ".secrets.env").write_text("MY_SECRET=hunter2\n")

        with patch("opcli.core.spread.run_command") as mock_run:
            spread_run(tmp_path, ci=True)

        kwargs = mock_run.call_args[1]
        assert kwargs.get("env") is None

    def test_no_secrets_file_passes_none(self, tmp_path: Path) -> None:
        """When .secrets.env doesn't exist, env=None is passed."""
        write_file(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        with patch("opcli.core.spread.run_command") as mock_run:
            spread_run(tmp_path, ci=False)

        kwargs = mock_run.call_args[1]
        assert kwargs.get("env") is None


class TestGeneratedSuiteBackends:
    """Tests for generated suite backend references."""

    def test_generated_suite_backends_replaced_after_expand(self, tmp_path: Path) -> None:
        """After expansion, suite backends reference the concrete backend name."""
        _, _ = spread_init(tmp_path)

        result = spread_expand(tmp_path, ci=False)
        parsed = loads_yaml(result)

        suite = parsed["suites"]["tests/integration/"]
        assert "integration-test" not in suite.get("backends", [])
        assert "integration-test-local" in suite["backends"]

    def test_generated_spread_yaml_has_type_field(self, tmp_path: Path) -> None:
        """spread_init generates a backend with type: integration-test."""
        write_file(tmp_path / "tests" / "integration" / "test_charm.py", "")
        spread_path, _ = spread_init(tmp_path)

        parsed = loads_yaml(spread_path.read_text())
        assert parsed["backends"]["integration-test"]["type"] == "integration-test"

    def test_custom_backend_name_with_integration_test_type(self, tmp_path: Path) -> None:
        """A user-defined backend name with type: integration-test is expanded."""
        spread = """\
project: test-project
path: /home/ubuntu/proj
backends:
  my-k8s-backend:
    type: integration-test
    systems:
      - ubuntu-24.04
suites:
  tests/integration/:
    summary: integration tests
    backends:
      - my-k8s-backend
    environment:
      MODULE/test_charm: test_charm
"""
        write_file(tmp_path / "spread.yaml", spread)

        result = spread_expand(tmp_path, ci=False)
        parsed = loads_yaml(result)

        assert "my-k8s-backend:" not in result
        assert "my-k8s-backend-local" in parsed["backends"]
        assert parsed["backends"]["my-k8s-backend-local"]["type"] == "adhoc"
        suite_backends = parsed["suites"]["tests/integration/"]["backends"]
        assert suite_backends == ["my-k8s-backend-local"]

    def test_multiple_virtual_backends_same_type(self, tmp_path: Path) -> None:
        """Multiple backends with the same virtual type expand independently."""
        spread = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-24.04
  integration-test-arm:
    type: integration-test
    systems:
      - ubuntu-24.04:
          runner: [self-hosted, arm64]
suites:
  tests/integration/:
    summary: integration tests
    backends:
      - integration-test
      - integration-test-arm
    environment:
      MODULE/test_charm: test_charm
"""
        write_file(tmp_path / "spread.yaml", spread)

        result = spread_expand(tmp_path, ci=False)
        parsed = loads_yaml(result)

        assert "integration-test-local" in parsed["backends"]
        assert "integration-test-arm-local" in parsed["backends"]
        suite_backends = parsed["suites"]["tests/integration/"]["backends"]
        assert "integration-test-local" in suite_backends
        assert "integration-test-arm-local" in suite_backends


_SPREAD_WITH_RUNNER = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-22.04:
          runner: ubuntu-22.04-runner
      - ubuntu-24.04:
          runner: [self-hosted, ubuntu-24.04]
environment:
  CONCIERGE: concierge.yaml
suites:
  tests/integration/:
    summary: integration tests
    backends:
      - integration-test
    environment:
      MODULE/test_charm: test_charm
      MODULE/test_other: test_other
"""

_SPREAD_NO_RUNNER = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-24.04
environment:
  CONCIERGE: concierge.yaml
suites:
  tests/integration/:
    summary: integration tests
    backends:
      - integration-test
    environment:
      MODULE/test_charm: test_charm
"""


class TestVirtualRunnerMap:
    """Tests for _virtual_runner_map()."""

    def test_string_runner_label(self) -> None:
        """Runner string label is JSON-encoded in result."""
        raw = loads_yaml(_SPREAD_WITH_RUNNER)
        runner_map, _, _ = _virtual_runner_map(raw)
        assert runner_map["ubuntu-22.04"] == '"ubuntu-22.04-runner"'

    def test_list_runner_label(self) -> None:
        """Runner list is JSON-encoded when system uses a list."""
        raw = loads_yaml(_SPREAD_WITH_RUNNER)
        runner_map, _, _ = _virtual_runner_map(raw)
        assert runner_map["ubuntu-24.04"] == json.dumps(["self-hosted", "ubuntu-24.04"])

    def test_no_runner_defaults_to_ubuntu_latest(self) -> None:
        """Systems without runner: default to JSON-encoded ubuntu-latest."""
        raw = loads_yaml(_SPREAD_NO_RUNNER)
        runner_map, _, _ = _virtual_runner_map(raw)
        assert runner_map.get("ubuntu-24.04") == '"ubuntu-latest"'

    def test_empty_backends(self) -> None:
        """No backends → empty runner map and no CI names."""
        runner_map, arch_map, ci_names = _virtual_runner_map({})
        assert runner_map == {}
        assert arch_map == {}
        assert ci_names == []

    def test_ci_backend_names_derived(self) -> None:
        """CI backend names are {virtual_name}-ci for each virtual backend."""
        raw = loads_yaml(_SPREAD_WITH_RUNNER)
        _, _, ci_names = _virtual_runner_map(raw)
        assert ci_names == ["integration-test-ci"]

    def test_non_virtual_backend_ignored(self) -> None:
        """Backends without a recognised virtual type are excluded."""
        raw = loads_yaml("""\
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-24.04
  lxd:
    systems:
      - ubuntu-22.04:
          runner: some-runner
""")
        runner_map, _, ci_names = _virtual_runner_map(raw)
        assert "ubuntu-24.04" in runner_map
        assert "ubuntu-22.04" not in runner_map
        assert ci_names == ["integration-test-ci"]

    def test_explicit_arch_returned(self) -> None:
        """Explicit arch: field on a system entry is captured in arch_map."""
        raw = loads_yaml("""\
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-24.04-arm64:
          runner: ["ubuntu-24.04-arm"]
          arch: arm64
""")
        _, arch_map, _ = _virtual_runner_map(raw)
        assert arch_map.get("ubuntu-24.04-arm64") == "arm64"

    def test_no_arch_field_returns_none(self) -> None:
        """Systems without an arch: field have None in arch_map."""
        raw = loads_yaml(_SPREAD_NO_RUNNER)
        _, arch_map, _ = _virtual_runner_map(raw)
        assert arch_map.get("ubuntu-24.04") is None

    def test_non_string_arch_falls_back_to_none(self) -> None:
        """Non-string arch values (e.g. integers) are ignored; arch_map gets None."""
        raw = loads_yaml("""\
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-24.04:
          arch: 123
""")
        _, arch_map, _ = _virtual_runner_map(raw)
        assert arch_map.get("ubuntu-24.04") is None

    def test_string_system_entry_arch_map_is_none(self) -> None:
        """String-format system entries (no props) get None in arch_map."""
        raw = loads_yaml("""\
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-24.04
""")
        _, arch_map, _ = _virtual_runner_map(raw)
        assert arch_map.get("ubuntu-24.04") is None


class TestArchFromRunner:
    """Tests for _arch_from_runner()."""

    def test_ubuntu_latest_returns_amd64(self) -> None:
        assert _arch_from_runner('"ubuntu-latest"') == "amd64"

    def test_arm64_label_in_list_returns_arm64(self) -> None:
        assert _arch_from_runner(json.dumps(["self-hosted", "arm64"])) == "arm64"

    def test_arm64_string_returns_arm64(self) -> None:
        assert _arch_from_runner('"arm64"') == "arm64"

    def test_non_arm64_list_returns_amd64(self) -> None:
        assert _arch_from_runner(json.dumps(["self-hosted", "ubuntu-24.04"])) == "amd64"

    def test_invalid_json_returns_amd64(self) -> None:
        assert _arch_from_runner("not-json") == "amd64"


class TestSpreadTasks:
    """Tests for spread_jobs()."""

    _SPREAD_LIST_TWO_VARIANTS = (
        "integration-test-ci:ubuntu-22.04:tests/integration/run:test_charm\n"
        "integration-test-ci:ubuntu-22.04:tests/integration/run:test_other\n"
        "integration-test-ci:ubuntu-24.04:tests/integration/run:test_charm\n"
        "integration-test-ci:ubuntu-24.04:tests/integration/run:test_other\n"
    )
    _SPREAD_LIST_ONE_VARIANT = (
        "integration-test-ci:ubuntu-24.04:tests/integration/run:test_charm\n"
    )
    _SPREAD_LIST_NO_VARIANT = "integration-test-ci:ubuntu-24.04:tests/integration/run\n"

    def _mock_list(self, stdout: str) -> SubprocessResult:
        return SubprocessResult(stdout=stdout, stderr="", returncode=0)

    def test_returns_selectors_for_each_variant(self, tmp_path: Path) -> None:
        """Returns one entry per (system, task_dir, variant) combination."""
        write_file(tmp_path / "spread.yaml", _SPREAD_WITH_RUNNER)

        with patch(
            "opcli.core.spread.run_command",
            return_value=self._mock_list(self._SPREAD_LIST_TWO_VARIANTS),
        ):
            entries = spread_jobs(tmp_path)

        names = [e["name"] for e in entries]
        assert "integration-test-ci:ubuntu-22.04:tests/integration/run:test_charm" in names
        assert "integration-test-ci:ubuntu-22.04:tests/integration/run:test_other" in names

    def test_selector_format(self, tmp_path: Path) -> None:
        """Selector is taken verbatim from spread -list output."""
        write_file(tmp_path / "spread.yaml", _SPREAD_NO_RUNNER)
        raw_selector = "integration-test-ci:ubuntu-24.04:tests/integration/run:test_charm"

        with patch(
            "opcli.core.spread.run_command",
            return_value=self._mock_list(raw_selector + "\n"),
        ):
            entries = spread_jobs(tmp_path)

        assert len(entries) == 1
        assert entries[0]["selector"] == raw_selector

    def test_runs_on_from_runner_field(self, tmp_path: Path) -> None:
        """runs-on matches the system's runner: label (JSON-encoded)."""
        write_file(tmp_path / "spread.yaml", _SPREAD_WITH_RUNNER)

        with patch(
            "opcli.core.spread.run_command",
            return_value=self._mock_list(self._SPREAD_LIST_TWO_VARIANTS),
        ):
            entries = spread_jobs(tmp_path)

        ubuntu_22_entries = [e for e in entries if "ubuntu-22.04" in e["selector"]]
        assert all(e["runs-on"] == '"ubuntu-22.04-runner"' for e in ubuntu_22_entries)

    def test_no_variants_name_is_full_selector(self, tmp_path: Path) -> None:
        """When spread -list returns no variant, name is the full selector."""
        write_file(tmp_path / "spread.yaml", _SPREAD_NO_RUNNER)

        with patch(
            "opcli.core.spread.run_command",
            return_value=self._mock_list(self._SPREAD_LIST_NO_VARIANT),
        ):
            entries = spread_jobs(tmp_path)

        assert len(entries) == 1
        assert entries[0]["name"] == "integration-test-ci:ubuntu-24.04:tests/integration/run"

    def test_missing_spread_yaml_raises(self, tmp_path: Path) -> None:
        """Raises ConfigurationError when spread.yaml is missing."""
        with pytest.raises(ConfigurationError):
            spread_jobs(tmp_path)

    def test_spread_list_called_with_ci_backend_selectors(self, tmp_path: Path) -> None:
        """Spread -list is invoked with one selector per virtual backend."""
        write_file(tmp_path / "spread.yaml", _SPREAD_NO_RUNNER)

        with patch(
            "opcli.core.spread.run_command",
            return_value=self._mock_list(self._SPREAD_LIST_ONE_VARIANT),
        ) as mock_run:
            spread_jobs(tmp_path)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "spread"
        assert cmd[1] == "-list"
        assert "integration-test-ci:" in cmd

    def test_spread_list_excludes_non_virtual_backends(self, tmp_path: Path) -> None:
        """Non-virtual backends are not passed as selectors to spread -list."""
        spread_mixed = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-24.04
  lxd:
    systems:
      - ubuntu-22.04
suites:
  tests/integration/:
    summary: integration tests
    backends:
      - integration-test
"""
        write_file(tmp_path / "spread.yaml", spread_mixed)

        with patch(
            "opcli.core.spread.run_command",
            return_value=self._mock_list(self._SPREAD_LIST_ONE_VARIANT),
        ) as mock_run:
            spread_jobs(tmp_path)

        cmd = mock_run.call_args[0][0]
        assert "integration-test-ci:" in cmd
        assert not any("lxd" in arg for arg in cmd)

    def test_ci_backend_has_username_root(self, tmp_path: Path) -> None:
        """Expanded CI backend sets username: root per system for SSH."""
        write_file(tmp_path / "spread.yaml", _SPREAD_NO_RUNNER)

        result = spread_expand(tmp_path, ci=True)
        parsed = loads_yaml(result)

        ci_backend = parsed["backends"].get("integration-test-ci")
        assert ci_backend is not None
        systems = ci_backend.get("systems", [])
        assert len(systems) > 0
        # username is set per-system entry
        for system_entry in systems:
            if isinstance(system_entry, dict):
                for _sys_name, sys_props in system_entry.items():
                    assert isinstance(sys_props, dict)
                    assert sys_props.get("username") == "root"

    def test_ci_backend_strips_runner_field(self, tmp_path: Path) -> None:
        """Expanded CI backend does not contain runner: key in systems."""
        write_file(tmp_path / "spread.yaml", _SPREAD_WITH_RUNNER)

        result = spread_expand(tmp_path, ci=True)
        parsed = loads_yaml(result)

        ci_backend = parsed["backends"].get("integration-test-ci")
        assert ci_backend is not None
        for system_entry in ci_backend.get("systems", []):
            if isinstance(system_entry, dict):
                for _sys_name, sys_props in system_entry.items():
                    if isinstance(sys_props, dict):
                        assert "runner" not in sys_props

    def test_ci_backend_strips_arch_field(self, tmp_path: Path) -> None:
        """Expanded CI backend does not contain arch: key in systems."""
        spread_with_arch = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-24.04-arm64:
          runner: ["ubuntu-24.04-arm"]
          arch: arm64
suites:
  tests/integration/:
    summary: integration tests
    backends:
      - integration-test
"""
        write_file(tmp_path / "spread.yaml", spread_with_arch)

        result = spread_expand(tmp_path, ci=True)
        parsed = loads_yaml(result)

        ci_backend = parsed["backends"].get("integration-test-ci")
        assert ci_backend is not None
        for system_entry in ci_backend.get("systems", []):
            if isinstance(system_entry, dict):
                for _sys_name, sys_props in system_entry.items():
                    if isinstance(sys_props, dict):
                        assert "arch" not in sys_props

    def test_arch_field_explicit_overrides_runner_label(self, tmp_path: Path) -> None:
        """An explicit arch: field takes precedence over deriving arch from runner."""
        spread_arm = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-24.04-arm64:
          runner: ["ubuntu-24.04-arm"]
          arch: arm64
suites:
  tests/integration/:
    summary: integration tests
    backends:
      - integration-test
"""
        write_file(tmp_path / "spread.yaml", spread_arm)

        with patch(
            "opcli.core.spread.run_command",
            return_value=self._mock_list(
                "integration-test-ci:ubuntu-24.04-arm64:tests/integration/run:test_charm\n"
            ),
        ):
            entries = spread_jobs(tmp_path)

        assert len(entries) == 1
        assert entries[0]["arch"] == "arm64"

        """Entries without arm64 runner label get arch=amd64."""
        write_file(tmp_path / "spread.yaml", _SPREAD_NO_RUNNER)

        with patch(
            "opcli.core.spread.run_command",
            return_value=self._mock_list(self._SPREAD_LIST_ONE_VARIANT),
        ):
            entries = spread_jobs(tmp_path)

        assert all(e["arch"] == "amd64" for e in entries)

    def test_arch_field_arm64_from_runner(self, tmp_path: Path) -> None:
        """Entries whose runner label contains arm64 get arch=arm64."""
        spread_arm = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-24.04:
          runner: [self-hosted, arm64]
suites:
  tests/integration/:
    summary: integration tests
    backends:
      - integration-test
"""
        write_file(tmp_path / "spread.yaml", spread_arm)

        with patch(
            "opcli.core.spread.run_command",
            return_value=self._mock_list(
                "integration-test-ci:ubuntu-24.04:tests/integration/run:test_charm\n"
            ),
        ):
            entries = spread_jobs(tmp_path)

        assert all(e["arch"] == "arm64" for e in entries)

    def test_duplicate_variant_keys_produce_distinct_selectors(self, tmp_path: Path) -> None:
        """Key != value in MODULE/* produces distinct selectors (the original bug)."""
        write_file(tmp_path / "spread.yaml", _SPREAD_NO_RUNNER)
        list_output = (
            "integration-test-ci:ubuntu-24.04:tests/integration/run:test_charm\n"
            "integration-test-ci:ubuntu-24.04:tests/integration/run:test_charm_k8s\n"
        )

        with patch(
            "opcli.core.spread.run_command",
            return_value=self._mock_list(list_output),
        ):
            entries = spread_jobs(tmp_path)

        selectors = [e["selector"] for e in entries]
        names = [e["name"] for e in entries]
        assert len(set(selectors)) == len(entries)
        assert "integration-test-ci:ubuntu-24.04:tests/integration/run:test_charm" in names
        assert "integration-test-ci:ubuntu-24.04:tests/integration/run:test_charm_k8s" in names

    def test_temp_dir_cleaned_up_after_tasks(self, tmp_path: Path) -> None:
        """Temporary spread.yaml directory is removed after spread_jobs returns."""
        write_file(tmp_path / "spread.yaml", _SPREAD_NO_RUNNER)

        with patch(
            "opcli.core.spread.run_command",
            return_value=self._mock_list(self._SPREAD_LIST_ONE_VARIANT),
        ):
            spread_jobs(tmp_path)

        leftover = list(tmp_path.glob(".spread-jobs-*"))
        assert leftover == []
