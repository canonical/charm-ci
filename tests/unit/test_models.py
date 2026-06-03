# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for Pydantic models (artifacts.yaml and artifacts.build.yaml)."""

import pytest
from pydantic import ValidationError

from opcli.models.artifacts import (
    ArtifactResource,
    ArtifactsPlan,
    BuildTarget,
    CharmArtifact,
    RockArtifact,
)
from opcli.models.artifacts_build import (
    ArtifactsGenerated,
    CharmOutput,
    GeneratedCharm,
    GeneratedRock,
    GeneratedSnap,
    RockOutput,
    SnapOutput,
)


class TestArtifactsPlan:
    """Validation tests for artifacts.yaml models."""

    def test_minimal_valid(self) -> None:
        plan = ArtifactsPlan()
        assert plan.version == 1
        assert plan.rocks == []
        assert plan.charms == []
        assert plan.snaps == []

    def test_full_example(self) -> None:
        plan = ArtifactsPlan(
            rocks=[
                RockArtifact(name="indico", rockcraft_yaml="indico_rock/rockcraft.yaml"),
                RockArtifact(name="indico-nginx", rockcraft_yaml="nginx_rock/rockcraft.yaml"),
            ],
            charms=[
                CharmArtifact(
                    name="indico",
                    charmcraft_yaml="charmcraft.yaml",
                    resources={
                        "indico-image": ArtifactResource(type="oci-image", rock="indico"),
                    },
                ),
            ],
        )
        assert len(plan.rocks) == 2  # noqa: PLR2004
        assert len(plan.charms) == 1
        assert plan.charms[0].resources["indico-image"].rock == "indico"

    def test_duplicate_rock_names_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Duplicate rock"):
            ArtifactsPlan(
                rocks=[
                    RockArtifact(name="myrock", rockcraft_yaml="a/rockcraft.yaml"),
                    RockArtifact(name="myrock", rockcraft_yaml="b/rockcraft.yaml"),
                ]
            )

    def test_duplicate_charm_names_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Duplicate charm"):
            ArtifactsPlan(
                charms=[
                    CharmArtifact(name="mycharm", charmcraft_yaml="a/charmcraft.yaml"),
                    CharmArtifact(name="mycharm", charmcraft_yaml="b/charmcraft.yaml"),
                ]
            )

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs"):
            ArtifactsPlan.model_validate({"version": 1, "unknown_key": "val"})

    def test_wrong_version_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ArtifactsPlan.model_validate({"version": 99})

    def test_resource_wrong_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ArtifactResource(type="file")  # type: ignore[arg-type]

    def test_from_yaml_dict(self) -> None:
        """Validate a dict that mimics YAML load output (hyphenated keys)."""
        data = {
            "version": 1,
            "rocks": [{"name": "myrock", "rockcraft-yaml": "rock_dir/rockcraft.yaml"}],
            "charms": [
                {
                    "name": "mycharm",
                    "charmcraft-yaml": "charmcraft.yaml",
                    "resources": {
                        "img": {"type": "oci-image", "rock": "myrock"},
                    },
                }
            ],
            "snaps": [
                {
                    "name": "mysnap",
                    "snapcraft-yaml": "snap/snapcraft.yaml",
                    "pack-dir": ".",
                }
            ],
        }
        plan = ArtifactsPlan.model_validate(data)
        assert plan.rocks[0].name == "myrock"
        assert plan.charms[0].resources["img"].rock == "myrock"
        assert plan.snaps[0].name == "mysnap"
        assert plan.snaps[0].pack_dir == "."

    def test_pack_dir_optional(self) -> None:
        rock = RockArtifact(name="myrock", rockcraft_yaml="rock/rockcraft.yaml")
        assert rock.pack_dir is None

    def test_pack_dir_set(self) -> None:
        rock = RockArtifact(name="myrock", rockcraft_yaml="rock/rockcraft.yaml", pack_dir=".")
        assert rock.pack_dir == "."

    def test_artifacts_yaml_serializes_with_hyphens(self) -> None:
        """dump_artifacts_plan must emit hyphenated keys, not underscored."""
        plan = ArtifactsPlan(
            rocks=[RockArtifact(name="r", rockcraft_yaml="r/rockcraft.yaml", pack_dir=".")],
        )
        dumped = plan.model_dump(by_alias=True, exclude_none=True)
        rock = dumped["rocks"][0]
        assert "rockcraft-yaml" in rock
        assert "pack-dir" in rock
        assert "rockcraft_yaml" not in rock
        assert "pack_dir" not in rock

    def test_charm_channel_optional(self) -> None:
        """CharmArtifact.channel defaults to None."""
        charm = CharmArtifact(name="mycharm", charmcraft_yaml="charmcraft.yaml")
        assert charm.channel is None

    def test_charm_channel_set(self) -> None:
        """CharmArtifact.channel can be set to a track/channel string."""
        charm = CharmArtifact(
            name="mycharm", charmcraft_yaml="charmcraft.yaml", channel="1.0/stable"
        )
        assert charm.channel == "1.0/stable"

    def test_charm_channel_from_yaml_dict(self) -> None:
        """Channel field is accepted when loading from a YAML-style dict."""
        data = {
            "version": 1,
            "charms": [
                {"name": "mycharm", "charmcraft-yaml": "charmcraft.yaml", "channel": "2.0/edge"},
            ],
        }
        plan = ArtifactsPlan.model_validate(data)
        assert plan.charms[0].channel == "2.0/edge"

    def test_charm_channel_serialized(self) -> None:
        """Channel is included in serialized output when set."""
        charm = CharmArtifact(
            name="mycharm", charmcraft_yaml="charmcraft.yaml", channel="latest/edge"
        )
        dumped = charm.model_dump(by_alias=True, exclude_none=True)
        assert dumped["channel"] == "latest/edge"

    def test_charm_channel_excluded_when_none(self) -> None:
        """Channel is absent from serialized output when not set."""
        charm = CharmArtifact(name="mycharm", charmcraft_yaml="charmcraft.yaml")
        dumped = charm.model_dump(by_alias=True, exclude_none=True)
        assert "channel" not in dumped


class TestArtifactsGenerated:
    """Validation tests for artifacts.build.yaml models."""

    def test_local_output(self) -> None:
        out = RockOutput(arch="amd64", file="./myrock.rock")
        assert out.file == "./myrock.rock"
        assert out.image is None

    def test_ci_output_with_image(self) -> None:
        out = RockOutput(arch="amd64", image="ghcr.io/canonical/indico:abc1234")
        assert out.image is not None

    def test_ci_output_artifact_requires_run_id(self) -> None:
        with pytest.raises(ValidationError, match="run-id is required"):
            CharmOutput(arch="amd64", artifact="charm-indico")

    def test_ci_output_artifact_with_run_id(self) -> None:
        out = CharmOutput(arch="amd64", artifact="charm-indico", run_id="123456")
        assert out.artifact == "charm-indico"
        assert out.run_id == "123456"

    def test_empty_output_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must specify file or image"):
            RockOutput(arch="amd64")

    def test_run_id_alias_from_yaml(self) -> None:
        """YAML uses ``run-id`` (hyphen), Python uses ``run_id``."""
        data = {"arch": "amd64", "artifact": "charm-x", "run-id": "999"}
        out = CharmOutput.model_validate(data)
        assert out.run_id == "999"

    def test_run_id_serialized_with_hyphen(self) -> None:
        out = CharmOutput(arch="amd64", artifact="charm-x", run_id="999")
        dumped = out.model_dump(by_alias=True, exclude_none=True)
        assert "run-id" in dumped
        assert "run_id" not in dumped

    def test_full_generated_example(self) -> None:
        gen = ArtifactsGenerated(
            rocks=[
                GeneratedRock(
                    name="indico",
                    rockcraft_yaml="indico_rock/rockcraft.yaml",
                    builds=[RockOutput(arch="amd64", file="./indico.rock")],
                )
            ],
            charms=[
                GeneratedCharm(
                    name="indico",
                    charmcraft_yaml="charmcraft.yaml",
                    builds=[
                        CharmOutput(
                            arch="amd64",
                            path="./indico.charm",
                            base="ubuntu@22.04",
                        )
                    ],
                )
            ],
        )
        assert gen.rocks[0].builds[0].file == "./indico.rock"
        assert gen.charms[0].builds[0].path == "./indico.charm"

    def test_generated_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs"):
            ArtifactsGenerated.model_validate({"version": 99, "junk": True})

    def test_snap_generated(self) -> None:
        gen = ArtifactsGenerated(
            snaps=[
                GeneratedSnap(
                    name="mysnap",
                    snapcraft_yaml="snap/snapcraft.yaml",
                    builds=[SnapOutput(arch="amd64", file="./mysnap.snap")],
                )
            ],
        )
        assert gen.snaps[0].name == "mysnap"


class TestCharmOutput:
    """Validation tests for CharmOutput model."""

    def test_local_single_base(self) -> None:
        out = CharmOutput(
            arch="amd64",
            path="./aproxy_ubuntu-22.04-amd64.charm",
            base="ubuntu@22.04",
        )
        assert out.path == "./aproxy_ubuntu-22.04-amd64.charm"
        assert out.base == "ubuntu@22.04"

    def test_local_multi_base_separate_entries(self) -> None:
        out1 = CharmOutput(
            arch="amd64",
            path="./a_ubuntu-20.04-amd64.charm",
            base="ubuntu@20.04",
        )
        out2 = CharmOutput(
            arch="amd64",
            path="./a_ubuntu-22.04-amd64.charm",
            base="ubuntu@22.04",
        )
        assert out1.path != out2.path
        assert out1.base != out2.base

    def test_ci_artifact_output(self) -> None:
        out = CharmOutput(arch="amd64", artifact="charm-aproxy", run_id="999")
        assert out.artifact == "charm-aproxy"
        assert out.run_id == "999"
        assert out.path is None

    def test_ci_artifact_yaml_alias(self) -> None:
        out = CharmOutput.model_validate({"arch": "amd64", "artifact": "charm-x", "run-id": "1"})
        assert out.run_id == "1"

    def test_empty_output_rejected(self) -> None:
        with pytest.raises(ValidationError, match="path or artifact"):
            CharmOutput(arch="amd64")

    def test_artifact_without_run_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="run-id"):
            CharmOutput(arch="amd64", artifact="charm-x")

    def test_base_none_allowed(self) -> None:
        """Base can be None when filename does not follow convention."""
        out = CharmOutput(arch="amd64", path="./mycharm.charm")
        assert out.base is None


class TestBuildTarget:
    """Validation tests for BuildTarget model."""

    def test_build_target_defaults_no_runner(self) -> None:
        bt = BuildTarget(arch="amd64")
        assert bt.runner is None

    def test_build_target_with_runner(self) -> None:
        bt = BuildTarget(arch="amd64", runner=["ubuntu-latest"])
        assert bt.runner == ["ubuntu-latest"]


class TestRockOutput:
    """Validation tests for RockOutput model."""

    def test_rock_local_build(self) -> None:
        build = RockOutput(arch="amd64", file="./my.rock")
        assert build.arch == "amd64"
        assert build.file == "./my.rock"
        assert build.image is None

    def test_rock_ci_build(self) -> None:
        build = RockOutput(arch="amd64", image="ghcr.io/canonical/my-rock:abc123")
        assert build.image == "ghcr.io/canonical/my-rock:abc123"
        assert build.file is None

    def test_rock_empty_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RockOutput(arch="amd64")

    def test_rock_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs"):
            RockOutput.model_validate({"arch": "amd64", "file": "./my.rock", "unknown": "x"})


class TestSnapOutput:
    """Validation tests for SnapOutput model."""

    def test_snap_local_build(self) -> None:
        build = SnapOutput(arch="amd64", file="./my.snap")
        assert build.arch == "amd64"
        assert build.file == "./my.snap"
        assert build.artifact is None

    def test_snap_ci_build(self) -> None:
        build = SnapOutput(arch="amd64", artifact="built-snap-my-snap-amd64", run_id="123")
        assert build.artifact == "built-snap-my-snap-amd64"
        assert build.run_id == "123"

    def test_snap_empty_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SnapOutput(arch="amd64")

    def test_snap_artifact_without_run_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="run-id"):
            SnapOutput(arch="amd64", artifact="built-snap-my-snap")
