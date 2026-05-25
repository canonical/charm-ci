# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Tests for pytest-invocation-mode key and its effect on pytest commands."""

from pathlib import Path
from unittest.mock import patch

import pytest

from opcli.core.exceptions import ConfigurationError
from opcli.core.pytest_args import assemble_tox_argv, pytest_run
from opcli.core.spread import get_pytest_invocation_mode, spread_expand
from tests.conftest import write_file

_SPREAD_YAML_PFE = """\
project: myproject
path: /home/ubuntu/proj
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-24.04
"""

_SPREAD_YAML_OBSERVABILITY = """\
project: myproject
path: /home/ubuntu/proj
backends:
  integration-test:
    type: integration-test
    pytest-invocation-mode: observability
    systems:
      - ubuntu-24.04
"""

_SPREAD_YAML_INVALID_MODE = """\
project: myproject
path: /home/ubuntu/proj
backends:
  integration-test:
    type: integration-test
    pytest-invocation-mode: invalid-team
    systems:
      - ubuntu-24.04
"""

_SINGLE_CHARM_BUILD = """\
version: 1
rocks: []
charms:
- name: traefik-k8s
  charmcraft-yaml: charmcraft.yaml
  builds:
  - arch: amd64
    path: traefik-k8s_ubuntu-22.04-amd64.charm
snaps: []
"""


class TestGetPytestInvocationMode:
    """Tests for get_pytest_invocation_mode()."""

    def test_default_pfe_when_no_spread_yaml(self, tmp_path: Path) -> None:
        assert get_pytest_invocation_mode(tmp_path) == "pfe"

    def test_default_pfe_when_key_absent(self, tmp_path: Path) -> None:
        write_file(tmp_path / "spread.yaml", _SPREAD_YAML_PFE)
        assert get_pytest_invocation_mode(tmp_path) == "pfe"

    def test_observability_mode(self, tmp_path: Path) -> None:
        write_file(tmp_path / "spread.yaml", _SPREAD_YAML_OBSERVABILITY)
        assert get_pytest_invocation_mode(tmp_path) == "observability"

    def test_invalid_mode_raises(self, tmp_path: Path) -> None:
        write_file(tmp_path / "spread.yaml", _SPREAD_YAML_INVALID_MODE)
        with pytest.raises(ConfigurationError, match="Invalid pytest-invocation-mode"):
            get_pytest_invocation_mode(tmp_path)


class TestAssembleToxArgvWithMode:
    """Tests for assemble_tox_argv respecting pytest-invocation-mode."""

    def test_pfe_mode_includes_charm_file(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)
        write_file(tmp_path / "spread.yaml", _SPREAD_YAML_PFE)

        with patch("opcli.core.pytest_args.current_arch", return_value="amd64"):
            argv = assemble_tox_argv(tmp_path, mode="pfe")

        joined = " ".join(argv)
        assert "--charm-file=" in joined

    def test_observability_mode_no_charm_file(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)
        write_file(tmp_path / "spread.yaml", _SPREAD_YAML_OBSERVABILITY)

        argv = assemble_tox_argv(tmp_path, mode="observability")

        joined = " ".join(argv)
        assert "--charm-file" not in joined
        assert argv == ["tox", "-e", "integration"]

    def test_observability_mode_with_extra_args(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)

        argv = assemble_tox_argv(tmp_path, mode="observability", extra_args=["-k", "test_foo"])

        assert argv == ["tox", "-e", "integration", "--", "-k", "test_foo"]


class TestPytestRunObservabilityMode:
    """Tests for pytest_run in observability mode."""

    def test_sets_charm_path_env(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)
        write_file(tmp_path / "spread.yaml", _SPREAD_YAML_OBSERVABILITY)

        with (
            patch("opcli.core.pytest_args.current_arch", return_value="amd64"),
            patch("opcli.core.artifacts.current_arch", return_value="amd64"),
            patch("opcli.core.pytest_args.run_command") as mock_run,
        ):
            pytest_run(tmp_path, ci=True)

        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args
        env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
        assert env is not None
        assert "CHARM_PATH" in env
        assert "traefik-k8s" in env["CHARM_PATH"]


class TestSpreadExpandStripsKey:
    """Test that pytest-invocation-mode is stripped during backend expansion."""

    def test_key_stripped_from_expanded_yaml(self, tmp_path: Path) -> None:
        write_file(tmp_path / "spread.yaml", _SPREAD_YAML_OBSERVABILITY)

        expanded = spread_expand(tmp_path)
        # The expanded YAML should not contain pytest-invocation-mode
        assert "pytest-invocation-mode" not in expanded
