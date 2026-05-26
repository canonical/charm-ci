# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for provision_prepare and _patch_concierge_image_registry."""

from pathlib import Path
from unittest.mock import patch

import pytest

from opcli.core.exceptions import ConfigurationError, SubprocessError
from opcli.core.provision import _patch_concierge_image_registry, provision_prepare
from opcli.core.yaml_io import load_yaml


class TestPatchConciergeImageRegistry:
    """Tests for _patch_concierge_image_registry."""

    def test_patches_enabled_providers(self, tmp_path: Path) -> None:
        """image-registry is injected into providers with enable: true."""
        concierge = tmp_path / "concierge.yaml"
        concierge.write_text(
            "juju:\n"
            "  channel: 3.6/stable\n"
            "providers:\n"
            "  lxd:\n"
            "    enable: true\n"
            "  microk8s:\n"
            "    enable: true\n"
            "    bootstrap: true\n"
            "  k8s:\n"
            "    enable: true\n"
        )

        _patch_concierge_image_registry(concierge, "https://mirror.example.com")

        data = load_yaml(concierge)
        # LXD is NOT patched — it doesn't use Docker Hub images
        assert "image-registry" not in data["providers"]["lxd"]
        assert data["providers"]["microk8s"]["image-registry"] == {
            "url": "https://mirror.example.com"
        }
        assert data["providers"]["k8s"]["image-registry"] == {"url": "https://mirror.example.com"}

    def test_skips_disabled_providers(self, tmp_path: Path) -> None:
        """Providers without enable: true are not patched."""
        concierge = tmp_path / "concierge.yaml"
        concierge.write_text(
            "providers:\n  microk8s:\n    enable: true\n  k8s:\n    enable: false\n"
        )

        _patch_concierge_image_registry(concierge, "https://mirror.example.com")

        data = load_yaml(concierge)
        assert data["providers"]["microk8s"]["image-registry"] == {
            "url": "https://mirror.example.com"
        }
        assert "image-registry" not in data["providers"]["k8s"]

    def test_skips_non_container_providers(self, tmp_path: Path) -> None:
        """LXD and other non-container providers are never patched."""
        concierge = tmp_path / "concierge.yaml"
        concierge.write_text("providers:\n  lxd:\n    enable: true\n")

        _patch_concierge_image_registry(concierge, "https://mirror.example.com")

        data = load_yaml(concierge)
        assert "image-registry" not in data["providers"]["lxd"]

    def test_overwrites_existing_image_registry(self, tmp_path: Path) -> None:
        """Existing image-registry is replaced."""
        concierge = tmp_path / "concierge.yaml"
        concierge.write_text(
            "providers:\n"
            "  microk8s:\n"
            "    enable: true\n"
            "    image-registry:\n"
            "      url: https://old-mirror.example.com\n"
        )

        _patch_concierge_image_registry(concierge, "https://new-mirror.example.com")

        data = load_yaml(concierge)
        assert data["providers"]["microk8s"]["image-registry"] == {
            "url": "https://new-mirror.example.com"
        }

    def test_no_providers_section(self, tmp_path: Path) -> None:
        """No error when concierge.yaml has no providers section."""
        concierge = tmp_path / "concierge.yaml"
        concierge.write_text("juju:\n  channel: 3.6/stable\n")

        # Should not raise
        _patch_concierge_image_registry(concierge, "https://mirror.example.com")

        data = load_yaml(concierge)
        assert "providers" not in data

    def test_preserves_other_provider_fields(self, tmp_path: Path) -> None:
        """Existing provider config (addons, bootstrap, etc.) is preserved."""
        concierge = tmp_path / "concierge.yaml"
        concierge.write_text(
            "providers:\n"
            "  microk8s:\n"
            "    enable: true\n"
            "    bootstrap: true\n"
            "    addons:\n"
            "      - hostpath-storage\n"
        )

        _patch_concierge_image_registry(concierge, "https://mirror.example.com")

        data = load_yaml(concierge)
        mk = data["providers"]["microk8s"]
        assert mk["bootstrap"] is True
        assert mk["addons"] == ["hostpath-storage"]
        assert mk["image-registry"] == {"url": "https://mirror.example.com"}


class TestProvisionPrepareImageRegistry:
    """Tests for provision_prepare with --image-registry."""

    @patch("opcli.core.provision.shutil.which", return_value="/snap/bin/concierge")
    @patch("opcli.core.provision.run_command")
    def test_empty_image_registry_is_noop(
        self, mock_run: object, mock_which: object, tmp_path: Path
    ) -> None:
        """Empty image_registry does not patch concierge.yaml."""
        concierge = tmp_path / "concierge.yaml"
        original_content = "providers:\n  lxd:\n    enable: true\n"
        concierge.write_text(original_content)

        provision_prepare(tmp_path, concierge_file="concierge.yaml", image_registry="")

        # File unchanged
        assert concierge.read_text() == original_content

    @patch("opcli.core.provision.shutil.which", return_value="/snap/bin/concierge")
    @patch("opcli.core.provision.run_command")
    def test_image_registry_patches_before_concierge(
        self, mock_run: object, mock_which: object, tmp_path: Path
    ) -> None:
        """Non-empty image_registry patches the file before running concierge."""
        concierge = tmp_path / "concierge.yaml"
        concierge.write_text("providers:\n  microk8s:\n    enable: true\n")

        provision_prepare(
            tmp_path, concierge_file="concierge.yaml", image_registry="https://mirror.test"
        )

        data = load_yaml(concierge)
        assert data["providers"]["microk8s"]["image-registry"] == {"url": "https://mirror.test"}

    @patch("opcli.core.provision.run_command")
    def test_missing_concierge_raises(self, mock_run: object, tmp_path: Path) -> None:
        """ConfigurationError when concierge file does not exist."""
        with pytest.raises(ConfigurationError, match="not found"):
            provision_prepare(
                tmp_path, concierge_file="missing.yaml", image_registry="https://mirror.test"
            )

    @patch("opcli.core.provision.shutil.which", return_value=None)
    @patch("opcli.core.provision.run_command")
    def test_concierge_not_installed_raises(
        self, mock_run: object, mock_which: object, tmp_path: Path
    ) -> None:
        """ConfigurationError when concierge binary is not on PATH."""
        concierge = tmp_path / "concierge.yaml"
        concierge.write_text("providers:\n  lxd:\n    enable: true\n")

        with pytest.raises(ConfigurationError, match="concierge is not installed"):
            provision_prepare(tmp_path)

    @patch("opcli.core.provision.shutil.which", return_value="/snap/bin/concierge")
    @patch("opcli.core.provision.run_command")
    def test_concierge_sudo_error_rewrapped(
        self, mock_run: object, mock_which: object, tmp_path: Path
    ) -> None:
        """ConfigurationError with sudo hint when concierge reports privilege error."""
        concierge = tmp_path / "concierge.yaml"
        concierge.write_text("providers:\n  lxd:\n    enable: true\n")
        mock_run.side_effect = SubprocessError(
            cmd=["concierge", "prepare"],
            returncode=1,
            stderr="this command should be run with `sudo`, or as `root`",
        )

        with pytest.raises(ConfigurationError, match="sudo opcli env provision"):
            provision_prepare(tmp_path)
