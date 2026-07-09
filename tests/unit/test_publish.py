# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for opcli artifacts publish."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from opcli.core.exceptions import ConfigurationError, DiscoveryError, SubprocessError
from opcli.core.publish import (
    PublishResult,
    ReleaseEntry,
    _parse_duplicate_revision,
    artifacts_publish,
    publish_results_to_dicts,
)
from opcli.core.subprocess import SubprocessResult

# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------


_BUILD_YAML_LOCAL = """\
version: 1
rocks:
  - name: k8s-rock
    rockcraft-yaml: k8s-rock/rockcraft.yaml
    builds:
      - arch: amd64
        file: k8s-rock/k8s-rock_amd64.rock
charms:
  - name: machine-charm
    charmcraft-yaml: machine-charm/charmcraft.yaml
    builds:
      - arch: amd64
        base: "ubuntu@24.04"
        path: machine-charm/machine-charm_ubuntu-24.04-amd64.charm
  - name: k8s-charm
    charmcraft-yaml: k8s-charm/charmcraft.yaml
    builds:
      - arch: amd64
        base: "ubuntu@24.04"
        path: k8s-charm/k8s-charm_ubuntu-24.04-amd64.charm
    resources:
      k8s-rock-image:
        type: oci-image
        rock: k8s-rock
"""

_BUILD_YAML_REGISTRY = """\
version: 1
rocks:
  - name: k8s-rock
    rockcraft-yaml: k8s-rock/rockcraft.yaml
    builds:
      - arch: amd64
        image: ghcr.io/canonical/charm-ci/k8s-rock:abc1234-amd64
charms:
  - name: k8s-charm
    charmcraft-yaml: k8s-charm/charmcraft.yaml
    builds:
      - arch: amd64
        base: "ubuntu@24.04"
        path: k8s-charm/k8s-charm_ubuntu-24.04-amd64.charm
    resources:
      k8s-rock-image:
        type: oci-image
        rock: k8s-rock
"""


def _setup_project(tmp_path: Path, build_yaml: str) -> Path:
    """Write manifest files to tmp_path and return it as root."""
    (tmp_path / "artifacts.build.yaml").write_text(build_yaml)
    # Create directories for charm paths
    (tmp_path / "k8s-rock").mkdir(exist_ok=True)
    (tmp_path / "k8s-charm").mkdir(exist_ok=True)
    (tmp_path / "machine-charm").mkdir(exist_ok=True)
    # Create .rock file so path resolution works
    (tmp_path / "k8s-rock" / "k8s-rock_amd64.rock").write_bytes(b"fake")
    # Create .charm files so upload path checks pass
    (tmp_path / "k8s-charm" / "k8s-charm_ubuntu-24.04-amd64.charm").write_bytes(b"fake")
    (tmp_path / "k8s-charm" / "k8s-charm_ubuntu-22.04-amd64.charm").write_bytes(b"fake")
    (tmp_path / "machine-charm" / "machine-charm_ubuntu-24.04-amd64.charm").write_bytes(b"fake")
    return tmp_path


def _mock_result(stdout: str = "", stderr: str = "", returncode: int = 0) -> SubprocessResult:
    return SubprocessResult(stdout=stdout, stderr=stderr, returncode=returncode)


# ---------------------------------------------------------------------------
#  Tests
# ---------------------------------------------------------------------------


class TestPublishCharmWithRockFile:
    """Charm with a local .rock file → upload-resource → upload charm."""

    def test_happy_path(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path, _BUILD_YAML_LOCAL)

        upload_resource_response = json.dumps({"revision": 5})
        upload_charm_response = json.dumps({"revision": 42})

        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            calls.append(cmd)
            if "upload-resource" in cmd:
                return _mock_result(stdout=upload_resource_response)
            return _mock_result(stdout=upload_charm_response)

        with patch("opcli.core.publish.run_command", side_effect=fake_run):
            results = artifacts_publish(root, channel="latest/edge")

        # machine-charm: 1 upload + 1 release (no resources, two separate calls)
        # k8s-charm: 1 upload (no release), 1 upload-resource, 1 release
        assert len(calls) == 5  # noqa: PLR2004

        # Check first k8s-charm upload (register resources - no release)
        reg_cmd = calls[2]
        assert "upload" in reg_cmd
        assert "k8s-charm" in str(reg_cmd)
        assert "--release=" not in " ".join(reg_cmd)
        assert "--resource=" not in " ".join(reg_cmd)

        # Check upload-resource call
        res_cmd = calls[3]
        assert "upload-resource" in res_cmd
        assert "k8s-charm" in res_cmd
        assert "k8s-rock-image" in res_cmd
        # Should use the file path (resolved)
        image_arg = next(a for a in res_cmd if a.startswith("--image="))
        assert "k8s-rock_amd64.rock" in image_arg

        # Check release call for k8s-charm has --resource flag
        charm_cmd = calls[4]
        assert "release" in charm_cmd
        assert "--resource=k8s-rock-image:5" in charm_cmd
        assert "--channel=latest/edge" in charm_cmd
        assert "--revision=42" in charm_cmd

        # Verify results
        assert len(results) == 2  # noqa: PLR2004
        machine_result = results[0]
        assert machine_result.charm_name == "machine-charm"
        assert machine_result.releases[0].revision == 42  # noqa: PLR2004
        assert machine_result.resources == {}

        k8s_result = results[1]
        assert k8s_result.charm_name == "k8s-charm"
        assert k8s_result.releases[0].revision == 42  # noqa: PLR2004
        assert k8s_result.resources == {"k8s-rock-image": 5}


class TestPublishCharmWithRockImage:
    """Charm with registry image ref → upload-resource with docker:// transport."""

    def test_uses_docker_transport(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path, _BUILD_YAML_REGISTRY)

        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            calls.append(cmd)
            if "upload-resource" in cmd:
                return _mock_result(stdout=json.dumps({"revision": 12}))
            return _mock_result(stdout=json.dumps({"revision": 50}))

        with patch("opcli.core.publish.run_command", side_effect=fake_run):
            results = artifacts_publish(root, channel="2.0/edge")

        # Check upload-resource uses docker:// transport
        res_cmd = next(c for c in calls if "upload-resource" in c)
        image_arg = next(a for a in res_cmd if a.startswith("--image="))
        assert image_arg == "--image=docker://ghcr.io/canonical/charm-ci/k8s-rock:abc1234-amd64"

        assert results[0].charm_name == "k8s-charm"
        assert results[0].resources == {"k8s-rock-image": 12}


class TestPublishCharmNoResources:
    """Machine charm with no resources → just upload, no resource flags."""

    def test_no_resource_flags(self, tmp_path: Path) -> None:
        # Only machine-charm in build manifest
        build_yaml = """\
version: 1
rocks: []
charms:
  - name: machine-charm
    charmcraft-yaml: machine-charm/charmcraft.yaml
    builds:
      - arch: amd64
        base: "ubuntu@24.04"
        path: machine-charm/machine-charm_ubuntu-24.04-amd64.charm
"""
        root = _setup_project(tmp_path, build_yaml)

        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            calls.append(cmd)
            return _mock_result(stdout=json.dumps({"revision": 10}))

        with patch("opcli.core.publish.run_command", side_effect=fake_run):
            results = artifacts_publish(root, channel="latest/stable")

        # upload + release (two separate calls for no-resources path)
        assert len(calls) == 2  # noqa: PLR2004
        # Upload call: no --resource flags, no --release
        assert not any("--resource" in arg for arg in calls[0])
        assert not any("--release" in arg for arg in calls[0])
        assert results[0].charm_name == "machine-charm"
        assert results[0].resources == {}


class TestPublishCharmExternalResource:
    """Resource without rock: → reads upstream-source from charmcraft metadata."""

    def test_reads_upstream_source(self, tmp_path: Path) -> None:
        artifacts_yaml = """\
version: 1
rocks: []
charms:
  - name: traefik-k8s
    charmcraft-yaml: charmcraft.yaml
    resources:
      traefik-image:
        type: oci-image
"""
        build_yaml = """\
version: 1
rocks: []
charms:
  - name: traefik-k8s
    charmcraft-yaml: charmcraft.yaml
    builds:
      - arch: amd64
        base: "ubuntu@20.04"
        path: traefik-k8s_ubuntu-20.04-amd64.charm
    resources:
      traefik-image:
        type: oci-image
"""
        root = tmp_path
        (root / "artifacts.yaml").write_text(artifacts_yaml)
        (root / "artifacts.build.yaml").write_text(build_yaml)
        # Create metadata.yaml with upstream-source
        (root / "metadata.yaml").write_text(
            """\
name: traefik-k8s
resources:
  traefik-image:
    type: oci-image
    upstream-source: docker.io/ubuntu/traefik:2-22.04
"""
        )
        # Create .charm file
        (root / "traefik-k8s_ubuntu-20.04-amd64.charm").write_bytes(b"fake")

        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            calls.append(cmd)
            if "upload-resource" in cmd:
                return _mock_result(stdout=json.dumps({"revision": 7}))
            return _mock_result(stdout=json.dumps({"revision": 20}))

        with patch("opcli.core.publish.run_command", side_effect=fake_run):
            results = artifacts_publish(root, channel="latest/edge")

        # Check upload-resource uses docker:// with upstream-source
        res_cmd = next(c for c in calls if "upload-resource" in c)
        image_arg = next(a for a in res_cmd if a.startswith("--image="))
        assert image_arg == "--image=docker://docker.io/ubuntu/traefik:2-22.04"

        assert results[0].resources == {"traefik-image": 7}


class TestPublishExternalResourceNoUpstreamSourceError:
    """Missing upstream-source in metadata → ConfigurationError."""

    def test_errors_when_no_upstream_source(self, tmp_path: Path) -> None:
        artifacts_yaml = """\
version: 1
rocks: []
charms:
  - name: traefik-k8s
    charmcraft-yaml: charmcraft.yaml
    resources:
      traefik-image:
        type: oci-image
"""
        build_yaml = """\
version: 1
rocks: []
charms:
  - name: traefik-k8s
    charmcraft-yaml: charmcraft.yaml
    builds:
      - arch: amd64
        base: "ubuntu@20.04"
        path: traefik-k8s_ubuntu-20.04-amd64.charm
    resources:
      traefik-image:
        type: oci-image
"""
        root = tmp_path
        (root / "artifacts.yaml").write_text(artifacts_yaml)
        (root / "artifacts.build.yaml").write_text(build_yaml)
        # charmcraft.yaml without upstream-source
        (root / "charmcraft.yaml").write_text("name: traefik-k8s\ntype: charm\n")
        # Create the charm file so validation passes
        (root / "traefik-k8s_ubuntu-20.04-amd64.charm").write_bytes(b"fake")

        with (
            pytest.raises(ConfigurationError, match="no 'upstream-source'"),
            patch(
                "opcli.core.publish.run_command",
                return_value=_mock_result(stdout=json.dumps({"revision": 1})),
            ),
        ):
            artifacts_publish(root, channel="latest/edge")


class TestPublishMultiBase:
    """Multiple .charm files → multiple upload calls, same resource revision."""

    def test_multi_base_same_resource_rev(self, tmp_path: Path) -> None:
        build_yaml = """\
version: 1
rocks:
  - name: k8s-rock
    rockcraft-yaml: k8s-rock/rockcraft.yaml
    builds:
      - arch: amd64
        file: k8s-rock/k8s-rock_amd64.rock
charms:
  - name: k8s-charm
    charmcraft-yaml: k8s-charm/charmcraft.yaml
    builds:
      - arch: amd64
        base: "ubuntu@22.04"
        path: k8s-charm/k8s-charm_ubuntu-22.04-amd64.charm
      - arch: amd64
        base: "ubuntu@24.04"
        path: k8s-charm/k8s-charm_ubuntu-24.04-amd64.charm
    resources:
      k8s-rock-image:
        type: oci-image
        rock: k8s-rock
"""
        root = _setup_project(tmp_path, build_yaml)

        call_count = {"upload": 0}

        def fake_run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            if "upload-resource" in cmd:
                return _mock_result(stdout=json.dumps({"revision": 5}))
            call_count["upload"] += 1
            return _mock_result(stdout=json.dumps({"revision": 40 + call_count["upload"]}))

        with patch("opcli.core.publish.run_command", side_effect=fake_run):
            results = artifacts_publish(root, channel="latest/edge", charm_names=["k8s-charm"])

        assert len(results) == 1
        assert len(results[0].releases) == 2  # noqa: PLR2004
        # Both builds uploaded (rev 41, 42), then released
        assert results[0].releases[0].revision == 41  # noqa: PLR2004
        assert results[0].releases[0].base == "ubuntu@22.04"
        assert results[0].releases[1].revision == 42  # noqa: PLR2004
        assert results[0].releases[1].base == "ubuntu@24.04"


class TestPublishCharmFilter:
    """--charm flag filters which charm gets published."""

    def test_only_publishes_selected(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path, _BUILD_YAML_LOCAL)

        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            calls.append(cmd)
            if "upload-resource" in cmd:
                return _mock_result(stdout=json.dumps({"revision": 5}))
            return _mock_result(stdout=json.dumps({"revision": 42}))

        with patch("opcli.core.publish.run_command", side_effect=fake_run):
            results = artifacts_publish(root, channel="latest/edge", charm_names=["k8s-charm"])

        # Only k8s-charm published (1 upload + 1 upload-resource + 1 release)
        assert len(results) == 1
        assert results[0].charm_name == "k8s-charm"
        assert len(calls) == 3  # noqa: PLR2004


class TestPublishDryRun:
    """--dry-run prints plan without executing commands."""

    def test_no_commands_executed(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path, _BUILD_YAML_LOCAL)

        with patch("opcli.core.publish.run_command") as mock_run:
            results = artifacts_publish(root, channel="latest/edge", dry_run=True)

        mock_run.assert_not_called()
        assert results == []

    def test_output_contains_commands(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = _setup_project(tmp_path, _BUILD_YAML_LOCAL)

        with patch("opcli.core.publish.run_command"):
            artifacts_publish(root, channel="latest/edge", dry_run=True)

        captured = capsys.readouterr()
        # Dry-run writes to stderr
        assert "k8s-charm" in captured.err
        assert "machine-charm" in captured.err
        assert "latest/edge" in captured.err
        assert "charmcraft upload-resource" in captured.err
        assert "charmcraft upload" in captured.err
        assert "charmcraft release" in captured.err
        assert "--image=" in captured.err
        assert "k8s-rock-image" in captured.err


class TestPublishUnfetchedError:
    """Charm with only artifact+run-id (not fetched) → ConfigurationError."""

    def test_errors_on_unfetched(self, tmp_path: Path) -> None:
        build_yaml = """\
version: 1
rocks: []
charms:
  - name: k8s-charm
    charmcraft-yaml: k8s-charm/charmcraft.yaml
    builds:
      - arch: amd64
        artifact: build-charm-amd64
        run-id: "12345"
"""
        root = _setup_project(tmp_path, build_yaml)

        with (
            pytest.raises(ConfigurationError, match="un-fetched CI artifacts"),
            patch("opcli.core.publish.run_command"),
        ):
            artifacts_publish(root, channel="latest/edge")


class TestPublishMissingRockError:
    """Resource references rock not in build manifest → DiscoveryError."""

    def test_errors_on_missing_rock(self, tmp_path: Path) -> None:
        # artifacts.yaml references k8s-rock but build manifest has no rocks
        build_yaml = """\
version: 1
rocks: []
charms:
  - name: k8s-charm
    charmcraft-yaml: k8s-charm/charmcraft.yaml
    builds:
      - arch: amd64
        base: "ubuntu@24.04"
        path: k8s-charm/k8s-charm_ubuntu-24.04-amd64.charm
    resources:
      k8s-rock-image:
        type: oci-image
        rock: k8s-rock
"""
        root = _setup_project(tmp_path, build_yaml)

        with (
            pytest.raises(DiscoveryError, match="not found"),
            patch(
                "opcli.core.publish.run_command",
                return_value=_mock_result(stdout=json.dumps({"revision": 1})),
            ),
        ):
            artifacts_publish(root, channel="latest/edge")


# ---------------------------------------------------------------------------
#  Additional coverage tests
# ---------------------------------------------------------------------------


class TestMissingManifestFiles:
    """Error when artifacts.build.yaml is missing."""

    def test_missing_build_yaml(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match=r"artifacts\.build\.yaml not found"):
            artifacts_publish(tmp_path, channel="latest/edge")


class TestUnknownCharmFilter:
    """Error when --charm specifies a name not in the build manifest."""

    def test_unknown_charm_name(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path, _BUILD_YAML_LOCAL)
        with pytest.raises(ConfigurationError, match="Charm 'nonexistent' not found"):
            artifacts_publish(root, channel="latest/edge", charm_names=["nonexistent"])


class TestMalformedCharmcraftOutput:
    """Handle malformed JSON from charmcraft gracefully."""

    def test_invalid_json(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path, _BUILD_YAML_REGISTRY)

        def fake_run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            if "upload-resource" in cmd:
                return _mock_result(stdout="not json at all")
            return _mock_result(stdout=json.dumps({"revision": 1}))

        with (
            pytest.raises(ConfigurationError, match="Failed to parse JSON"),
            patch("opcli.core.publish.run_command", side_effect=fake_run),
        ):
            artifacts_publish(root, channel="latest/edge")

    def test_missing_revision_key(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path, _BUILD_YAML_REGISTRY)

        def fake_run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            if "upload-resource" in cmd:
                return _mock_result(stdout=json.dumps({"status": "ok"}))
            return _mock_result(stdout=json.dumps({"revision": 1}))

        with (
            pytest.raises(ConfigurationError, match=r"expected.*revision"),
            patch("opcli.core.publish.run_command", side_effect=fake_run),
        ):
            artifacts_publish(root, channel="latest/edge")


class TestRockNoBuilds:
    """Error when rock exists but has no builds."""

    def test_rock_no_builds(self, tmp_path: Path) -> None:
        build_yaml = """\
version: 1
rocks:
  - name: k8s-rock
    rockcraft-yaml: k8s-rock/rockcraft.yaml
    builds: []
charms:
  - name: k8s-charm
    charmcraft-yaml: k8s-charm/charmcraft.yaml
    builds:
      - arch: amd64
        path: k8s-charm/k8s-charm_ubuntu-24.04-amd64.charm
    resources:
      k8s-rock-image:
        type: oci-image
        rock: k8s-rock
"""
        root = _setup_project(tmp_path, build_yaml)

        with (
            pytest.raises(DiscoveryError, match="has no builds"),
            patch(
                "opcli.core.publish.run_command",
                return_value=_mock_result(stdout=json.dumps({"revision": 1})),
            ),
        ):
            artifacts_publish(root, channel="latest/edge")


class TestTransportPrefixHandling:
    """Already-qualified refs should not get double-prefixed."""

    def test_docker_prefix_passthrough(self, tmp_path: Path) -> None:
        """An upstream-source that already has docker:// prefix."""
        artifacts_yaml = """\
version: 1
rocks: []
charms:
  - name: traefik-k8s
    charmcraft-yaml: charmcraft.yaml
    resources:
      traefik-image:
        type: oci-image
"""
        build_yaml = """\
version: 1
rocks: []
charms:
  - name: traefik-k8s
    charmcraft-yaml: charmcraft.yaml
    builds:
      - arch: amd64
        path: traefik-k8s.charm
    resources:
      traefik-image:
        type: oci-image
"""
        root = tmp_path
        (root / "artifacts.yaml").write_text(artifacts_yaml)
        (root / "artifacts.build.yaml").write_text(build_yaml)
        (root / "charmcraft.yaml").write_text(
            "name: traefik-k8s\ntype: charm\nresources:\n"
            "  traefik-image:\n    type: oci-image\n"
            "    upstream-source: docker://ghcr.io/canonical/traefik:latest\n"
        )
        (root / "traefik-k8s.charm").write_bytes(b"fake")

        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            calls.append(cmd)
            return _mock_result(stdout=json.dumps({"revision": 1}))

        with patch("opcli.core.publish.run_command", side_effect=fake_run):
            artifacts_publish(root, channel="latest/edge")

        res_cmd = next(c for c in calls if "upload-resource" in c)
        image_arg = next(a for a in res_cmd if a.startswith("--image="))
        # Should NOT double-prefix to docker://docker://...
        assert image_arg == "--image=docker://ghcr.io/canonical/traefik:latest"

    def test_oci_archive_prefix_passthrough(self, tmp_path: Path) -> None:
        """An upstream-source with oci-archive: prefix passes through."""
        artifacts_yaml = """\
version: 1
rocks: []
charms:
  - name: test-charm
    charmcraft-yaml: charmcraft.yaml
    resources:
      img:
        type: oci-image
"""
        build_yaml = """\
version: 1
rocks: []
charms:
  - name: test-charm
    charmcraft-yaml: charmcraft.yaml
    builds:
      - arch: amd64
        path: test-charm.charm
    resources:
      img:
        type: oci-image
"""
        root = tmp_path
        (root / "artifacts.yaml").write_text(artifacts_yaml)
        (root / "artifacts.build.yaml").write_text(build_yaml)
        (root / "charmcraft.yaml").write_text(
            "name: test-charm\ntype: charm\nresources:\n"
            "  img:\n    type: oci-image\n"
            "    upstream-source: oci-archive:/path/to/image.tar\n"
        )
        (root / "test-charm.charm").write_bytes(b"fake")

        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            calls.append(cmd)
            return _mock_result(stdout=json.dumps({"revision": 1}))

        with patch("opcli.core.publish.run_command", side_effect=fake_run):
            artifacts_publish(root, channel="latest/edge")

        res_cmd = next(c for c in calls if "upload-resource" in c)
        image_arg = next(a for a in res_cmd if a.startswith("--image="))
        assert image_arg == "--image=oci-archive:/path/to/image.tar"


class TestEmptyCharmBuilds:
    """Error when charm has no builds."""

    def test_empty_builds(self, tmp_path: Path) -> None:
        build_yaml = """\
version: 1
rocks: []
charms:
  - name: machine-charm
    charmcraft-yaml: machine-charm/charmcraft.yaml
    builds: []
"""
        root = _setup_project(tmp_path, build_yaml)

        with (
            pytest.raises(ConfigurationError, match="has no builds"),
            patch("opcli.core.publish.run_command"),
        ):
            artifacts_publish(root, channel="latest/edge")


class TestMissingCharmFile:
    """Error when a charm .charm file doesn't exist on disk."""

    def test_missing_charm_file(self, tmp_path: Path) -> None:
        build_yaml = """\
version: 1
rocks: []
charms:
  - name: machine-charm
    charmcraft-yaml: machine-charm/charmcraft.yaml
    builds:
      - arch: amd64
        path: machine-charm/nonexistent.charm
"""
        root = _setup_project(tmp_path, build_yaml)

        with (
            pytest.raises(DiscoveryError, match="not found"),
            patch("opcli.core.publish.run_command"),
        ):
            artifacts_publish(root, channel="latest/edge")


class TestMalformedYamlError:
    """Malformed YAML in metadata raises ConfigurationError, not silent fallthrough."""

    def test_invalid_yaml_in_charmcraft(self, tmp_path: Path) -> None:
        artifacts_yaml = """\
version: 1
rocks: []
charms:
  - name: my-charm
    charmcraft-yaml: charmcraft.yaml
    resources:
      img:
        type: oci-image
"""
        build_yaml = """\
version: 1
rocks: []
charms:
  - name: my-charm
    charmcraft-yaml: charmcraft.yaml
    builds:
      - arch: amd64
        path: my-charm.charm
    resources:
      img:
        type: oci-image
"""
        root = tmp_path
        (root / "artifacts.yaml").write_text(artifacts_yaml)
        (root / "artifacts.build.yaml").write_text(build_yaml)
        # Write truly invalid YAML (unclosed flow sequence)
        (root / "charmcraft.yaml").write_text("resources: [unclosed\n")
        (root / "my-charm.charm").write_bytes(b"fake")

        with (
            pytest.raises(ConfigurationError, match="Failed to parse"),
            patch(
                "opcli.core.publish.run_command",
                return_value=_mock_result(stdout=json.dumps({"revision": 1})),
            ),
        ):
            artifacts_publish(root, channel="latest/edge")


class TestPublishPrefersImageOverFile:
    """Rock with both image and file → prefers docker:// transport."""

    def test_prefers_image(self, tmp_path: Path) -> None:
        build_yaml = """\
version: 1
rocks:
  - name: k8s-rock
    rockcraft-yaml: k8s-rock/rockcraft.yaml
    builds:
      - arch: amd64
        file: k8s-rock/k8s-rock_amd64.rock
        image: ghcr.io/canonical/charm-ci/k8s-rock:abc-amd64
charms:
  - name: k8s-charm
    charmcraft-yaml: k8s-charm/charmcraft.yaml
    builds:
      - arch: amd64
        base: "ubuntu@24.04"
        path: k8s-charm/k8s-charm_ubuntu-24.04-amd64.charm
    resources:
      k8s-rock-image:
        type: oci-image
        rock: k8s-rock
"""
        root = _setup_project(tmp_path, build_yaml)

        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            calls.append(cmd)
            if "upload-resource" in cmd:
                return _mock_result(stdout=json.dumps({"revision": 3}))
            return _mock_result(stdout=json.dumps({"revision": 10}))

        with patch("opcli.core.publish.run_command", side_effect=fake_run):
            artifacts_publish(root, channel="latest/edge", charm_names=["k8s-charm"])

        res_cmd = next(c for c in calls if "upload-resource" in c)
        image_arg = next(a for a in res_cmd if a.startswith("--image="))
        assert image_arg == "--image=docker://ghcr.io/canonical/charm-ci/k8s-rock:abc-amd64"


class TestNonIntegerRevision:
    """charmcraft returns non-integer revision → ConfigurationError."""

    def test_string_revision(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path, _BUILD_YAML_REGISTRY)

        def fake_run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            if "upload-resource" in cmd:
                return _mock_result(stdout=json.dumps({"revision": "not-a-number"}))
            return _mock_result(stdout=json.dumps({"revision": 1}))

        with (
            pytest.raises(ConfigurationError, match="Expected integer"),
            patch("opcli.core.publish.run_command", side_effect=fake_run),
        ):
            artifacts_publish(root, channel="latest/edge")


class TestNonStringUpstreamSource:
    """upstream-source that is not a string → ConfigurationError."""

    def test_list_upstream_source(self, tmp_path: Path) -> None:
        build_yaml = """\
version: 1
rocks: []
charms:
  - name: my-charm
    charmcraft-yaml: charmcraft.yaml
    builds:
      - arch: amd64
        path: my-charm.charm
    resources:
      img:
        type: oci-image
"""
        root = tmp_path
        (root / "artifacts.build.yaml").write_text(build_yaml)
        (root / "charmcraft.yaml").write_text(
            "name: my-charm\ntype: charm\nresources:\n"
            "  img:\n    type: oci-image\n"
            "    upstream-source:\n      - item1\n      - item2\n"
        )
        (root / "my-charm.charm").write_bytes(b"fake")

        with (
            pytest.raises(ConfigurationError, match="must be a string"),
            patch(
                "opcli.core.publish.run_command",
                return_value=_mock_result(stdout=json.dumps({"revision": 1})),
            ),
        ):
            artifacts_publish(root, channel="latest/edge")


# ---------------------------------------------------------------------------
#  Tests for publish_results_to_dicts
# ---------------------------------------------------------------------------


class TestPublishResultsToDicts:
    """Tests for the JSON serialization helper."""

    def test_empty_results(self) -> None:
        assert publish_results_to_dicts([]) == []

    def test_single_charm_no_resources(self) -> None:
        results = [
            PublishResult(
                charm_name="my-charm",
                channel="latest/edge",
                releases=[ReleaseEntry(revision=7, base="ubuntu@22.04", arch="amd64")],
            )
        ]
        expected = [
            {
                "charm_name": "my-charm",
                "channel": "latest/edge",
                "releases": [{"revision": 7, "base": "ubuntu@22.04", "arch": "amd64"}],
                "resources": {},
            }
        ]
        assert publish_results_to_dicts(results) == expected

    def test_multi_charm_with_resources(self) -> None:
        results = [
            PublishResult(
                charm_name="indico",
                channel="latest/edge",
                releases=[
                    ReleaseEntry(revision=12, base="ubuntu@22.04", arch="amd64"),
                    ReleaseEntry(revision=13, base="ubuntu@24.04", arch="amd64"),
                ],
                resources={"redis-image": 5, "postgres-image": 3},
            ),
            PublishResult(
                charm_name="indico-redis",
                channel="latest/edge",
                releases=[ReleaseEntry(revision=5, base=None, arch="amd64")],
            ),
        ]
        dicts = publish_results_to_dicts(results)
        assert len(dicts) == len(results)
        assert dicts[0]["charm_name"] == "indico"
        assert dicts[0]["resources"] == {"redis-image": 5, "postgres-image": 3}
        assert dicts[1]["releases"][0]["base"] is None

    def test_json_roundtrip(self) -> None:
        """Verify the output is JSON-serializable."""
        results = [
            PublishResult(
                charm_name="test",
                channel="2.0/stable",
                releases=[ReleaseEntry(revision=1, base="ubuntu@22.04", arch="arm64")],
                resources={"img": 10},
            )
        ]
        serialized = json.dumps(publish_results_to_dicts(results))
        parsed = json.loads(serialized)
        assert parsed[0]["charm_name"] == "test"
        assert parsed[0]["resources"]["img"] == parsed[0]["resources"]["img"]


# ---------------------------------------------------------------------------
#  Tests for per-charm channel resolution
# ---------------------------------------------------------------------------

_PLAN_YAML_MACHINE_ONLY = """\
version: 1
charms:
  - name: machine-charm
    charmcraft-yaml: machine-charm/charmcraft.yaml
    channel: 1.0/stable
"""

_PLAN_YAML_BOTH_CHARMS_WITH_CHANNELS = """\
version: 1
charms:
  - name: machine-charm
    charmcraft-yaml: machine-charm/charmcraft.yaml
    channel: 1.0/stable
  - name: k8s-charm
    charmcraft-yaml: k8s-charm/charmcraft.yaml
    channel: 2.0/edge
"""

_PLAN_YAML_MIXED_CHANNELS = """\
version: 1
charms:
  - name: machine-charm
    charmcraft-yaml: machine-charm/charmcraft.yaml
    channel: 1.0/stable
  - name: k8s-charm
    charmcraft-yaml: k8s-charm/charmcraft.yaml
"""

_BUILD_YAML_MACHINE_ONLY = """\
version: 1
charms:
  - name: machine-charm
    charmcraft-yaml: machine-charm/charmcraft.yaml
    builds:
      - arch: amd64
        base: "ubuntu@24.04"
        path: machine-charm/machine-charm_ubuntu-24.04-amd64.charm
"""


def _setup_project_with_plan(tmp_path: Path, build_yaml: str, plan_yaml: str) -> Path:
    """Write both artifacts.yaml and artifacts.build.yaml to tmp_path."""
    (tmp_path / "artifacts.yaml").write_text(plan_yaml)
    (tmp_path / "artifacts.build.yaml").write_text(build_yaml)
    (tmp_path / "machine-charm").mkdir(exist_ok=True)
    (tmp_path / "k8s-charm").mkdir(exist_ok=True)
    (tmp_path / "k8s-rock").mkdir(exist_ok=True)
    (tmp_path / "machine-charm" / "machine-charm_ubuntu-24.04-amd64.charm").write_bytes(b"fake")
    (tmp_path / "k8s-charm" / "k8s-charm_ubuntu-24.04-amd64.charm").write_bytes(b"fake")
    (tmp_path / "k8s-rock" / "k8s-rock_amd64.rock").write_bytes(b"fake")
    return tmp_path


class TestPerCharmChannel:
    """Tests for per-charm channel resolution."""

    def test_per_charm_channel_used_without_global_channel(self, tmp_path: Path) -> None:
        """Charm's channel from artifacts.yaml is used when --channel is not passed."""
        root = _setup_project_with_plan(
            tmp_path, _BUILD_YAML_MACHINE_ONLY, _PLAN_YAML_MACHINE_ONLY
        )
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            calls.append(cmd)
            return _mock_result(stdout=json.dumps({"revision": 10}))

        with patch("opcli.core.publish.run_command", side_effect=fake_run):
            results = artifacts_publish(root, channel=None)

        assert len(results) == 1
        assert results[0].channel == "1.0/stable"
        # calls[0] is the upload, calls[1] is the release
        release_cmd = calls[1]
        assert "--channel=1.0/stable" in release_cmd

    def test_global_channel_used_as_fallback(self, tmp_path: Path) -> None:
        """--channel is used when a charm has no per-charm channel set."""
        root = _setup_project_with_plan(
            tmp_path,
            _BUILD_YAML_MACHINE_ONLY,
            _PLAN_YAML_MACHINE_ONLY.replace("channel: 1.0/stable\n", ""),
        )
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            calls.append(cmd)
            return _mock_result(stdout=json.dumps({"revision": 7}))

        with patch("opcli.core.publish.run_command", side_effect=fake_run):
            results = artifacts_publish(root, channel="latest/edge")

        assert results[0].channel == "latest/edge"
        # calls[1] is the release command (calls[0] is upload)
        assert "--channel=latest/edge" in calls[1]

    def test_global_channel_overrides_per_charm_channel(self, tmp_path: Path) -> None:
        """--channel takes precedence over the charm's channel in artifacts.yaml."""
        root = _setup_project_with_plan(
            tmp_path, _BUILD_YAML_MACHINE_ONLY, _PLAN_YAML_MACHINE_ONLY
        )
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            calls.append(cmd)
            return _mock_result(stdout=json.dumps({"revision": 3}))

        with patch("opcli.core.publish.run_command", side_effect=fake_run):
            results = artifacts_publish(root, channel="latest/edge")

        # --channel (latest/edge) must win over per-charm channel (1.0/stable)
        assert results[0].channel == "latest/edge"
        # calls[1] is the release command (calls[0] is upload)
        assert "--channel=latest/edge" in calls[1]
        assert "--channel=1.0/stable" not in " ".join(" ".join(c) for c in calls)

    def test_mixed_channels_with_global_override(self, tmp_path: Path) -> None:
        """--channel overrides ALL charms, ignoring per-charm config."""
        root = _setup_project_with_plan(tmp_path, _BUILD_YAML_LOCAL, _PLAN_YAML_MIXED_CHANNELS)
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            calls.append(cmd)
            if "upload-resource" in cmd:
                return _mock_result(stdout=json.dumps({"revision": 5}))
            return _mock_result(stdout=json.dumps({"revision": 20}))

        with patch("opcli.core.publish.run_command", side_effect=fake_run):
            results = artifacts_publish(root, channel="hotfix/edge")

        # Both charms must use the --channel override
        machine_result = next(r for r in results if r.charm_name == "machine-charm")
        k8s_result = next(r for r in results if r.charm_name == "k8s-charm")
        assert machine_result.channel == "hotfix/edge"
        assert k8s_result.channel == "hotfix/edge"

    def test_error_when_no_channel_and_no_plan_channel(self, tmp_path: Path) -> None:
        """ConfigurationError raised when neither --channel nor per-charm channel is set."""
        root = _setup_project_with_plan(
            tmp_path,
            _BUILD_YAML_MACHINE_ONLY,
            _PLAN_YAML_MACHINE_ONLY.replace("channel: 1.0/stable\n", ""),
        )

        with pytest.raises(ConfigurationError, match=r"No channel.*machine-charm"):
            artifacts_publish(root, channel=None)

    def test_error_when_no_artifacts_yaml_and_no_global_channel(self, tmp_path: Path) -> None:
        """ConfigurationError raised when artifacts.yaml is absent and --channel is not set."""
        root = _setup_project(tmp_path, _BUILD_YAML_MACHINE_ONLY)

        with pytest.raises(ConfigurationError, match=r"No channel.*machine-charm"):
            artifacts_publish(root, channel=None)

    def test_all_per_charm_channels_no_global_needed(self, tmp_path: Path) -> None:
        """All charms publish successfully when every charm has a per-charm channel."""
        root = _setup_project_with_plan(
            tmp_path, _BUILD_YAML_LOCAL, _PLAN_YAML_BOTH_CHARMS_WITH_CHANNELS
        )

        def fake_run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            if "upload-resource" in cmd:
                return _mock_result(stdout=json.dumps({"revision": 5}))
            return _mock_result(stdout=json.dumps({"revision": 1}))

        with patch("opcli.core.publish.run_command", side_effect=fake_run):
            results = artifacts_publish(root, channel=None)

        assert len(results) == 2  # noqa: PLR2004
        machine_result = next(r for r in results if r.charm_name == "machine-charm")
        k8s_result = next(r for r in results if r.charm_name == "k8s-charm")
        assert machine_result.channel == "1.0/stable"
        assert k8s_result.channel == "2.0/edge"


# ---------------------------------------------------------------------------
#  cwd / pack-dir tests (charmcraft ≥ 4.x bug workaround)
# ---------------------------------------------------------------------------


class TestPublishCharmcraftCwd:
    """charmcraft upload must run from pack_dir, not the project root.

    Charmcraft ≥ 4.x crashes with ``RuntimeError('Project not configured
    yet.')`` when ``charmcraft upload`` is run from a directory without a
    ``charmcraft.yaml``.  This affects multi-charm monorepos where the
    individual charms live in subdirectories.
    See https://github.com/canonical/charmcraft/issues/2492.
    """

    def test_upload_cwd_is_charmcraft_yaml_dir_no_resources(self, tmp_path: Path) -> None:
        """Charmcraft upload runs from the charm's charmcraft.yaml directory."""
        build_yaml = """\
version: 1
charms:
  - name: my-charm
    charmcraft-yaml: my-charm/charmcraft.yaml
    builds:
      - arch: amd64
        base: "ubuntu@24.04"
        path: built-my-charm-amd64/my-charm/my-charm_amd64.charm
"""
        root = tmp_path
        (root / "artifacts.build.yaml").write_text(build_yaml)
        charm_subdir = root / "my-charm"
        charm_subdir.mkdir()
        (charm_subdir / "charmcraft.yaml").write_text("name: my-charm\n")
        artifact_dir = root / "built-my-charm-amd64" / "my-charm"
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "my-charm_amd64.charm").write_bytes(b"fake")

        kwarg_cwds: list[str] = []

        def fake_run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            kwarg_cwds.append(str(kwargs.get("cwd", "")))
            return _mock_result(stdout=json.dumps({"revision": 7}))

        with patch("opcli.core.publish.run_command", side_effect=fake_run):
            results = artifacts_publish(root, channel="latest/edge")

        assert len(results) == 1
        assert results[0].charm_name == "my-charm"
        # cwd must be the charm's charmcraft.yaml directory, not root
        expected_cwd = str(charm_subdir.resolve())
        assert kwarg_cwds[0] == expected_cwd, (
            f"Expected cwd={expected_cwd!r}, got {kwarg_cwds[0]!r}"
        )

    def test_upload_cmd_uses_absolute_charm_path(self, tmp_path: Path) -> None:
        """Charmcraft upload receives an absolute path for the .charm file."""
        build_yaml = """\
version: 1
charms:
  - name: my-charm
    charmcraft-yaml: my-charm/charmcraft.yaml
    builds:
      - arch: amd64
        base: "ubuntu@24.04"
        path: built-my-charm-amd64/my-charm/my-charm_amd64.charm
"""
        root = tmp_path
        (root / "artifacts.build.yaml").write_text(build_yaml)
        charm_subdir = root / "my-charm"
        charm_subdir.mkdir()
        (charm_subdir / "charmcraft.yaml").write_text("name: my-charm\n")
        artifact_dir = root / "built-my-charm-amd64" / "my-charm"
        artifact_dir.mkdir(parents=True)
        charm_file = artifact_dir / "my-charm_amd64.charm"
        charm_file.write_bytes(b"fake")

        upload_cmds: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            if "upload" in cmd and "upload-resource" not in cmd:
                upload_cmds.append(cmd)
            return _mock_result(stdout=json.dumps({"revision": 7}))

        with patch("opcli.core.publish.run_command", side_effect=fake_run):
            artifacts_publish(root, channel="latest/edge")

        assert len(upload_cmds) == 1
        charm_path_arg = upload_cmds[0][2]  # charmcraft upload <path> ...
        assert charm_path_arg == str(charm_file.resolve()), (
            f"Expected absolute path, got {charm_path_arg!r}"
        )

    def test_upload_cwd_is_pack_dir_from_build_manifest(self, tmp_path: Path) -> None:
        """When pack-dir is stored in the build manifest, upload runs from pack-dir.

        This exercises the symlink creation path: pack_dir is the repo root (".")
        but charmcraft.yaml lives in my-charm/. No pre-existing charmcraft.yaml is
        present at the root, so with_pack_yaml_symlink must create one.
        """
        build_yaml = """\
version: 1
charms:
  - name: my-charm
    charmcraft-yaml: my-charm/charmcraft.yaml
    pack-dir: .
    builds:
      - arch: amd64
        base: "ubuntu@24.04"
        path: built-my-charm-amd64/my-charm/my-charm_amd64.charm
"""
        root = tmp_path
        (root / "artifacts.build.yaml").write_text(build_yaml)
        # Write artifacts.yaml with channel only — pack-dir is in build manifest
        (root / "artifacts.yaml").write_text(
            "version: 1\ncharms:\n  - name: my-charm\n"
            "    charmcraft-yaml: my-charm/charmcraft.yaml\n"
            "    channel: latest/edge\n"
        )
        charm_subdir = root / "my-charm"
        charm_subdir.mkdir()
        (charm_subdir / "charmcraft.yaml").write_text("name: my-charm\n")
        # Do NOT create root/charmcraft.yaml — the symlink code must create it
        artifact_dir = root / "built-my-charm-amd64" / "my-charm"
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "my-charm_amd64.charm").write_bytes(b"fake")

        kwarg_cwds: list[str] = []
        symlink_existed_during_run = False

        def fake_run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            nonlocal symlink_existed_during_run
            kwarg_cwds.append(str(kwargs.get("cwd", "")))
            if (root / "charmcraft.yaml").is_symlink():
                symlink_existed_during_run = True
            return _mock_result(stdout=json.dumps({"revision": 9}))

        with patch("opcli.core.publish.run_command", side_effect=fake_run):
            results = artifacts_publish(root, channel=None)

        assert len(results) == 1
        # cwd must be pack-dir (root ".")
        expected_cwd = str(root.resolve())
        assert kwarg_cwds[0] == expected_cwd, (
            f"Expected cwd={expected_cwd!r}, got {kwarg_cwds[0]!r}"
        )
        # Symlink must have been created during the run
        assert symlink_existed_during_run, (
            "charmcraft.yaml symlink was not created at pack_dir during upload"
        )
        # Symlink must be cleaned up after upload
        assert not (root / "charmcraft.yaml").exists()

    def test_upload_cwd_is_charmcraft_yaml_dir_with_resources(self, tmp_path: Path) -> None:
        """Upload (no-release) also runs from charmcraft.yaml dir for charms with resources."""
        build_yaml = """\
version: 1
rocks:
  - name: my-rock
    rockcraft-yaml: my-rock/rockcraft.yaml
    builds:
      - arch: amd64
        image: ghcr.io/canonical/my-rock:latest
charms:
  - name: my-charm
    charmcraft-yaml: my-charm/charmcraft.yaml
    builds:
      - arch: amd64
        base: "ubuntu@24.04"
        path: built-my-charm-amd64/my-charm/my-charm_amd64.charm
    resources:
      my-rock-image:
        type: oci-image
        rock: my-rock
"""
        root = tmp_path
        (root / "artifacts.build.yaml").write_text(build_yaml)
        charm_subdir = root / "my-charm"
        charm_subdir.mkdir()
        (charm_subdir / "charmcraft.yaml").write_text("name: my-charm\n")
        artifact_dir = root / "built-my-charm-amd64" / "my-charm"
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "my-charm_amd64.charm").write_bytes(b"fake")

        upload_cwds: list[str] = []

        def fake_run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            if "upload" in cmd and "upload-resource" not in cmd:
                upload_cwds.append(str(kwargs.get("cwd", "")))
            return _mock_result(stdout=json.dumps({"revision": 5}))

        with patch("opcli.core.publish.run_command", side_effect=fake_run):
            artifacts_publish(root, channel="latest/edge")

        assert len(upload_cwds) == 1
        expected_cwd = str(charm_subdir.resolve())
        assert upload_cwds[0] == expected_cwd, (
            f"Expected cwd={expected_cwd!r}, got {upload_cwds[0]!r}"
        )

    def test_nonstandard_yaml_name_creates_symlink(self, tmp_path: Path) -> None:
        """When charmcraft-yaml has a non-standard name, a symlink is created in pack_dir."""
        build_yaml = """\
version: 1
charms:
  - name: my-charm
    charmcraft-yaml: my-charm/charmcraft-my-charm.yaml
    builds:
      - arch: amd64
        base: "ubuntu@24.04"
        path: my-charm/my-charm_ubuntu-24.04-amd64.charm
"""
        root = tmp_path
        (root / "artifacts.build.yaml").write_text(build_yaml)
        charm_subdir = root / "my-charm"
        charm_subdir.mkdir()
        (charm_subdir / "charmcraft-my-charm.yaml").write_text("name: my-charm\n")
        (charm_subdir / "my-charm_ubuntu-24.04-amd64.charm").write_bytes(b"fake")

        symlink_existed_during_run = False

        def fake_run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            nonlocal symlink_existed_during_run
            symlink_link = charm_subdir / "charmcraft.yaml"
            if symlink_link.is_symlink():
                symlink_existed_during_run = True
            return _mock_result(stdout=json.dumps({"revision": 11}))

        with patch("opcli.core.publish.run_command", side_effect=fake_run):
            artifacts_publish(root, channel="latest/edge")

        assert symlink_existed_during_run, (
            "charmcraft.yaml symlink was not present during run_command"
        )
        # Symlink must be cleaned up after upload
        assert not (charm_subdir / "charmcraft.yaml").exists(), (
            "charmcraft.yaml symlink was not cleaned up after upload"
        )

    def test_release_cwd_is_pack_dir(self, tmp_path: Path) -> None:
        """Charmcraft release also runs from pack_dir (consistent with upload)."""
        build_yaml = """\
version: 1
rocks:
  - name: my-rock
    rockcraft-yaml: my-rock/rockcraft.yaml
    builds:
      - arch: amd64
        image: ghcr.io/canonical/my-rock:latest
charms:
  - name: my-charm
    charmcraft-yaml: my-charm/charmcraft.yaml
    builds:
      - arch: amd64
        base: "ubuntu@24.04"
        path: built-charm/my-charm_amd64.charm
    resources:
      my-rock-image:
        type: oci-image
        rock: my-rock
"""
        root = tmp_path
        (root / "artifacts.build.yaml").write_text(build_yaml)
        charm_subdir = root / "my-charm"
        charm_subdir.mkdir()
        (charm_subdir / "charmcraft.yaml").write_text("name: my-charm\n")
        (root / "built-charm").mkdir()
        (root / "built-charm" / "my-charm_amd64.charm").write_bytes(b"fake")

        cwds_by_cmd: dict[str, str] = {}

        def fake_run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            cwd = str(kwargs.get("cwd", ""))
            if "upload" in cmd and "upload-resource" not in cmd:
                cwds_by_cmd["upload"] = cwd
            elif "release" in cmd:
                cwds_by_cmd["release"] = cwd
            return _mock_result(stdout=json.dumps({"revision": 3}))

        with patch("opcli.core.publish.run_command", side_effect=fake_run):
            artifacts_publish(root, channel="latest/edge")

        expected_cwd = str(charm_subdir.resolve())
        assert cwds_by_cmd.get("upload") == expected_cwd, (
            f"upload cwd: expected {expected_cwd!r}, got {cwds_by_cmd.get('upload')!r}"
        )
        assert cwds_by_cmd.get("release") == expected_cwd, (
            f"release cwd: expected {expected_cwd!r}, got {cwds_by_cmd.get('release')!r}"
        )

    def test_upload_resource_cwd_is_root(self, tmp_path: Path) -> None:
        """Charmcraft upload-resource runs from root (not affected by the cwd fix)."""
        build_yaml = """\
version: 1
rocks:
  - name: my-rock
    rockcraft-yaml: my-rock/rockcraft.yaml
    builds:
      - arch: amd64
        image: ghcr.io/canonical/my-rock:latest
charms:
  - name: my-charm
    charmcraft-yaml: my-charm/charmcraft.yaml
    builds:
      - arch: amd64
        base: "ubuntu@24.04"
        path: built-charm/my-charm_amd64.charm
    resources:
      my-rock-image:
        type: oci-image
        rock: my-rock
"""
        root = tmp_path
        (root / "artifacts.build.yaml").write_text(build_yaml)
        charm_subdir = root / "my-charm"
        charm_subdir.mkdir()
        (charm_subdir / "charmcraft.yaml").write_text("name: my-charm\n")
        (root / "built-charm").mkdir()
        (root / "built-charm" / "my-charm_amd64.charm").write_bytes(b"fake")

        upload_resource_cwds: list[str] = []

        def fake_run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            if "upload-resource" in cmd:
                upload_resource_cwds.append(str(kwargs.get("cwd", "")))
            return _mock_result(stdout=json.dumps({"revision": 7}))

        with patch("opcli.core.publish.run_command", side_effect=fake_run):
            artifacts_publish(root, channel="latest/edge")

        assert len(upload_resource_cwds) == 1
        # upload-resource is NOT affected by the charmcraft 4.x bug and
        # intentionally runs from root, not pack_dir.
        assert upload_resource_cwds[0] == str(root.resolve()), (
            f"Expected upload-resource cwd to be root, got {upload_resource_cwds[0]!r}"
        )

    def test_symlink_is_restored_after_replacement(self, tmp_path: Path) -> None:
        """A pre-existing symlink pointing elsewhere is restored after upload."""
        root = tmp_path
        charm_subdir = root / "my-charm"
        charm_subdir.mkdir()
        other_subdir = root / "other-charm"
        other_subdir.mkdir()
        # The "real" yaml for my-charm
        (charm_subdir / "charmcraft-my-charm.yaml").write_text("name: my-charm\n")
        # A pre-existing symlink in my-charm/ pointing somewhere else
        other_yaml = other_subdir / "charmcraft.yaml"
        other_yaml.write_text("name: other-charm\n")
        (charm_subdir / "charmcraft.yaml").symlink_to(Path("../other-charm/charmcraft.yaml"))

        build_yaml = """\
version: 1
charms:
  - name: my-charm
    charmcraft-yaml: my-charm/charmcraft-my-charm.yaml
    builds:
      - arch: amd64
        base: "ubuntu@24.04"
        path: my-charm/my-charm_ubuntu-24.04-amd64.charm
"""
        (root / "artifacts.build.yaml").write_text(build_yaml)
        (charm_subdir / "my-charm_ubuntu-24.04-amd64.charm").write_bytes(b"fake")

        def fake_run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            return _mock_result(stdout=json.dumps({"revision": 5}))

        with patch("opcli.core.publish.run_command", side_effect=fake_run):
            artifacts_publish(root, channel="latest/edge")

        # The original symlink must be restored (pointing back to other-charm's yaml)
        restored = charm_subdir / "charmcraft.yaml"
        assert restored.is_symlink(), "Original symlink was not restored"
        assert restored.resolve() == other_yaml.resolve(), (
            f"Expected symlink to point to {other_yaml}, got {restored.resolve()}"
        )


# ---------------------------------------------------------------------------
#  Duplicate digest handling
# ---------------------------------------------------------------------------

_EXISTING_REVISION = 425

_DUPLICATE_STDOUT = json.dumps(
    {
        "errors": [
            {
                "code": "review-error",
                "message": (
                    "Cannot insert package. An upload with that digest "
                    "(SHA3-384: 'abc123') already exists in the database. "
                    f"Revision of the existing package is: {_EXISTING_REVISION}"
                ),
            }
        ]
    }
)


class TestParseDuplicateRevision:
    """Unit tests for the _parse_duplicate_revision helper."""

    def test_matches_known_error_shape(self) -> None:
        """Extracts the revision from charmcraft's standard duplicate-upload JSON."""
        result = _parse_duplicate_revision(_DUPLICATE_STDOUT)
        assert result == _EXISTING_REVISION

    def test_returns_none_for_unrelated_error(self) -> None:
        """Returns None when the error message is unrelated."""
        other = json.dumps({"errors": [{"code": "some-error", "message": "Something else"}]})
        assert _parse_duplicate_revision(other) is None

    def test_returns_none_for_non_json(self) -> None:
        """Returns None rather than raising when stdout is not valid JSON."""
        assert _parse_duplicate_revision("not json") is None

    def test_returns_none_for_empty_string(self) -> None:
        """Returns None for empty stdout."""
        assert _parse_duplicate_revision("") is None

    def test_returns_none_when_errors_key_missing(self) -> None:
        """Returns None when stdout is valid JSON but has no 'errors' key."""
        assert _parse_duplicate_revision(json.dumps({"revision": 5})) is None


class TestDuplicateUploadNoResources:
    """Duplicate digest reuse for charms without resources.

    The no-resources path now uses _upload_charm_no_release + _release_charm
    (two separate charmcraft calls), so release is always issued regardless
    of whether the upload was new or a duplicate.
    """

    def test_reuses_existing_revision_on_duplicate(self, tmp_path: Path) -> None:
        """When charmcraft upload returns a duplicate-digest error, reuses existing revision and releases it."""
        build_yaml = """\
version: 1
charms:
  - name: my-charm
    charmcraft-yaml: my-charm/charmcraft.yaml
    builds:
      - arch: amd64
        base: "ubuntu@24.04"
        path: my-charm/my-charm_ubuntu-24.04-amd64.charm
"""
        root = tmp_path
        (root / "artifacts.build.yaml").write_text(build_yaml)
        charm_subdir = root / "my-charm"
        charm_subdir.mkdir()
        (charm_subdir / "charmcraft.yaml").write_text("name: my-charm\n")
        (charm_subdir / "my-charm_ubuntu-24.04-amd64.charm").write_bytes(b"fake")

        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            calls.append(cmd)
            if "upload" in cmd:
                return _mock_result(stdout=_DUPLICATE_STDOUT, returncode=1)
            # release command succeeds
            return _mock_result()

        with patch("opcli.core.publish.run_command", side_effect=fake_run):
            results = artifacts_publish(root, channel="latest/edge")

        assert len(results) == 1
        assert results[0].releases[0].revision == _EXISTING_REVISION
        # A release command must have been issued with the reused revision
        release_cmds = [c for c in calls if "release" in c]
        assert any(f"--revision={_EXISTING_REVISION}" in c for c in release_cmds), (
            f"Expected --revision={_EXISTING_REVISION} in a release call, got: {release_cmds}"
        )

    def test_reraises_non_duplicate_error(self, tmp_path: Path) -> None:
        """Non-duplicate SubprocessError is still raised."""
        build_yaml = """\
version: 1
charms:
  - name: my-charm
    charmcraft-yaml: my-charm/charmcraft.yaml
    builds:
      - arch: amd64
        base: "ubuntu@24.04"
        path: my-charm/my-charm_ubuntu-24.04-amd64.charm
"""
        root = tmp_path
        (root / "artifacts.build.yaml").write_text(build_yaml)
        charm_subdir = root / "my-charm"
        charm_subdir.mkdir()
        (charm_subdir / "charmcraft.yaml").write_text("name: my-charm\n")
        (charm_subdir / "my-charm_ubuntu-24.04-amd64.charm").write_bytes(b"fake")

        other_error = json.dumps({"errors": [{"code": "auth-error", "message": "Unauthorized"}]})

        def fake_run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            return _mock_result(stdout=other_error, stderr="Unauthorized", returncode=1)

        with (
            patch("opcli.core.publish.run_command", side_effect=fake_run),
            pytest.raises(SubprocessError),
        ):
            artifacts_publish(root, channel="latest/edge")


_WARNING_EXIT_REVISION = 56

_CHARM_BUILD_YAML_NO_RESOURCES = """\
version: 1
charms:
  - name: my-charm
    charmcraft-yaml: my-charm/charmcraft.yaml
    builds:
      - arch: amd64
        base: "ubuntu@24.04"
        path: my-charm/my-charm_ubuntu-24.04-amd64.charm
"""


def _make_charm_fixture(root: Path) -> None:
    """Write minimal charm files under *root* for no-resources publish tests."""
    (root / "artifacts.build.yaml").write_text(_CHARM_BUILD_YAML_NO_RESOURCES)
    charm_subdir = root / "my-charm"
    charm_subdir.mkdir()
    (charm_subdir / "charmcraft.yaml").write_text("name: my-charm\n")
    (charm_subdir / "my-charm_ubuntu-24.04-amd64.charm").write_bytes(b"fake")


class TestExperimentalExtensionsEnvVar:
    """charmcraft upload/upload-resource must be called with CHARMCRAFT_ENABLE_EXPERIMENTAL_EXTENSIONS=1.

    Without this env var, charmcraft raises ExtensionError for charms that use
    experimental extensions (e.g. go-framework on ubuntu@24.04) during a
    post-upload unpublished-libs check.  The exception fires BEFORE
    emit.message({"revision": N}) is called, so stdout is empty and charmcraft
    exits 1 — making the upload result unrecoverable.

    Setting the env var converts the exception into a harmless progress message
    so the command exits 0 and emits the JSON revision normally.
    """

    def test_upload_passes_experimental_extensions_env(self, tmp_path: Path) -> None:
        """Charmcraft upload is called with CHARMCRAFT_ENABLE_EXPERIMENTAL_EXTENSIONS=1."""
        _make_charm_fixture(tmp_path)

        upload_envs: list[dict[str, str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            # "upload" in cmd uses list element equality; "upload-resource" is a
            # distinct element and will never match "upload", so no guard needed.
            if "upload" in cmd:
                env = kwargs.get("env") or {}
                upload_envs.append(dict(env) if isinstance(env, dict) else {})
                return _mock_result(stdout=json.dumps({"revision": _WARNING_EXIT_REVISION}))
            return _mock_result()

        with patch("opcli.core.publish.run_command", side_effect=fake_run):
            artifacts_publish(tmp_path, channel="latest/edge")

        assert len(upload_envs) == 1, f"Expected exactly 1 upload call, got: {len(upload_envs)}"
        assert upload_envs[0].get("CHARMCRAFT_ENABLE_EXPERIMENTAL_EXTENSIONS") == "1", (
            f"Expected env var in upload call, got: {upload_envs[0]}"
        )

    def test_upload_resource_passes_experimental_extensions_env(self, tmp_path: Path) -> None:
        """Charmcraft upload-resource is also called with CHARMCRAFT_ENABLE_EXPERIMENTAL_EXTENSIONS=1."""
        build_yaml = """\
version: 1
rocks:
  - name: my-rock
    rockcraft-yaml: my-rock/rockcraft.yaml
    builds:
      - arch: amd64
        image: ghcr.io/canonical/my-rock:latest
charms:
  - name: my-charm
    charmcraft-yaml: my-charm/charmcraft.yaml
    builds:
      - arch: amd64
        base: "ubuntu@24.04"
        path: built-charm/my-charm_amd64.charm
    resources:
      my-rock-image:
        type: oci-image
        rock: my-rock
"""
        root = tmp_path
        (root / "artifacts.build.yaml").write_text(build_yaml)
        charm_subdir = root / "my-charm"
        charm_subdir.mkdir()
        (charm_subdir / "charmcraft.yaml").write_text("name: my-charm\n")
        built = root / "built-charm"
        built.mkdir()
        (built / "my-charm_amd64.charm").write_bytes(b"fake")

        upload_resource_calls: list[tuple[list[str], dict[str, str]]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            if "upload-resource" in cmd:
                env = kwargs.get("env") or {}
                upload_resource_calls.append(
                    (list(cmd), dict(env) if isinstance(env, dict) else {})
                )
                return _mock_result(stdout=json.dumps({"revision": 1}))
            if "upload" in cmd:
                return _mock_result(stdout=json.dumps({"revision": _WARNING_EXIT_REVISION}))
            return _mock_result()

        with patch("opcli.core.publish.run_command", side_effect=fake_run):
            artifacts_publish(root, channel="latest/edge")

        assert len(upload_resource_calls) == 1, (
            f"Expected exactly 1 upload-resource call, got: {len(upload_resource_calls)}"
        )
        cmd_used, env_used = upload_resource_calls[0]
        assert "my-charm" in cmd_used, f"Expected charm name in cmd: {cmd_used}"
        assert "my-rock-image" in cmd_used, f"Expected resource name in cmd: {cmd_used}"
        assert env_used.get("CHARMCRAFT_ENABLE_EXPERIMENTAL_EXTENSIONS") == "1", (
            f"Expected env var in upload-resource call, got: {env_used}"
        )


class TestDuplicateUploadWithResources:
    """Duplicate digest reuse for charms with resources (_upload_charm_no_release)."""

    def test_reuses_existing_revision_on_duplicate(self, tmp_path: Path) -> None:
        """When charmcraft upload returns duplicate-digest, revision is reused for release."""
        build_yaml = """\
version: 1
rocks:
  - name: my-rock
    rockcraft-yaml: my-rock/rockcraft.yaml
    builds:
      - arch: amd64
        image: ghcr.io/canonical/my-rock:latest
charms:
  - name: my-charm
    charmcraft-yaml: my-charm/charmcraft.yaml
    builds:
      - arch: amd64
        base: "ubuntu@24.04"
        path: built-charm/my-charm_amd64.charm
    resources:
      my-rock-image:
        type: oci-image
        rock: my-rock
"""
        root = tmp_path
        (root / "artifacts.build.yaml").write_text(build_yaml)
        charm_subdir = root / "my-charm"
        charm_subdir.mkdir()
        (charm_subdir / "charmcraft.yaml").write_text("name: my-charm\n")
        (root / "built-charm").mkdir()
        (root / "built-charm" / "my-charm_amd64.charm").write_bytes(b"fake")

        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            calls.append(cmd)
            if "upload-resource" in cmd:
                return _mock_result(stdout=json.dumps({"revision": 7}))
            # charm upload returns duplicate error
            return _mock_result(stdout=_DUPLICATE_STDOUT, returncode=1)

        with patch("opcli.core.publish.run_command", side_effect=fake_run):
            results = artifacts_publish(root, channel="latest/edge")

        assert len(results) == 1
        assert results[0].releases[0].revision == _EXISTING_REVISION
        # release command must have been called with the reused revision
        release_cmds = [c for c in calls if "release" in c and "upload-resource" not in c]
        assert any(f"--revision={_EXISTING_REVISION}" in c for c in release_cmds), (
            f"Expected --revision={_EXISTING_REVISION} in a release command, got: {release_cmds}"
        )
