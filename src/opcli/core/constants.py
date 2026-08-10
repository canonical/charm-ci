# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared constants for the opcli core layer."""

from pathlib import Path

ARTIFACTS_YAML = "artifacts.yaml"
ARTIFACTS_BUILD_YAML = "artifacts.build.yaml"

# Directory (relative to the project root) that holds all opcli-generated
# build output: the expanded spread.yaml/task.yaml tree (see core/spread.py)
# and artifacts.build.yaml.  Consumers should add this directory to their
# .gitignore.
BUILD_DIR = "build"


def artifacts_build_path(root: Path) -> Path:
    """Return the path to ``artifacts.build.yaml`` under *root*'s build directory."""
    return root / BUILD_DIR / ARTIFACTS_BUILD_YAML
