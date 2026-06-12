# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared utilities for determining pack directory and yaml symlink management.

Used by both ``artifacts`` (build) and ``publish`` (upload) to ensure the
working directory presented to craft tools (charmcraft, rockcraft, snapcraft)
is identical in both operations.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from opcli.core.exceptions import ConfigurationError


def resolve_pack_dir(yaml_path: Path, pack_dir_str: str | None, root: Path) -> Path:
    """Resolve the directory from which the pack/upload command should run.

    Args:
        yaml_path: Resolved absolute path to the craft YAML file.
        pack_dir_str: Optional ``pack-dir`` string from ``artifacts.yaml``
            (relative to *root*).  When ``None``, defaults to the parent
            directory of *yaml_path*.
        root: Project root directory (used to resolve *pack_dir_str*).

    Returns:
        Resolved absolute path to the pack directory.
    """
    if pack_dir_str:
        return (root / pack_dir_str).resolve()
    return yaml_path.parent.resolve()


@contextmanager
def with_pack_yaml_symlink(target_name: str, yaml_path: Path, pack_dir: Path) -> Iterator[None]:
    """Temporarily provide ``target_name`` in *pack_dir* via a relative symlink.

    If a regular file already exists at the target path, it is accepted only
    when its content matches *yaml_path* exactly. Existing symlinks are replaced
    for the duration of the context and restored afterwards.  Only symlinks
    are ever removed — regular files created by the craft tool are never touched.

    Args:
        target_name: Filename to create in *pack_dir* (e.g. ``"charmcraft.yaml"``).
        yaml_path: Absolute path to the source YAML file to link to.
        pack_dir: Directory where the symlink will be created.
    """
    target = pack_dir / target_name
    if target.resolve() == yaml_path.resolve():
        yield
        return

    if target.exists() and not target.is_symlink():
        if target.read_bytes() == yaml_path.read_bytes():
            yield
            return
        msg = (
            f"A regular file already exists at {target} and it differs from "
            f"{yaml_path}. Remove it or set pack-dir to a directory without a "
            f"{target_name}."
        )
        raise ConfigurationError(msg)

    # Save the old symlink target so we can restore it on exit.
    old_link_target: str | None = os.readlink(target) if target.is_symlink() else None
    if target.is_symlink():
        target.unlink()
    try:
        target.symlink_to(os.path.relpath(yaml_path, pack_dir))
        yield
    finally:
        if target.is_symlink():
            target.unlink()
        if old_link_target is not None:
            target.symlink_to(old_link_target)
