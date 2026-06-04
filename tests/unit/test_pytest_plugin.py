# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for opcli.pytest_plugin."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from opcli.models.artifacts_build import ArtifactsGenerated, CharmOutput, RockOutput
from opcli.pytest_plugin import (
    _build_charm_path,
    _build_charm_paths,
    _build_charm_resource_images,
    _build_resource_images,
    _build_rock_images,
    _discover_artifacts_build,
    _select_arch_builds_charm,
    _select_arch_builds_rock,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_config(rootdir: Path, cli_path: str | None = None) -> MagicMock:
    """Build a minimal pytest.Config mock."""
    config = MagicMock(spec=pytest.Config)
    config.rootpath = rootdir

    def _getoption(name: str, default: object = None) -> object:
        if name == "--artifacts-build-yaml":
            return cli_path
        return default

    config.getoption.side_effect = _getoption
    return config


# ---------------------------------------------------------------------------
# _discover_artifacts_build
# ---------------------------------------------------------------------------


class TestDiscoverArtifactsBuild:
    def test_env_var_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        f = tmp_path / "my_artifacts.build.yaml"
        f.write_text("version: 1\n")
        monkeypatch.setenv("OPCLI_ARTIFACTS_BUILD_YAML", str(f))
        result = _discover_artifacts_build(_mock_config(tmp_path))
        assert result == f

    def test_env_var_missing_file_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPCLI_ARTIFACTS_BUILD_YAML", str(tmp_path / "nope.yaml"))
        with pytest.raises(pytest.UsageError, match="OPCLI_ARTIFACTS_BUILD_YAML"):
            _discover_artifacts_build(_mock_config(tmp_path))

    def test_cli_option_wins_over_walk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPCLI_ARTIFACTS_BUILD_YAML", raising=False)
        f = tmp_path / "custom.yaml"
        f.write_text("version: 1\n")
        result = _discover_artifacts_build(_mock_config(tmp_path, cli_path=str(f)))
        assert result == f

    def test_cli_option_missing_file_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPCLI_ARTIFACTS_BUILD_YAML", raising=False)
        with pytest.raises(pytest.UsageError, match=r"--artifacts-build-yaml"):
            _discover_artifacts_build(_mock_config(tmp_path, cli_path=str(tmp_path / "nope.yaml")))

    def test_walk_up_finds_file_in_ancestor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPCLI_ARTIFACTS_BUILD_YAML", raising=False)
        nested = tmp_path / "sub" / "nested"
        nested.mkdir(parents=True)
        f = tmp_path / "artifacts.build.yaml"
        f.write_text("version: 1\n")
        result = _discover_artifacts_build(_mock_config(nested))
        assert result == f

    def test_walk_up_finds_in_rootdir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPCLI_ARTIFACTS_BUILD_YAML", raising=False)
        f = tmp_path / "artifacts.build.yaml"
        f.write_text("version: 1\n")
        result = _discover_artifacts_build(_mock_config(tmp_path))
        assert result == f

    def test_not_found_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPCLI_ARTIFACTS_BUILD_YAML", raising=False)
        with pytest.raises(pytest.UsageError, match=r"artifacts\.build\.yaml"):
            _discover_artifacts_build(_mock_config(tmp_path))


# ---------------------------------------------------------------------------
# _select_arch_builds helpers
# ---------------------------------------------------------------------------


class TestSelectArchBuilds:
    def test_charm_exact_match(self) -> None:
        builds = [
            CharmOutput(arch="amd64", path="a.charm"),
            CharmOutput(arch="arm64", path="b.charm"),
        ]
        result = _select_arch_builds_charm(builds, "amd64", "mycharm")
        assert [b.arch for b in result] == ["amd64"]

    def test_charm_fallback_to_all(self, caplog: pytest.LogCaptureFixture) -> None:
        builds = [CharmOutput(arch="arm64", path="b.charm")]
        result = _select_arch_builds_charm(builds, "amd64", "mycharm")
        assert result == builds
        assert "No charm build" in caplog.text

    def test_rock_exact_match(self) -> None:
        builds = [
            RockOutput(arch="amd64", image="img:amd64"),
            RockOutput(arch="arm64", image="img:arm64"),
        ]
        result = _select_arch_builds_rock(builds, "amd64", "myrock")
        assert [b.arch for b in result] == ["amd64"]

    def test_rock_fallback_to_all(self, caplog: pytest.LogCaptureFixture) -> None:
        builds = [RockOutput(arch="arm64", image="img:arm64")]
        result = _select_arch_builds_rock(builds, "amd64", "myrock")
        assert result == builds


# ---------------------------------------------------------------------------
# _build_charm_path
# ---------------------------------------------------------------------------


class TestBuildCharmPath:
    def test_single_charm_single_base(self) -> None:
        arts = ArtifactsGenerated.model_validate(
            {
                "version": 1,
                "charms": [
                    {
                        "name": "mycharm",
                        "charmcraft-yaml": "charmcraft.yaml",
                        "builds": [
                            {"arch": "amd64", "path": "./mycharm.charm", "base": "ubuntu@22.04"}
                        ],
                    }
                ],
            }
        )
        with patch("opcli.pytest_plugin.current_arch", return_value="amd64"):
            result = _build_charm_path(arts)
        assert result == "./mycharm.charm"

    def test_fails_no_charms(self) -> None:
        arts = ArtifactsGenerated(version=1)
        with pytest.raises(pytest.fail.Exception, match="no charms"):
            _build_charm_path(arts)

    def test_fails_multi_charm(self) -> None:
        arts = ArtifactsGenerated.model_validate(
            {
                "version": 1,
                "charms": [
                    {
                        "name": "charm-a",
                        "charmcraft-yaml": "a/charmcraft.yaml",
                        "builds": [{"arch": "amd64", "path": "./charm-a.charm"}],
                    },
                    {
                        "name": "charm-b",
                        "charmcraft-yaml": "b/charmcraft.yaml",
                        "builds": [{"arch": "amd64", "path": "./charm-b.charm"}],
                    },
                ],
            }
        )
        with (
            patch("opcli.pytest_plugin.current_arch", return_value="amd64"),
            pytest.raises(pytest.fail.Exception, match="multiple charms"),
        ):
            _build_charm_path(arts)

    def test_fails_multi_base(self) -> None:
        arts = ArtifactsGenerated.model_validate(
            {
                "version": 1,
                "charms": [
                    {
                        "name": "mycharm",
                        "charmcraft-yaml": "charmcraft.yaml",
                        "builds": [
                            {"arch": "amd64", "path": "./a-22.charm", "base": "ubuntu@22.04"},
                            {"arch": "amd64", "path": "./a-24.charm", "base": "ubuntu@24.04"},
                        ],
                    }
                ],
            }
        )
        with (
            patch("opcli.pytest_plugin.current_arch", return_value="amd64"),
            pytest.raises(pytest.fail.Exception, match="2 builds"),
        ):
            _build_charm_path(arts)

    def test_fails_ci_artifact_no_path(self) -> None:
        arts = ArtifactsGenerated.model_validate(
            {
                "version": 1,
                "charms": [
                    {
                        "name": "mycharm",
                        "charmcraft-yaml": "charmcraft.yaml",
                        "builds": [{"arch": "amd64", "artifact": "mycharm", "run-id": "1"}],
                    }
                ],
            }
        )
        with (
            patch("opcli.pytest_plugin.current_arch", return_value="amd64"),
            pytest.raises(pytest.fail.Exception, match="no local path"),
        ):
            _build_charm_path(arts)


# ---------------------------------------------------------------------------
# _build_charm_paths
# ---------------------------------------------------------------------------


class TestBuildCharmPaths:
    def test_single_charm_single_base(self) -> None:
        arts = ArtifactsGenerated.model_validate(
            {
                "version": 1,
                "charms": [
                    {
                        "name": "mycharm",
                        "charmcraft-yaml": "c.yaml",
                        "builds": [{"arch": "amd64", "path": "./a.charm", "base": "ubuntu@22.04"}],
                    }
                ],
            }
        )
        with patch("opcli.pytest_plugin.current_arch", return_value="amd64"):
            result = _build_charm_paths(arts)
        assert result == {"mycharm": ["./a.charm"]}

    def test_multi_base_returns_list(self) -> None:
        arts = ArtifactsGenerated.model_validate(
            {
                "version": 1,
                "charms": [
                    {
                        "name": "mycharm",
                        "charmcraft-yaml": "c.yaml",
                        "builds": [
                            {"arch": "amd64", "path": "./a-22.charm", "base": "ubuntu@22.04"},
                            {"arch": "amd64", "path": "./a-24.charm", "base": "ubuntu@24.04"},
                        ],
                    }
                ],
            }
        )
        with patch("opcli.pytest_plugin.current_arch", return_value="amd64"):
            result = _build_charm_paths(arts)
        assert result == {"mycharm": ["./a-22.charm", "./a-24.charm"]}

    def test_multi_charm(self) -> None:
        arts = ArtifactsGenerated.model_validate(
            {
                "version": 1,
                "charms": [
                    {
                        "name": "op",
                        "charmcraft-yaml": "op.yaml",
                        "builds": [{"arch": "amd64", "path": "./op.charm"}],
                    },
                    {
                        "name": "agent",
                        "charmcraft-yaml": "agent.yaml",
                        "builds": [{"arch": "amd64", "path": "./agent.charm"}],
                    },
                ],
            }
        )
        with patch("opcli.pytest_plugin.current_arch", return_value="amd64"):
            result = _build_charm_paths(arts)
        assert result == {"op": ["./op.charm"], "agent": ["./agent.charm"]}

    def test_skips_ci_artifacts(self) -> None:
        arts = ArtifactsGenerated.model_validate(
            {
                "version": 1,
                "charms": [
                    {
                        "name": "mycharm",
                        "charmcraft-yaml": "c.yaml",
                        "builds": [{"arch": "amd64", "artifact": "mycharm", "run-id": "1"}],
                    }
                ],
            }
        )
        with patch("opcli.pytest_plugin.current_arch", return_value="amd64"):
            result = _build_charm_paths(arts)
        assert result == {"mycharm": []}


# ---------------------------------------------------------------------------
# _build_rock_images
# ---------------------------------------------------------------------------


class TestBuildRockImages:
    def test_returns_image_ref(self) -> None:
        arts = ArtifactsGenerated.model_validate(
            {
                "version": 1,
                "rocks": [
                    {
                        "name": "myrock",
                        "rockcraft-yaml": "r.yaml",
                        "builds": [{"arch": "amd64", "image": "ghcr.io/org/myrock:1.0"}],
                    }
                ],
            }
        )
        with patch("opcli.pytest_plugin.current_arch", return_value="amd64"):
            result = _build_rock_images(arts)
        assert result == {"myrock": "ghcr.io/org/myrock:1.0"}

    def test_returns_file_when_no_image(self) -> None:
        arts = ArtifactsGenerated.model_validate(
            {
                "version": 1,
                "rocks": [
                    {
                        "name": "myrock",
                        "rockcraft-yaml": "r.yaml",
                        "builds": [{"arch": "amd64", "file": "./myrock.rock"}],
                    }
                ],
            }
        )
        with patch("opcli.pytest_plugin.current_arch", return_value="amd64"):
            result = _build_rock_images(arts)
        assert result == {"myrock": "./myrock.rock"}

    def test_filters_by_arch(self) -> None:
        arts = ArtifactsGenerated.model_validate(
            {
                "version": 1,
                "rocks": [
                    {
                        "name": "myrock",
                        "rockcraft-yaml": "r.yaml",
                        "builds": [
                            {"arch": "amd64", "image": "img:amd64"},
                            {"arch": "arm64", "image": "img:arm64"},
                        ],
                    }
                ],
            }
        )
        with patch("opcli.pytest_plugin.current_arch", return_value="arm64"):
            result = _build_rock_images(arts)
        assert result == {"myrock": "img:arm64"}


# ---------------------------------------------------------------------------
# _build_charm_resource_images
# ---------------------------------------------------------------------------


class TestBuildCharmResourceImages:
    def test_resolves_resource_to_rock_image(self) -> None:
        arts = ArtifactsGenerated.model_validate(
            {
                "version": 1,
                "rocks": [
                    {
                        "name": "myrock",
                        "rockcraft-yaml": "r.yaml",
                        "builds": [{"arch": "amd64", "image": "ghcr.io/org/myrock:1.0"}],
                    }
                ],
                "charms": [
                    {
                        "name": "mycharm",
                        "charmcraft-yaml": "c.yaml",
                        "builds": [{"arch": "amd64", "path": "./mycharm.charm"}],
                        "resources": {"myrock-image": {"type": "oci-image", "rock": "myrock"}},
                    }
                ],
            }
        )
        with patch("opcli.pytest_plugin.current_arch", return_value="amd64"):
            ri = _build_rock_images(arts)
        result = _build_charm_resource_images(arts, ri)
        assert result == {"mycharm": {"myrock-image": "ghcr.io/org/myrock:1.0"}}

    def test_skips_resource_without_rock_link(self) -> None:
        arts = ArtifactsGenerated.model_validate(
            {
                "version": 1,
                "charms": [
                    {
                        "name": "mycharm",
                        "charmcraft-yaml": "c.yaml",
                        "builds": [{"arch": "amd64", "path": "./mycharm.charm"}],
                        "resources": {"standalone-image": {"type": "oci-image"}},
                    }
                ],
            }
        )
        result = _build_charm_resource_images(arts, {})
        assert result == {"mycharm": {}}

    def test_multi_charm(self) -> None:
        arts = ArtifactsGenerated.model_validate(
            {
                "version": 1,
                "rocks": [
                    {
                        "name": "backend-rock",
                        "rockcraft-yaml": "r.yaml",
                        "builds": [{"arch": "amd64", "image": "ghcr.io/org/backend:1.0"}],
                    }
                ],
                "charms": [
                    {
                        "name": "operator",
                        "charmcraft-yaml": "op.yaml",
                        "builds": [{"arch": "amd64", "path": "./op.charm"}],
                        "resources": {
                            "backend-image": {"type": "oci-image", "rock": "backend-rock"}
                        },
                    },
                    {
                        "name": "agent",
                        "charmcraft-yaml": "agent.yaml",
                        "builds": [{"arch": "amd64", "path": "./agent.charm"}],
                    },
                ],
            }
        )
        with patch("opcli.pytest_plugin.current_arch", return_value="amd64"):
            ri = _build_rock_images(arts)
        result = _build_charm_resource_images(arts, ri)
        assert result["operator"] == {"backend-image": "ghcr.io/org/backend:1.0"}
        assert result["agent"] == {}


# ---------------------------------------------------------------------------
# _build_resource_images
# ---------------------------------------------------------------------------


class TestBuildResourceImages:
    def test_single_charm_returns_resource_dict(self) -> None:
        arts = ArtifactsGenerated.model_validate(
            {
                "version": 1,
                "charms": [
                    {
                        "name": "mycharm",
                        "charmcraft-yaml": "c.yaml",
                        "builds": [{"arch": "amd64", "path": "./mycharm.charm"}],
                    }
                ],
            }
        )
        cri = {"mycharm": {"myrock-image": "ghcr.io/org/myrock:1.0"}}
        result = _build_resource_images(arts, cri)
        assert result == {"myrock-image": "ghcr.io/org/myrock:1.0"}

    def test_fails_no_charms(self) -> None:
        arts = ArtifactsGenerated(version=1)
        with pytest.raises(pytest.fail.Exception, match="no charms"):
            _build_resource_images(arts, {})

    def test_fails_multi_charm(self) -> None:
        arts = ArtifactsGenerated.model_validate(
            {
                "version": 1,
                "charms": [
                    {
                        "name": "charm-a",
                        "charmcraft-yaml": "a.yaml",
                        "builds": [{"arch": "amd64", "path": "./a.charm"}],
                    },
                    {
                        "name": "charm-b",
                        "charmcraft-yaml": "b.yaml",
                        "builds": [{"arch": "amd64", "path": "./b.charm"}],
                    },
                ],
            }
        )
        cri: dict[str, dict[str, str]] = {"charm-a": {}, "charm-b": {}}
        with pytest.raises(pytest.fail.Exception, match="multiple charms"):
            _build_resource_images(arts, cri)
