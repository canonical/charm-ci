# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for pack_utils module."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from opcli.core.pack_utils import with_pack_yaml_symlink


class TestWithPackYamlSymlink:
    """Tests for the with_pack_yaml_symlink context manager."""

    def test_restores_old_symlink_when_replacement_creation_fails(self, tmp_path: Path) -> None:
        """Pre-existing symlink is restored even if symlink_to() raises.

        This covers the edge case where the unlink→symlink_to window is
        interrupted (e.g. concurrent process, permission error). Without
        the symlink_to inside try the finally block would never run and
        the original symlink would be permanently destroyed.
        """
        pack_dir = tmp_path / "my-charm"
        pack_dir.mkdir()
        yaml_path = pack_dir / "charmcraft-real.yaml"
        yaml_path.write_text("name: my-charm\n")

        # Pre-existing symlink pointing to some other target
        other = tmp_path / "other.yaml"
        other.write_text("name: other\n")
        target = pack_dir / "charmcraft.yaml"
        target.symlink_to("../other.yaml")
        original_target = os.readlink(str(target))

        # Force symlink_to to raise after the old symlink has been unlinked
        real_symlink_to = Path.symlink_to

        call_count = 0

        def failing_symlink_to(self: Path, *args: object, **kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            # Only fail on the first call (the replacement creation)
            if call_count == 1:
                raise OSError("Simulated failure during symlink creation")
            real_symlink_to(self, *args, **kwargs)

        with (
            patch.object(Path, "symlink_to", failing_symlink_to),
            pytest.raises(OSError, match="Simulated failure"),
            with_pack_yaml_symlink("charmcraft.yaml", yaml_path, pack_dir),
        ):
            pass  # never reached

        # The original symlink must be restored
        assert target.is_symlink(), "Original symlink was not restored after failure"
        assert os.readlink(str(target)) == original_target, (
            f"Expected symlink to point to {original_target!r}, got {os.readlink(str(target))!r}"
        )

    def test_restores_old_symlink_when_context_body_raises(self, tmp_path: Path) -> None:
        """Pre-existing symlink is restored when the context body raises."""
        pack_dir = tmp_path / "my-charm"
        pack_dir.mkdir()
        yaml_path = pack_dir / "charmcraft-real.yaml"
        yaml_path.write_text("name: my-charm\n")

        other = tmp_path / "other.yaml"
        other.write_text("name: other\n")
        target = pack_dir / "charmcraft.yaml"
        target.symlink_to("../other.yaml")
        original_target = os.readlink(str(target))

        with (
            pytest.raises(RuntimeError, match="body error"),
            with_pack_yaml_symlink("charmcraft.yaml", yaml_path, pack_dir),
        ):
            raise RuntimeError("body error")

        assert target.is_symlink(), "Original symlink was not restored after body error"
        assert os.readlink(str(target)) == original_target

    def test_no_symlink_created_when_target_matches_yaml(self, tmp_path: Path) -> None:
        """Early return when target already resolves to yaml_path (no-op)."""
        pack_dir = tmp_path
        yaml_path = pack_dir / "charmcraft.yaml"
        yaml_path.write_text("name: my-charm\n")

        ran = False
        with with_pack_yaml_symlink("charmcraft.yaml", yaml_path, pack_dir):
            ran = True

        assert ran
        assert yaml_path.exists() and not yaml_path.is_symlink()
