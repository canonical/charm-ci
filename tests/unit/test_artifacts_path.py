# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Tests for opcli artifacts path command."""

from pathlib import Path
from unittest.mock import patch

import pytest

from opcli.core.artifacts import artifacts_path
from opcli.core.exceptions import ConfigurationError, DiscoveryError
from tests.conftest import write_file

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

_MULTI_CHARM_BUILD = """\
version: 1
rocks: []
charms:
- name: traefik-k8s
  charmcraft-yaml: charmcraft.yaml
  builds:
  - arch: amd64
    path: traefik-k8s_ubuntu-22.04-amd64.charm
- name: worker-k8s
  charmcraft-yaml: worker/charmcraft.yaml
  builds:
  - arch: amd64
    path: worker-k8s_ubuntu-22.04-amd64.charm
snaps: []
"""

_MULTI_ARCH_BUILD = """\
version: 1
rocks: []
charms:
- name: traefik-k8s
  charmcraft-yaml: charmcraft.yaml
  builds:
  - arch: amd64
    path: traefik-k8s_ubuntu-22.04-amd64.charm
  - arch: arm64
    path: traefik-k8s_ubuntu-22.04-arm64.charm
snaps: []
"""

_ROCK_BUILD = """\
version: 1
rocks:
- name: myrock
  rockcraft-yaml: rockcraft.yaml
  builds:
  - arch: amd64
    file: myrock_1.0_amd64.rock
    image: ghcr.io/canonical/myrock:1.0
charms: []
snaps: []
"""

_SNAP_BUILD = """\
version: 1
rocks: []
charms: []
snaps:
- name: mysnap
  snapcraft-yaml: snapcraft.yaml
  builds:
  - arch: amd64
    file: mysnap_1.0_amd64.snap
"""


class TestArtifactsPath:
    """Tests for artifacts_path()."""

    def test_single_charm_returns_path(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)

        with patch("opcli.core.artifacts.current_arch", return_value="amd64"):
            result = artifacts_path(tmp_path)

        assert len(result) == 1
        assert result[0] == (tmp_path / "traefik-k8s_ubuntu-22.04-amd64.charm").resolve()

    def test_multi_charm_no_name_raises(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _MULTI_CHARM_BUILD)

        with (
            patch("opcli.core.artifacts.current_arch", return_value="amd64"),
            pytest.raises(DiscoveryError, match="Multiple charms"),
        ):
            artifacts_path(tmp_path, artifact_type="charm")

    def test_multi_charm_with_name_filters(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _MULTI_CHARM_BUILD)

        with patch("opcli.core.artifacts.current_arch", return_value="amd64"):
            result = artifacts_path(tmp_path, name="worker-k8s")

        assert len(result) == 1
        assert "worker-k8s" in str(result[0])

    def test_type_filter_charm(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _ROCK_BUILD)

        with (
            patch("opcli.core.artifacts.current_arch", return_value="amd64"),
            pytest.raises(DiscoveryError, match="No built artifacts"),
        ):
            artifacts_path(tmp_path, artifact_type="charm")

    def test_type_filter_rock(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _ROCK_BUILD)

        with patch("opcli.core.artifacts.current_arch", return_value="amd64"):
            result = artifacts_path(tmp_path, artifact_type="rock")

        assert len(result) == 1
        assert "myrock" in str(result[0])

    def test_type_filter_snap(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SNAP_BUILD)

        with patch("opcli.core.artifacts.current_arch", return_value="amd64"):
            result = artifacts_path(tmp_path, artifact_type="snap")

        assert len(result) == 1
        assert "mysnap" in str(result[0])

    def test_arch_override(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _MULTI_ARCH_BUILD)

        result = artifacts_path(tmp_path, arch="arm64")

        assert len(result) == 1
        assert "arm64" in str(result[0])

    def test_no_build_yaml_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="not found"):
            artifacts_path(tmp_path)

    def test_no_matching_artifact_raises(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)

        with (
            patch("opcli.core.artifacts.current_arch", return_value="amd64"),
            pytest.raises(DiscoveryError, match="No built artifact named"),
        ):
            artifacts_path(tmp_path, name="nonexistent")

    def test_arch_fallback_when_no_match(self, tmp_path: Path) -> None:
        """When no build matches the requested arch, falls back to all builds."""
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)

        with patch("opcli.core.artifacts.current_arch", return_value="s390x"):
            result = artifacts_path(tmp_path)

        assert len(result) == 1
        assert "amd64" in str(result[0])
