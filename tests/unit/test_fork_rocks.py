# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Tests for fork PR rock build handling (issue #15).

Covers:
- OPCLI_ROCK_UPLOAD=artifact mode in artifacts_build
- OPCLI_ROCK_UPLOAD=registry mode (existing behavior preserved)
- push-images --missing-registry policies (deploy, skip, fail)
- push-images refusing deploy for non-local registry
- artifacts_fetch downloading rock artifacts
- CI prepare script content
"""

import os
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest

from opcli.core.artifacts import artifacts_build, artifacts_fetch, artifacts_localize
from opcli.core.exceptions import ConfigurationError
from opcli.core.provision import provision_load
from opcli.core.spread import _CI_PREPARE_AFTER_USER, _LOCAL_PREPARE_BEFORE_USER
from opcli.core.yaml_io import load_artifacts_build
from tests.conftest import write_file


class TestRockBuildArtifactMode:
    """Test rock build with OPCLI_ROCK_UPLOAD=artifact (fork PR mode)."""

    _CI_ENV: ClassVar[dict[str, str]] = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_RUN_ID": "9876543210",
        "GITHUB_REPOSITORY_OWNER": "MyOrg",
        "GITHUB_REPOSITORY": "MyOrg/my-repo",
        "GITHUB_SHA": "abc1234def5678",
    }

    def test_artifact_mode_skips_ghcr_push(self, tmp_path: Path) -> None:
        """OPCLI_ROCK_UPLOAD=artifact should keep file ref and add artifact metadata."""
        write_file(tmp_path / "rockcraft.yaml", "name: my-rock\n")
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\nrocks:\n- name: my-rock\n  rockcraft-yaml: rockcraft.yaml\n",
        )
        rock_file = tmp_path / "my-rock_1.0_amd64.rock"

        env = {**self._CI_ENV, "OPCLI_ROCK_UPLOAD": "artifact"}
        with (
            patch("opcli.core.artifacts.run_command") as mock_run,
            patch.dict(os.environ, env, clear=False),
        ):
            mock_run.side_effect = lambda cmd, **_: rock_file.write_bytes(b"fake")
            result = artifacts_build(tmp_path, rock_names=["my-rock"])

        gen = load_artifacts_build(result)
        assert len(gen.rocks) == 1
        build = gen.rocks[0].builds[0]
        # Should have file reference preserved
        assert build.file is not None
        assert "my-rock" in build.file
        # Should have artifact metadata
        assert build.artifact == "built-rock-my-rock-amd64"
        assert build.run_id == "9876543210"
        # Should NOT have GHCR image ref
        assert build.image is None
        # Verify no skopeo push was called
        skopeo_calls = [c for c in mock_run.call_args_list if "skopeo" in str(c)]
        assert skopeo_calls == []

    def test_registry_mode_pushes_to_ghcr(self, tmp_path: Path) -> None:
        """OPCLI_ROCK_UPLOAD=registry should push to GHCR (default CI behavior)."""
        write_file(tmp_path / "rockcraft.yaml", "name: my-rock\n")
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\nrocks:\n- name: my-rock\n  rockcraft-yaml: rockcraft.yaml\n",
        )
        rock_file = tmp_path / "my-rock_1.0_amd64.rock"

        env = {**self._CI_ENV, "OPCLI_ROCK_UPLOAD": "registry"}
        with (
            patch("opcli.core.artifacts.run_command") as mock_run,
            patch.dict(os.environ, env, clear=False),
        ):
            mock_run.side_effect = lambda cmd, **_: rock_file.write_bytes(b"fake")
            result = artifacts_build(tmp_path, rock_names=["my-rock"])

        gen = load_artifacts_build(result)
        build = gen.rocks[0].builds[0]
        assert build.image == "ghcr.io/myorg/my-repo/my-rock:abc1234-amd64"
        assert build.file is None
        # Verify skopeo was called
        skopeo_calls = [c for c in mock_run.call_args_list if "skopeo" in str(c)]
        assert len(skopeo_calls) == 1

    def test_invalid_upload_mode_raises(self, tmp_path: Path) -> None:
        """Invalid OPCLI_ROCK_UPLOAD value should raise ConfigurationError."""
        write_file(tmp_path / "rockcraft.yaml", "name: my-rock\n")
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\nrocks:\n- name: my-rock\n  rockcraft-yaml: rockcraft.yaml\n",
        )
        rock_file = tmp_path / "my-rock_1.0_amd64.rock"

        env = {**self._CI_ENV, "OPCLI_ROCK_UPLOAD": "invalid"}
        with (
            patch("opcli.core.artifacts.run_command") as mock_run,
            patch.dict(os.environ, env, clear=False),
            pytest.raises(ConfigurationError, match="OPCLI_ROCK_UPLOAD"),
        ):
            mock_run.side_effect = lambda cmd, **_: rock_file.write_bytes(b"fake")
            artifacts_build(tmp_path, rock_names=["my-rock"])


class TestPushImagesMissingRegistryPolicy:
    """Test push-images --missing-registry policy behavior."""

    _GENERATED_WITH_FILE_ROCK = """\
version: 1
rocks:
- name: myrock
  rockcraft-yaml: rockcraft.yaml
  builds:
  - arch: amd64
    file: ./myrock.rock
charms: []
"""

    _GENERATED_WITH_IMAGE_ROCK = """\
version: 1
rocks:
- name: myrock
  rockcraft-yaml: rockcraft.yaml
  builds:
  - arch: amd64
    image: ghcr.io/org/repo/myrock:abc-amd64
charms: []
"""

    def test_deploy_policy_calls_provision_registry(self, tmp_path: Path) -> None:
        """--missing-registry=deploy should deploy registry then push."""
        write_file(tmp_path / "artifacts.build.yaml", self._GENERATED_WITH_FILE_ROCK)
        (tmp_path / "myrock.rock").write_bytes(b"fake")

        with (
            patch("opcli.core.provision.run_command"),
            patch("opcli.core.provision._is_port_open") as mock_port,
            patch("opcli.core.provision.provision_registry", return_value="deployed") as mock_reg,
        ):
            # First call: port not open; after deploy: port open
            mock_port.side_effect = [False, True]
            pushed = provision_load(tmp_path, missing_registry="deploy")

        mock_reg.assert_called_once_with(tmp_path)
        assert len(pushed) == 1
        assert "myrock" in pushed[0]

    def test_skip_policy_returns_empty(self, tmp_path: Path) -> None:
        """--missing-registry=skip should return empty when registry is down."""
        write_file(tmp_path / "artifacts.build.yaml", self._GENERATED_WITH_FILE_ROCK)

        with patch("opcli.core.provision._is_port_open", return_value=False):
            pushed = provision_load(tmp_path, missing_registry="skip")

        assert pushed == []

    def test_fail_policy_raises(self, tmp_path: Path) -> None:
        """--missing-registry=fail should raise when registry is down."""
        write_file(tmp_path / "artifacts.build.yaml", self._GENERATED_WITH_FILE_ROCK)

        with (
            patch("opcli.core.provision._is_port_open", return_value=False),
            pytest.raises(ConfigurationError, match="--missing-registry=fail"),
        ):
            provision_load(tmp_path, missing_registry="fail")

    def test_deploy_refuses_external_registry(self, tmp_path: Path) -> None:
        """--missing-registry=deploy with non-local registry should error."""
        write_file(tmp_path / "artifacts.build.yaml", self._GENERATED_WITH_FILE_ROCK)

        with (
            patch("opcli.core.provision._is_port_open", return_value=False),
            pytest.raises(ConfigurationError, match="only works with the managed local"),
        ):
            provision_load(tmp_path, registry="external.host:5000", missing_registry="deploy")

    def test_image_only_rocks_noop(self, tmp_path: Path) -> None:
        """Rocks with only image: and no file: should result in no-op."""
        write_file(tmp_path / "artifacts.build.yaml", self._GENERATED_WITH_IMAGE_ROCK)

        with patch("opcli.core.provision._is_port_open", return_value=True):
            pushed = provision_load(tmp_path)

        assert pushed == []


class TestArtifactsFetchRocks:
    """Test that artifacts_fetch downloads rock artifacts."""

    def test_fetch_includes_rocks_with_artifact_field(self, tmp_path: Path) -> None:
        """Rocks with artifact: should be downloaded alongside charms/snaps."""
        generated_yaml = """\
version: 1
rocks:
- name: my-rock
  rockcraft-yaml: rockcraft.yaml
  builds:
  - arch: amd64
    file: ./my-rock.rock
    artifact: built-rock-my-rock-amd64
    run-id: "12345"
charms: []
"""
        write_file(tmp_path / "artifacts.build.yaml", generated_yaml)

        download_calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: object) -> None:
            download_calls.append(cmd)

        with (
            patch("opcli.core.artifacts.run_command", side_effect=fake_run),
            patch("opcli.core.artifacts._infer_repo_from_git", return_value="org/repo"),
            patch("opcli.core.artifacts.artifacts_localize", return_value=0),
        ):
            artifacts_fetch(tmp_path, run_id="12345", repo="org/repo")

        # Should have called gh run download for the rock artifact
        artifact_downloads = [
            c for c in download_calls if "--name" in c and "built-rock" in " ".join(c)
        ]
        assert len(artifact_downloads) == 1
        assert "built-rock-my-rock-amd64" in " ".join(artifact_downloads[0])


class TestCIPrepareScript:
    """Test that CI prepare script includes push-images."""

    def test_ci_prepare_after_user_includes_push_images(self) -> None:
        """_CI_PREPARE_AFTER_USER should call push-images --missing-registry deploy."""
        assert "opcli artifacts push-images --missing-registry deploy" in _CI_PREPARE_AFTER_USER

    def test_local_prepare_before_user_includes_push_images(self) -> None:
        """_LOCAL_PREPARE_BEFORE_USER should call push-images --missing-registry deploy."""
        assert (
            "opcli artifacts push-images --missing-registry deploy" in _LOCAL_PREPARE_BEFORE_USER
        )


class TestRockLocalize:
    """Test artifacts_localize rewrites rock artifact paths."""

    def test_localize_rewrites_rock_file_path(self, tmp_path: Path) -> None:
        """Rock with artifact field gets file rewritten to downloaded location."""
        # Create artifacts.build.yaml with a rock that has artifact set
        build_yaml = tmp_path / "artifacts.build.yaml"
        build_yaml.write_text(
            "rocks:\n"
            "- name: k8s-rock\n"
            "  rockcraft-yaml: k8s-rock/rockcraft.yaml\n"
            "  builds:\n"
            "  - arch: amd64\n"
            "    file: k8s-rock/k8s-rock_1.0_amd64.rock\n"
            "    artifact: built-rock-k8s-rock-amd64\n"
            "    run-id: '12345'\n"
            "charms: []\n"
            "snaps: []\n"
        )

        # Simulate the downloaded artifact directory
        artifact_dir = tmp_path / "built-rock-k8s-rock-amd64"
        artifact_dir.mkdir()
        rock_file = artifact_dir / "k8s-rock_1.0_amd64.rock"
        rock_file.write_bytes(b"fake rock")

        updated = artifacts_localize(tmp_path)

        assert updated == 1
        # Verify the file was rewritten in the YAML
        result = load_artifacts_build(build_yaml)
        assert (
            result.rocks[0].builds[0].file == "./built-rock-k8s-rock-amd64/k8s-rock_1.0_amd64.rock"
        )
