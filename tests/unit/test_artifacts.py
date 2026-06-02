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

        # Symlink removed after build
        assert not existing_symlink.exists()
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


class TestArtifactsFetch:
    """Tests for artifacts_fetch()."""

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

    _GH_RESULT = SubprocessResult(stdout="", stderr="", returncode=0)
    _GIT_RESULT = SubprocessResult(
        stdout="https://github.com/owner/my-repo.git\n",
        stderr="",
        returncode=0,
    )

    def _make_charm_files(self, tmp_path: Path) -> None:
        """Create dummy .charm files so localize succeeds."""
        d1 = tmp_path / "built-charm-my-charm-amd64"
        d1.mkdir()
        (d1 / "my-charm_ubuntu-24.04-amd64.charm").write_bytes(b"")
        d2 = tmp_path / "built-charm-other-charm-amd64"
        d2.mkdir()
        (d2 / "other-charm_ubuntu-24.04-amd64.charm").write_bytes(b"")

    def _make_snap_files(self, tmp_path: Path) -> None:
        """Create dummy .snap file so localize succeeds for snaps."""
        d = tmp_path / "built-snap-my-snap-amd64"
        d.mkdir(exist_ok=True)
        (d / "my-snap_amd64.snap").write_bytes(b"")

    def test_downloads_generated_and_charm_artifacts(self, tmp_path: Path) -> None:
        """Downloads artifacts-build + each charm/snap artifact, then localises."""
        write_file(tmp_path / "artifacts.build.yaml", self._GENERATED_CI)
        self._make_charm_files(tmp_path)
        self._make_snap_files(tmp_path)

        patch_target = "opcli.core.artifacts.run_command"
        with patch(patch_target, return_value=self._GH_RESULT) as mock_run:
            result = artifacts_fetch(tmp_path, run_id="99887766", repo="owner/my-repo")

        assert result == tmp_path / "artifacts.build.yaml"
        calls = mock_run.call_args_list
        # First call: download artifacts-build
        assert calls[0] == call(
            [
                "gh",
                "run",
                "download",
                "99887766",
                "--repo",
                "owner/my-repo",
                "--name",
                "artifacts-build",
                "--dir",
                str(tmp_path),
            ],
            cwd=str(tmp_path),
        )
        # Subsequent calls: one per charm/snap artifact (rocks are skipped)
        artifact_names = {c.args[0][7] for c in calls[1:]}
        assert artifact_names == {
            "built-charm-my-charm-amd64",
            "built-charm-other-charm-amd64",
            "built-snap-my-snap-amd64",
        }

    def test_skips_rocks_no_download(self, tmp_path: Path) -> None:
        """Rock OCI images are not downloaded — only the initial yaml + charms/snaps."""
        write_file(tmp_path / "artifacts.build.yaml", self._GENERATED_CI)
        self._make_charm_files(tmp_path)
        self._make_snap_files(tmp_path)

        # 1 artifacts-build + 2 charms + 1 snap = 4; no rock download
        expected_calls = 4
        patch_target = "opcli.core.artifacts.run_command"
        with patch(patch_target, return_value=self._GH_RESULT) as mock_run:
            artifacts_fetch(tmp_path, run_id="99887766", repo="owner/my-repo")

        assert mock_run.call_count == expected_calls

    def test_infers_repo_from_git_remote(self, tmp_path: Path) -> None:
        """Infers owner/repo from git remote when --repo is not given."""
        write_file(tmp_path / "artifacts.build.yaml", self._GENERATED_CI)
        self._make_charm_files(tmp_path)
        self._make_snap_files(tmp_path)

        # git + artifacts-build + 2 charms + 1 snap = 5 calls
        gh = self._GH_RESULT
        results = [self._GIT_RESULT, gh, gh, gh, gh]
        patch_target = "opcli.core.artifacts.run_command"
        with patch(patch_target, side_effect=results) as mock_run:
            artifacts_fetch(tmp_path, run_id="99887766")

        # First call is git remote get-url
        git_call = mock_run.call_args_list[0]
        assert git_call.args[0] == ["git", "remote", "get-url", "origin"]
        # Subsequent gh calls use the inferred repo
        for c in mock_run.call_args_list[1:]:
            assert "--repo" in c.args[0]
            assert "owner/my-repo" in c.args[0]

    def test_infers_repo_from_ssh_remote(self, tmp_path: Path) -> None:
        """Parses SSH-format git remote URLs (git@github.com:owner/repo.git)."""
        write_file(tmp_path / "artifacts.build.yaml", self._GENERATED_CI)
        self._make_charm_files(tmp_path)
        self._make_snap_files(tmp_path)

        ssh_result = SubprocessResult(
            stdout="git@github.com:owner/my-repo.git\n", stderr="", returncode=0
        )
        gh = self._GH_RESULT
        results = [ssh_result, gh, gh, gh, gh]
        with patch("opcli.core.artifacts.run_command", side_effect=results):
            artifacts_fetch(tmp_path, run_id="99887766")

    def test_infers_repo_strips_trailing_slash(self, tmp_path: Path) -> None:
        """Strips trailing slash from git remote URLs like https://github.com/o/r/."""
        write_file(tmp_path / "artifacts.build.yaml", self._GENERATED_CI)
        self._make_charm_files(tmp_path)
        self._make_snap_files(tmp_path)

        trailing_slash = SubprocessResult(
            stdout="https://github.com/owner/my-repo/\n", stderr="", returncode=0
        )
        gh = self._GH_RESULT
        results = [trailing_slash, gh, gh, gh, gh]
        with patch("opcli.core.artifacts.run_command", side_effect=results) as mock_run:
            artifacts_fetch(tmp_path, run_id="99887766")

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
            artifacts_fetch(tmp_path, run_id="99887766")

    def test_localises_after_download(self, tmp_path: Path) -> None:
        """artifacts.build.yaml is updated with local file paths after fetch."""
        write_file(tmp_path / "artifacts.build.yaml", self._GENERATED_CI)
        self._make_charm_files(tmp_path)
        self._make_snap_files(tmp_path)

        with patch("opcli.core.artifacts.run_command", return_value=self._GH_RESULT):
            artifacts_fetch(tmp_path, run_id="99887766", repo="owner/my-repo")

        gen = load_artifacts_build(tmp_path / "artifacts.build.yaml")
        for charm in gen.charms:
            charm_paths = [o.path for o in charm.builds if o.path]
            assert charm_paths, f"Charm '{charm.name}' was not localised"
            assert charm_paths[0].endswith(".charm")
        for snap in gen.snaps:
            assert snap.builds[0].file, f"Snap '{snap.name}' was not localised"
            assert snap.builds[0].file.endswith(".snap")

    def test_wait_retries_until_artifact_appears(self, tmp_path: Path) -> None:
        """With wait=True, retries the initial download and succeeds on 2nd attempt."""
        write_file(tmp_path / "artifacts.build.yaml", self._GENERATED_CI)
        self._make_charm_files(tmp_path)
        self._make_snap_files(tmp_path)

        not_ready = SubprocessError(["gh"], 1, "artifact not found")
        # First call fails (not ready), second succeeds, rest succeed
        gh = self._GH_RESULT
        results = [not_ready, gh, gh, gh, gh]
        with (
            patch("opcli.core.artifacts.run_command", side_effect=results),
            patch("opcli.core.artifacts._check_collect_job_conclusion", return_value=None),
            patch("opcli.core.artifacts.time.sleep"),
        ):
            artifacts_fetch(tmp_path, run_id="99887766", repo="owner/my-repo", wait=True)

    def test_wait_false_does_not_retry(self, tmp_path: Path) -> None:
        """Without wait=True, a failed download raises immediately (no retry)."""
        not_ready = SubprocessError(["gh"], 1, "artifact not found")
        with (
            patch("opcli.core.artifacts.run_command", side_effect=not_ready),
            pytest.raises(SubprocessError),
        ):
            artifacts_fetch(tmp_path, run_id="99887766", repo="owner/my-repo", wait=False)

    def test_wait_fails_fast_on_auth_error(self, tmp_path: Path) -> None:
        """With wait=True, fails immediately on authentication errors (no sleep)."""
        auth_error = SubprocessError(["gh"], 1, "HTTP 401 Unauthorized: bad credentials")
        with (
            patch("opcli.core.artifacts.run_command", side_effect=auth_error),
            patch("opcli.core.artifacts.time.sleep") as mock_sleep,
            pytest.raises(ConfigurationError, match="Authentication"),
        ):
            artifacts_fetch(tmp_path, run_id="99887766", repo="owner/my-repo", wait=True)

        mock_sleep.assert_not_called()

    def test_file_exists_deletes_and_retries(self, tmp_path: Path) -> None:
        """When gh reports 'file exists', the file is deleted and download retried."""
        # Use a rocks-only yaml: no charm/snap downloads follow, so run_command
        # is called exactly twice (fail + retry).
        rocks_only = (
            "version: 1\n"
            "rocks:\n"
            "- name: my-rock\n"
            "  rockcraft-yaml: rock/rockcraft.yaml\n"
            "  builds:\n"
            "  - arch: amd64\n"
            "    image: ghcr.io/owner/repo/my-rock:abc1234\n"
        )
        write_file(tmp_path / "artifacts.build.yaml", rocks_only)
        file_exists_error = SubprocessError(
            ["gh"],
            1,
            'error extracting "artifacts.build.yaml": open ...: file exists',
        )
        gh = self._GH_RESULT
        call_count = 0

        def side_effect(cmd: list[str], cwd: str | None = None) -> object:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise file_exists_error
            write_file(tmp_path / "artifacts.build.yaml", rocks_only)
            return gh

        with patch("opcli.core.artifacts.run_command", side_effect=side_effect):
            result = artifacts_fetch(tmp_path, run_id="99887766", repo="owner/my-repo")

        assert result == tmp_path / "artifacts.build.yaml"
        expected_calls = 2
        assert call_count == expected_calls

    def test_wait_file_exists_deletes_and_retries(self, tmp_path: Path) -> None:
        """With wait=True, 'file exists' triggers delete-and-retry (no sleep)."""
        rocks_only = (
            "version: 1\n"
            "rocks:\n"
            "- name: my-rock\n"
            "  rockcraft-yaml: rock/rockcraft.yaml\n"
            "  builds:\n"
            "  - arch: amd64\n"
            "    image: ghcr.io/owner/repo/my-rock:abc1234\n"
        )
        write_file(tmp_path / "artifacts.build.yaml", rocks_only)
        file_exists_error = SubprocessError(
            ["gh"],
            1,
            'error extracting "artifacts.build.yaml": open ...: file exists',
        )
        gh = self._GH_RESULT
        call_count = 0

        def side_effect(cmd: list[str], cwd: str | None = None) -> object:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise file_exists_error
            write_file(tmp_path / "artifacts.build.yaml", rocks_only)
            return gh

        with (
            patch("opcli.core.artifacts.run_command", side_effect=side_effect),
            patch("opcli.core.artifacts.time.sleep") as mock_sleep,
        ):
            artifacts_fetch(tmp_path, run_id="99887766", repo="owner/my-repo", wait=True)

        mock_sleep.assert_not_called()
        expected_calls = 2
        assert call_count == expected_calls

    def test_wait_times_out_with_last_error(self, tmp_path: Path) -> None:
        """With wait=True, raises ConfigurationError after exhausting all attempts."""
        not_ready = SubprocessError(["gh"], 1, "no artifact named X in run")
        _orig = _artifacts_mod._WAIT_MAX_ATTEMPTS
        try:
            _artifacts_mod._WAIT_MAX_ATTEMPTS = 2  # speed up the test
            with (
                patch("opcli.core.artifacts.run_command", side_effect=not_ready),
                patch(
                    "opcli.core.artifacts._check_collect_job_conclusion",
                    return_value=None,
                ),
                patch("opcli.core.artifacts.time.sleep"),
                pytest.raises(ConfigurationError, match="Timed out"),
            ):
                artifacts_fetch(tmp_path, run_id="99887766", repo="owner/my-repo", wait=True)
        finally:
            _artifacts_mod._WAIT_MAX_ATTEMPTS = _orig

    @pytest.mark.parametrize("conclusion", ["skipped", "failure", "cancelled"])
    def test_wait_fails_fast_on_terminal_collect_conclusion(
        self, tmp_path: Path, conclusion: str
    ) -> None:
        """With wait=True, bails immediately on a terminal collect job conclusion."""
        not_ready = SubprocessError(["gh"], 1, "artifact not found")
        with (
            patch("opcli.core.artifacts.run_command", side_effect=not_ready),
            patch(
                "opcli.core.artifacts._check_collect_job_conclusion",
                return_value=conclusion,
            ),
            patch("opcli.core.artifacts.time.sleep") as mock_sleep,
            pytest.raises(ConfigurationError, match=conclusion),
        ):
            artifacts_fetch(tmp_path, run_id="99887766", repo="owner/my-repo", wait=True)

        mock_sleep.assert_not_called()

    def test_wait_continues_when_collect_job_still_running(self, tmp_path: Path) -> None:
        """With wait=True, keeps retrying when collect job conclusion is None."""
        write_file(tmp_path / "artifacts.build.yaml", self._GENERATED_CI)
        self._make_charm_files(tmp_path)
        self._make_snap_files(tmp_path)

        not_ready = SubprocessError(["gh"], 1, "artifact not found")
        gh = self._GH_RESULT
        with (
            patch(
                "opcli.core.artifacts.run_command",
                side_effect=[not_ready, gh, gh, gh, gh],
            ),
            patch(
                "opcli.core.artifacts._check_collect_job_conclusion",
                return_value=None,
            ),
            patch("opcli.core.artifacts.time.sleep"),
        ):
            artifacts_fetch(tmp_path, run_id="99887766", repo="owner/my-repo", wait=True)


class TestCheckCollectJobConclusion:
    """Unit tests for _check_collect_job_conclusion."""

    _GH_JOBS_RESPONSE = json.dumps(
        {
            "jobs": [
                {"name": "Build charm my-charm (amd64)", "conclusion": "success"},
                {"name": "Collect artifacts", "conclusion": "skipped"},
            ]
        }
    )

    def _gh_result(self, stdout: str) -> SubprocessResult:
        return SubprocessResult(stdout=stdout, stderr="", returncode=0)

    def test_returns_conclusion_when_job_found(self) -> None:
        result = SubprocessResult(stdout=self._GH_JOBS_RESPONSE, stderr="", returncode=0)
        with patch("opcli.core.artifacts.run_command", return_value=result):
            conclusion = _artifacts_mod._check_collect_job_conclusion("123", "owner/repo")
        assert conclusion == "skipped"

    def test_matches_with_reusable_workflow_prefix(self) -> None:
        """Job name 'build / Collect artifacts' is matched as a substring."""
        data = json.dumps(
            {"jobs": [{"name": "build / Collect artifacts", "conclusion": "failure"}]}
        )
        result = SubprocessResult(stdout=data, stderr="", returncode=0)
        with patch("opcli.core.artifacts.run_command", return_value=result):
            conclusion = _artifacts_mod._check_collect_job_conclusion("123", "owner/repo")
        assert conclusion == "failure"

    def test_returns_none_when_job_not_found(self) -> None:
        data = json.dumps({"jobs": [{"name": "Build charm", "conclusion": "success"}]})
        result = SubprocessResult(stdout=data, stderr="", returncode=0)
        with patch("opcli.core.artifacts.run_command", return_value=result):
            conclusion = _artifacts_mod._check_collect_job_conclusion("123", "owner/repo")
        assert conclusion is None

    def test_returns_none_when_conclusion_is_null(self) -> None:
        """Job still running — conclusion is null in JSON."""
        data = json.dumps({"jobs": [{"name": "Collect artifacts", "conclusion": None}]})
        result = SubprocessResult(stdout=data, stderr="", returncode=0)
        with patch("opcli.core.artifacts.run_command", return_value=result):
            conclusion = _artifacts_mod._check_collect_job_conclusion("123", "owner/repo")
        assert conclusion is None

    def test_returns_none_when_gh_command_fails(self) -> None:
        error = SubprocessError(["gh"], 1, "API error")
        with patch("opcli.core.artifacts.run_command", side_effect=error):
            conclusion = _artifacts_mod._check_collect_job_conclusion("123", "owner/repo")
        assert conclusion is None

    def test_returns_none_on_invalid_json(self) -> None:
        result = SubprocessResult(stdout="not-json", stderr="", returncode=0)
        with patch("opcli.core.artifacts.run_command", return_value=result):
            conclusion = _artifacts_mod._check_collect_job_conclusion("123", "owner/repo")
        assert conclusion is None


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
