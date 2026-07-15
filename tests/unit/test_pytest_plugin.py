# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for opcli.pytest_plugin."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from opcli.core.constants import artifacts_build_path
from opcli.models.artifacts_build import ArtifactsGenerated, CharmOutput, RockOutput
from opcli.pytest_plugin import (
    CharmPathList,
    _build_charm_path,
    _build_charm_paths,
    _build_charm_resource_images,
    _build_resource_images,
    _discover_artifacts_build,
    _parse_kv_flags,
    _resolve_path,
    _select_arch_builds_charm,
    _select_arch_builds_rock,
    build_rock_images,
)
from opcli.pytest_plugin import (
    charm_path as _charm_path_fixture,
)
from opcli.pytest_plugin import (
    charm_paths as _charm_paths_fixture,
)
from opcli.pytest_plugin import (
    charm_resource_images as _charm_resource_images_fixture,
)
from opcli.pytest_plugin import (
    resource_images as _resource_images_fixture,
)
from tests.conftest import write_file

# Patch target: lazy-imported inside functions
_ARCH = "opcli.core.env.current_arch"

# Fixed root used when a real directory is not required
_FAKE_ROOT = Path("/fake/root")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_config(
    rootdir: Path,
    cli_path: str | None = None,
    charm_files: list[str] | None = None,
    resource_images: list[str] | None = None,
) -> MagicMock:
    """Build a minimal pytest.Config mock."""
    config = MagicMock(spec=pytest.Config)
    config.rootpath = rootdir

    def _getoption(name: str, default: object = None) -> object:
        if name == "--artifacts-build-yaml":
            return cli_path
        if name == "--charm-file":
            return charm_files
        if name == "--resource-image":
            return resource_images
        return default

    config.getoption.side_effect = _getoption
    return config


# ---------------------------------------------------------------------------
# _discover_artifacts_build
# ---------------------------------------------------------------------------


class TestDiscoverArtifactsBuild:
    def test_cli_option_wins_over_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli_file = tmp_path / "cli.yaml"
        cli_file.write_text("version: 1\n")
        env_file = tmp_path / "env.yaml"
        env_file.write_text("version: 1\n")
        monkeypatch.setenv("OPCLI_ARTIFACTS_BUILD_YAML", str(env_file))
        result = _discover_artifacts_build(_mock_config(tmp_path, cli_path=str(cli_file)))
        assert result == cli_file

    def test_env_var_wins_over_walk(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        f = artifacts_build_path(tmp_path)
        write_file(f, "version: 1\n")
        result = _discover_artifacts_build(_mock_config(nested))
        assert result == f

    def test_walk_up_finds_in_rootdir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPCLI_ARTIFACTS_BUILD_YAML", raising=False)
        f = artifacts_build_path(tmp_path)
        write_file(f, "version: 1\n")
        result = _discover_artifacts_build(_mock_config(tmp_path))
        assert result == f

    def test_not_found_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPCLI_ARTIFACTS_BUILD_YAML", raising=False)
        with pytest.raises(pytest.UsageError, match=r"artifacts\.build\.yaml"):
            _discover_artifacts_build(_mock_config(tmp_path))

    def test_walk_stops_at_git_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Walk-up does not cross a .git boundary into an unrelated parent."""
        monkeypatch.delenv("OPCLI_ARTIFACTS_BUILD_YAML", raising=False)
        # Place an artifacts.build.yaml ABOVE the .git root — should not be found.
        parent_file = artifacts_build_path(tmp_path)
        write_file(parent_file, "version: 1\n")
        git_root = tmp_path / "repo"
        git_root.mkdir()
        (git_root / ".git").mkdir()
        nested = git_root / "sub"
        nested.mkdir()
        with pytest.raises(pytest.UsageError, match=r"artifacts\.build\.yaml"):
            _discover_artifacts_build(_mock_config(nested))


# ---------------------------------------------------------------------------
# _resolve_path
# ---------------------------------------------------------------------------


class TestResolvePath:
    def test_relative_path_resolved_against_root(self) -> None:
        result = _resolve_path("./foo.charm", Path("/proj/root"))
        assert result == "/proj/root/foo.charm"

    def test_subdir_relative_path(self) -> None:
        result = _resolve_path("./k8s-charm/foo.charm", Path("/proj/root"))
        assert result == "/proj/root/k8s-charm/foo.charm"

    def test_absolute_path_returned_unchanged(self) -> None:
        result = _resolve_path("/abs/path/foo.charm", Path("/proj/root"))
        assert result == "/abs/path/foo.charm"


# ---------------------------------------------------------------------------
# _select_arch_builds helpers
# ---------------------------------------------------------------------------


class TestSelectArchBuilds:
    def test_charm_exact_match(self) -> None:
        builds = [
            CharmOutput(arch="amd64", path="a.charm"),
            CharmOutput(arch="arm64", path="b.charm"),
        ]
        result = _select_arch_builds_charm(builds, "amd64")
        assert [b.arch for b in result] == ["amd64"]

    def test_charm_no_match_returns_empty(self) -> None:
        """No match returns empty list — callers handle the empty case."""
        builds = [CharmOutput(arch="arm64", path="b.charm")]
        result = _select_arch_builds_charm(builds, "amd64")
        assert result == []

    def test_charm_empty_input_returns_empty(self) -> None:
        result = _select_arch_builds_charm([], "amd64")
        assert result == []

    def test_rock_exact_match(self) -> None:
        builds = [
            RockOutput(arch="amd64", image="img:amd64"),
            RockOutput(arch="arm64", image="img:arm64"),
        ]
        result = _select_arch_builds_rock(builds, "amd64")
        assert [b.arch for b in result] == ["amd64"]

    def test_rock_no_match_returns_empty(self) -> None:
        builds = [RockOutput(arch="arm64", image="img:arm64")]
        result = _select_arch_builds_rock(builds, "amd64")
        assert result == []

    def test_rock_empty_input_returns_empty(self) -> None:
        result = _select_arch_builds_rock([], "amd64")
        assert result == []


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
        with patch(_ARCH, return_value="amd64"):
            result = _build_charm_path(arts, _FAKE_ROOT)
        assert result == str(_FAKE_ROOT / "mycharm.charm")

    def test_fails_no_charms(self) -> None:
        arts = ArtifactsGenerated(version=1)
        with pytest.raises(pytest.fail.Exception, match="no charms"):
            _build_charm_path(arts, _FAKE_ROOT)

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
            patch(_ARCH, return_value="amd64"),
            pytest.raises(pytest.fail.Exception, match="multiple charms"),
        ):
            _build_charm_path(arts, _FAKE_ROOT)

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
            patch(_ARCH, return_value="amd64"),
            pytest.raises(pytest.fail.Exception, match="2 builds"),
        ):
            _build_charm_path(arts, _FAKE_ROOT)

    def test_fails_arch_fallback_multi_build(self) -> None:
        """Arch fallback with multiple builds gives a clear 'no match' error."""
        arts = ArtifactsGenerated.model_validate(
            {
                "version": 1,
                "charms": [
                    {
                        "name": "mycharm",
                        "charmcraft-yaml": "charmcraft.yaml",
                        "builds": [
                            {"arch": "arm64", "path": "./a-22.charm", "base": "ubuntu@22.04"},
                            {"arch": "arm64", "path": "./a-24.charm", "base": "ubuntu@24.04"},
                        ],
                    }
                ],
            }
        )
        with (
            patch(_ARCH, return_value="amd64"),
            pytest.raises(
                pytest.fail.Exception, match="no build for charm 'mycharm' matches arch 'amd64'"
            ),
        ):
            _build_charm_path(arts, _FAKE_ROOT)

    def test_fails_arch_mismatch_single_build(self) -> None:
        """Single build of the wrong arch must fail hard, not silently use it."""
        arts = ArtifactsGenerated.model_validate(
            {
                "version": 1,
                "charms": [
                    {
                        "name": "mycharm",
                        "charmcraft-yaml": "charmcraft.yaml",
                        "builds": [{"arch": "arm64", "path": "./mycharm.charm"}],
                    }
                ],
            }
        )
        with (
            patch(_ARCH, return_value="amd64"),
            pytest.raises(
                pytest.fail.Exception, match="no build for charm 'mycharm' matches arch 'amd64'"
            ),
        ):
            _build_charm_path(arts, _FAKE_ROOT)

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
            patch(_ARCH, return_value="amd64"),
            pytest.raises(pytest.fail.Exception, match="no local path"),
        ):
            _build_charm_path(arts, _FAKE_ROOT)


# ---------------------------------------------------------------------------
# CharmPathList
# ---------------------------------------------------------------------------


class TestCharmPathList:
    def test_path_single(self) -> None:
        cpl = CharmPathList([("ubuntu@24.04", "/a.charm")])
        assert cpl.path == "/a.charm"

    def test_path_no_base_info(self) -> None:
        cpl = CharmPathList([(None, "/a.charm")])
        assert cpl.path == "/a.charm"

    def test_path_multi_fails(self) -> None:
        cpl = CharmPathList([("ubuntu@22.04", "/a-22.charm"), ("ubuntu@24.04", "/a-24.charm")])
        with pytest.raises(pytest.fail.Exception, match="ambiguous"):
            cpl.path  # noqa: B018

    def test_getitem_by_base(self) -> None:
        cpl = CharmPathList([("ubuntu@22.04", "/a-22.charm"), ("ubuntu@24.04", "/a-24.charm")])
        assert cpl["ubuntu@22.04"] == "/a-22.charm"
        assert cpl["ubuntu@24.04"] == "/a-24.charm"

    def test_getitem_base_not_found(self) -> None:
        cpl = CharmPathList([("ubuntu@22.04", "/a.charm")])
        with pytest.raises(KeyError, match=r"ubuntu@24\.04"):
            cpl["ubuntu@24.04"]

    def test_getitem_no_base_info_raises(self) -> None:
        cpl = CharmPathList([(None, "/a.charm")])
        with pytest.raises(KeyError, match="no base information"):
            cpl["ubuntu@24.04"]

    def test_getitem_wrong_type_raises(self) -> None:
        cpl = CharmPathList([("ubuntu@24.04", "/a.charm")])
        with pytest.raises(TypeError, match="str"):
            cpl[0]  # type: ignore[index]

    def test_iter(self) -> None:
        cpl = CharmPathList([("ubuntu@22.04", "/a.charm"), ("ubuntu@24.04", "/b.charm")])
        assert list(cpl) == ["/a.charm", "/b.charm"]

    def test_len(self) -> None:
        cpl = CharmPathList([("ubuntu@22.04", "/a.charm"), ("ubuntu@24.04", "/b.charm")])
        assert len(cpl) == len(["/a.charm", "/b.charm"])

    def test_bases(self) -> None:
        cpl = CharmPathList([("ubuntu@22.04", "/a.charm"), ("ubuntu@24.04", "/b.charm")])
        assert cpl.bases == ["ubuntu@22.04", "ubuntu@24.04"]

    def test_bases_none(self) -> None:
        cpl = CharmPathList([(None, "/a.charm")])
        assert cpl.bases == [None]

    def test_eq(self) -> None:
        a = CharmPathList([("ubuntu@24.04", "/a.charm")])
        b = CharmPathList([("ubuntu@24.04", "/a.charm")])
        assert a == b

    def test_neq(self) -> None:
        a = CharmPathList([("ubuntu@22.04", "/a.charm")])
        b = CharmPathList([("ubuntu@24.04", "/a.charm")])
        assert a != b

    def test_repr(self) -> None:
        cpl = CharmPathList([("ubuntu@24.04", "/a.charm")])
        assert "CharmPathList" in repr(cpl)


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
        with patch(_ARCH, return_value="amd64"):
            result = _build_charm_paths(arts, _FAKE_ROOT)
        assert result == {
            "mycharm": CharmPathList([("ubuntu@22.04", str(_FAKE_ROOT / "a.charm"))])
        }

    def test_multi_base_returns_charm_path_list(self) -> None:
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
        with patch(_ARCH, return_value="amd64"):
            result = _build_charm_paths(arts, _FAKE_ROOT)
        cpl = result["mycharm"]
        assert cpl["ubuntu@22.04"] == str(_FAKE_ROOT / "a-22.charm")
        assert cpl["ubuntu@24.04"] == str(_FAKE_ROOT / "a-24.charm")
        assert list(cpl) == [str(_FAKE_ROOT / "a-22.charm"), str(_FAKE_ROOT / "a-24.charm")]

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
        with patch(_ARCH, return_value="amd64"):
            result = _build_charm_paths(arts, _FAKE_ROOT)
        assert result["op"].path == str(_FAKE_ROOT / "op.charm")
        assert result["agent"].path == str(_FAKE_ROOT / "agent.charm")

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
        with (
            patch(_ARCH, return_value="amd64"),
            pytest.raises(pytest.fail.Exception, match="opcli artifacts localize"),
        ):
            _build_charm_paths(arts, _FAKE_ROOT)

    def test_skips_charm_not_built_for_arch(self) -> None:
        # A charm built only for arm64 is silently omitted when running on amd64.
        arts = ArtifactsGenerated.model_validate(
            {
                "version": 1,
                "charms": [
                    {
                        "name": "mycharm",
                        "charmcraft-yaml": "c.yaml",
                        "builds": [{"arch": "arm64", "path": "./a.charm", "base": "ubuntu@24.04"}],
                    }
                ],
            }
        )
        with patch(_ARCH, return_value="amd64"):
            result = _build_charm_paths(arts, _FAKE_ROOT)
        assert result == {}

    def test_multi_charm_partial_arch_skips_unsupported(self) -> None:
        # Mirrors the haproxy-operator scenario: two charms on arm64, one is
        # amd64-only.  The amd64-only charm should be absent; the arm64 charm
        # should be present.
        arts = ArtifactsGenerated.model_validate(
            {
                "version": 1,
                "charms": [
                    {
                        "name": "main-charm",
                        "charmcraft-yaml": "main.yaml",
                        "builds": [
                            {
                                "arch": "amd64",
                                "path": "./main-amd64.charm",
                                "base": "ubuntu@24.04",
                            },
                            {
                                "arch": "arm64",
                                "path": "./main-arm64.charm",
                                "base": "ubuntu@24.04",
                            },
                        ],
                    },
                    {
                        "name": "amd64-only-charm",
                        "charmcraft-yaml": "helper.yaml",
                        "builds": [
                            {"arch": "amd64", "path": "./helper.charm", "base": "ubuntu@24.04"},
                        ],
                    },
                ],
            }
        )
        with patch(_ARCH, return_value="arm64"):
            result = _build_charm_paths(arts, _FAKE_ROOT)
        assert "main-charm" in result
        assert result["main-charm"].path == str(_FAKE_ROOT / "main-arm64.charm")
        assert "amd64-only-charm" not in result


# ---------------------------------------------------------------------------
# build_rock_images
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
        with patch(_ARCH, return_value="amd64"):
            result = build_rock_images(arts, _FAKE_ROOT)
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
        with patch(_ARCH, return_value="amd64"):
            result = build_rock_images(arts, _FAKE_ROOT)
        assert result == {"myrock": str(_FAKE_ROOT / "myrock.rock")}

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
        with patch(_ARCH, return_value="arm64"):
            result = build_rock_images(arts, _FAKE_ROOT)
        assert result == {"myrock": "img:arm64"}

    def test_fails_arch_mismatch(self) -> None:
        arts = ArtifactsGenerated.model_validate(
            {
                "version": 1,
                "rocks": [
                    {
                        "name": "myrock",
                        "rockcraft-yaml": "r.yaml",
                        "builds": [{"arch": "arm64", "image": "img:arm64"}],
                    }
                ],
            }
        )
        with (
            patch(_ARCH, return_value="amd64"),
            pytest.raises(pytest.fail.Exception, match=r"rock_images.*myrock.*amd64"),
        ):
            build_rock_images(arts, _FAKE_ROOT)


# ---------------------------------------------------------------------------
# _build_resource_images
# ---------------------------------------------------------------------------


class TestBuildResourceImages:
    def test_single_charm_returns_resource_dict(self) -> None:
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
        rock_imgs = {"myrock": "ghcr.io/org/myrock:1.0"}
        result = _build_resource_images(arts, rock_imgs)
        assert result == {"myrock-image": "ghcr.io/org/myrock:1.0"}

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
        result = _build_resource_images(arts, {})
        assert result == {}

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
        with pytest.raises(pytest.fail.Exception, match="multiple charms"):
            _build_resource_images(arts, {})

    def test_fails_unresolved_rock(self) -> None:
        arts = ArtifactsGenerated.model_validate(
            {
                "version": 1,
                "charms": [
                    {
                        "name": "mycharm",
                        "charmcraft-yaml": "c.yaml",
                        "builds": [{"arch": "amd64", "path": "./mycharm.charm"}],
                        "resources": {"oci-image": {"type": "oci-image", "rock": "missing-rock"}},
                    }
                ],
            }
        )
        with pytest.raises(pytest.fail.Exception, match="missing-rock"):
            _build_resource_images(arts, {})


# ---------------------------------------------------------------------------
# _build_charm_resource_images
# ---------------------------------------------------------------------------


class TestBuildCharmResourceImages:
    def _arts_two_charms(self) -> ArtifactsGenerated:
        return ArtifactsGenerated.model_validate(
            {
                "version": 1,
                "rocks": [
                    {
                        "name": "rock-a",
                        "rockcraft-yaml": "ra.yaml",
                        "builds": [{"arch": "amd64", "image": "ghcr.io/org/rock-a:1.0"}],
                    },
                    {
                        "name": "rock-b",
                        "rockcraft-yaml": "rb.yaml",
                        "builds": [{"arch": "amd64", "image": "ghcr.io/org/rock-b:1.0"}],
                    },
                ],
                "charms": [
                    {
                        "name": "charm-a",
                        "charmcraft-yaml": "a.yaml",
                        "builds": [{"arch": "amd64", "path": "./a.charm"}],
                        "resources": {"oci-image": {"type": "oci-image", "rock": "rock-a"}},
                    },
                    {
                        "name": "charm-b",
                        "charmcraft-yaml": "b.yaml",
                        "builds": [{"arch": "amd64", "path": "./b.charm"}],
                        "resources": {"oci-image": {"type": "oci-image", "rock": "rock-b"}},
                    },
                ],
            }
        )

    def test_multi_charm_returns_nested_dict(self) -> None:
        arts = self._arts_two_charms()
        rock_imgs = {"rock-a": "ghcr.io/org/rock-a:1.0", "rock-b": "ghcr.io/org/rock-b:1.0"}
        result = _build_charm_resource_images(arts, rock_imgs)
        assert result == {
            "charm-a": {"oci-image": "ghcr.io/org/rock-a:1.0"},
            "charm-b": {"oci-image": "ghcr.io/org/rock-b:1.0"},
        }

    def test_single_charm_returns_single_entry_dict(self) -> None:
        arts = ArtifactsGenerated.model_validate(
            {
                "version": 1,
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
        result = _build_charm_resource_images(arts, {"myrock": "ghcr.io/org/myrock:1.0"})
        assert result == {"mycharm": {"myrock-image": "ghcr.io/org/myrock:1.0"}}

    def test_charm_with_no_rock_resources_gets_empty_dict(self) -> None:
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

    def test_fails_no_charms(self) -> None:
        arts = ArtifactsGenerated(version=1)
        with pytest.raises(pytest.fail.Exception, match="no charms"):
            _build_charm_resource_images(arts, {})

    def test_fails_unresolved_rock(self) -> None:
        with pytest.raises(pytest.fail.Exception, match="missing-rock"):
            _build_charm_resource_images(
                ArtifactsGenerated.model_validate(
                    {
                        "version": 1,
                        "charms": [
                            {
                                "name": "mycharm",
                                "charmcraft-yaml": "c.yaml",
                                "builds": [{"arch": "amd64", "path": "./mycharm.charm"}],
                                "resources": {
                                    "oci-image": {"type": "oci-image", "rock": "missing-rock"}
                                },
                            }
                        ],
                    }
                ),
                {},
            )


# ---------------------------------------------------------------------------
# _parse_kv_flags
# ---------------------------------------------------------------------------


class TestParseKvFlags:
    def test_single_entry(self) -> None:
        assert _parse_kv_flags(["foo=bar"], "--charm-file") == {"foo": "bar"}

    def test_multiple_entries(self) -> None:
        result = _parse_kv_flags(["a=1", "b=2"], "--charm-file")
        assert result == {"a": "1", "b": "2"}

    def test_value_contains_equals(self) -> None:
        result = _parse_kv_flags(["img=ghcr.io/org/rock:sha=abc"], "--resource-image")
        assert result == {"img": "ghcr.io/org/rock:sha=abc"}

    def test_missing_equals_raises(self) -> None:
        with pytest.raises(pytest.UsageError, match="NAME=VALUE"):
            _parse_kv_flags(["noequalssign"], "--charm-file")

    def test_empty_name_raises(self) -> None:
        with pytest.raises(pytest.UsageError, match="NAME must not be empty"):
            _parse_kv_flags(["=value"], "--charm-file")

    def test_empty_value_raises(self) -> None:
        with pytest.raises(pytest.UsageError, match="VALUE must not be empty"):
            _parse_kv_flags(["name="], "--charm-file")

    def test_duplicate_name_raises(self) -> None:
        with pytest.raises(pytest.UsageError, match="duplicate NAME"):
            _parse_kv_flags(["foo=a", "foo=b"], "--charm-file")


# ---------------------------------------------------------------------------
# CLI-flag mode (charm_path, charm_paths, resource_images)
# ---------------------------------------------------------------------------


class TestCliMode:
    """Tests for the CLI-flag input mode exercised via fixtures."""

    # We call the underlying helpers directly via _parse_kv_flags and
    # verify fixture behaviour using pytester for integration-style checks
    # where needed, or via the _mock_config helper for unit-style checks.

    # ---------- charm_path via --charm-file ----------

    def test_charm_path_single_flag(self, tmp_path: Path) -> None:
        charm = tmp_path / "my.charm"
        charm.touch()
        config = _mock_config(tmp_path, charm_files=[f"my-charm={charm}"])

        request = MagicMock()
        request.config = config
        result = _charm_path_fixture.__wrapped__(request)  # type: ignore[attr-defined]
        assert result == str(charm.resolve())

    def test_charm_path_multiple_flags_fails(self, tmp_path: Path) -> None:
        config = _mock_config(
            tmp_path,
            charm_files=["charm-a=./a.charm", "charm-b=./b.charm"],
        )
        request = MagicMock()
        request.config = config
        with pytest.raises(pytest.fail.Exception, match="use charm_paths"):
            _charm_path_fixture.__wrapped__(request)  # type: ignore[attr-defined]

    def test_charm_path_nonexistent_file_raises(self, tmp_path: Path) -> None:
        config = _mock_config(tmp_path, charm_files=["my-charm=/nonexistent/path.charm"])
        request = MagicMock()
        request.config = config
        with pytest.raises(pytest.UsageError, match="does not exist"):
            _charm_path_fixture.__wrapped__(request)  # type: ignore[attr-defined]

    # ---------- charm_paths via --charm-file ----------

    def test_charm_paths_from_flags(self, tmp_path: Path) -> None:
        a = tmp_path / "a.charm"
        b = tmp_path / "b.charm"
        a.touch()
        b.touch()
        config = _mock_config(
            tmp_path,
            charm_files=[f"charm-a={a}", f"charm-b={b}"],
        )
        request = MagicMock()
        request.config = config
        result = _charm_paths_fixture.__wrapped__(request)  # type: ignore[attr-defined]
        assert result["charm-a"].path == str(a.resolve())
        assert result["charm-b"].path == str(b.resolve())

    def test_charm_paths_nonexistent_file_raises(self, tmp_path: Path) -> None:
        config = _mock_config(tmp_path, charm_files=["my-charm=/nonexistent/path.charm"])
        request = MagicMock()
        request.config = config
        with pytest.raises(pytest.UsageError, match="does not exist"):
            _charm_paths_fixture.__wrapped__(request)  # type: ignore[attr-defined]

    # ---------- resource_images via --resource-image ----------

    def test_resource_images_from_flags(self, tmp_path: Path) -> None:
        config = _mock_config(
            tmp_path,
            resource_images=["oci-image=ghcr.io/org/rock:sha"],
        )
        request = MagicMock()
        request.config = config
        result = _resource_images_fixture.__wrapped__(request)  # type: ignore[attr-defined]
        assert result == {"oci-image": "ghcr.io/org/rock:sha"}

    # ---------- resource_images missing both sources ----------

    def test_resource_images_no_yaml_no_flags_raises(self, tmp_path: Path) -> None:
        config = _mock_config(tmp_path)
        request = MagicMock()
        request.config = config
        with pytest.raises(pytest.UsageError, match="opcli artifacts build"):
            _resource_images_fixture.__wrapped__(request)  # type: ignore[attr-defined]

    def test_resource_images_missing_resource_image_flag_hints(self, tmp_path: Path) -> None:
        """When --charm-file set but --resource-image absent and no yaml, hint."""
        config = _mock_config(tmp_path, charm_files=["my-charm=./my.charm"])
        request = MagicMock()
        request.config = config
        with pytest.raises(pytest.UsageError, match="--resource-image"):
            _resource_images_fixture.__wrapped__(request)  # type: ignore[attr-defined]

    # ---------- resource_images multi-charm failure message ----------

    def test_resource_images_multi_charm_points_to_charm_resource_images(
        self, tmp_path: Path
    ) -> None:
        """Multi-charm fail message should guide users to charm_resource_images."""
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
        with pytest.raises(pytest.fail.Exception, match="charm_resource_images"):
            _build_resource_images(arts, {})


# ---------------------------------------------------------------------------
# charm_resource_images fixture (public contract)
# ---------------------------------------------------------------------------


class TestCharmResourceImagesFixture:
    """Fixture-level tests for charm_resource_images."""

    def test_no_yaml_raises_usage_error(self, tmp_path: Path) -> None:
        """Missing artifacts.build.yaml raises UsageError with helpful hint."""
        config = _mock_config(tmp_path)
        request = MagicMock()
        request.config = config
        with pytest.raises(pytest.UsageError, match="opcli artifacts build"):
            _charm_resource_images_fixture.__wrapped__(request)  # type: ignore[attr-defined]

    def test_yaml_mode_returns_nested_dict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fixture resolves resources from artifacts.build.yaml correctly."""
        monkeypatch.setenv(_ARCH.replace("opcli.core.env.", ""), "amd64")
        yaml_file = artifacts_build_path(tmp_path)
        write_file(
            yaml_file,
            "version: 1\n"
            "rocks:\n"
            "  - name: myrock\n"
            "    rockcraft-yaml: r.yaml\n"
            "    builds:\n"
            "      - arch: amd64\n"
            "        image: ghcr.io/org/myrock:1.0\n"
            "charms:\n"
            "  - name: mycharm\n"
            "    charmcraft-yaml: c.yaml\n"
            "    builds:\n"
            "      - arch: amd64\n"
            "        path: ./mycharm.charm\n"
            "    resources:\n"
            "      oci-image:\n"
            "        type: oci-image\n"
            "        rock: myrock\n",
        )
        config = _mock_config(str(tmp_path))
        request = MagicMock()
        request.config = config

        with patch(_ARCH, return_value="amd64"):
            result = _charm_resource_images_fixture.__wrapped__(request)  # type: ignore[attr-defined]

        assert result == {"mycharm": {"oci-image": "ghcr.io/org/myrock:1.0"}}
