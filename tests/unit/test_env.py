# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Tests for env provisioning helpers and the ``opcli env`` CLI commands."""

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from opcli.commands.env import app as env_app
from opcli.core.env import current_arch
from opcli.core.exceptions import ConfigurationError
from opcli.core.provision import provision_load, provision_prepare, provision_registry
from opcli.core.yaml_io import load_artifacts_build
from tests.conftest import write_file

_RUNNER = CliRunner()

_GENERATED_WITH_ROCKS = """\
version: 1
rocks:
- name: myrock
  rockcraft-yaml: rock_dir/rockcraft.yaml
  builds:
  - arch: amd64
    file: ./rock_dir/myrock.rock
- name: otherrock
  rockcraft-yaml: other/rockcraft.yaml
  builds:
  - arch: amd64
    image: ghcr.io/canonical/otherrock:abc
charms:
- name: mycharm
  charmcraft-yaml: charmcraft.yaml
  builds:
  - arch: amd64
    path: ./mycharm_ubuntu-22.04-amd64.charm
    base: ubuntu@22.04
"""

_GENERATED_WITH_MULTIPLE_LOCAL_ROCKS = """\
version: 1
rocks:
- name: myrock
  rockcraft-yaml: rock_dir/rockcraft.yaml
  builds:
  - arch: amd64
    file: ./rock_dir/myrock.rock
- name: otherrock
  rockcraft-yaml: other_dir/rockcraft.yaml
  builds:
  - arch: arm64
    file: ./other_dir/otherrock.rock
charms: []
"""

_GENERATED_WITH_STALE_IMAGE = """\
version: 1
rocks:
- name: myrock
  rockcraft-yaml: rock_dir/rockcraft.yaml
  builds:
  - arch: amd64
    file: ./rock_dir/myrock.rock
    image: old-registry:5000/myrock:amd64
charms: []
"""

_GENERATED_WITH_ROCKS_AND_RESOURCES = """\
version: 1
rocks:
- name: myrock
  rockcraft-yaml: rock_dir/rockcraft.yaml
  builds:
  - arch: amd64
    file: ./rock_dir/myrock.rock
- name: otherrock
  rockcraft-yaml: other_dir/rockcraft.yaml
  builds:
  - arch: amd64
    image: ghcr.io/canonical/otherrock:abc
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
    other-res:
      type: oci-image
      rock: otherrock
"""


class TestProvisionPrepare:
    """Tests for provision_prepare()."""

    def test_runs_concierge(self, tmp_path: Path) -> None:
        write_file(tmp_path / "concierge.yaml", "providers: {}\n")

        with (
            patch("opcli.core.provision.os.getuid", return_value=0),
            patch("opcli.core.provision.shutil.which", return_value="/snap/bin/concierge"),
            patch("opcli.core.provision.run_command") as mock_run,
        ):
            provision_prepare(tmp_path)

        mock_run.assert_called_once_with(
            ["concierge", "prepare", "-c", str(tmp_path / "concierge.yaml")],
            cwd=str(tmp_path),
        )

    def test_missing_concierge_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="not found"):
            provision_prepare(tmp_path)

    def test_custom_concierge_file(self, tmp_path: Path) -> None:
        write_file(tmp_path / "concierge_juju4.yaml", "providers: {}\n")

        with (
            patch("opcli.core.provision.os.getuid", return_value=0),
            patch("opcli.core.provision.shutil.which", return_value="/snap/bin/concierge"),
            patch("opcli.core.provision.run_command") as mock_run,
        ):
            provision_prepare(tmp_path, concierge_file="concierge_juju4.yaml")

        mock_run.assert_called_once_with(
            ["concierge", "prepare", "-c", str(tmp_path / "concierge_juju4.yaml")],
            cwd=str(tmp_path),
        )


class TestProvisionLoad:
    """Tests for provision_load()."""

    def test_missing_generated_returns_empty(self, tmp_path: Path) -> None:
        result = provision_load(tmp_path)
        assert result == []

    def test_registry_port_closed_returns_empty_without_pushing(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _GENERATED_WITH_ROCKS)

        with (
            patch("opcli.core.provision.run_command") as mock_run,
            patch("opcli.core.provision._is_port_open", return_value=False),
        ):
            pushed = provision_load(tmp_path)

        assert pushed == []
        mock_run.assert_not_called()

    def test_pushes_local_rocks(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _GENERATED_WITH_ROCKS)

        with (
            patch("opcli.core.provision.run_command") as mock_run,
            patch("opcli.core.provision._is_port_open", return_value=True),
        ):
            pushed = provision_load(tmp_path)

        assert pushed == ["localhost:32000/myrock:amd64"]
        mock_run.assert_called_once_with(
            [
                "sudo",
                "rockcraft.skopeo",
                "--insecure-policy",
                "copy",
                "--dest-tls-verify=false",
                "oci-archive:rock_dir/myrock.rock",
                "docker://localhost:32000/myrock:amd64",
            ],
            cwd=str(tmp_path),
        )

    def test_custom_registry(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _GENERATED_WITH_ROCKS)

        with (
            patch("opcli.core.provision.run_command") as mock_run,
            patch("opcli.core.provision._is_port_open", return_value=True),
        ):
            pushed = provision_load(tmp_path, registry="myregistry:5000")

        assert pushed == ["myregistry:5000/myrock:amd64"]
        mock_run.assert_called_once_with(
            [
                "sudo",
                "rockcraft.skopeo",
                "--insecure-policy",
                "copy",
                "--dest-tls-verify=false",
                "oci-archive:rock_dir/myrock.rock",
                "docker://myregistry:5000/myrock:amd64",
            ],
            cwd=str(tmp_path),
        )

    def test_repushes_when_existing_image_differs_from_target(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _GENERATED_WITH_STALE_IMAGE)

        with (
            patch("opcli.core.provision.run_command") as mock_run,
            patch("opcli.core.provision._is_port_open", return_value=True),
        ):
            pushed = provision_load(tmp_path)

        assert pushed == ["localhost:32000/myrock:amd64"]
        mock_run.assert_called_once()
        updated = load_artifacts_build(tmp_path / "artifacts.build.yaml")
        assert updated.rocks[0].builds[0].image == "localhost:32000/myrock:amd64"

    def test_pushes_multiple_local_rocks(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _GENERATED_WITH_MULTIPLE_LOCAL_ROCKS)

        with (
            patch("opcli.core.provision.run_command") as mock_run,
            patch("opcli.core.provision._is_port_open", return_value=True),
        ):
            pushed = provision_load(tmp_path)

        assert pushed == [
            "localhost:32000/myrock:amd64",
            "localhost:32000/otherrock:arm64",
        ]
        assert mock_run.call_count == len(pushed)
        assert mock_run.call_args_list[0].args == (
            [
                "sudo",
                "rockcraft.skopeo",
                "--insecure-policy",
                "copy",
                "--dest-tls-verify=false",
                "oci-archive:rock_dir/myrock.rock",
                "docker://localhost:32000/myrock:amd64",
            ],
        )
        assert mock_run.call_args_list[0].kwargs == {"cwd": str(tmp_path)}
        assert mock_run.call_args_list[1].args == (
            [
                "sudo",
                "rockcraft.skopeo",
                "--insecure-policy",
                "copy",
                "--dest-tls-verify=false",
                "oci-archive:other_dir/otherrock.rock",
                "docker://localhost:32000/otherrock:arm64",
            ],
        )
        assert mock_run.call_args_list[1].kwargs == {"cwd": str(tmp_path)}

    def test_no_local_rocks_returns_empty(self, tmp_path: Path) -> None:
        write_file(
            tmp_path / "artifacts.build.yaml",
            "version: 1\n"
            "rocks:\n- name: r1\n  rockcraft-yaml: rd/rockcraft.yaml\n"
            "  builds:\n  - arch: amd64\n    image: ghcr.io/r1:v1\n",
        )

        with (
            patch("opcli.core.provision.run_command") as mock_run,
            patch("opcli.core.provision._is_port_open", return_value=True),
        ):
            pushed = provision_load(tmp_path)

        assert pushed == []
        mock_run.assert_not_called()

    def test_empty_generated_returns_empty(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", "version: 1\n")

        with (
            patch("opcli.core.provision.run_command") as mock_run,
            patch("opcli.core.provision._is_port_open", return_value=True),
        ):
            pushed = provision_load(tmp_path)

        assert pushed == []
        mock_run.assert_not_called()

    def test_pushes_oci_archive_directly_without_docker_daemon(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _GENERATED_WITH_ROCKS)

        with (
            patch("opcli.core.provision.run_command") as mock_run,
            patch("opcli.core.provision._is_port_open", return_value=True),
        ):
            provision_load(tmp_path)

        mock_run.assert_called_once()
        cmd = mock_run.call_args.args[0]
        assert "rockcraft.skopeo" in cmd
        assert any("oci-archive:" in arg for arg in cmd)
        assert any("docker://" in arg for arg in cmd)
        assert not any("docker-daemon:" in arg for arg in cmd)
        assert "--dest-tls-verify=false" in cmd

    def test_updates_artifacts_build_with_image_ref(self, tmp_path: Path) -> None:
        """After pushing, rock.builds.image is set and file is preserved."""
        write_file(tmp_path / "artifacts.build.yaml", _GENERATED_WITH_ROCKS)

        with (
            patch("opcli.core.provision.run_command"),
            patch("opcli.core.provision._is_port_open", return_value=True),
        ):
            provision_load(tmp_path)

        updated = load_artifacts_build(tmp_path / "artifacts.build.yaml")
        myrock = next(r for r in updated.rocks if r.name == "myrock")
        assert myrock.builds[0].image == "localhost:32000/myrock:amd64"
        assert myrock.builds[0].file == "./rock_dir/myrock.rock"

    def test_updates_charm_resources_for_pushed_rock(self, tmp_path: Path) -> None:
        """provision_load pushes rocks; charm resources reference via rock: field."""
        write_file(
            tmp_path / "artifacts.build.yaml",
            _GENERATED_WITH_ROCKS_AND_RESOURCES,
        )

        with (
            patch("opcli.core.provision.run_command"),
            patch("opcli.core.provision._is_port_open", return_value=True),
        ):
            provision_load(tmp_path)

        updated = load_artifacts_build(tmp_path / "artifacts.build.yaml")
        myrock = next(r for r in updated.rocks if r.name == "myrock")
        assert myrock.builds[0].image == "localhost:32000/myrock:amd64"
        charm = updated.charms[0]
        assert charm.resources is not None
        assert charm.resources["myrock-image"].rock == "myrock"
        assert charm.resources["other-res"].rock == "otherrock"

    def test_idempotent_skips_already_loaded_rock(self, tmp_path: Path) -> None:
        """Rock with image already set to the target ref is skipped."""
        write_file(
            tmp_path / "artifacts.build.yaml",
            "version: 1\n"
            "rocks:\n- name: myrock\n  rockcraft-yaml: rock_dir/rockcraft.yaml\n"
            "  builds:\n  - arch: amd64\n    file: ./rock_dir/myrock.rock\n"
            "    image: localhost:32000/myrock:amd64\n",
        )

        with (
            patch("opcli.core.provision.run_command") as mock_run,
            patch("opcli.core.provision._is_port_open", return_value=True),
        ):
            pushed = provision_load(tmp_path)

        assert pushed == []
        mock_run.assert_not_called()

    def test_no_writeback_when_nothing_pushed(self, tmp_path: Path) -> None:
        """artifacts.build.yaml is not written when no rocks are pushed."""
        write_file(tmp_path / "artifacts.build.yaml", "version: 1\n")

        with (
            patch("opcli.core.provision.dump_artifacts_build") as mock_dump,
            patch("opcli.core.provision.run_command"),
            patch("opcli.core.provision._is_port_open", return_value=True),
        ):
            provision_load(tmp_path)

        mock_dump.assert_not_called()


class TestProvisionRegistry:
    """Tests for provision_registry()."""

    def _which_for(self, *providers: str):
        """Return a shutil.which side_effect that resolves only *providers*."""

        def _which(name: str) -> str | None:
            if name in providers:
                return f"/usr/bin/{name}"
            return None

        return _which

    def test_skipped_when_no_k8s_on_path(self, tmp_path: Path) -> None:
        with (
            patch("opcli.core.provision._is_port_open", return_value=False),
            patch("opcli.core.provision.shutil.which", return_value=None),
            patch("opcli.core.provision.run_command") as mock_run,
        ):
            result = provision_registry(tmp_path)
        assert result == "skipped"
        mock_run.assert_not_called()

    def test_already_running_skips_deployment(self, tmp_path: Path) -> None:
        with (
            patch("opcli.core.provision._is_port_open", return_value=True),
            patch(
                "opcli.core.provision.shutil.which",
                side_effect=self._which_for("microk8s"),
            ),
            patch("opcli.core.provision.run_command") as mock_run,
        ):
            result = provision_registry(tmp_path)
        assert result == "already_running"
        mock_run.assert_not_called()

    @pytest.mark.parametrize(
        ("provider", "expected_prefix"),
        [
            pytest.param("microk8s", ["sudo", "microk8s", "kubectl"], id="microk8s"),
            pytest.param("k8s", ["sudo", "k8s", "kubectl"], id="k8s"),
            pytest.param("kubectl", ["sudo", "kubectl"], id="kubectl"),
        ],
    )
    def test_detected_k8s_provider_applies_manifest(
        self,
        tmp_path: Path,
        provider: str,
        expected_prefix: list[str],
    ) -> None:
        with (
            patch("opcli.core.provision._is_port_open", return_value=False),
            patch(
                "opcli.core.provision.shutil.which",
                side_effect=self._which_for(provider),
            ),
            patch("opcli.core.provision.run_command") as mock_run,
        ):
            result = provision_registry(tmp_path)

        assert result == "deployed"
        assert mock_run.call_count == 3  # noqa: PLR2004

        wait_cmd = mock_run.call_args_list[0].args[0]
        assert wait_cmd[: len(expected_prefix) + 1] == [*expected_prefix, "wait"]
        assert "--for=condition=Ready" in wait_cmd

        apply_call = mock_run.call_args_list[1]
        assert apply_call.args[0] == [*expected_prefix, "apply", "-f", "-"]
        assert apply_call.kwargs["stdin"]

        rollout_cmd = mock_run.call_args_list[2].args[0]
        assert rollout_cmd[: len(expected_prefix) + 1] == [*expected_prefix, "rollout"]
        assert "status" in rollout_cmd
        assert "deployment/registry" in rollout_cmd
        assert "container-registry" in rollout_cmd

    def test_microk8s_preferred_over_k8s(self, tmp_path: Path) -> None:
        """When both microk8s and k8s are on PATH, microk8s wins."""
        with (
            patch("opcli.core.provision._is_port_open", return_value=False),
            patch(
                "opcli.core.provision.shutil.which",
                side_effect=self._which_for("microk8s", "k8s"),
            ),
            patch("opcli.core.provision.run_command") as mock_run,
        ):
            result = provision_registry(tmp_path)
        assert result == "deployed"
        assert mock_run.call_args_list[0].args[0][:4] == [
            "sudo",
            "microk8s",
            "kubectl",
            "wait",
        ]

    def test_skipped_when_no_rocks(self, tmp_path: Path) -> None:
        """Skip if artifacts.build.yaml exists but has no rocks."""
        content = (
            "version: 1\nrocks: []\ncharms:\n"
            "- name: c\n  charmcraft-yaml: charmcraft.yaml\n"
            "  builds:\n  - arch: amd64\n"
            "    path: ./c.charm\n    base: ubuntu@22.04\n"
        )
        write_file(tmp_path / "artifacts.build.yaml", content)
        with (
            patch("opcli.core.provision._is_port_open", return_value=False),
            patch(
                "opcli.core.provision.shutil.which",
                side_effect=self._which_for("microk8s"),
            ),
            patch("opcli.core.provision.run_command") as mock_run,
        ):
            result = provision_registry(tmp_path)
        assert result == "skipped"
        mock_run.assert_not_called()

    def test_k8s_manifest_contains_registry_image(self, tmp_path: Path) -> None:
        """Verify the registry.yaml manifest references registry:2 on NodePort 32000."""
        applied_stdin: list[str] = []

        def capture_apply(cmd: list[str], **kwargs: object) -> object:
            if "apply" in cmd:
                stdin_content = kwargs.get("stdin")
                if isinstance(stdin_content, str):
                    applied_stdin.append(stdin_content)
            return None

        with (
            patch("opcli.core.provision._is_port_open", return_value=False),
            patch("opcli.core.provision.shutil.which", side_effect=self._which_for("k8s")),
            patch("opcli.core.provision.run_command", side_effect=capture_apply),
        ):
            provision_registry(tmp_path)

        assert applied_stdin, "apply was not called with stdin"
        content = applied_stdin[0]
        assert "registry:2" in content
        assert "nodePort: 32000" in content
        assert "container-registry" in content


class TestEnvCli:
    """Tests for opcli.commands.env CLI wiring."""

    def test_provision_command_calls_provision_prepare(self, tmp_path: Path) -> None:
        with (
            patch("opcli.commands.env.Path.cwd", return_value=tmp_path),
            patch("opcli.commands.env.provision_prepare") as mock_prepare,
        ):
            result = _RUNNER.invoke(env_app, ["provision", "--concierge", "concierge_juju4.yaml"])

        assert result.exit_code == 0
        mock_prepare.assert_called_once_with(
            tmp_path, concierge_file="concierge_juju4.yaml", image_registry=""
        )
        assert result.stdout == "Provisioning complete.\n"

    @pytest.mark.parametrize(
        ("status", "message"),
        [
            pytest.param("deployed", "Registry deployed at localhost:32000.\n", id="deployed"),
            pytest.param(
                "already_running",
                "Registry already running at localhost:32000.\n",
                id="already-running",
            ),
            pytest.param(
                "skipped",
                "No k8s provider found — skipping registry setup.\n",
                id="skipped",
            ),
        ],
    )
    def test_deploy_registry_outputs_status_message(
        self,
        tmp_path: Path,
        status: str,
        message: str,
    ) -> None:
        with (
            patch("opcli.commands.env.Path.cwd", return_value=tmp_path),
            patch("opcli.commands.env.provision_registry", return_value=status) as mock_registry,
        ):
            result = _RUNNER.invoke(env_app, ["deploy-registry"])

        assert result.exit_code == 0
        mock_registry.assert_called_once_with(tmp_path)
        assert result.stdout == message


class TestCurrentArch:
    """Tests for current_arch() architecture normalisation."""

    @pytest.mark.parametrize(
        ("machine", "expected"),
        [
            pytest.param("x86_64", "amd64", id="x86_64->amd64"),
            pytest.param("amd64", "amd64", id="amd64->amd64"),
            pytest.param("aarch64", "arm64", id="aarch64->arm64"),
            pytest.param("arm64", "arm64", id="arm64->arm64"),
            pytest.param("ppc64le", "ppc64el", id="ppc64le->ppc64el"),
            pytest.param("s390x", "s390x", id="s390x-passthrough"),
            pytest.param("riscv64", "riscv64", id="riscv64-passthrough"),
        ],
    )
    def test_arch_normalisation(self, machine: str, expected: str) -> None:
        with patch("platform.machine", return_value=machine):
            assert current_arch() == expected
