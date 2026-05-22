# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Tests for ``opcli pytest expand`` and ``opcli pytest run``."""

import logging
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from opcli.core.exceptions import ConfigurationError
from opcli.core.pytest_args import assemble_pytest_args, assemble_tox_argv, pytest_run
from tests.conftest import write_file

_V1_ERROR_MATCH = "validation error"


# ---------------------------------------------------------------------------
# Fixtures — all version 2 (resources embedded in charm entries)
# ---------------------------------------------------------------------------

_GENERATED_LOCAL = """\
version: 1
rocks:
- name: myrock
  rockcraft-yaml: rock_dir/rockcraft.yaml
  builds:
  - arch: amd64
    file: ./rock_dir/myrock.rock
charms:
- name: mycharm
  charmcraft-yaml: charmcraft.yaml
  builds:
  - arch: amd64
    path: ./mycharm_ubuntu-22.04-amd64.charm
    base: ubuntu@22.04
  resources:
    myrock-image:
      type: oci-image
      rock: myrock
"""

_GENERATED_CI = """\
version: 1
rocks:
- name: myrock
  rockcraft-yaml: rock_dir/rockcraft.yaml
  builds:
  - arch: amd64
    image: ghcr.io/canonical/myrock:abc123
charms:
- name: mycharm
  charmcraft-yaml: charmcraft.yaml
  builds:
  - arch: amd64
    artifact: charm-mycharm
    run-id: "999"
  resources:
    myrock-image:
      type: oci-image
      rock: myrock
"""

_GENERATED_NO_RESOURCES = """\
version: 1
charms:
- name: simple
  charmcraft-yaml: charmcraft.yaml
  builds:
  - arch: amd64
    path: ./simple_ubuntu-22.04-amd64.charm
    base: ubuntu@22.04
"""


class TestAssemblePytestArgs:
    """Tests for assemble_pytest_args()."""

    def test_missing_generated_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="not found"):
            assemble_pytest_args(tmp_path)

    def test_invalid_generated_fields_raises(self, tmp_path: Path) -> None:
        write_file(
            tmp_path / "artifacts.build.yaml",
            "version: 1\ncharms:\n- name: c\n  source: .\n"
            "  builds:\n  - arch: amd64\n    path: ./c.charm\n",
        )
        with pytest.raises(Exception, match=_V1_ERROR_MATCH):
            assemble_pytest_args(tmp_path)

    def test_local_charm_with_embedded_rock_resource(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _GENERATED_LOCAL)

        args = assemble_pytest_args(tmp_path)

        assert "--charm-file=./mycharm_ubuntu-22.04-amd64.charm" in args
        assert "--myrock-image=./rock_dir/myrock.rock" in args

    def test_ci_scenario_only_generated_file(self, tmp_path: Path) -> None:
        """Pytest expand works with only artifacts.build.yaml (no repo checkout)."""
        write_file(tmp_path / "artifacts.build.yaml", _GENERATED_CI)
        # Intentionally no artifacts.yaml present

        args = assemble_pytest_args(tmp_path)

        # CI charm has artifact output, not file — no --charm-file
        assert not any(a.startswith("--charm-file=") for a in args)
        # Rock image ref comes from iterating the rocks list directly
        assert "--myrock-image=ghcr.io/canonical/myrock:abc123" in args

    def test_ci_charm_logs_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """CI-format charm (artifact output only) emits a warning, no --charm-file."""
        write_file(tmp_path / "artifacts.build.yaml", _GENERATED_CI)

        with caplog.at_level(logging.WARNING, logger="opcli.core.pytest_args"):
            args = assemble_pytest_args(tmp_path)

        assert not any(a.startswith("--charm-file=") for a in args)
        assert any("charm-mycharm" in msg for msg in caplog.messages), (
            "expected warning mentioning the artifact name"
        )

    def test_charm_without_resources(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _GENERATED_NO_RESOURCES)

        args = assemble_pytest_args(tmp_path)

        assert args == ["--charm-file=./simple_ubuntu-22.04-amd64.charm"]

    def test_multi_base_charm_emits_multiple_charm_file_flags(self, tmp_path: Path) -> None:
        """Multi-base charm produces one --charm-file per output entry."""
        write_file(
            tmp_path / "artifacts.build.yaml",
            "version: 1\ncharms:\n- name: aproxy\n"
            "  charmcraft-yaml: charmcraft.yaml\n"
            "  builds:\n"
            "  - arch: amd64\n    path: ./aproxy_ubuntu-20.04-amd64.charm\n"
            "    base: ubuntu@20.04\n"
            "  - arch: amd64\n    path: ./aproxy_ubuntu-22.04-amd64.charm\n"
            "    base: ubuntu@22.04\n"
            "  - arch: amd64\n    path: ./aproxy_ubuntu-24.04-amd64.charm\n"
            "    base: ubuntu@24.04\n",
        )

        args = assemble_pytest_args(tmp_path)

        assert "--charm-file=./aproxy_ubuntu-20.04-amd64.charm" in args
        assert "--charm-file=./aproxy_ubuntu-22.04-amd64.charm" in args
        assert "--charm-file=./aproxy_ubuntu-24.04-amd64.charm" in args
        assert args.count("--charm-file=./aproxy_ubuntu-22.04-amd64.charm") == 1

    def test_empty_generated(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", "version: 1\n")

        args = assemble_pytest_args(tmp_path)
        assert args == []

    def test_unresolved_resource_produces_no_flag(self, tmp_path: Path) -> None:
        """Resource with no file or image (rock not built) emits no flag."""
        write_file(
            tmp_path / "artifacts.build.yaml",
            "version: 1\ncharms:\n- name: c\n  charmcraft-yaml: charmcraft.yaml\n"
            "  builds:\n  - arch: amd64\n    path: ./c_ubuntu-22.04-amd64.charm\n"
            "    base: ubuntu@22.04\n"
            "  resources:\n    img:\n      type: oci-image\n      rock: myrock\n",
        )

        args = assemble_pytest_args(tmp_path)

        assert args == ["--charm-file=./c_ubuntu-22.04-amd64.charm"]
        assert not any(a.startswith("--img=") for a in args)

    def test_image_takes_priority_over_file_when_both_set(self, tmp_path: Path) -> None:
        """After provision load, image ref is preferred over local file path."""
        write_file(
            tmp_path / "artifacts.build.yaml",
            "version: 1\n"
            "rocks:\n- name: myrock\n  rockcraft-yaml: rock_dir/rockcraft.yaml\n"
            "  builds:\n  - arch: amd64\n    file: ./rock_dir/myrock.rock\n"
            "    image: localhost:32000/myrock:latest\n"
            "charms:\n- name: c\n  charmcraft-yaml: charmcraft.yaml\n"
            "  builds:\n  - arch: amd64\n    path: ./c_ubuntu-22.04-amd64.charm\n"
            "    base: ubuntu@22.04\n"
            "  resources:\n    myrock-image:\n      type: oci-image\n"
            "      rock: myrock\n",
        )

        args = assemble_pytest_args(tmp_path)

        assert "--myrock-image=localhost:32000/myrock:latest" in args
        assert not any("myrock.rock" in a for a in args)

    def test_rock_name_used_for_flag_not_resource_name(self, tmp_path: Path) -> None:
        """Flag uses rock name, not resource name — matches operator-workflows.

        When the resource name (e.g. ``app-image``) differs from the rock name
        (e.g. ``expressjs-app``), the generated flag must be
        ``--expressjs-app-image=...``, not ``--app-image=...``.
        """
        write_file(
            tmp_path / "artifacts.build.yaml",
            "version: 1\n"
            "rocks:\n- name: expressjs-app\n  rockcraft-yaml: rockcraft.yaml\n"
            "  builds:\n  - arch: amd64\n    file: ./expressjs-app_1.0_amd64.rock\n"
            "charms:\n- name: expressjs-k8s\n"
            "  charmcraft-yaml: charmcraft.yaml\n"
            "  builds:\n  - arch: amd64\n"
            "    path: ./expressjs-k8s_ubuntu-22.04-amd64.charm\n"
            "    base: ubuntu@22.04\n"
            "  resources:\n    app-image:\n      type: oci-image\n"
            "      rock: expressjs-app\n",
        )

        args = assemble_pytest_args(tmp_path)

        assert "--expressjs-app-image=./expressjs-app_1.0_amd64.rock" in args
        assert not any(a.startswith("--app-image=") for a in args)

    def test_rock_without_resource_link_emits_image_flag(self, tmp_path: Path) -> None:
        """Rock with no charm resource link still generates --{rock-name}-image flag.

        This is the core operator-workflows behaviour: image flags come from
        iterating rocks directly, no explicit rock: annotation required.
        """
        write_file(
            tmp_path / "artifacts.build.yaml",
            "version: 1\n"
            "rocks:\n"
            "- name: expressjs-app\n  rockcraft-yaml: rockcraft.yaml\n"
            "  builds:\n  - arch: amd64\n    file: ./expressjs-app_1.0_amd64.rock\n"
            "- name: fastapi-app\n  rockcraft-yaml: fastapi/rockcraft.yaml\n"
            "  builds:\n  - arch: amd64\n    file: ./fastapi-app_1.0_amd64.rock\n"
            "charms:\n- name: my-charm\n  charmcraft-yaml: charmcraft.yaml\n"
            "  builds:\n  - arch: amd64\n"
            "    path: ./my-charm_ubuntu-22.04-amd64.charm\n"
            "    base: ubuntu@22.04\n",
        )

        args = assemble_pytest_args(tmp_path)

        assert "--expressjs-app-image=./expressjs-app_1.0_amd64.rock" in args
        assert "--fastapi-app-image=./fastapi-app_1.0_amd64.rock" in args

    def test_resource_without_rock_link_produces_no_flag(self, tmp_path: Path) -> None:
        """Resources not linked to a rock (no rock: field) produce no image flag."""
        write_file(
            tmp_path / "artifacts.build.yaml",
            "version: 1\ncharms:\n- name: mycharm\n"
            "  charmcraft-yaml: charmcraft.yaml\n"
            "  builds:\n  - arch: amd64\n"
            "    path: ./mycharm_ubuntu-22.04-amd64.charm\n"
            "    base: ubuntu@22.04\n"
            "  resources:\n    standalone-image:\n      type: oci-image\n",
        )

        args = assemble_pytest_args(tmp_path)

        # Only charm-file; no image flag for a resource with no rock backing
        assert args == ["--charm-file=./mycharm_ubuntu-22.04-amd64.charm"]


class TestAssembleToxArgv:
    """Tests for assemble_tox_argv()."""

    def test_no_flags_no_extra_omits_separator(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", "version: 1\n")

        argv = assemble_tox_argv(tmp_path)

        assert argv == ["tox", "-e", "integration"]
        assert "--" not in argv

    def test_assembled_flags_include_separator(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _GENERATED_LOCAL)

        argv = assemble_tox_argv(tmp_path)

        assert argv[:3] == ["tox", "-e", "integration"]
        assert "--" in argv
        assert "--charm-file=./mycharm_ubuntu-22.04-amd64.charm" in argv

    def test_extra_args_only_include_separator(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", "version: 1\n")

        argv = assemble_tox_argv(tmp_path, extra_args=["-k", "test_foo"])

        assert "--" in argv
        assert "-k" in argv
        assert "test_foo" in argv

    def test_custom_tox_env(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", "version: 1\n")

        argv = assemble_tox_argv(tmp_path, tox_env="e2e")

        assert argv[2] == "e2e"

    def test_extra_args_appended_after_assembled(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _GENERATED_LOCAL)

        argv = assemble_tox_argv(tmp_path, extra_args=["-v", "-k", "test_charm"])

        sep_idx = argv.index("--")
        tail = argv[sep_idx + 1 :]
        assert "--charm-file=./mycharm_ubuntu-22.04-amd64.charm" in tail
        assert "-v" in tail
        assert "-k" in tail
        assert "test_charm" in tail

    def test_missing_generated_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="not found"):
            assemble_tox_argv(tmp_path)


class TestPytestRun:
    """Tests for ``pytest_run`` — executes tox interactively."""

    def test_runs_tox_interactively(self, tmp_path: Path, mocker: MockerFixture) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _GENERATED_LOCAL)
        mock_run = mocker.patch("opcli.core.pytest_args.run_command")
        mocker.patch("opcli.core.pytest_args.is_ci", return_value=False)
        mocker.patch("opcli.core.pytest_args.load_secrets_env", return_value={})

        pytest_run(tmp_path)

        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args
        assert call_kwargs.kwargs["interactive"] is True
        assert call_kwargs.kwargs["cwd"] == str(tmp_path)
        cmd = call_kwargs.args[0]
        assert cmd[:3] == ["tox", "-e", "integration"]

    def test_forwards_extra_args(self, tmp_path: Path, mocker: MockerFixture) -> None:
        write_file(tmp_path / "artifacts.build.yaml", "version: 1\n")
        mock_run = mocker.patch("opcli.core.pytest_args.run_command")
        mocker.patch("opcli.core.pytest_args.is_ci", return_value=False)
        mocker.patch("opcli.core.pytest_args.load_secrets_env", return_value={})

        pytest_run(tmp_path, extra_args=["-k", "test_charm"])

        cmd = mock_run.call_args.args[0]
        assert "-k" in cmd
        assert "test_charm" in cmd

    def test_custom_tox_env(self, tmp_path: Path, mocker: MockerFixture) -> None:
        write_file(tmp_path / "artifacts.build.yaml", "version: 1\n")
        mock_run = mocker.patch("opcli.core.pytest_args.run_command")
        mocker.patch("opcli.core.pytest_args.is_ci", return_value=False)
        mocker.patch("opcli.core.pytest_args.load_secrets_env", return_value={})

        pytest_run(tmp_path, tox_env="e2e")

        cmd = mock_run.call_args.args[0]
        assert cmd[2] == "e2e"

    def test_loads_secrets_env_locally(self, tmp_path: Path, mocker: MockerFixture) -> None:
        write_file(tmp_path / "artifacts.build.yaml", "version: 1\n")
        mock_run = mocker.patch("opcli.core.pytest_args.run_command")
        mocker.patch("opcli.core.pytest_args.is_ci", return_value=False)
        mocker.patch(
            "opcli.core.pytest_args.load_secrets_env",
            return_value={"SECRET_KEY": "val"},
        )

        pytest_run(tmp_path)

        assert mock_run.call_args.kwargs["env"] == {"SECRET_KEY": "val"}

    def test_skips_secrets_in_ci(self, tmp_path: Path, mocker: MockerFixture) -> None:
        write_file(tmp_path / "artifacts.build.yaml", "version: 1\n")
        mock_run = mocker.patch("opcli.core.pytest_args.run_command")
        mocker.patch("opcli.core.pytest_args.is_ci", return_value=True)
        mock_load = mocker.patch("opcli.core.pytest_args.load_secrets_env")

        pytest_run(tmp_path)

        mock_load.assert_not_called()
        assert mock_run.call_args.kwargs["env"] is None
