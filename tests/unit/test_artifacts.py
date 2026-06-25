# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Tests for opcli artifacts commands: init, build, matrix, collect, fetch."""

import json
import os
from pathlib import Path
from typing import ClassVar
from unittest.mock import call, patch

import pytest

import opcli.core.artifacts as _artifacts_mod
from opcli.core.artifacts import (
    _normalize_partial_dir_layout,
    artifacts_build,
    artifacts_collect,
    artifacts_fetch,
    artifacts_init,
    artifacts_localize,
    artifacts_matrix,
)
from opcli.core.exceptions import ConfigurationError, OpcliError, SubprocessError
from opcli.core.subprocess import SubprocessResult
from opcli.core.yaml_io import load_artifacts_build, load_artifacts_plan
from tests.conftest import write_file


class TestArtifactsInit:
    """Tests for artifacts_init()."""

    def test_generates_artifacts_yaml(self, tmp_path: Path) -> None:
        write_file(tmp_path / "charmcraft.yaml", "name: mycharm\ntype: charm\n")
        write_file(tmp_path / "rock_dir" / "rockcraft.yaml", "name: myrock\n")

        result = artifacts_init(tmp_path)

        assert result == tmp_path / "artifacts.yaml"
        assert result.exists()
        plan = load_artifacts_plan(result)
        assert len(plan.charms) == 1
        assert len(plan.rocks) == 1
        assert plan.charms[0].name == "mycharm"
        assert plan.rocks[0].name == "myrock"

    def test_refuses_overwrite_without_force(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.yaml", "version: 1\n")

        with pytest.raises(ConfigurationError, match="already exists"):
            artifacts_init(tmp_path)

    def test_overwrites_with_force(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.yaml", "version: 1\n")
        write_file(tmp_path / "charmcraft.yaml", "name: new-charm\ntype: charm\n")

        result = artifacts_init(tmp_path, force=True)

        plan = load_artifacts_plan(result)
        assert plan.charms[0].name == "new-charm"

    def test_empty_repo(self, tmp_path: Path) -> None:
        result = artifacts_init(tmp_path)
        plan = load_artifacts_plan(result)
        assert plan.charms == []
        assert plan.rocks == []
        assert plan.snaps == []


class TestArtifactsBuild:
    """Tests for artifacts_build()."""

    def test_missing_artifacts_yaml_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="not found"):
            artifacts_build(tmp_path)

    def test_build_single_charm(self, tmp_path: Path) -> None:
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\ncharms:\n- name: mycharm\n  charmcraft-yaml: charmcraft.yaml\n",
        )
        write_file(tmp_path / "charmcraft.yaml", "name: mycharm\n")
        # Simulate charmcraft pack producing a .charm file
        write_file(tmp_path / "mycharm_ubuntu-22.04-amd64.charm", "fake charm")

        with patch("opcli.core.artifacts.run_command") as mock_run:
            result = artifacts_build(tmp_path)

        mock_run.assert_called_once()
        assert "charmcraft" in mock_run.call_args[0][0]
        gen = load_artifacts_build(result)
        assert len(gen.charms) == 1
        assert gen.charms[0].name == "mycharm"
        assert len(gen.charms[0].builds) == 1
        assert gen.charms[0].builds[0].path.startswith("./")
        assert gen.charms[0].builds[0].path.endswith(".charm")

    def test_build_multi_base_charm(self, tmp_path: Path) -> None:
        """Multi-base charm: all produced files appear as flat output entries."""
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\ncharms:\n- name: aproxy\n  charmcraft-yaml: charmcraft.yaml\n",
        )
        write_file(tmp_path / "charmcraft.yaml", "name: aproxy\n")
        # Simulate charmcraft pack producing three .charm files (one per base)
        write_file(tmp_path / "aproxy_ubuntu-20.04-amd64.charm", "fake")
        write_file(tmp_path / "aproxy_ubuntu-22.04-amd64.charm", "fake")
        write_file(tmp_path / "aproxy_ubuntu-24.04-amd64.charm", "fake")

        with patch("opcli.core.artifacts.run_command"):
            result = artifacts_build(tmp_path)

        gen = load_artifacts_build(result)
        outputs = gen.charms[0].builds
        expected_count = 3
        assert len(outputs) == expected_count
        paths = {o.path for o in outputs}
        assert "./aproxy_ubuntu-20.04-amd64.charm" in paths
        assert "./aproxy_ubuntu-22.04-amd64.charm" in paths
        assert "./aproxy_ubuntu-24.04-amd64.charm" in paths
        bases = {o.base for o in outputs}
        assert "ubuntu@20.04" in bases
        assert "ubuntu@22.04" in bases
        assert "ubuntu@24.04" in bases

    def test_build_multi_base_charm_at_separator(self, tmp_path: Path) -> None:
        """Multi-base charm with modern charmcraft ``@`` filename format."""
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\ncharms:\n- name: traefik-k8s\n  charmcraft-yaml: charmcraft.yaml\n",
        )
        write_file(tmp_path / "charmcraft.yaml", "name: traefik-k8s\n")
        # Modern charmcraft uses @ between distro and version
        write_file(tmp_path / "traefik-k8s_ubuntu@20.04-amd64.charm", "fake")
        write_file(tmp_path / "traefik-k8s_ubuntu@26.04-amd64.charm", "fake")

        with patch("opcli.core.artifacts.run_command"):
            result = artifacts_build(tmp_path)

        gen = load_artifacts_build(result)
        outputs = gen.charms[0].builds
        assert len(outputs) == 2  # noqa: PLR2004
        paths = {o.path for o in outputs}
        assert "./traefik-k8s_ubuntu@20.04-amd64.charm" in paths
        assert "./traefik-k8s_ubuntu@26.04-amd64.charm" in paths
        bases = {o.base for o in outputs}
        assert "ubuntu@20.04" in bases
        assert "ubuntu@26.04" in bases

    def test_build_multi_base_charm_incremental(self, tmp_path: Path) -> None:
        """Adding a new base: pre-existing files + new file all appear in output.

        charmcraft pack always rebuilds all declared bases, so after adding a
        base we must return all files in the output directory, not just new ones.
        """
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\ncharms:\n- name: aproxy\n  charmcraft-yaml: charmcraft.yaml\n",
        )
        write_file(tmp_path / "charmcraft.yaml", "name: aproxy\n")
        # Pre-existing file from a previous single-base build
        write_file(tmp_path / "aproxy_ubuntu-20.04-amd64.charm", "old")
        # charmcraft pack rebuilds ubuntu-20.04 AND produces ubuntu-22.04
        # (simulated: file already existed before, pack just overwrites)
        write_file(tmp_path / "aproxy_ubuntu-22.04-amd64.charm", "new")

        with patch("opcli.core.artifacts.run_command"):
            result = artifacts_build(tmp_path)

        gen = load_artifacts_build(result)
        outputs = gen.charms[0].builds
        expected_count = 2
        assert len(outputs) == expected_count
        paths = {o.path for o in outputs}
        assert "./aproxy_ubuntu-20.04-amd64.charm" in paths
        assert "./aproxy_ubuntu-22.04-amd64.charm" in paths

    def test_build_single_rock(self, tmp_path: Path) -> None:
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\nrocks:\n- name: myrock\n  rockcraft-yaml: rock_dir/rockcraft.yaml\n",
        )
        rock_dir = tmp_path / "rock_dir"
        rock_dir.mkdir()
        write_file(rock_dir / "rockcraft.yaml", "name: myrock\n")
        write_file(rock_dir / "myrock_1.0_amd64.rock", "fake rock")

        with patch("opcli.core.artifacts.run_command") as mock_run:
            result = artifacts_build(tmp_path)

        mock_run.assert_called_once()
        assert "rockcraft" in mock_run.call_args[0][0]
        gen = load_artifacts_build(result)
        assert len(gen.rocks) == 1
        assert gen.rocks[0].builds[0].file is not None
        assert gen.rocks[0].builds[0].file.startswith("./")

    def test_build_rock_overwrite_with_multiple_rocks_in_pack_dir(self, tmp_path: Path) -> None:
        """When multiple rocks share a pack-dir and rockcraft overwrites in place,
        the name-prefix fallback identifies the correct output file.
        """
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\nrocks:\n"
            "- name: garm\n  rockcraft-yaml: garm-rockcraft.yaml\n"
            "- name: planner\n  rockcraft-yaml: planner-rockcraft.yaml\n",
        )
        write_file(tmp_path / "garm-rockcraft.yaml", "name: garm\n")
        write_file(tmp_path / "planner-rockcraft.yaml", "name: planner\n")
        # Pre-existing .rock files from a previous build run
        write_file(tmp_path / "garm_0.1_amd64.rock", "old garm")
        write_file(tmp_path / "planner_0.1_amd64.rock", "old planner")

        # rockcraft pack overwrites the target file in place — no new files appear
        with patch("opcli.core.artifacts.run_command"):
            result = artifacts_build(tmp_path)

        gen = load_artifacts_build(result)
        expected_rock_count = 2
        assert len(gen.rocks) == expected_rock_count
        rock_names = {r.name for r in gen.rocks}
        assert rock_names == {"garm", "planner"}
        garm_file = next(r.builds[0].file for r in gen.rocks if r.name == "garm")
        planner_file = next(r.builds[0].file for r in gen.rocks if r.name == "planner")
        assert garm_file is not None and "garm" in garm_file
        assert planner_file is not None and "planner" in planner_file

    def test_build_rock_sets_experimental_extensions_env(self, tmp_path: Path) -> None:
        """Rockcraft pack must always pass ROCKCRAFT_ENABLE_EXPERIMENTAL_EXTENSIONS."""
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\nrocks:\n- name: myrock\n  rockcraft-yaml: rock_dir/rockcraft.yaml\n",
        )
        rock_dir = tmp_path / "rock_dir"
        rock_dir.mkdir()
        write_file(rock_dir / "rockcraft.yaml", "name: myrock\n")
        write_file(rock_dir / "myrock_1.0_amd64.rock", "fake rock")

        with patch("opcli.core.artifacts.run_command") as mock_run:
            artifacts_build(tmp_path)

        env_kwarg = mock_run.call_args.kwargs.get("env")
        assert env_kwarg is not None
        assert env_kwarg.get("ROCKCRAFT_ENABLE_EXPERIMENTAL_EXTENSIONS") == "1"

    def test_build_single_snap(self, tmp_path: Path) -> None:
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\nsnaps:\n- name: mysnap\n  snapcraft-yaml: snap_dir/snapcraft.yaml\n",
        )
        snap_dir = tmp_path / "snap_dir"
        snap_dir.mkdir()
        write_file(snap_dir / "snapcraft.yaml", "name: mysnap\n")
        write_file(snap_dir / "mysnap_1.0_amd64.snap", "fake snap")

        with patch("opcli.core.artifacts.run_command") as mock_run:
            result = artifacts_build(tmp_path)

        mock_run.assert_called_once()
        assert "snapcraft" in mock_run.call_args[0][0]
        gen = load_artifacts_build(result)
        assert len(gen.snaps) == 1

    def test_build_filtered_by_charm_name(self, tmp_path: Path) -> None:
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\ncharms:\n"
            "- name: charm-a\n  charmcraft-yaml: a/charmcraft.yaml\n"
            "- name: charm-b\n  charmcraft-yaml: b/charmcraft.yaml\n",
        )
        (tmp_path / "a").mkdir()
        write_file(tmp_path / "a" / "charmcraft.yaml", "name: charm-a\n")
        write_file(tmp_path / "a" / "charm-a_ubuntu-22.04-amd64.charm", "fake")

        with patch("opcli.core.artifacts.run_command"):
            result = artifacts_build(tmp_path, charm_names=["charm-a"])

        gen = load_artifacts_build(result)
        assert len(gen.charms) == 1
        assert gen.charms[0].name == "charm-a"

    def test_charm_filter_does_not_build_rocks(self, tmp_path: Path) -> None:
        """--charm only must not build rocks (each matrix job builds one artifact)."""
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\n"
            "rocks:\n- name: myrock\n  rockcraft-yaml: rock_dir/rockcraft.yaml\n"
            "charms:\n- name: mycharm\n  charmcraft-yaml: charm_dir/charmcraft.yaml\n",
        )
        (tmp_path / "charm_dir").mkdir()
        write_file(tmp_path / "charm_dir" / "charmcraft.yaml", "name: mycharm\n")
        write_file(tmp_path / "charm_dir" / "mycharm_ubuntu-22.04-amd64.charm", "fake")

        with patch("opcli.core.artifacts.run_command") as mock_run:
            artifacts_build(tmp_path, charm_names=["mycharm"])

        # Only charmcraft should have been called — rockcraft must not be invoked.
        for c in mock_run.call_args_list:
            cmd = c[0][0]
            assert "rockcraft" not in cmd[0], f"rockcraft unexpectedly invoked: {cmd}"

    def test_unknown_charm_name_raises(self, tmp_path: Path) -> None:
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\ncharms:\n- name: real\n  charmcraft-yaml: charmcraft.yaml\n",
        )
        with pytest.raises(ConfigurationError, match="Unknown charm"):
            artifacts_build(tmp_path, charm_names=["nonexistent"])

    def test_no_output_file_raises(self, tmp_path: Path) -> None:
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\nrocks:\n- name: myrock\n  rockcraft-yaml: rock_dir/rockcraft.yaml\n",
        )
        rock_dir = tmp_path / "rock_dir"
        rock_dir.mkdir()
        write_file(rock_dir / "rockcraft.yaml", "name: myrock\n")

        with (
            patch("opcli.core.artifacts.run_command"),
            pytest.raises(OpcliError, match=r"No \*.rock found"),
        ):
            artifacts_build(tmp_path)

    def test_build_monorepo(self, tmp_path: Path) -> None:
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\n"
            "rocks:\n- name: myrock\n  rockcraft-yaml: rock_dir/rockcraft.yaml\n"
            "charms:\n- name: mycharm\n  charmcraft-yaml: charmcraft.yaml\n",
        )
        (tmp_path / "rock_dir").mkdir()
        write_file(tmp_path / "rock_dir" / "rockcraft.yaml", "name: myrock\n")
        write_file(tmp_path / "rock_dir" / "myrock.rock", "fake")
        write_file(tmp_path / "charmcraft.yaml", "name: mycharm\n")
        write_file(tmp_path / "mycharm_ubuntu-22.04-amd64.charm", "fake")

        with patch("opcli.core.artifacts.run_command"):
            result = artifacts_build(tmp_path)

        gen = load_artifacts_build(result)
        assert len(gen.rocks) == 1
        assert len(gen.charms) == 1

    def test_build_propagates_resources_to_generated(self, tmp_path: Path) -> None:
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\n"
            "rocks:\n- name: myrock\n  rockcraft-yaml: rock_dir/rockcraft.yaml\n"
            "charms:\n- name: mycharm\n  charmcraft-yaml: charmcraft.yaml\n"
            "  resources:\n"
            "    myrock-image:\n"
            "      type: oci-image\n"
            "      rock: myrock\n",
        )
        (tmp_path / "rock_dir").mkdir()
        write_file(tmp_path / "rock_dir" / "rockcraft.yaml", "name: myrock\n")
        write_file(tmp_path / "rock_dir" / "myrock_1.0_amd64.rock", "fake")
        write_file(tmp_path / "charmcraft.yaml", "name: mycharm\n")
        write_file(tmp_path / "mycharm_ubuntu-22.04-amd64.charm", "fake")

        with patch("opcli.core.artifacts.run_command"):
            result = artifacts_build(tmp_path)

        gen = load_artifacts_build(result)
        charm = gen.charms[0]
        assert charm.resources is not None
        assert "myrock-image" in charm.resources
        res = charm.resources["myrock-image"]
        assert res.type == "oci-image"
        assert res.rock == "myrock"

    def test_resource_only_carries_rock_link(self, tmp_path: Path) -> None:
        """Resource referencing a rock only stores type + rock; no file/image."""
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\n"
            "charms:\n- name: mycharm\n  charmcraft-yaml: charmcraft.yaml\n"
            "  resources:\n"
            "    myrock-image:\n"
            "      type: oci-image\n"
            "      rock: nonexistent-rock\n",
        )
        write_file(tmp_path / "charmcraft.yaml", "name: mycharm\n")
        write_file(tmp_path / "mycharm_ubuntu-22.04-amd64.charm", "fake")

        with patch("opcli.core.artifacts.run_command"):
            result = artifacts_build(tmp_path)

        gen = load_artifacts_build(result)
        charm = gen.charms[0]
        assert charm.resources is not None
        res = charm.resources["myrock-image"]
        assert res.type == "oci-image"
        assert res.rock == "nonexistent-rock"

    def test_invalid_generated_fields_rejected(self, tmp_path: Path) -> None:
        write_file(
            tmp_path / "artifacts.build.yaml",
            "version: 1\ncharms:\n- name: c\n  source: .\n  builds:\n    file: ./c.charm\n",
        )
        with pytest.raises(Exception, match="validation error"):
            load_artifacts_build(tmp_path / "artifacts.build.yaml")

    def test_build_rock_with_pack_dir_creates_symlink(self, tmp_path: Path) -> None:
        """pack-dir: a temporary rockcraft.yaml symlink is created and removed."""
        rock_subdir = tmp_path / "rocks" / "myrock"
        rock_subdir.mkdir(parents=True)
        write_file(rock_subdir / "rockcraft.yaml", "name: myrock\n")
        # The .rock output lands in pack_dir (repo root), not rock_subdir
        write_file(tmp_path / "myrock_1.0_amd64.rock", "fake rock")

        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\n"
            "rocks:\n- name: myrock\n"
            "  rockcraft-yaml: rocks/myrock/rockcraft.yaml\n"
            "  pack-dir: .\n",
        )

        created_symlinks: list[Path] = []

        def fake_run(cmd: list[str], **kwargs: object) -> None:
            # Verify symlink exists during the build
            symlink = tmp_path / "rockcraft.yaml"
            assert symlink.is_symlink(), "symlink should exist while pack runs"
            created_symlinks.append(symlink)

        with patch("opcli.core.artifacts.run_command", side_effect=fake_run):
            result = artifacts_build(tmp_path)

        # Symlink must be removed after build
        assert not (tmp_path / "rockcraft.yaml").exists()
        gen = load_artifacts_build(result)
        assert gen.rocks[0].builds[0].file is not None

    def test_build_rock_pack_dir_real_file_raises(self, tmp_path: Path) -> None:
        """A real rockcraft.yaml with different content at pack-dir raises."""
        rock_subdir = tmp_path / "rocks" / "myrock"
        rock_subdir.mkdir(parents=True)
        write_file(rock_subdir / "rockcraft.yaml", "name: myrock\n")
        # Real file at the pack-dir location with DIFFERENT content
        write_file(tmp_path / "rockcraft.yaml", "name: other\n")

        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\n"
            "rocks:\n- name: myrock\n"
            "  rockcraft-yaml: rocks/myrock/rockcraft.yaml\n"
            "  pack-dir: .\n",
        )

        with (
            patch("opcli.core.artifacts.run_command"),
            pytest.raises(ConfigurationError, match="regular file already exists"),
        ):
            artifacts_build(tmp_path)

    def test_build_rock_pack_dir_identical_real_file_ok(self, tmp_path: Path) -> None:
        """A real rockcraft.yaml with identical content at pack-dir is allowed."""
        content = "name: myrock\n"
        rock_subdir = tmp_path / "rocks" / "myrock"
        rock_subdir.mkdir(parents=True)
        write_file(rock_subdir / "rockcraft.yaml", content)
        # Real file at the pack-dir location with identical content
        write_file(tmp_path / "rockcraft.yaml", content)
        write_file(tmp_path / "myrock_1.0_amd64.rock", "fake rock")

        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\n"
            "rocks:\n- name: myrock\n"
            "  rockcraft-yaml: rocks/myrock/rockcraft.yaml\n"
            "  pack-dir: .\n",
        )

        symlink_created: list[bool] = []

        def fake_run(cmd: list[str], **kwargs: object) -> None:
            symlink_created.append((tmp_path / "rockcraft.yaml").is_symlink())

        with patch("opcli.core.artifacts.run_command", side_effect=fake_run):
            result = artifacts_build(tmp_path)

        assert symlink_created == [False], "no symlink when content is identical"
        gen = load_artifacts_build(result)
        assert gen.rocks[0].builds[0].file is not None

    def test_build_rock_pack_dir_existing_symlink_replaced(self, tmp_path: Path) -> None:
        """An existing symlink at pack-dir is replaced without error."""
        rock_subdir = tmp_path / "rocks" / "myrock"
        rock_subdir.mkdir(parents=True)
        write_file(rock_subdir / "rockcraft.yaml", "name: myrock\n")
        write_file(tmp_path / "myrock_1.0_amd64.rock", "fake rock")

        # Pre-existing symlink pointing somewhere else
        existing_symlink = tmp_path / "rockcraft.yaml"
        existing_symlink.symlink_to("/dev/null")

        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\n"
            "rocks:\n- name: myrock\n"
            "  rockcraft-yaml: rocks/myrock/rockcraft.yaml\n"
            "  pack-dir: .\n",
        )

        with patch("opcli.core.artifacts.run_command"):
            result = artifacts_build(tmp_path)

        # Pre-existing symlink is restored to its original target after build
        assert existing_symlink.is_symlink()
        assert os.readlink(str(existing_symlink)) == "/dev/null"
        gen = load_artifacts_build(result)
        assert gen.rocks[0].builds[0].file is not None

    def test_build_rock_nonstandard_yaml_name_creates_symlink(self, tmp_path: Path) -> None:
        """Non-standard yaml name (e.g. planner-rockcraft.yaml) always gets a symlink.

        Even when pack-dir == dirname(yaml), rockcraft needs a file named
        'rockcraft.yaml'. A non-standard name must be symlinked.
        """
        write_file(tmp_path / "planner-rockcraft.yaml", "name: planner\n")
        write_file(tmp_path / "planner_1.0_amd64.rock", "fake rock")

        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\n"
            "rocks:\n- name: planner\n"
            "  rockcraft-yaml: planner-rockcraft.yaml\n"
            "  pack-dir: .\n",
        )

        symlink_seen: list[bool] = []
        symlink_target: list[str] = []

        def fake_run(cmd: list[str], **kwargs: object) -> None:
            symlink = tmp_path / "rockcraft.yaml"
            symlink_seen.append(symlink.is_symlink())
            if symlink.is_symlink():
                symlink_target.append(str(os.readlink(symlink)))

        with patch("opcli.core.artifacts.run_command", side_effect=fake_run):
            result = artifacts_build(tmp_path)

        assert symlink_seen == [True], "symlink must exist while pack runs"
        assert symlink_target == ["planner-rockcraft.yaml"], "symlink target must be relative"
        assert not (tmp_path / "rockcraft.yaml").exists(), "symlink removed after build"
        gen = load_artifacts_build(result)
        assert gen.rocks[0].builds[0].file is not None

    def test_build_rock_standard_yaml_name_no_symlink(self, tmp_path: Path) -> None:
        """When yaml is already named rockcraft.yaml in pack-dir, no symlink needed."""
        write_file(tmp_path / "rockcraft.yaml", "name: myrock\n")
        write_file(tmp_path / "myrock_1.0_amd64.rock", "fake rock")

        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\n"
            "rocks:\n- name: myrock\n"
            "  rockcraft-yaml: rockcraft.yaml\n"
            "  pack-dir: .\n",
        )

        symlink_created: list[bool] = []

        def fake_run(cmd: list[str], **kwargs: object) -> None:
            symlink = tmp_path / "rockcraft.yaml"
            symlink_created.append(symlink.is_symlink())

        with patch("opcli.core.artifacts.run_command", side_effect=fake_run):
            artifacts_build(tmp_path)

        assert symlink_created == [False], "no symlink should be created"

    def test_build_missing_yaml_raises(self, tmp_path: Path) -> None:
        """Missing yaml file raises ConfigurationError before running pack."""
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\nrocks:\n- name: myrock\n  rockcraft-yaml: nonexistent/rockcraft.yaml\n",
        )

        with (
            patch("opcli.core.artifacts.run_command") as mock_run,
            pytest.raises(ConfigurationError, match="rockcraft-yaml not found"),
        ):
            artifacts_build(tmp_path)

        mock_run.assert_not_called()

    def test_build_missing_pack_dir_raises(self, tmp_path: Path) -> None:
        """Missing pack-dir raises ConfigurationError before running pack."""
        write_file(tmp_path / "rockcraft.yaml", "name: myrock\n")
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\nrocks:\n- name: myrock\n"
            "  rockcraft-yaml: rockcraft.yaml\n"
            "  pack-dir: nonexistent-dir\n",
        )

        with (
            patch("opcli.core.artifacts.run_command") as mock_run,
            pytest.raises(ConfigurationError, match="pack-dir not found"),
        ):
            artifacts_build(tmp_path)

        mock_run.assert_not_called()

    def test_build_charm_nonstandard_yaml_name_creates_symlink(self, tmp_path: Path) -> None:
        """Non-standard charmcraft yaml name triggers symlink creation during build."""
        write_file(tmp_path / "charmcraft-mycharm.yaml", "name: mycharm\n")
        write_file(tmp_path / "mycharm_ubuntu-22.04-amd64.charm", "fake charm")

        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\ncharms:\n- name: mycharm\n  charmcraft-yaml: charmcraft-mycharm.yaml\n",
        )

        symlink_seen: list[bool] = []
        symlink_target: list[str] = []

        def fake_run(cmd: list[str], **kwargs: object) -> None:
            symlink = tmp_path / "charmcraft.yaml"
            symlink_seen.append(symlink.is_symlink())
            if symlink.is_symlink():
                symlink_target.append(str(os.readlink(symlink)))

        with patch("opcli.core.artifacts.run_command", side_effect=fake_run):
            artifacts_build(tmp_path)

        assert symlink_seen == [True], "symlink must exist while pack runs"
        assert symlink_target == ["charmcraft-mycharm.yaml"]
        assert not (tmp_path / "charmcraft.yaml").exists(), "symlink removed after build"

    def test_build_charm_real_charmcraft_yaml_same_content_ok(self, tmp_path: Path) -> None:
        """A real charmcraft.yaml with identical content to charmcraft-yaml is allowed.

        This handles repos that keep both charmcraft.yaml and charmcraft-mycharm.yaml
        as duplicate files.  Charmcraft will use the existing charmcraft.yaml and
        produce the correct charm — no symlink is needed, no error raised.
        """
        content = "name: mycharm\n"
        write_file(tmp_path / "charmcraft-mycharm.yaml", content)
        write_file(tmp_path / "charmcraft.yaml", content)  # identical content
        write_file(tmp_path / "mycharm_ubuntu-22.04-amd64.charm", "fake charm")

        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\ncharms:\n- name: mycharm\n  charmcraft-yaml: charmcraft-mycharm.yaml\n",
        )

        symlink_created: list[bool] = []

        def fake_run(cmd: list[str], **kwargs: object) -> None:
            symlink_created.append((tmp_path / "charmcraft.yaml").is_symlink())

        with patch("opcli.core.artifacts.run_command", side_effect=fake_run):
            artifacts_build(tmp_path)

        assert symlink_created == [False], "no symlink when content is identical"

    def test_build_charm_real_charmcraft_yaml_blocks_build(self, tmp_path: Path) -> None:
        """A real charmcraft.yaml in pack-dir that differs from charmcraft-yaml raises.

        This prevents silently building the wrong charm when the repo root
        already has a charmcraft.yaml pointing to a different charm.
        """
        write_file(tmp_path / "charmcraft-mycharm.yaml", "name: mycharm\n")
        write_file(tmp_path / "charmcraft.yaml", "name: some-other-charm\n")

        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\ncharms:\n- name: mycharm\n  charmcraft-yaml: charmcraft-mycharm.yaml\n",
        )

        with (
            patch("opcli.core.artifacts.run_command") as mock_run,
            pytest.raises(ConfigurationError, match="regular file already exists"),
        ):
            artifacts_build(tmp_path)

        mock_run.assert_not_called()

    def test_build_charm_standard_yaml_name_no_symlink(self, tmp_path: Path) -> None:
        """charmcraft-yaml named charmcraft.yaml in pack-dir needs no symlink."""
        write_file(tmp_path / "charmcraft.yaml", "name: mycharm\n")
        write_file(tmp_path / "mycharm_ubuntu-22.04-amd64.charm", "fake charm")

        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\ncharms:\n- name: mycharm\n  charmcraft-yaml: charmcraft.yaml\n",
        )

        symlink_created: list[bool] = []

        def fake_run(cmd: list[str], **kwargs: object) -> None:
            symlink = tmp_path / "charmcraft.yaml"
            symlink_created.append(symlink.is_symlink())

        with patch("opcli.core.artifacts.run_command", side_effect=fake_run):
            artifacts_build(tmp_path)

        assert symlink_created == [False], "no symlink should be created for standard name"

    def test_two_charms_same_pack_dir_no_cross_attribution(self, tmp_path: Path) -> None:
        """Two charms in the same pack-dir only claim their own output files.

        When charms share pack-dir (e.g. both yamls are in the repo root), the
        second charm's build must not inherit the first charm's .charm files.
        """
        charm1_content = "name: charm-a\n"
        charm2_content = "name: charm-b\n"
        write_file(tmp_path / "charmcraft-a.yaml", charm1_content)
        write_file(tmp_path / "charmcraft-b.yaml", charm2_content)

        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\ncharms:\n"
            "- name: charm-a\n  charmcraft-yaml: charmcraft-a.yaml\n"
            "- name: charm-b\n  charmcraft-yaml: charmcraft-b.yaml\n",
        )

        call_count = [0]

        def fake_run(cmd: list[str], **kwargs: object) -> None:
            call_count[0] += 1
            if call_count[0] == 1:
                # First charm pack produces charm-a files
                write_file(tmp_path / "charm-a_ubuntu-22.04-amd64.charm", "a1")
                write_file(tmp_path / "charm-a_ubuntu-24.04-amd64.charm", "a2")
            else:
                # Second charm pack produces charm-b files
                write_file(tmp_path / "charm-b_ubuntu-22.04-amd64.charm", "b1")
                write_file(tmp_path / "charm-b_ubuntu-24.04-amd64.charm", "b2")

        with patch("opcli.core.artifacts.run_command", side_effect=fake_run):
            result = artifacts_build(tmp_path)

        gen = load_artifacts_build(result)
        charm_a = next(c for c in gen.charms if c.name == "charm-a")
        charm_b = next(c for c in gen.charms if c.name == "charm-b")

        a_paths = {o.path for o in charm_a.builds}
        b_paths = {o.path for o in charm_b.builds}

        assert a_paths == {
            "./charm-a_ubuntu-22.04-amd64.charm",
            "./charm-a_ubuntu-24.04-amd64.charm",
        }, "charm-a must only claim its own output files"
        assert b_paths == {
            "./charm-b_ubuntu-22.04-amd64.charm",
            "./charm-b_ubuntu-24.04-amd64.charm",
        }, "charm-b must not inherit charm-a files"

    def test_pick_new_charm_output_overwrite_in_place_multi(self, tmp_path: Path) -> None:
        """Overwrite-in-place with multiple pre-existing charm files returns all.

        This is the multi-base scenario: charmcraft pack rebuilds the same set of
        files in-place (no new files appear). All pre-existing files are returned
        since they were all just rebuilt.
        """
        write_file(tmp_path / "charmcraft.yaml", "name: mycharm\n")
        write_file(tmp_path / "mycharm_ubuntu-22.04-amd64.charm", "old1")
        write_file(tmp_path / "mycharm_ubuntu-24.04-amd64.charm", "old2")

        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\ncharms:\n- name: mycharm\n  charmcraft-yaml: charmcraft.yaml\n",
        )

        with patch("opcli.core.artifacts.run_command"):
            result = artifacts_build(tmp_path)

        gen = load_artifacts_build(result)
        outputs = gen.charms[0].builds
        expected_count = 2
        assert len(outputs) == expected_count
        paths = {o.path for o in outputs}
        assert "./mycharm_ubuntu-22.04-amd64.charm" in paths
        assert "./mycharm_ubuntu-24.04-amd64.charm" in paths

    def test_charm_name_from_artifacts_yaml_not_charmcraft_yaml(self, tmp_path: Path) -> None:
        """Output matching uses charm.name from artifacts.yaml, not charmcraft.yaml.

        This covers the legacy split-format case where charmcraft.yaml has no
        'name' field (name lives in metadata.yaml).  Previously opcli tried to
        read the name from charmcraft.yaml and raised ConfigurationError; now it
        relies on the name declared in artifacts.yaml.
        """
        # charmcraft.yaml has no 'name' field (split format)
        write_file(tmp_path / "charmcraft.yaml", "type: charm\n")

        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\ncharms:\n- name: indico\n  charmcraft-yaml: charmcraft.yaml\n",
        )

        def fake_run(cmd: list[str], **kwargs: object) -> None:
            write_file(tmp_path / "indico_ubuntu-20.04-amd64.charm", "charm")

        with patch("opcli.core.artifacts.run_command", side_effect=fake_run):
            result = artifacts_build(tmp_path)

        gen = load_artifacts_build(result)
        assert len(gen.charms) == 1
        assert gen.charms[0].name == "indico"
        assert len(gen.charms[0].builds) == 1
        assert "indico_ubuntu-20.04-amd64.charm" in gen.charms[0].builds[0].path

    def test_symlink_not_removed_if_replaced_by_real_file(self, tmp_path: Path) -> None:
        """If pack replaces the symlink with a real file, cleanup does not delete it.

        A crafting tool could theoretically create a real charmcraft.yaml during
        the build (unlikely but possible). The cleanup must only remove symlinks,
        not real files.
        """
        write_file(tmp_path / "charmcraft-mycharm.yaml", "name: mycharm\n")
        write_file(tmp_path / "mycharm_ubuntu-22.04-amd64.charm", "fake charm")

        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\ncharms:\n- name: mycharm\n  charmcraft-yaml: charmcraft-mycharm.yaml\n",
        )

        def fake_run(cmd: list[str], **kwargs: object) -> None:
            # Simulate pack replacing the symlink with a real file
            symlink = tmp_path / "charmcraft.yaml"
            if symlink.is_symlink():
                symlink.unlink()
            write_file(symlink, "name: mycharm\n")  # real file now

        with patch("opcli.core.artifacts.run_command", side_effect=fake_run):
            artifacts_build(tmp_path)

        # Real file must still be there — cleanup must not have deleted it
        assert (tmp_path / "charmcraft.yaml").exists()
        assert not (tmp_path / "charmcraft.yaml").is_symlink()

    def test_filtered_build_merges_into_existing(self, tmp_path: Path) -> None:
        """Filtered build preserves previously built artifacts in the file."""
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\n"
            "rocks:\n- name: myrock\n  rockcraft-yaml: rock_dir/rockcraft.yaml\n"
            "charms:\n- name: mycharm\n  charmcraft-yaml: charmcraft.yaml\n",
        )
        (tmp_path / "rock_dir").mkdir()
        write_file(tmp_path / "rock_dir" / "rockcraft.yaml", "name: myrock\n")
        write_file(tmp_path / "rock_dir" / "myrock_1.0_amd64.rock", "fake")
        write_file(tmp_path / "charmcraft.yaml", "name: mycharm\n")
        write_file(tmp_path / "mycharm_ubuntu-22.04-amd64.charm", "fake")

        # First: build everything (creates artifacts.build.yaml with rock + charm)
        with patch("opcli.core.artifacts.run_command"):
            artifacts_build(tmp_path)

        # Second: rebuild only the charm with a filter
        with patch("opcli.core.artifacts.run_command"):
            result = artifacts_build(tmp_path, charm_names=["mycharm"])

        gen = load_artifacts_build(result)
        # Rock from previous build must still be present
        assert len(gen.rocks) == 1
        assert gen.rocks[0].name == "myrock"
        # Charm must be the freshly rebuilt one
        assert len(gen.charms) == 1
        assert gen.charms[0].name == "mycharm"

    def test_filtered_build_replaces_same_name_entry(self, tmp_path: Path) -> None:
        """Rebuilding an artifact replaces its entry, not duplicates it."""
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\nrocks:\n- name: myrock\n  rockcraft-yaml: rock_dir/rockcraft.yaml\n",
        )
        (tmp_path / "rock_dir").mkdir()
        write_file(tmp_path / "rock_dir" / "rockcraft.yaml", "name: myrock\n")
        write_file(tmp_path / "rock_dir" / "myrock_1.0_amd64.rock", "fake")

        # Build the rock once
        with patch("opcli.core.artifacts.run_command"):
            artifacts_build(tmp_path)

        # Rebuild same rock with filter — should replace, not duplicate
        with patch("opcli.core.artifacts.run_command"):
            result = artifacts_build(tmp_path, rock_names=["myrock"])

        gen = load_artifacts_build(result)
        assert len(gen.rocks) == 1
        assert gen.rocks[0].name == "myrock"

    def test_unfiltered_build_does_not_merge(self, tmp_path: Path) -> None:
        """Without filters, the build file is overwritten (no merge)."""
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\ncharms:\n- name: mycharm\n  charmcraft-yaml: charmcraft.yaml\n",
        )
        write_file(tmp_path / "charmcraft.yaml", "name: mycharm\n")
        write_file(tmp_path / "mycharm_ubuntu-22.04-amd64.charm", "fake")

        # Manually seed a build file with a rock entry that shouldn't survive
        write_file(
            tmp_path / "artifacts.build.yaml",
            "version: 1\nrocks:\n- name: leftover\n  rockcraft-yaml: x.yaml\n"
            "  builds:\n  - arch: amd64\n    file: ./x.rock\n",
        )

        with patch("opcli.core.artifacts.run_command"):
            result = artifacts_build(tmp_path)

        gen = load_artifacts_build(result)
        # Unfiltered build should overwrite entirely — no leftover rock
        assert len(gen.rocks) == 0
        assert len(gen.charms) == 1

    def test_build_timeout_passed_to_run_command(self, tmp_path: Path) -> None:
        """build_timeout is forwarded to run_command as the timeout kwarg."""
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\ncharms:\n- name: mycharm\n  charmcraft-yaml: charmcraft.yaml\n",
        )
        write_file(tmp_path / "charmcraft.yaml", "name: mycharm\n")
        write_file(tmp_path / "mycharm_ubuntu-22.04-amd64.charm", "fake charm")

        custom_timeout = 7200
        with patch("opcli.core.artifacts.run_command") as mock_run:
            artifacts_build(tmp_path, build_timeout=custom_timeout)

        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs.get("timeout") == custom_timeout


class TestArtifactsMatrix:
    """Tests for artifacts_matrix()."""

    def test_returns_include_list_for_all_types(self, tmp_path: Path) -> None:
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\n"
            "rocks:\n- name: my-rock\n  rockcraft-yaml: rockcraft.yaml\n"
            "charms:\n- name: my-charm\n  charmcraft-yaml: charmcraft.yaml\n"
            "snaps:\n- name: my-snap\n  snapcraft-yaml: snap/snapcraft.yaml\n",
        )

        result = artifacts_matrix(tmp_path)

        assert result == {
            "include": [
                {
                    "name": "my-rock",
                    "type": "rock",
                    "arch": "amd64",
                    "runner": '["ubuntu-latest"]',
                    "rockcraft-yaml": "rockcraft.yaml",
                    "pack-dir": "",
                },
                {
                    "name": "my-charm",
                    "type": "charm",
                    "arch": "amd64",
                    "runner": '["ubuntu-latest"]',
                },
                {
                    "name": "my-snap",
                    "type": "snap",
                    "arch": "amd64",
                    "runner": '["ubuntu-latest"]',
                },
            ]
        }

    def test_rock_matrix_includes_pack_dir_when_set(self, tmp_path: Path) -> None:
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\n"
            "rocks:\n- name: my-rock\n  rockcraft-yaml: rocks/rockcraft.yaml\n  pack-dir: .\n",
        )

        result = artifacts_matrix(tmp_path)

        rock_entry = result["include"][0]
        assert rock_entry["rockcraft-yaml"] == "rocks/rockcraft.yaml"
        assert rock_entry["pack-dir"] == "."

    def test_rock_matrix_pack_dir_empty_string_when_null(self, tmp_path: Path) -> None:
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\nrocks:\n- name: my-rock\n  rockcraft-yaml: rockcraft.yaml\n",
        )

        result = artifacts_matrix(tmp_path)

        rock_entry = result["include"][0]
        assert rock_entry["pack-dir"] == ""

    def test_only_charms_no_rocks_no_snaps(self, tmp_path: Path) -> None:
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\ncharms:\n- name: my-charm\n  charmcraft-yaml: charmcraft.yaml\n",
        )

        result = artifacts_matrix(tmp_path)

        assert result == {
            "include": [
                {
                    "name": "my-charm",
                    "type": "charm",
                    "arch": "amd64",
                    "runner": '["ubuntu-latest"]',
                }
            ]
        }

    def test_empty_artifacts_yaml_returns_empty_include(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.yaml", "version: 1\n")

        result = artifacts_matrix(tmp_path)

        assert result == {"include": []}

    def test_missing_artifacts_yaml_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match=r"artifacts\.yaml"):
            artifacts_matrix(tmp_path)

    def test_result_is_json_serializable(self, tmp_path: Path) -> None:
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\n"
            "rocks:\n- name: my-rock\n  rockcraft-yaml: rockcraft.yaml\n"
            "charms:\n- name: my-charm\n  charmcraft-yaml: charmcraft.yaml\n",
        )

        result = artifacts_matrix(tmp_path)

        serialized = json.dumps(result)
        assert json.loads(serialized) == result


class TestArtifactsCollect:
    """Tests for artifacts_collect()."""

    def _partial(
        self,
        tmp_path: Path,
        name: str,
        content: str,
    ) -> Path:
        p = tmp_path / name / "artifacts.build.yaml"
        write_file(p, content)
        return p

    def test_merges_rock_and_charm_partials(self, tmp_path: Path) -> None:
        rock_partial = self._partial(
            tmp_path,
            "rock-job",
            "version: 1\n"
            "rocks:\n- name: my-rock\n  rockcraft-yaml: rockcraft.yaml\n"
            "  builds:\n  - arch: amd64\n    file: ./my-rock_1.0_amd64.rock\n",
        )
        charm_partial = self._partial(
            tmp_path,
            "charm-job",
            "version: 1\n"
            "charms:\n- name: my-charm\n  charmcraft-yaml: charmcraft.yaml\n"
            "  builds:\n  - arch: amd64\n"
            "    path: ./my-charm_ubuntu-24.04-amd64.charm\n"
            "    base: ubuntu@24.04\n",
        )

        dest = tmp_path / "artifacts.build.yaml"
        artifacts_collect(tmp_path, [rock_partial, charm_partial])

        gen = load_artifacts_build(dest)
        assert len(gen.rocks) == 1
        assert len(gen.charms) == 1
        assert gen.rocks[0].name == "my-rock"
        assert gen.charms[0].name == "my-charm"

    def test_fills_charm_resource_from_merged_rock(self, tmp_path: Path) -> None:
        """Collect validates rock reference; image lives on rock, not resource."""
        rock_partial = self._partial(
            tmp_path,
            "rock-job",
            "version: 1\n"
            "rocks:\n- name: my-rock\n  rockcraft-yaml: rockcraft.yaml\n"
            "  builds:\n  - arch: amd64\n    file: ./my-rock_1.0_amd64.rock\n",
        )
        charm_partial = self._partial(
            tmp_path,
            "charm-job",
            "version: 1\n"
            "charms:\n- name: my-charm\n  charmcraft-yaml: charmcraft.yaml\n"
            "  builds:\n  - arch: amd64\n"
            "    path: ./my-charm_ubuntu-24.04-amd64.charm\n"
            "    base: ubuntu@24.04\n"
            "  resources:\n"
            "    my-rock-image:\n"
            "      type: oci-image\n"
            "      rock: my-rock\n",
        )

        artifacts_collect(tmp_path, [rock_partial, charm_partial])

        gen = load_artifacts_build(tmp_path / "artifacts.build.yaml")
        # Image lives on the rock, not on the resource
        assert gen.rocks[0].builds[0].file == "./my-rock_1.0_amd64.rock"
        resource = gen.charms[0].resources["my-rock-image"]  # type: ignore[index]
        assert resource.rock == "my-rock"

    def test_merges_multiple_rocks(self, tmp_path: Path) -> None:
        rock1 = self._partial(
            tmp_path,
            "rock1-job",
            "version: 1\n"
            "rocks:\n- name: rock-a\n  rockcraft-yaml: rock-a/rockcraft.yaml\n"
            "  builds:\n  - arch: amd64\n    file: ./rock-a_1.0_amd64.rock\n",
        )
        rock2 = self._partial(
            tmp_path,
            "rock2-job",
            "version: 1\n"
            "rocks:\n- name: rock-b\n  rockcraft-yaml: rock-b/rockcraft.yaml\n"
            "  builds:\n  - arch: amd64\n    file: ./rock-b_1.0_amd64.rock\n",
        )

        artifacts_collect(tmp_path, [rock1, rock2])

        gen = load_artifacts_build(tmp_path / "artifacts.build.yaml")
        expected_count = 2
        assert len(gen.rocks) == expected_count
        names = {r.name for r in gen.rocks}
        assert names == {"rock-a", "rock-b"}

    def test_no_partials_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="partial"):
            artifacts_collect(tmp_path, [])

    def test_missing_partial_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="not found"):
            artifacts_collect(tmp_path, [tmp_path / "nonexistent.yaml"])

    def test_missing_rock_partial_raises(self, tmp_path: Path) -> None:
        """Charm references a rock that has no corresponding partial — must fail."""
        charm_partial = self._partial(
            tmp_path,
            "charm-job",
            "version: 1\n"
            "charms:\n- name: my-charm\n  charmcraft-yaml: charmcraft.yaml\n"
            "  builds:\n  - arch: amd64\n"
            "    path: ./my-charm_ubuntu-24.04-amd64.charm\n"
            "    base: ubuntu@24.04\n"
            "  resources:\n"
            "    missing-rock-image:\n"
            "      type: oci-image\n"
            "      rock: missing-rock\n",
        )

        with pytest.raises(ConfigurationError, match="missing-rock"):
            artifacts_collect(tmp_path, [charm_partial])

    def test_duplicate_rock_names_across_partials_raises(self, tmp_path: Path) -> None:
        """Two partials with the same rock name must be rejected."""
        rock1 = self._partial(
            tmp_path,
            "rock-job-1",
            "version: 1\n"
            "rocks:\n- name: my-rock\n  rockcraft-yaml: rockcraft.yaml\n"
            "  builds:\n  - arch: amd64\n    file: ./my-rock_1.0_amd64.rock\n",
        )
        rock2 = self._partial(
            tmp_path,
            "rock-job-2",
            "version: 1\n"
            "rocks:\n- name: my-rock\n  rockcraft-yaml: rockcraft.yaml\n"
            "  builds:\n  - arch: amd64\n    file: ./my-rock_2.0_amd64.rock\n",
        )

        with pytest.raises(ConfigurationError, match="my-rock"):
            artifacts_collect(tmp_path, [rock1, rock2])

    def test_merges_same_rock_different_arches(self, tmp_path: Path) -> None:
        """Same rock name, different arches → output lists are merged."""
        rock_amd64 = self._partial(
            tmp_path,
            "rock-amd64-job",
            "version: 1\n"
            "rocks:\n- name: my-rock\n  rockcraft-yaml: rockcraft.yaml\n"
            "  builds:\n  - arch: amd64\n    file: ./my-rock_1.0_amd64.rock\n",
        )
        rock_arm64 = self._partial(
            tmp_path,
            "rock-arm64-job",
            "version: 1\n"
            "rocks:\n- name: my-rock\n  rockcraft-yaml: rockcraft.yaml\n"
            "  builds:\n  - arch: arm64\n    file: ./my-rock_1.0_arm64.rock\n",
        )

        artifacts_collect(tmp_path, [rock_amd64, rock_arm64])

        gen = load_artifacts_build(tmp_path / "artifacts.build.yaml")
        assert len(gen.rocks) == 1
        assert gen.rocks[0].name == "my-rock"
        expected_arch_count = 2
        assert len(gen.rocks[0].builds) == expected_arch_count
        arches = {b.arch for b in gen.rocks[0].builds}
        assert arches == {"amd64", "arm64"}

    def test_merges_same_charm_different_arches(self, tmp_path: Path) -> None:
        """Same charm name, different arches → output lists are merged."""
        charm_amd64 = self._partial(
            tmp_path,
            "charm-amd64-job",
            "version: 1\n"
            "charms:\n- name: my-charm\n  charmcraft-yaml: charmcraft.yaml\n"
            "  builds:\n  - arch: amd64\n"
            "    path: ./my-charm_ubuntu-24.04-amd64.charm\n"
            "    base: ubuntu@24.04\n",
        )
        charm_arm64 = self._partial(
            tmp_path,
            "charm-arm64-job",
            "version: 1\n"
            "charms:\n- name: my-charm\n  charmcraft-yaml: charmcraft.yaml\n"
            "  builds:\n  - arch: arm64\n"
            "    path: ./my-charm_ubuntu-24.04-arm64.charm\n"
            "    base: ubuntu@24.04\n",
        )

        artifacts_collect(tmp_path, [charm_amd64, charm_arm64])

        gen = load_artifacts_build(tmp_path / "artifacts.build.yaml")
        assert len(gen.charms) == 1
        assert gen.charms[0].name == "my-charm"
        expected_arch_count = 2
        assert len(gen.charms[0].builds) == expected_arch_count
        arches = {b.arch for b in gen.charms[0].builds}
        assert arches == {"amd64", "arm64"}

    def test_merges_same_snap_different_arches(self, tmp_path: Path) -> None:
        """Same snap name, different arches → output lists are merged."""
        snap_amd64 = self._partial(
            tmp_path,
            "snap-amd64-job",
            "version: 1\n"
            "snaps:\n- name: my-snap\n  snapcraft-yaml: snap/snapcraft.yaml\n"
            "  builds:\n  - arch: amd64\n    file: ./my-snap_1.0_amd64.snap\n",
        )
        snap_arm64 = self._partial(
            tmp_path,
            "snap-arm64-job",
            "version: 1\n"
            "snaps:\n- name: my-snap\n  snapcraft-yaml: snap/snapcraft.yaml\n"
            "  builds:\n  - arch: arm64\n    file: ./my-snap_1.0_arm64.snap\n",
        )

        artifacts_collect(tmp_path, [snap_amd64, snap_arm64])

        gen = load_artifacts_build(tmp_path / "artifacts.build.yaml")
        assert len(gen.snaps) == 1
        assert gen.snaps[0].name == "my-snap"
        expected_arch_count = 2
        assert len(gen.snaps[0].builds) == expected_arch_count
        arches = {b.arch for b in gen.snaps[0].builds}
        assert arches == {"amd64", "arm64"}


class TestArtifactsBuildCIMode:
    """Tests for artifacts_build() GitHub Actions CI output format."""

    _CI_ENV: ClassVar[dict[str, str]] = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_RUN_ID": "9876543210",
        "GITHUB_REPOSITORY_OWNER": "MyOrg",
        "GITHUB_REPOSITORY": "MyOrg/my-repo",
        "GITHUB_SHA": "abc1234def5678",
    }

    def test_rock_build_pushes_to_ghcr_and_writes_image_ref(self, tmp_path: Path) -> None:
        """In CI, rock output should be a GHCR image ref, not a local file."""
        write_file(tmp_path / "rockcraft.yaml", "name: my-rock\n")
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\nrocks:\n- name: my-rock\n  rockcraft-yaml: rockcraft.yaml\n",
        )
        rock_file = tmp_path / "my-rock_1.0_amd64.rock"
        rock_file.write_bytes(b"fake rock")

        with (
            patch("opcli.core.artifacts.run_command") as mock_run,
            patch.dict(os.environ, self._CI_ENV, clear=False),
        ):
            mock_run.side_effect = lambda cmd, **_: rock_file.touch()
            result = artifacts_build(tmp_path, rock_names=["my-rock"])

        gen = load_artifacts_build(result)
        assert len(gen.rocks) == 1
        rock_out = gen.rocks[0].builds
        assert rock_out[0].file is None
        assert rock_out[0].image == "ghcr.io/myorg/my-repo/my-rock:abc1234-amd64"

        # Verify skopeo was called to push to GHCR
        skopeo_calls = [c for c in mock_run.call_args_list if "skopeo" in str(c)]
        assert len(skopeo_calls) == 1
        skopeo_args = skopeo_calls[0][0][0]
        assert "skopeo" in skopeo_args
        image_ref = "ghcr.io/myorg/my-repo/my-rock:abc1234-amd64"
        assert any(image_ref in a for a in skopeo_args)

    def test_charm_build_writes_artifact_ref(self, tmp_path: Path) -> None:
        """In CI, charm output should be a GitHub artifact reference."""
        write_file(tmp_path / "charmcraft.yaml", "name: my-charm\n")
        write_file(
            tmp_path / "artifacts.yaml",
            ("version: 1\ncharms:\n- name: my-charm\n  charmcraft-yaml: charmcraft.yaml\n"),
        )
        charm_file = tmp_path / "my-charm_ubuntu-24.04-amd64.charm"

        with (
            patch("opcli.core.artifacts.run_command") as mock_run,
            patch.dict(os.environ, self._CI_ENV, clear=False),
        ):
            mock_run.side_effect = lambda cmd, **_: charm_file.touch()
            result = artifacts_build(tmp_path, charm_names=["my-charm"])

        gen = load_artifacts_build(result)
        assert len(gen.charms) == 1
        charm_out = gen.charms[0].builds
        assert charm_out[0].path is None
        assert charm_out[0].artifact == "built-charm-my-charm-amd64"
        assert charm_out[0].run_id == "9876543210"

    def test_snap_build_writes_artifact_ref(self, tmp_path: Path) -> None:
        """In CI, snap output should be a GitHub artifact reference."""
        write_file(tmp_path / "snap" / "snapcraft.yaml", "name: my-snap\n")
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\nsnaps:\n- name: my-snap\n  snapcraft-yaml: snap/snapcraft.yaml\n",
        )
        snap_file = tmp_path / "snap" / "my-snap_1.0_amd64.snap"

        with (
            patch("opcli.core.artifacts.run_command") as mock_run,
            patch.dict(os.environ, self._CI_ENV, clear=False),
        ):
            mock_run.side_effect = lambda cmd, **_: snap_file.touch()
            result = artifacts_build(tmp_path, snap_names=["my-snap"])

        gen = load_artifacts_build(result)
        assert len(gen.snaps) == 1
        snap_out = gen.snaps[0].builds
        assert snap_out[0].file is None
        assert snap_out[0].artifact == "built-snap-my-snap-amd64"
        assert snap_out[0].run_id == "9876543210"

    def test_local_build_unchanged_when_no_github_actions(self, tmp_path: Path) -> None:
        """Without GITHUB_ACTIONS=true, build produces local file refs."""
        write_file(tmp_path / "charmcraft.yaml", "name: my-charm\n")
        write_file(
            tmp_path / "artifacts.yaml",
            ("version: 1\ncharms:\n- name: my-charm\n  charmcraft-yaml: charmcraft.yaml\n"),
        )
        charm_file = tmp_path / "my-charm_ubuntu-24.04-amd64.charm"

        with (
            patch("opcli.core.artifacts.run_command") as mock_run,
            patch.dict(os.environ, {"GITHUB_ACTIONS": ""}, clear=False),
        ):
            mock_run.side_effect = lambda cmd, **_: charm_file.touch()
            result = artifacts_build(tmp_path, charm_names=["my-charm"])

        gen = load_artifacts_build(result)
        charm_out = gen.charms[0].builds
        assert charm_out[0].artifact is None
        assert len(charm_out) == 1
        assert "my-charm_ubuntu-24.04-amd64.charm" in charm_out[0].path

    def test_ci_missing_env_vars_raises(self, tmp_path: Path) -> None:
        """GITHUB_ACTIONS=true with missing env vars raises ConfigurationError."""
        write_file(tmp_path / "charmcraft.yaml", "name: my-charm\n")
        write_file(
            tmp_path / "artifacts.yaml",
            ("version: 1\ncharms:\n- name: my-charm\n  charmcraft-yaml: charmcraft.yaml\n"),
        )
        charm_file = tmp_path / "my-charm_ubuntu-24.04-amd64.charm"

        with (
            patch("opcli.core.artifacts.run_command") as mock_run,
            patch.dict(
                os.environ,
                {
                    "GITHUB_ACTIONS": "true",
                    "GITHUB_RUN_ID": "",
                    "GITHUB_REPOSITORY_OWNER": "",
                    "GITHUB_REPOSITORY": "",
                    "GITHUB_SHA": "",
                },
                clear=False,
            ),
            pytest.raises(ConfigurationError, match="GITHUB_RUN_ID"),
        ):
            mock_run.side_effect = lambda cmd, **_: charm_file.touch()
            artifacts_build(tmp_path, charm_names=["my-charm"])

    def test_owner_is_lowercased(self, tmp_path: Path) -> None:
        """GITHUB_REPOSITORY_OWNER is lowercased in the image ref."""
        write_file(tmp_path / "rockcraft.yaml", "name: my-rock\n")
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\nrocks:\n- name: my-rock\n  rockcraft-yaml: rockcraft.yaml\n",
        )
        rock_file = tmp_path / "my-rock_1.0_amd64.rock"
        rock_file.write_bytes(b"fake rock")

        with (
            patch("opcli.core.artifacts.run_command") as mock_run,
            patch.dict(os.environ, self._CI_ENV, clear=False),
        ):
            mock_run.side_effect = lambda cmd, **_: rock_file.touch()
            result = artifacts_build(tmp_path, rock_names=["my-rock"])

        gen = load_artifacts_build(result)
        assert gen.rocks[0].builds[0].image is not None
        assert "MyOrg" not in gen.rocks[0].builds[0].image
        assert "myorg" in gen.rocks[0].builds[0].image


class TestArtifactsCollectCIMode:
    """Tests for artifacts_collect() with CI-format (image/artifact) partials."""

    def _partial(self, tmp_path: Path, name: str, content: str) -> Path:
        p = tmp_path / name / "artifacts.build.yaml"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return p

    def test_collect_fills_charm_resource_image_from_ghcr_rock(self, tmp_path: Path) -> None:
        """Collect merges partials; rock GHCR image lives on rock, not the resource."""
        rock_partial = self._partial(
            tmp_path,
            "rock-job",
            "version: 1\n"
            "rocks:\n- name: my-rock\n  rockcraft-yaml: rockcraft.yaml\n"
            "  builds:\n  - arch: amd64\n"
            "    image: ghcr.io/myorg/my-repo/my-rock:abc1234\n",
        )
        charm_partial = self._partial(
            tmp_path,
            "charm-job",
            "version: 1\n"
            "charms:\n- name: my-charm\n  charmcraft-yaml: charmcraft.yaml\n"
            "  builds:\n  - arch: amd64\n    artifact: built-charm-my-charm\n"
            "    run-id: '9876543210'\n"
            "  resources:\n"
            "    my-rock-image:\n"
            "      type: oci-image\n"
            "      rock: my-rock\n",
        )

        artifacts_collect(tmp_path, [rock_partial, charm_partial])

        gen = load_artifacts_build(tmp_path / "artifacts.build.yaml")
        assert gen.rocks[0].builds[0].image == "ghcr.io/myorg/my-repo/my-rock:abc1234"
        resource = gen.charms[0].resources["my-rock-image"]  # type: ignore[index]
        # Resource carries the rock reference; image resolved from rock.builds.image
        assert resource.rock == "my-rock"
        # Charm itself still has artifact ref
        assert gen.charms[0].builds[0].artifact == "built-charm-my-charm"
        assert gen.charms[0].builds[0].run_id == "9876543210"


class TestArtifactsLocalize:
    """Tests for artifacts_localize()."""

    _GENERATED_CI = (
        "version: 1\n"
        "charms:\n"
        "- name: my-charm\n"
        "  charmcraft-yaml: charmcraft.yaml\n"
        "  builds:\n"
        "  - arch: amd64\n"
        "    artifact: built-charm-my-charm\n"
        "    run-id: '9876543210'\n"
    )

    def test_localises_charm_from_downloaded_file(self, tmp_path: Path) -> None:
        """Finds .charm file and updates output.files."""
        write_file(tmp_path / "artifacts.build.yaml", self._GENERATED_CI)
        charm_file = tmp_path / "my-charm_ubuntu-24.04-amd64.charm"
        charm_file.write_bytes(b"")

        count = artifacts_localize(tmp_path)

        assert count == 1
        gen = load_artifacts_build(tmp_path / "artifacts.build.yaml")
        assert len(gen.charms[0].builds) == 1
        path = gen.charms[0].builds[0].path
        assert path is not None
        assert path.endswith(".charm")
        assert path.startswith("./"), f"Expected relative path, got: {path}"
        assert "/home/" not in path, f"Expected no absolute home path, got: {path}"

    def test_skips_charm_already_with_local_files(self, tmp_path: Path) -> None:
        """Does not overwrite charms that already have output path."""
        generated = (
            "version: 1\n"
            "charms:\n"
            "- name: my-charm\n"
            "  charmcraft-yaml: charmcraft.yaml\n"
            "  builds:\n"
            "  - arch: amd64\n"
            "    path: ./my-charm_ubuntu-24.04-amd64.charm\n"
        )
        write_file(tmp_path / "artifacts.build.yaml", generated)
        charm_file = tmp_path / "my-charm_new.charm"
        charm_file.write_bytes(b"")

        count = artifacts_localize(tmp_path)

        assert count == 0

    def test_raises_when_no_charm_file_found(self, tmp_path: Path) -> None:
        """Raises ConfigurationError when a CI-ref charm has no matching .charm file."""
        write_file(tmp_path / "artifacts.build.yaml", self._GENERATED_CI)

        with pytest.raises(ConfigurationError, match="my-charm"):
            artifacts_localize(tmp_path)

    def test_missing_generated_yaml_raises(self, tmp_path: Path) -> None:
        """Raises ConfigurationError when artifacts.build.yaml is missing."""
        with pytest.raises(ConfigurationError):
            artifacts_localize(tmp_path)

    def test_skips_charm_without_artifact_ref(self, tmp_path: Path) -> None:
        """Skips charms that have no CI artifact ref."""
        generated = (
            "version: 1\n"
            "charms:\n"
            "- name: my-charm\n"
            "  charmcraft-yaml: charmcraft.yaml\n"
            "  builds:\n"
            "  - arch: amd64\n"
            "    path: ./my-charm_ubuntu-24.04-amd64.charm\n"
        )
        write_file(tmp_path / "artifacts.build.yaml", generated)
        # Create a second charm file — should not be picked up since charm
        # already has output.files
        (tmp_path / "my-charm_new.charm").write_bytes(b"")

        count = artifacts_localize(tmp_path)

        assert count == 0

    def test_does_not_match_charm_with_longer_prefix_name(self, tmp_path: Path) -> None:
        """Does not pick up 'my-charm-k8s_*.charm' when localising 'my-charm'."""
        write_file(tmp_path / "artifacts.build.yaml", self._GENERATED_CI)
        # Only the longer-prefix file exists — pattern must NOT match it
        (tmp_path / "my-charm-k8s_ubuntu-24.04-amd64.charm").write_bytes(b"")

        with pytest.raises(ConfigurationError, match="my-charm"):
            artifacts_localize(tmp_path)

    def test_localises_all_files_for_multi_base_charm(self, tmp_path: Path) -> None:
        """Populates output.files with all per-base .charm files."""
        write_file(tmp_path / "artifacts.build.yaml", self._GENERATED_CI)
        (tmp_path / "my-charm_ubuntu-22.04-amd64.charm").write_bytes(b"")
        (tmp_path / "my-charm_ubuntu-24.04-amd64.charm").write_bytes(b"")

        artifacts_localize(tmp_path)

        gen = load_artifacts_build(tmp_path / "artifacts.build.yaml")
        charm = gen.charms[0]
        assert len(charm.builds) == 2  # noqa: PLR2004
        paths = {o.path for o in charm.builds}
        assert any("22.04" in p for p in paths)
        assert any("24.04" in p for p in paths)
        bases = {o.base for o in charm.builds}
        assert "ubuntu@22.04" in bases
        assert "ubuntu@24.04" in bases

    def test_localises_all_files_for_multi_base_charm_at_separator(self, tmp_path: Path) -> None:
        """Localize populates base correctly for modern ``@`` filename format."""
        write_file(tmp_path / "artifacts.build.yaml", self._GENERATED_CI)
        # Modern charmcraft uses @ between distro and version
        (tmp_path / "my-charm_ubuntu@22.04-amd64.charm").write_bytes(b"")
        (tmp_path / "my-charm_ubuntu@24.04-amd64.charm").write_bytes(b"")

        artifacts_localize(tmp_path)

        gen = load_artifacts_build(tmp_path / "artifacts.build.yaml")
        charm = gen.charms[0]
        assert len(charm.builds) == 2  # noqa: PLR2004
        paths = {o.path for o in charm.builds}
        assert any("22.04" in p for p in paths)
        assert any("24.04" in p for p in paths)
        bases = {o.base for o in charm.builds}
        assert "ubuntu@22.04" in bases
        assert "ubuntu@24.04" in bases


class TestArtifactsFetch:
    """Tests for artifacts_fetch(arch="all") — all-architectures path."""

    # Merged artifacts.build.yaml — matches the union of _PARTIAL_FILES below.
    _GENERATED_CI = (
        "version: 1\n"
        "rocks:\n"
        "- name: my-rock\n"
        "  rockcraft-yaml: rock/rockcraft.yaml\n"
        "  builds:\n"
        "  - arch: amd64\n"
        "    image: ghcr.io/owner/repo/my-rock:abc1234-amd64\n"
        "charms:\n"
        "- name: my-charm\n"
        "  charmcraft-yaml: charmcraft.yaml\n"
        "  builds:\n"
        "  - arch: amd64\n"
        "    artifact: built-charm-my-charm-amd64\n"
        "    run-id: '99887766'\n"
        "- name: other-charm\n"
        "  charmcraft-yaml: other/charmcraft.yaml\n"
        "  builds:\n"
        "  - arch: amd64\n"
        "    artifact: built-charm-other-charm-amd64\n"
        "    run-id: '99887766'\n"
        "snaps:\n"
        "- name: my-snap\n"
        "  snapcraft-yaml: snap/snapcraft.yaml\n"
        "  builds:\n"
        "  - arch: amd64\n"
        "    artifact: built-snap-my-snap-amd64\n"
        "    run-id: '99887766'\n"
    )

    # artifacts.yaml for all four artifact types above (default platform = amd64).
    _PLAN_YAML: ClassVar[str] = (
        "version: 1\n"
        "rocks:\n"
        "- name: my-rock\n"
        "  rockcraft-yaml: rock/rockcraft.yaml\n"
        "charms:\n"
        "- name: my-charm\n"
        "  charmcraft-yaml: charmcraft.yaml\n"
        "- name: other-charm\n"
        "  charmcraft-yaml: other/charmcraft.yaml\n"
        "snaps:\n"
        "- name: my-snap\n"
        "  snapcraft-yaml: snap/snapcraft.yaml\n"
    )

    # One partial per artifact; their union equals _GENERATED_CI.
    _PARTIAL_FILES: ClassVar[dict[str, str]] = {
        "artifacts-build-rock-my-rock-amd64": (
            "version: 1\n"
            "rocks:\n"
            "- name: my-rock\n"
            "  rockcraft-yaml: rock/rockcraft.yaml\n"
            "  builds:\n"
            "  - arch: amd64\n"
            "    image: ghcr.io/owner/repo/my-rock:abc1234-amd64\n"
            "charms: []\n"
            "snaps: []\n"
        ),
        "artifacts-build-charm-my-charm-amd64": (
            "version: 1\n"
            "rocks: []\n"
            "charms:\n"
            "- name: my-charm\n"
            "  charmcraft-yaml: charmcraft.yaml\n"
            "  builds:\n"
            "  - arch: amd64\n"
            "    artifact: built-charm-my-charm-amd64\n"
            "    run-id: '99887766'\n"
            "snaps: []\n"
        ),
        "artifacts-build-charm-other-charm-amd64": (
            "version: 1\n"
            "rocks: []\n"
            "charms:\n"
            "- name: other-charm\n"
            "  charmcraft-yaml: other/charmcraft.yaml\n"
            "  builds:\n"
            "  - arch: amd64\n"
            "    artifact: built-charm-other-charm-amd64\n"
            "    run-id: '99887766'\n"
            "snaps: []\n"
        ),
        "artifacts-build-snap-my-snap-amd64": (
            "version: 1\n"
            "rocks: []\n"
            "charms: []\n"
            "snaps:\n"
            "- name: my-snap\n"
            "  snapcraft-yaml: snap/snapcraft.yaml\n"
            "  builds:\n"
            "  - arch: amd64\n"
            "    artifact: built-snap-my-snap-amd64\n"
            "    run-id: '99887766'\n"
        ),
    }

    _GH_RESULT = SubprocessResult(stdout="", stderr="", returncode=0)
    _GIT_RESULT = SubprocessResult(
        stdout="https://github.com/owner/my-repo.git\n",
        stderr="",
        returncode=0,
    )

    def _write_plan(self, tmp_path: Path) -> None:
        """Write artifacts.yaml so _artifacts_fetch_all_arches can load it."""
        write_file(tmp_path / "artifacts.yaml", self._PLAN_YAML)

    def _make_charm_files(self, tmp_path: Path) -> None:
        """Create dummy .charm files so localize succeeds."""
        d1 = tmp_path / "built-charm-my-charm-amd64"
        d1.mkdir(exist_ok=True)
        (d1 / "my-charm_ubuntu-24.04-amd64.charm").write_bytes(b"")
        d2 = tmp_path / "built-charm-other-charm-amd64"
        d2.mkdir(exist_ok=True)
        (d2 / "other-charm_ubuntu-24.04-amd64.charm").write_bytes(b"")

    def _make_snap_files(self, tmp_path: Path) -> None:
        """Create dummy .snap file so localize succeeds for snaps."""
        d = tmp_path / "built-snap-my-snap-amd64"
        d.mkdir(exist_ok=True)
        (d / "my-snap_amd64.snap").write_bytes(b"")

    def _make_side_effect(
        self,
        tmp_path: Path,
        results: list[SubprocessResult | BaseException] | None = None,
    ) -> object:
        """Return a ``run_command`` side-effect for all-arches fetch tests.

        When a ``--pattern`` call succeeds it creates all expected partial files
        under ``partial-artifacts-fetch/`` so that ``rglob`` finds them.
        If *results* is provided the values are consumed in order; otherwise
        every call returns ``_GH_RESULT``.
        """
        idx = [0]

        def side_effect(cmd: list[str], **kw: object) -> SubprocessResult:
            if results is None:
                r: SubprocessResult | BaseException = self._GH_RESULT
            else:
                r = results[idx[0]]
                idx[0] += 1
            if isinstance(r, BaseException):
                raise r
            if "--pattern" in cmd:
                dir_idx = cmd.index("--dir") + 1
                partial_dir = Path(cmd[dir_idx])
                for name, content in self._PARTIAL_FILES.items():
                    d = partial_dir / name
                    d.mkdir(parents=True, exist_ok=True)
                    write_file(d / "artifacts.build.yaml", content)
            return r

        return side_effect

    def test_downloads_generated_and_charm_artifacts(self, tmp_path: Path) -> None:
        """Downloads all partials via --pattern + each charm/snap archive, then localises."""
        self._write_plan(tmp_path)
        self._make_charm_files(tmp_path)
        self._make_snap_files(tmp_path)

        patch_target = "opcli.core.artifacts.run_command"
        with patch(patch_target, side_effect=self._make_side_effect(tmp_path)) as mock_run:
            result = artifacts_fetch(tmp_path, run_id="99887766", repo="owner/my-repo", arch="all")

        assert result == tmp_path / "artifacts.build.yaml"
        calls = mock_run.call_args_list
        # First call: download all partial manifests via pattern
        partial_dir = tmp_path / "partial-artifacts-fetch"
        assert calls[0] == call(
            [
                "gh",
                "run",
                "download",
                "99887766",
                "--repo",
                "owner/my-repo",
                "--pattern",
                "artifacts-build-*",
                "--dir",
                str(partial_dir),
            ],
            cwd=str(tmp_path),
        )
        # Subsequent calls: one per charm/snap archive (rocks have image refs, not artifacts)
        artifact_names = {c.args[0][c.args[0].index("--name") + 1] for c in calls[1:]}
        assert artifact_names == {
            "built-charm-my-charm-amd64",
            "built-charm-other-charm-amd64",
            "built-snap-my-snap-amd64",
        }

    def test_skips_rocks_no_download(self, tmp_path: Path) -> None:
        """Rock OCI images are not downloaded — only pattern + charms/snaps."""
        self._write_plan(tmp_path)
        self._make_charm_files(tmp_path)
        self._make_snap_files(tmp_path)

        # 1 pattern download + 2 charms + 1 snap = 4; no rock archive download
        expected_calls = 4
        patch_target = "opcli.core.artifacts.run_command"
        with patch(patch_target, side_effect=self._make_side_effect(tmp_path)) as mock_run:
            artifacts_fetch(tmp_path, run_id="99887766", repo="owner/my-repo", arch="all")

        assert mock_run.call_count == expected_calls

    def test_infers_repo_from_git_remote(self, tmp_path: Path) -> None:
        """Infers owner/repo from git remote when --repo is not given."""
        self._write_plan(tmp_path)
        self._make_charm_files(tmp_path)
        self._make_snap_files(tmp_path)

        # git + pattern + 2 charms + 1 snap = 5 calls
        gh = self._GH_RESULT
        results: list[SubprocessResult | BaseException] = [self._GIT_RESULT, gh, gh, gh, gh]
        patch_target = "opcli.core.artifacts.run_command"
        with patch(
            patch_target, side_effect=self._make_side_effect(tmp_path, results)
        ) as mock_run:
            artifacts_fetch(tmp_path, run_id="99887766", arch="all")

        # First call is git remote get-url
        git_call = mock_run.call_args_list[0]
        assert git_call.args[0] == ["git", "remote", "get-url", "origin"]
        # Subsequent gh calls use the inferred repo
        for c in mock_run.call_args_list[1:]:
            assert "--repo" in c.args[0]
            assert "owner/my-repo" in c.args[0]

    def test_infers_repo_from_ssh_remote(self, tmp_path: Path) -> None:
        """Parses SSH-format git remote URLs (git@github.com:owner/repo.git)."""
        self._write_plan(tmp_path)
        self._make_charm_files(tmp_path)
        self._make_snap_files(tmp_path)

        ssh_result = SubprocessResult(
            stdout="git@github.com:owner/my-repo.git\n", stderr="", returncode=0
        )
        gh = self._GH_RESULT
        results: list[SubprocessResult | BaseException] = [ssh_result, gh, gh, gh, gh]
        with patch(
            "opcli.core.artifacts.run_command",
            side_effect=self._make_side_effect(tmp_path, results),
        ):
            artifacts_fetch(tmp_path, run_id="99887766", arch="all")

    def test_infers_repo_strips_trailing_slash(self, tmp_path: Path) -> None:
        """Strips trailing slash from git remote URLs like https://github.com/o/r/."""
        self._write_plan(tmp_path)
        self._make_charm_files(tmp_path)
        self._make_snap_files(tmp_path)

        trailing_slash = SubprocessResult(
            stdout="https://github.com/owner/my-repo/\n", stderr="", returncode=0
        )
        gh = self._GH_RESULT
        results: list[SubprocessResult | BaseException] = [trailing_slash, gh, gh, gh, gh]
        with patch(
            "opcli.core.artifacts.run_command",
            side_effect=self._make_side_effect(tmp_path, results),
        ) as mock_run:
            artifacts_fetch(tmp_path, run_id="99887766", arch="all")

        for c in mock_run.call_args_list[1:]:
            repo_val = c.args[0][c.args[0].index("--repo") + 1]
            assert not repo_val.endswith("/"), f"repo has trailing slash: {repo_val!r}"
            assert repo_val == "owner/my-repo"

    def test_raises_when_git_remote_not_github(self, tmp_path: Path) -> None:
        """Raises ConfigurationError when remote is not a GitHub URL."""
        non_github = SubprocessResult(
            stdout="https://gitlab.com/owner/repo.git\n", stderr="", returncode=0
        )
        with (
            patch("opcli.core.artifacts.run_command", return_value=non_github),
            pytest.raises(ConfigurationError, match="--repo"),
        ):
            artifacts_fetch(tmp_path, run_id="99887766", arch="all")

    def test_localises_after_download(self, tmp_path: Path) -> None:
        """artifacts.build.yaml is updated with local file paths after fetch."""
        self._write_plan(tmp_path)
        self._make_charm_files(tmp_path)
        self._make_snap_files(tmp_path)

        with patch(
            "opcli.core.artifacts.run_command", side_effect=self._make_side_effect(tmp_path)
        ):
            artifacts_fetch(tmp_path, run_id="99887766", repo="owner/my-repo", arch="all")

        gen = load_artifacts_build(tmp_path / "artifacts.build.yaml")
        for charm in gen.charms:
            charm_paths = [o.path for o in charm.builds if o.path]
            assert charm_paths, f"Charm '{charm.name}' was not localised"
            assert charm_paths[0].endswith(".charm")
        for snap in gen.snaps:
            assert snap.builds[0].file, f"Snap '{snap.name}' was not localised"
            assert snap.builds[0].file.endswith(".snap")

    def test_wait_retries_until_artifact_appears(self, tmp_path: Path) -> None:
        """With wait=True, retries the pattern download and succeeds on 2nd attempt."""
        self._write_plan(tmp_path)
        self._make_charm_files(tmp_path)
        self._make_snap_files(tmp_path)

        not_ready = SubprocessError(["gh"], 1, "artifact not found")
        # First pattern call fails (not ready), second succeeds + creates partials, then archives
        gh = self._GH_RESULT
        results: list[SubprocessResult | BaseException] = [not_ready, gh, gh, gh, gh]
        with (
            patch(
                "opcli.core.artifacts.run_command",
                side_effect=self._make_side_effect(tmp_path, results),
            ),
            patch("opcli.core.artifacts._check_run_conclusion", return_value=None),
            patch("opcli.core.artifacts.time.sleep"),
        ):
            artifacts_fetch(
                tmp_path, run_id="99887766", repo="owner/my-repo", wait=True, arch="all"
            )

    def test_wait_false_does_not_retry(self, tmp_path: Path) -> None:
        """Without wait=True, a failed download raises immediately (no retry)."""
        self._write_plan(tmp_path)
        not_ready = SubprocessError(["gh"], 1, "artifact not found")
        with (
            patch("opcli.core.artifacts.run_command", side_effect=not_ready),
            pytest.raises(SubprocessError),
        ):
            artifacts_fetch(
                tmp_path, run_id="99887766", repo="owner/my-repo", wait=False, arch="all"
            )

    def test_wait_fails_fast_on_auth_error(self, tmp_path: Path) -> None:
        """With wait=True, fails immediately on authentication errors (no sleep)."""
        self._write_plan(tmp_path)
        auth_error = SubprocessError(["gh"], 1, "HTTP 401 Unauthorized: bad credentials")
        with (
            patch("opcli.core.artifacts.run_command", side_effect=auth_error),
            patch("opcli.core.artifacts.time.sleep") as mock_sleep,
            pytest.raises(ConfigurationError, match="Authentication"),
        ):
            artifacts_fetch(
                tmp_path, run_id="99887766", repo="owner/my-repo", wait=True, arch="all"
            )

        mock_sleep.assert_not_called()

    def test_wait_times_out_with_last_error(self, tmp_path: Path) -> None:
        """With wait=True, raises ConfigurationError after exhausting all attempts."""
        self._write_plan(tmp_path)
        not_ready = SubprocessError(["gh"], 1, "no artifact named X in run")
        # 2 * _WAIT_SLEEP_SECONDS gives exactly 2 attempts (max = timeout // sleep).
        two_attempt_timeout = _artifacts_mod._WAIT_SLEEP_SECONDS * 2
        with (
            patch("opcli.core.artifacts.run_command", side_effect=not_ready),
            patch("opcli.core.artifacts._check_run_conclusion", return_value=None),
            patch("opcli.core.artifacts.time.sleep"),
            pytest.raises(ConfigurationError, match="Timed out"),
        ):
            artifacts_fetch(
                tmp_path,
                run_id="99887766",
                repo="owner/my-repo",
                wait=True,
                wait_timeout=two_attempt_timeout,
                arch="all",
            )

    @pytest.mark.parametrize("conclusion", ["failure", "cancelled"])
    def test_wait_fails_fast_on_terminal_run_conclusion(
        self, tmp_path: Path, conclusion: str
    ) -> None:
        """With wait=True, bails immediately when the run itself has a terminal conclusion."""
        self._write_plan(tmp_path)
        not_ready = SubprocessError(["gh"], 1, "artifact not found")
        with (
            patch("opcli.core.artifacts.run_command", side_effect=not_ready),
            patch(
                "opcli.core.artifacts._check_run_conclusion",
                return_value=conclusion,
            ),
            patch("opcli.core.artifacts.time.sleep") as mock_sleep,
            pytest.raises(ConfigurationError, match=conclusion),
        ):
            artifacts_fetch(
                tmp_path, run_id="99887766", repo="owner/my-repo", wait=True, arch="all"
            )

        mock_sleep.assert_not_called()

    def test_wait_bails_on_success_conclusion_with_missing_partials_all_arch(
        self, tmp_path: Path
    ) -> None:
        """With arch=all + wait, bails when run='success' AND download succeeded but partials are missing."""
        self._write_plan(tmp_path)

        def partial_side_effect(cmd: list[str], **kw: object) -> SubprocessResult:
            """Download succeeds but only writes 1 of 4 expected partials."""
            if "--pattern" in cmd:
                dir_idx = cmd.index("--dir") + 1
                partial_dir = Path(cmd[dir_idx])
                # Write only the rock partial; 3 charm/snap partials remain missing
                name = "artifacts-build-rock-my-rock-amd64"
                d = partial_dir / name
                d.mkdir(parents=True, exist_ok=True)
                write_file(
                    d / "artifacts.build.yaml",
                    self._PARTIAL_FILES["artifacts-build-rock-my-rock-amd64"],
                )
            return self._GH_RESULT

        with (
            patch("opcli.core.artifacts.run_command", side_effect=partial_side_effect),
            patch("opcli.core.artifacts._check_run_conclusion", return_value="success"),
            patch("opcli.core.artifacts.time.sleep") as mock_sleep,
            pytest.raises(ConfigurationError, match="skipped"),
        ):
            artifacts_fetch(
                tmp_path, run_id="99887766", repo="owner/my-repo", wait=True, arch="all"
            )

        mock_sleep.assert_not_called()

    def test_wait_retries_on_success_conclusion_with_missing_artifact_single_arch(
        self, tmp_path: Path
    ) -> None:
        """With single arch + wait, retries (not immediate abort) when run='success' but artifact missing.

        A download failure when the run is already 'success' could be a transient
        network error.  We cannot distinguish that from a skipped build job, so we
        retry until the deadline and surface a timeout error with a hint.
        """
        self._write_plan(tmp_path)
        not_ready = SubprocessError(["gh"], 1, "artifact not found")
        with (
            patch("opcli.core.artifacts._gh_download", side_effect=not_ready),
            patch("opcli.core.artifacts._run_gh_download", side_effect=not_ready),
            patch("opcli.core.artifacts._check_run_conclusion", return_value="success"),
            patch("opcli.core.artifacts.time.sleep"),
            patch("opcli.core.artifacts.time.monotonic", side_effect=[0.0] * 200),
            pytest.raises(ConfigurationError, match="Timed out"),
        ):
            artifacts_fetch(
                tmp_path,
                run_id="99887766",
                repo="owner/my-repo",
                wait=True,
                arch="amd64",
                wait_timeout=60,
            )

    def test_wait_success_conclusion_with_transient_single_arch_failure_retries(
        self, tmp_path: Path
    ) -> None:
        """Transient download failure on single-arch path retries and succeeds on second attempt."""
        self._write_plan(tmp_path)
        self._make_charm_files(tmp_path)
        self._make_snap_files(tmp_path)

        not_ready = SubprocessError(["gh"], 1, "network error")

        call_count = 0

        def single_arch_side_effect(cmd: list[str], **kw: object) -> SubprocessResult:
            nonlocal call_count
            call_count += 1
            if "--pattern" not in cmd and "--name" not in cmd:
                return self._GH_RESULT
            if "--pattern" in cmd and call_count == 1:
                raise not_ready
            # Second call: write partials correctly
            if "--pattern" in cmd:
                dir_idx = cmd.index("--dir") + 1
                partial_dir = Path(cmd[dir_idx])
                for name, content in self._PARTIAL_FILES.items():
                    d = partial_dir / name
                    d.mkdir(parents=True, exist_ok=True)
                    write_file(d / "artifacts.build.yaml", content)
            elif "--name" in cmd:
                dir_idx = cmd.index("--dir") + 1
                archive_dir = Path(cmd[dir_idx])
                archive_dir.mkdir(parents=True, exist_ok=True)
                for fname in [
                    "my-charm_ubuntu-24.04-amd64.charm",
                    "my-snap_amd64.snap",
                ]:
                    (archive_dir / fname).write_bytes(b"")
            return self._GH_RESULT

        with (
            patch("opcli.core.artifacts.run_command", side_effect=single_arch_side_effect),
            patch("opcli.core.artifacts._check_run_conclusion", return_value="success"),
            patch("opcli.core.artifacts.time.sleep"),
        ):
            artifacts_fetch(
                tmp_path, run_id="99887766", repo="owner/my-repo", wait=True, arch="all"
            )

    def test_gh_flat_extraction_single_artifact(self, tmp_path: Path) -> None:
        """When --pattern matches one artifact, gh extracts flat; layout is normalised."""
        # Use a plan with a single charm so --pattern returns exactly one artifact,
        # triggering gh's flat-extraction layout (partial_dir/artifacts.build.yaml
        # instead of partial_dir/<name>/artifacts.build.yaml).
        single_charm_plan = (
            "version: 1\ncharms:\n- name: my-charm\n  charmcraft-yaml: charmcraft.yaml\n"
        )
        write_file(tmp_path / "artifacts.yaml", single_charm_plan)

        single_partial = (
            "version: 1\n"
            "rocks: []\n"
            "charms:\n"
            "- name: my-charm\n"
            "  charmcraft-yaml: charmcraft.yaml\n"
            "  builds:\n"
            "  - arch: amd64\n"
            "    artifact: built-charm-my-charm-amd64\n"
            "    run-id: '99887766'\n"
            "snaps: []\n"
        )

        def flat_side_effect(cmd: list[str], **kw: object) -> SubprocessResult:
            if "--pattern" in cmd:
                # Simulate gh's flat extraction: write directly into partial_dir
                dir_idx = cmd.index("--dir") + 1
                partial_dir = Path(cmd[dir_idx])
                write_file(partial_dir / "artifacts.build.yaml", single_partial)
            elif "--name" in cmd:
                # Archive download: create the charm file
                dir_idx = cmd.index("--dir") + 1
                archive_dir = Path(cmd[dir_idx])
                archive_dir.mkdir(parents=True, exist_ok=True)
                (archive_dir / "my-charm_ubuntu-24.04-amd64.charm").write_bytes(b"")
            return self._GH_RESULT

        with patch("opcli.core.artifacts.run_command", side_effect=flat_side_effect):
            result = artifacts_fetch(tmp_path, run_id="99887766", repo="owner/my-repo", arch="all")

        assert result == tmp_path / "artifacts.build.yaml"
        # The normalised partial must be in the expected subdir
        expected_subdir = (
            tmp_path
            / "partial-artifacts-fetch"
            / "artifacts-build-charm-my-charm-amd64"
            / "artifacts.build.yaml"
        )
        assert expected_subdir.exists()

    def test_normalize_partial_dir_layout_corrupt_yaml_leaves_file(self, tmp_path: Path) -> None:
        """Corrupted flat-extracted YAML is left in place and reported as missing (triggers retry)."""
        partial_dir = tmp_path / "partials"
        partial_dir.mkdir()
        corrupt = partial_dir / "artifacts.build.yaml"
        corrupt.write_text(": this is not valid yaml :\n\t: [")

        _normalize_partial_dir_layout(partial_dir)

        # File still at flat location (not moved) because we couldn't infer the name
        assert corrupt.exists()
        # No subdirectories were created
        assert list(partial_dir.iterdir()) == [corrupt]

    def test_normalize_partial_dir_layout_empty_manifest_leaves_file(self, tmp_path: Path) -> None:
        """Flat-extracted manifest with no builds is left in place (artifact name cannot be inferred)."""
        partial_dir = tmp_path / "partials"
        partial_dir.mkdir()
        empty_manifest = "version: 1\nrocks: []\ncharms: []\nsnaps: []\n"
        flat = partial_dir / "artifacts.build.yaml"
        flat.write_text(empty_manifest)

        _normalize_partial_dir_layout(partial_dir)

        # Left in place because no builds → name is None
        assert flat.exists()

    def test_normalize_partial_dir_layout_already_normalized_no_op(self, tmp_path: Path) -> None:
        """Already-normalized layout (no flat file) is left unchanged."""
        partial_dir = tmp_path / "partials"
        subdir = partial_dir / "artifacts-build-charm-my-charm-amd64"
        subdir.mkdir(parents=True)
        manifest = subdir / "artifacts.build.yaml"
        manifest.write_text("version: 1\nrocks: []\ncharms: []\nsnaps: []\n")

        _normalize_partial_dir_layout(partial_dir)

        # Subdir manifest is untouched; no flat file was moved
        assert manifest.exists()
        assert not (partial_dir / "artifacts.build.yaml").exists()

    def test_wait_success_conclusion_with_transient_download_failure_retries(
        self, tmp_path: Path
    ) -> None:
        """Transient download failure when run='success' retries rather than raising 'skipped'."""
        self._write_plan(tmp_path)
        self._make_charm_files(tmp_path)
        self._make_snap_files(tmp_path)

        not_ready = SubprocessError(["gh"], 1, "network error")
        gh = self._GH_RESULT
        # Attempt 1: run_command fails (transient) + conclusion is "success"
        # Attempt 2: run_command succeeds + all partials present
        results: list[SubprocessResult | BaseException] = [not_ready, gh, gh, gh, gh]
        with (
            patch(
                "opcli.core.artifacts.run_command",
                side_effect=self._make_side_effect(tmp_path, results),
            ),
            # "success" on first attempt (transient failure) — should not bail
            patch(
                "opcli.core.artifacts._check_run_conclusion",
                side_effect=["success", None, None],
            ),
            patch("opcli.core.artifacts.time.sleep"),
        ):
            # Should succeed on second attempt, not raise "skipped"
            artifacts_fetch(
                tmp_path, run_id="99887766", repo="owner/my-repo", wait=True, arch="all"
            )

    def test_wait_continues_when_run_still_in_progress(self, tmp_path: Path) -> None:
        """With wait=True and arch=all, keeps retrying when run conclusion is None (still running)."""
        self._write_plan(tmp_path)
        self._make_charm_files(tmp_path)
        self._make_snap_files(tmp_path)

        not_ready = SubprocessError(["gh"], 1, "artifact not found")
        gh = self._GH_RESULT
        results: list[SubprocessResult | BaseException] = [not_ready, gh, gh, gh, gh]
        with (
            patch(
                "opcli.core.artifacts.run_command",
                side_effect=self._make_side_effect(tmp_path, results),
            ),
            patch(
                "opcli.core.artifacts._check_run_conclusion",
                return_value=None,
            ),
            patch("opcli.core.artifacts.time.sleep"),
        ):
            artifacts_fetch(
                tmp_path, run_id="99887766", repo="owner/my-repo", wait=True, arch="all"
            )

    def test_wait_timeout_uses_custom_duration(self, tmp_path: Path) -> None:
        """wait_timeout controls how many attempts are made (max = timeout // sleep)."""
        not_ready = SubprocessError(["gh"], 1, "artifact not found")
        sleep_calls: list[object] = []
        self._write_plan(tmp_path)

        with (
            patch("opcli.core.artifacts.run_command", side_effect=not_ready),
            patch("opcli.core.artifacts._check_run_conclusion", return_value=None),
            patch(
                "opcli.core.artifacts.time.sleep",
                side_effect=sleep_calls.append,
            ),
            pytest.raises(ConfigurationError, match="Timed out"),
        ):
            # 60s timeout / 30s sleep interval = 2 attempts max
            artifacts_fetch(
                tmp_path,
                run_id="99887766",
                repo="owner/my-repo",
                wait=True,
                wait_timeout=60,
                arch="all",
            )

        assert len(sleep_calls) == 1  # only between attempts, not after the last one

    def test_wait_timeout_implies_wait(self, tmp_path: Path) -> None:
        """Providing wait_timeout without wait=True still enables waiting."""
        self._write_plan(tmp_path)
        self._make_charm_files(tmp_path)
        self._make_snap_files(tmp_path)

        not_ready = SubprocessError(["gh"], 1, "artifact not found")
        gh = self._GH_RESULT
        results = [not_ready, gh, gh, gh, gh]
        with (
            patch(
                "opcli.core.artifacts.run_command",
                side_effect=self._make_side_effect(tmp_path, results),
            ),
            patch("opcli.core.artifacts._check_run_conclusion", return_value=None),
            patch("opcli.core.artifacts.time.sleep"),
        ):
            # wait=False but wait_timeout is not None → should retry (any value)
            artifacts_fetch(
                tmp_path,
                run_id="99887766",
                repo="owner/my-repo",
                wait=False,
                wait_timeout=300,
                arch="all",
            )

    def test_wait_timeout_implies_wait_even_at_default_value(self, tmp_path: Path) -> None:
        """wait_timeout=_DEFAULT_WAIT_TIMEOUT_SECONDS (the default numeric value) still enables waiting."""
        self._write_plan(tmp_path)
        self._make_charm_files(tmp_path)
        self._make_snap_files(tmp_path)

        not_ready = SubprocessError(["gh"], 1, "artifact not found")
        gh = self._GH_RESULT
        results = [not_ready, gh, gh, gh, gh]
        with (
            patch(
                "opcli.core.artifacts.run_command",
                side_effect=self._make_side_effect(tmp_path, results),
            ),
            patch("opcli.core.artifacts._check_run_conclusion", return_value=None),
            patch("opcli.core.artifacts.time.sleep"),
        ):
            # Even passing exactly the default value enables waiting (not None → wait)
            artifacts_fetch(
                tmp_path,
                run_id="99887766",
                repo="owner/my-repo",
                wait=False,
                wait_timeout=_artifacts_mod._DEFAULT_WAIT_TIMEOUT_SECONDS,
                arch="all",
            )


class TestCheckRunConclusion:
    """Unit tests for _check_run_conclusion."""

    def _gh_result(self, stdout: str) -> SubprocessResult:
        return SubprocessResult(stdout=stdout, stderr="", returncode=0)

    def test_returns_conclusion_when_run_finished(self) -> None:
        """Returns the conclusion string when the run has completed."""
        data = json.dumps({"conclusion": "success"})
        result = SubprocessResult(stdout=data, stderr="", returncode=0)
        with patch("opcli.core.artifacts.run_command", return_value=result):
            conclusion = _artifacts_mod._check_run_conclusion("123", "owner/repo")
        assert conclusion == "success"

    def test_returns_failure_conclusion(self) -> None:
        """Returns 'failure' when the run failed."""
        data = json.dumps({"conclusion": "failure"})
        result = SubprocessResult(stdout=data, stderr="", returncode=0)
        with patch("opcli.core.artifacts.run_command", return_value=result):
            conclusion = _artifacts_mod._check_run_conclusion("123", "owner/repo")
        assert conclusion == "failure"

    def test_returns_cancelled_conclusion(self) -> None:
        """Returns 'cancelled' when the run was cancelled."""
        data = json.dumps({"conclusion": "cancelled"})
        result = SubprocessResult(stdout=data, stderr="", returncode=0)
        with patch("opcli.core.artifacts.run_command", return_value=result):
            conclusion = _artifacts_mod._check_run_conclusion("123", "owner/repo")
        assert conclusion == "cancelled"

    def test_returns_none_when_conclusion_is_null(self) -> None:
        """Returns None when the run is still in progress (conclusion is null)."""
        data = json.dumps({"conclusion": None})
        result = SubprocessResult(stdout=data, stderr="", returncode=0)
        with patch("opcli.core.artifacts.run_command", return_value=result):
            conclusion = _artifacts_mod._check_run_conclusion("123", "owner/repo")
        assert conclusion is None

    def test_returns_none_when_gh_command_fails(self) -> None:
        """Returns None if the gh API call fails (don't know yet — keep retrying)."""
        error = SubprocessError(["gh"], 1, "API error")
        with patch("opcli.core.artifacts.run_command", side_effect=error):
            conclusion = _artifacts_mod._check_run_conclusion("123", "owner/repo")
        assert conclusion is None

    def test_returns_none_on_invalid_json(self) -> None:
        """Returns None if gh returns non-JSON output."""
        result = SubprocessResult(stdout="not-json", stderr="", returncode=0)
        with patch("opcli.core.artifacts.run_command", return_value=result):
            conclusion = _artifacts_mod._check_run_conclusion("123", "owner/repo")
        assert conclusion is None

    def test_uses_correct_gh_command(self) -> None:
        """Calls gh run view with --json conclusion."""
        data = json.dumps({"conclusion": "success"})
        result = SubprocessResult(stdout=data, stderr="", returncode=0)
        with patch("opcli.core.artifacts.run_command", return_value=result) as mock_run:
            _artifacts_mod._check_run_conclusion("456", "owner/myrepo")
        cmd = mock_run.call_args.args[0]
        assert cmd == [
            "gh",
            "run",
            "view",
            "456",
            "--repo",
            "owner/myrepo",
            "--json",
            "conclusion",
        ]


class TestSafeArtifactDir:
    """Tests for _safe_artifact_dir path traversal prevention."""

    def test_valid_subdir(self, tmp_path: Path) -> None:
        """Normal artifact name resolves under root."""
        result = _artifacts_mod._safe_artifact_dir(tmp_path, "my-artifact")
        assert result == (tmp_path / "my-artifact").resolve()

    def test_nested_subdir(self, tmp_path: Path) -> None:
        """Nested artifact name resolves under root."""
        result = _artifacts_mod._safe_artifact_dir(tmp_path, "built/charm-foo")
        assert result == (tmp_path / "built" / "charm-foo").resolve()

    def test_traversal_rejected(self, tmp_path: Path) -> None:
        """Path traversal via .. is rejected."""
        with pytest.raises(ConfigurationError, match="resolves outside"):
            _artifacts_mod._safe_artifact_dir(tmp_path, "../../etc/evil")

    def test_absolute_path_rejected(self, tmp_path: Path) -> None:
        """Absolute path that escapes root is rejected."""
        with pytest.raises(ConfigurationError, match="resolves outside"):
            _artifacts_mod._safe_artifact_dir(tmp_path, "/tmp/evil")

    def test_dot_dot_in_middle_rejected(self, tmp_path: Path) -> None:
        """Traversal hidden in the middle of the path is rejected."""
        with pytest.raises(ConfigurationError, match="resolves outside"):
            _artifacts_mod._safe_artifact_dir(tmp_path, "legit/../../../etc/passwd")

    def test_root_itself_allowed(self, tmp_path: Path) -> None:
        """Artifact name '.' resolves to root itself (edge case)."""
        result = _artifacts_mod._safe_artifact_dir(tmp_path, ".")
        assert result == tmp_path.resolve()

    def test_symlink_escape_rejected(self, tmp_path: Path) -> None:
        """Symlink pointing outside root is rejected."""
        outside = tmp_path.parent / "outside"
        outside.mkdir()
        symlink_path = tmp_path / "innocent-artifact"
        symlink_path.symlink_to(outside)

        with pytest.raises(ConfigurationError, match="resolves outside"):
            _artifacts_mod._safe_artifact_dir(tmp_path, "innocent-artifact")


class TestArtifactsFetchByArch:
    """Tests for artifacts_fetch() with arch= (arch-filtered mode)."""

    # artifacts.yaml declaring multi-arch builds.
    _PLAN_YAML = (
        "version: 1\n"
        "charms:\n"
        "- name: my-charm\n"
        "  charmcraft-yaml: charmcraft.yaml\n"
        "  platforms:\n"
        "  - arch: amd64\n"
        "  - arch: arm64\n"
        "rocks:\n"
        "- name: my-rock\n"
        "  rockcraft-yaml: rock/rockcraft.yaml\n"
        "  platforms:\n"
        "  - arch: amd64\n"
        "  - arch: arm64\n"
        "snaps: []\n"
    )

    # Partial manifest for amd64 charm build.
    _PARTIAL_CHARM_AMD64 = (
        "version: 1\n"
        "rocks: []\n"
        "charms:\n"
        "- name: my-charm\n"
        "  charmcraft-yaml: charmcraft.yaml\n"
        "  builds:\n"
        "  - arch: amd64\n"
        "    artifact: built-charm-my-charm-amd64\n"
        "    run-id: '12345'\n"
        "snaps: []\n"
    )

    # Partial manifest for amd64 rock build.
    _PARTIAL_ROCK_AMD64 = (
        "version: 1\n"
        "rocks:\n"
        "- name: my-rock\n"
        "  rockcraft-yaml: rock/rockcraft.yaml\n"
        "  builds:\n"
        "  - arch: amd64\n"
        "    image: ghcr.io/owner/repo/my-rock:abc1234-amd64\n"
        "charms: []\n"
        "snaps: []\n"
    )

    _GH_RESULT = SubprocessResult(stdout="", stderr="", returncode=0)

    def _write_plan(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.yaml", self._PLAN_YAML)

    def _write_partial_charm(self, tmp_path: Path) -> None:
        d = tmp_path / "partial-artifacts-fetch" / "artifacts-build-charm-my-charm-amd64"
        d.mkdir(parents=True, exist_ok=True)
        write_file(d / "artifacts.build.yaml", self._PARTIAL_CHARM_AMD64)

    def _write_partial_rock(self, tmp_path: Path) -> None:
        d = tmp_path / "partial-artifacts-fetch" / "artifacts-build-rock-my-rock-amd64"
        d.mkdir(parents=True, exist_ok=True)
        write_file(d / "artifacts.build.yaml", self._PARTIAL_ROCK_AMD64)

    def _make_charm_files(self, tmp_path: Path) -> None:
        d = tmp_path / "built-charm-my-charm-amd64"
        d.mkdir(exist_ok=True)
        (d / "my-charm_ubuntu-24.04-amd64.charm").write_bytes(b"")

    def test_downloads_only_arch_specific_partials(self, tmp_path: Path) -> None:
        """Downloads only partial manifests for the requested arch, not arm64."""
        self._write_plan(tmp_path)
        self._write_partial_charm(tmp_path)
        self._write_partial_rock(tmp_path)
        self._make_charm_files(tmp_path)

        with patch("opcli.core.artifacts.run_command", return_value=self._GH_RESULT) as mock_run:
            artifacts_fetch(tmp_path, run_id="12345", repo="owner/repo", arch="amd64")

        # Collect the --name values from gh run download calls.
        download_names = [
            c.args[0][c.args[0].index("--name") + 1]
            for c in mock_run.call_args_list
            if "download" in c.args[0]
        ]
        # Should download exactly the amd64 partials + the amd64 charm archive.
        assert "artifacts-build-charm-my-charm-amd64" in download_names
        assert "artifacts-build-rock-my-rock-amd64" in download_names
        assert "built-charm-my-charm-amd64" in download_names
        # Must NOT touch arm64 artifacts or the merged artifacts-build.
        assert not any("arm64" in n for n in download_names)
        assert "artifacts-build" not in download_names

    def test_does_not_download_merged_artifacts_build(self, tmp_path: Path) -> None:
        """Arch-filtered fetch never downloads the 'artifacts-build' merged artifact."""
        self._write_plan(tmp_path)
        self._write_partial_charm(tmp_path)
        self._write_partial_rock(tmp_path)
        self._make_charm_files(tmp_path)

        with patch("opcli.core.artifacts.run_command", return_value=self._GH_RESULT) as mock_run:
            artifacts_fetch(tmp_path, run_id="12345", repo="owner/repo", arch="amd64")

        download_names = [
            c.args[0][c.args[0].index("--name") + 1]
            for c in mock_run.call_args_list
            if "download" in c.args[0]
        ]
        assert "artifacts-build" not in download_names

    def test_raises_when_artifacts_yaml_missing(self, tmp_path: Path) -> None:
        """Arch-filtered fetch requires artifacts.yaml to determine partial names."""
        with pytest.raises(ConfigurationError, match=r"artifacts\.yaml"):
            artifacts_fetch(tmp_path, run_id="12345", repo="owner/repo", arch="amd64")

    def test_raises_when_arch_not_in_plan(self, tmp_path: Path) -> None:
        """Raises ConfigurationError if no artifacts match the requested arch."""
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\ncharms:\n- name: x\n  charmcraft-yaml: charmcraft.yaml\n"
            "  platforms:\n  - arch: amd64\n",
        )
        with pytest.raises(ConfigurationError, match="s390x"):
            artifacts_fetch(tmp_path, run_id="12345", repo="owner/repo", arch="s390x")

    def test_localises_after_arch_filtered_fetch(self, tmp_path: Path) -> None:
        """artifacts.build.yaml is updated with local paths after arch-filtered fetch."""
        self._write_plan(tmp_path)
        self._write_partial_charm(tmp_path)
        self._write_partial_rock(tmp_path)
        self._make_charm_files(tmp_path)

        with patch("opcli.core.artifacts.run_command", return_value=self._GH_RESULT):
            gen_path = artifacts_fetch(tmp_path, run_id="12345", repo="owner/repo", arch="amd64")

        gen = load_artifacts_build(gen_path)
        for charm in gen.charms:
            amd64_builds = [b for b in charm.builds if b.arch == "amd64"]
            assert amd64_builds, f"Charm '{charm.name}' has no amd64 build"
            assert amd64_builds[0].path, f"Charm '{charm.name}' amd64 not localised"

    def test_wait_retries_partials_without_collect_check(self, tmp_path: Path) -> None:
        """With wait=True and arch=, retries partial manifests without checking collect job."""
        # Use a charm-only plan so the side-effect only needs to handle one partial manifest.
        write_file(
            tmp_path / "artifacts.yaml",
            "version: 1\n"
            "charms:\n"
            "- name: my-charm\n"
            "  charmcraft-yaml: charmcraft.yaml\n"
            "  platforms:\n"
            "  - arch: amd64\n"
            "rocks: []\n"
            "snaps: []\n",
        )
        self._make_charm_files(tmp_path)

        not_ready = SubprocessError(["gh"], 1, "artifact not found")
        gh = self._GH_RESULT
        charm_partial = "artifacts-build-charm-my-charm-amd64"
        attempts = 0

        def side_effect(cmd: list[str], cwd: str | None = None, **kw: object) -> object:
            nonlocal attempts
            if "download" not in cmd or "--name" not in cmd:
                return gh
            name = cmd[cmd.index("--name") + 1]
            if name != charm_partial:
                return gh
            attempts += 1
            if attempts == 1:
                raise not_ready
            # Second attempt: write the partial file and succeed.
            dest_dir = tmp_path / "partial-artifacts-fetch" / name
            dest_dir.mkdir(parents=True, exist_ok=True)
            write_file(
                dest_dir / "artifacts.build.yaml",
                "version: 1\n"
                "rocks: []\n"
                "charms:\n"
                "- name: my-charm\n"
                "  charmcraft-yaml: charmcraft.yaml\n"
                "  builds:\n"
                "  - arch: amd64\n"
                "    artifact: built-charm-my-charm-amd64\n"
                "    run-id: '12345'\n"
                "snaps: []\n",
            )
            return gh

        with (
            patch("opcli.core.artifacts.run_command", side_effect=side_effect),
            patch("opcli.core.artifacts.time.sleep"),
        ):
            artifacts_fetch(tmp_path, run_id="12345", repo="owner/repo", arch="amd64", wait=True)

    def test_wait_arch_fails_fast_on_auth_error(self, tmp_path: Path) -> None:
        """Arch-filtered wait fails immediately on authentication errors."""
        self._write_plan(tmp_path)

        auth_error = SubprocessError(["gh"], 1, "HTTP 401 Unauthorized: bad credentials")
        with (
            patch("opcli.core.artifacts.run_command", side_effect=auth_error),
            patch("opcli.core.artifacts.time.sleep") as mock_sleep,
            pytest.raises(ConfigurationError, match="Authentication"),
        ):
            artifacts_fetch(tmp_path, run_id="12345", repo="owner/repo", arch="amd64", wait=True)

        mock_sleep.assert_not_called()

    def test_arch_all_downloads_all_partials_via_pattern(self, tmp_path: Path) -> None:
        """With arch='all', fetch uses --pattern to download all partial manifests."""
        self._write_plan(tmp_path)

        # Simulate pattern download creating the partial files.
        partial_dir = tmp_path / "partial-artifacts-fetch"
        gh = self._GH_RESULT

        def side_effect(cmd: list[str], cwd: str | None = None, **kw: object) -> object:
            if "--pattern" in cmd:
                # Write both amd64 and arm64 partials as the pattern would.
                for name in [
                    "artifacts-build-charm-my-charm-amd64",
                    "artifacts-build-rock-my-rock-amd64",
                    "artifacts-build-charm-my-charm-arm64",
                    "artifacts-build-rock-my-rock-arm64",
                ]:
                    d = partial_dir / name
                    d.mkdir(parents=True, exist_ok=True)
                    partial_content = (
                        (
                            "version: 1\n"
                            "rocks: []\n"
                            "charms:\n"
                            "- name: my-charm\n"
                            "  charmcraft-yaml: charmcraft.yaml\n"
                            "  builds:\n"
                            f"  - arch: {'amd64' if 'amd64' in name else 'arm64'}\n"
                            "    artifact: "
                            f"built-charm-my-charm-{'amd64' if 'amd64' in name else 'arm64'}\n"
                            "    run-id: '12345'\n"
                            "snaps: []\n"
                        )
                        if "charm" in name
                        else (
                            "version: 1\n"
                            "rocks:\n"
                            "- name: my-rock\n"
                            "  rockcraft-yaml: rock/rockcraft.yaml\n"
                            "  builds:\n"
                            f"  - arch: {'amd64' if 'amd64' in name else 'arm64'}\n"
                            "    image: "
                            f"ghcr.io/owner/repo/my-rock:abc1234-{'amd64' if 'amd64' in name else 'arm64'}\n"
                            "charms: []\n"
                            "snaps: []\n"
                        )
                    )
                    write_file(d / "artifacts.build.yaml", partial_content)
            elif "--name" in cmd:
                name = cmd[cmd.index("--name") + 1]
                # Create placeholder charm file for localization.
                if name.startswith("built-charm"):
                    arch = "amd64" if "amd64" in name else "arm64"
                    d = tmp_path / name
                    d.mkdir(exist_ok=True)
                    (d / f"my-charm_ubuntu-24.04-{arch}.charm").write_bytes(b"")
            return gh

        with patch("opcli.core.artifacts.run_command", side_effect=side_effect) as mock_run:
            artifacts_fetch(tmp_path, run_id="12345", repo="owner/repo", arch="all")

        # Verify --pattern was used (not --name for individual partials).
        pattern_calls = [c for c in mock_run.call_args_list if "--pattern" in c.args[0]]
        assert pattern_calls, "Expected --pattern call for arch='all'"
        assert "artifacts-build-*" in pattern_calls[0].args[0]
