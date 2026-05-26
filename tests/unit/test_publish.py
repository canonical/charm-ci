# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for opcli artifacts publish."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from opcli.core.exceptions import ConfigurationError, DiscoveryError
from opcli.core.publish import artifacts_publish
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

        # machine-charm: 1 upload (no resources)
        # k8s-charm: 1 upload (register), 1 upload-resource, 1 upload (with resource bindings)
        assert len(calls) == 4  # noqa: PLR2004

        # Check first k8s-charm upload (register resources - no release)
        reg_cmd = calls[1]
        assert "upload" in reg_cmd
        assert "k8s-charm" in str(reg_cmd)
        assert "--release=" not in " ".join(reg_cmd)
        assert "--resource=" not in " ".join(reg_cmd)

        # Check upload-resource call
        res_cmd = calls[2]
        assert "upload-resource" in res_cmd
        assert "k8s-charm" in res_cmd
        assert "k8s-rock-image" in res_cmd
        # Should use the file path (resolved)
        image_arg = next(a for a in res_cmd if a.startswith("--image="))
        assert "k8s-rock_amd64.rock" in image_arg

        # Check upload call for k8s-charm has --resource flag
        charm_cmd = calls[3]
        assert "upload" in charm_cmd
        assert "--resource=k8s-rock-image:5" in charm_cmd
        assert "--release=latest/edge" in charm_cmd

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

        assert len(calls) == 1
        # No --resource flags
        assert not any("--resource" in arg for arg in calls[0])
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
        # First upload is for registration (rev 41)
        # Next two uploads are the final releases (rev 42, 43)
        assert results[0].releases[0].revision == 42  # noqa: PLR2004
        assert results[0].releases[0].base == "ubuntu@22.04"
        assert results[0].releases[1].revision == 43  # noqa: PLR2004
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

        # Only k8s-charm published (1 register + 1 upload-resource + 1 upload with bindings)
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
