# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Tests for artifact discovery and YAML I/O."""

from pathlib import Path

import pytest

from opcli.core.discovery import discover_artifacts
from opcli.core.exceptions import DiscoveryError
from opcli.core.yaml_io import (
    dump_artifacts_build,
    dump_artifacts_plan,
    load_artifacts_build,
    load_artifacts_plan,
)
from opcli.models.artifacts import ArtifactsPlan
from opcli.models.artifacts_build import (
    ArtifactsGenerated,
    CharmOutput,
    GeneratedCharm,
    GeneratedRock,
    RockOutput,
)
from tests.conftest import write_file


class TestDiscovery:
    """Tests for discover_artifacts()."""

    def test_empty_repo(self, tmp_path: Path) -> None:
        plan = discover_artifacts(tmp_path)
        assert plan.rocks == []
        assert plan.charms == []
        assert plan.snaps == []

    def test_single_charm(self, tmp_path: Path) -> None:
        write_file(tmp_path / "charmcraft.yaml", "name: mycharm\ntype: charm\n")
        plan = discover_artifacts(tmp_path)
        assert len(plan.charms) == 1
        assert plan.charms[0].name == "mycharm"
        assert plan.charms[0].charmcraft_yaml == "charmcraft.yaml"

    def test_single_rock(self, tmp_path: Path) -> None:
        write_file(tmp_path / "myrock" / "rockcraft.yaml", "name: myrock\n")
        plan = discover_artifacts(tmp_path)
        assert len(plan.rocks) == 1
        assert plan.rocks[0].name == "myrock"
        assert plan.rocks[0].rockcraft_yaml == "myrock/rockcraft.yaml"

    def test_single_snap(self, tmp_path: Path) -> None:
        write_file(tmp_path / "snap_dir" / "snapcraft.yaml", "name: mysnap\n")
        plan = discover_artifacts(tmp_path)
        assert len(plan.snaps) == 1
        assert plan.snaps[0].name == "mysnap"
        assert plan.snaps[0].snapcraft_yaml == "snap_dir/snapcraft.yaml"
        assert plan.snaps[0].pack_dir is None

    def test_snap_under_snap_subdir(self, tmp_path: Path) -> None:
        """snapcraft.yaml under snap/ → pack_dir is the parent directory."""
        write_file(
            tmp_path / "myproject" / "snap" / "snapcraft.yaml",
            "name: mysnap\n",
        )
        plan = discover_artifacts(tmp_path)
        assert len(plan.snaps) == 1
        assert plan.snaps[0].name == "mysnap"
        assert plan.snaps[0].snapcraft_yaml == "myproject/snap/snapcraft.yaml"
        assert plan.snaps[0].pack_dir == "myproject"

    def test_snap_under_snap_subdir_at_root(self, tmp_path: Path) -> None:
        """snap/snapcraft.yaml at repo root → pack_dir is '.'."""
        write_file(tmp_path / "snap" / "snapcraft.yaml", "name: mysnap\n")
        plan = discover_artifacts(tmp_path)
        assert len(plan.snaps) == 1
        assert plan.snaps[0].name == "mysnap"
        assert plan.snaps[0].snapcraft_yaml == "snap/snapcraft.yaml"
        assert plan.snaps[0].pack_dir == "."

    def test_monorepo(self, tmp_path: Path) -> None:
        write_file(tmp_path / "charmcraft.yaml", "name: main-charm\ntype: charm\n")
        write_file(tmp_path / "rock_a" / "rockcraft.yaml", "name: rock-a\n")
        write_file(tmp_path / "rock_b" / "rockcraft.yaml", "name: rock-b\n")
        write_file(tmp_path / "snap_c" / "snapcraft.yaml", "name: snap-c\n")
        plan = discover_artifacts(tmp_path)
        assert len(plan.rocks) == 2  # noqa: PLR2004
        assert len(plan.charms) == 1
        assert len(plan.snaps) == 1

    def test_deep_nested_artifact(self, tmp_path: Path) -> None:
        write_file(tmp_path / "a" / "b" / "c" / "rockcraft.yaml", "name: deep-rock\n")
        plan = discover_artifacts(tmp_path)
        assert len(plan.rocks) == 1
        assert plan.rocks[0].rockcraft_yaml == "a/b/c/rockcraft.yaml"

    def test_pruned_directories_skipped(self, tmp_path: Path) -> None:
        write_file(tmp_path / ".venv" / "rockcraft.yaml", "name: venv-rock\n")
        write_file(tmp_path / ".git" / "rockcraft.yaml", "name: git-rock\n")
        write_file(tmp_path / ".tox" / "rockcraft.yaml", "name: tox-rock\n")
        plan = discover_artifacts(tmp_path)
        assert plan.rocks == []

    def test_charm_with_oci_resource_auto_links_rock(self, tmp_path: Path) -> None:
        write_file(tmp_path / "rock_dir" / "rockcraft.yaml", "name: myrock\n")
        write_file(
            tmp_path / "charmcraft.yaml",
            "name: mycharm\ntype: charm\nresources:\n  myrock:\n    type: oci-image\n",
        )
        plan = discover_artifacts(tmp_path)
        assert plan.charms[0].resources["myrock"].rock == "myrock"

    def test_charm_resource_no_match_leaves_rock_none(self, tmp_path: Path) -> None:
        write_file(
            tmp_path / "charmcraft.yaml",
            "name: mycharm\ntype: charm\nresources:\n  unknown-img:\n    type: oci-image\n",
        )
        plan = discover_artifacts(tmp_path)
        assert plan.charms[0].resources["unknown-img"].rock is None

    def test_malformed_yaml_raises(self, tmp_path: Path) -> None:
        write_file(tmp_path / "charmcraft.yaml", "- just a list\n")
        with pytest.raises(DiscoveryError, match="not contain a YAML mapping"):
            discover_artifacts(tmp_path)

    def test_missing_name_raises(self, tmp_path: Path) -> None:
        write_file(tmp_path / "rockcraft.yaml", "version: 1\n")
        with pytest.raises(DiscoveryError, match="missing a valid 'name'"):
            discover_artifacts(tmp_path)

    def test_charm_split_format_name_from_metadata_yaml(self, tmp_path: Path) -> None:
        """Legacy split format: name in metadata.yaml, not charmcraft.yaml."""
        write_file(
            tmp_path / "charmcraft.yaml",
            "type: charm\nbases:\n  - run-on:\n    - name: ubuntu\n",
        )
        write_file(tmp_path / "metadata.yaml", "name: indico\nsummary: An indico charm\n")
        plan = discover_artifacts(tmp_path)
        assert len(plan.charms) == 1
        assert plan.charms[0].name == "indico"

    def test_charm_split_format_resources_from_metadata_yaml(self, tmp_path: Path) -> None:
        """Legacy split format: resources in metadata.yaml, not charmcraft.yaml."""
        write_file(tmp_path / "rock_dir" / "rockcraft.yaml", "name: indico-rock\n")
        write_file(
            tmp_path / "charmcraft.yaml",
            "type: charm\nbases:\n  - run-on:\n    - name: ubuntu\n",
        )
        write_file(
            tmp_path / "metadata.yaml",
            "name: indico\n"
            "resources:\n"
            "  indico-image:\n"
            "    type: oci-image\n"
            "    upstream-source: indico-rock\n",
        )
        plan = discover_artifacts(tmp_path)
        assert len(plan.charms) == 1
        assert plan.charms[0].name == "indico"
        assert "indico-image" in plan.charms[0].resources
        assert plan.charms[0].resources["indico-image"].rock == "indico-rock"

    def test_charm_unified_format_takes_precedence_over_metadata_yaml(
        self, tmp_path: Path
    ) -> None:
        """Unified format: name in charmcraft.yaml wins even if metadata.yaml exists."""
        write_file(
            tmp_path / "charmcraft.yaml",
            "name: charm-from-charmcraft\ntype: charm\n",
        )
        write_file(tmp_path / "metadata.yaml", "name: charm-from-metadata\n")
        plan = discover_artifacts(tmp_path)
        assert plan.charms[0].name == "charm-from-charmcraft"

    def test_charm_split_format_no_metadata_yaml_raises(self, tmp_path: Path) -> None:
        """No name in charmcraft.yaml and no metadata.yaml → DiscoveryError."""
        write_file(tmp_path / "charmcraft.yaml", "type: charm\n")
        with pytest.raises(DiscoveryError, match="missing a valid 'name'"):
            discover_artifacts(tmp_path)


class TestYamlIO:
    """Tests for YAML round-trip helpers."""

    def test_artifacts_plan_round_trip(self, tmp_path: Path) -> None:
        plan = ArtifactsPlan.model_validate(
            {
                "version": 1,
                "rocks": [{"name": "r1", "rockcraft-yaml": "rd/rockcraft.yaml"}],
                "charms": [{"name": "c1", "charmcraft-yaml": "charmcraft.yaml"}],
            }
        )
        path = tmp_path / "artifacts.yaml"
        dump_artifacts_plan(plan, path)
        raw = path.read_text()
        # hyphenated keys must be present in the file
        assert "rockcraft-yaml" in raw
        assert "charmcraft-yaml" in raw
        loaded = load_artifacts_plan(path)
        assert loaded == plan

    def test_artifacts_build_round_trip(self, tmp_path: Path) -> None:
        gen = ArtifactsGenerated(
            rocks=[
                GeneratedRock(
                    name="r1",
                    rockcraft_yaml="rd/rockcraft.yaml",
                    builds=[RockOutput(arch="amd64", file="./r1.rock")],
                )
            ],
        )
        path = tmp_path / "artifacts.build.yaml"
        dump_artifacts_build(gen, path)
        loaded = load_artifacts_build(path)
        assert loaded == gen

    def test_run_id_alias_survives_round_trip(self, tmp_path: Path) -> None:
        gen = ArtifactsGenerated(
            charms=[
                GeneratedCharm(
                    name="c1",
                    charmcraft_yaml="charmcraft.yaml",
                    builds=[CharmOutput(arch="amd64", artifact="a1", run_id="42")],
                )
            ],
        )
        path = tmp_path / "gen.yaml"
        dump_artifacts_build(gen, path)
        raw = path.read_text()
        assert "run-id" in raw
        loaded = load_artifacts_build(path)
        assert loaded.charms[0].builds[0].run_id == "42"

    def test_load_invalid_yaml_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("- just a list\n")
        with pytest.raises(ValueError, match="YAML mapping"):
            load_artifacts_plan(path)

    def test_dump_creates_parent_dirs(self, tmp_path: Path) -> None:
        path = tmp_path / "sub" / "dir" / "artifacts.yaml"
        dump_artifacts_plan(ArtifactsPlan(), path)
        assert path.exists()
