# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""pytest-opcli: pytest plugin for automatic artifact injection.

Auto-discovers ``artifacts.build.yaml`` and exposes built artifacts as
session-scoped fixtures, eliminating the need for CLI flag plumbing in
``conftest.py``.

Discovery order:

1. ``OPCLI_ARTIFACTS_BUILD_YAML`` environment variable (absolute path).
2. ``--artifacts-build-yaml`` pytest CLI option.
3. Walk up from pytest's ``rootdir`` until ``artifacts.build.yaml`` is found.
4. ``pytest.UsageError`` if not found.

Fixtures
--------
opcli_artifacts
    The full :class:`~opcli.models.artifacts_build.ArtifactsGenerated` model.
charm_path
    Path string for a single charm with a single base.  Fails if there are
    zero or more than one charm, or if the single charm has more than one
    build for the current architecture (ambiguous base).
charm_paths
    ``{charm_name: [path, ...]}`` -- list of paths per charm for the current
    architecture.  Handles multi-base builds correctly.
rock_images
    ``{rock_name: image_or_file}`` -- image reference or local file path for
    the current architecture.
charm_resource_images
    ``{charm_name: {resource_name: image_ref}}`` -- resolves the OCI-image
    resource -> rock -> image mapping for every charm.  Useful for multi-charm
    repos.
resource_images
    ``{resource_name: image_ref}`` -- single-charm shortcut that resolves
    resource -> rock -> image.  Designed for use with ``jubilant.Juju.deploy``:

    .. code-block:: python

        def test_deploy(juju, charm_path, resource_images):
            juju.deploy(charm_path, resources=resource_images)
            juju.wait(jubilant.all_active)

    Fails if there are zero or more than one charm.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from opcli.models.artifacts_build import ArtifactsGenerated, CharmOutput, RockOutput

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# pytest hooks
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:  # pragma: no cover
    """Register the ``--artifacts-build-yaml`` option under the "opcli" group."""
    group = parser.getgroup("opcli")
    group.addoption(
        "--artifacts-build-yaml",
        action="store",
        default=None,
        metavar="PATH",
        help=(
            "Path to artifacts.build.yaml.  Overrides the OPCLI_ARTIFACTS_BUILD_YAML "
            "environment variable and automatic walk-up discovery."
        ),
    )


# ---------------------------------------------------------------------------
# Internal helpers -- tested directly by unit tests
# ---------------------------------------------------------------------------


def _discover_artifacts_build(config: pytest.Config) -> Path:
    """Resolve the path to ``artifacts.build.yaml``.

    Checks, in order:

    1. ``OPCLI_ARTIFACTS_BUILD_YAML`` environment variable.
    2. ``--artifacts-build-yaml`` pytest CLI option.
    3. Walk up from ``config.rootpath`` until the file is found.

    Raises:
        pytest.UsageError: If the file cannot be found.
    """
    from opcli.core.constants import ARTIFACTS_BUILD_YAML

    env_path = os.environ.get("OPCLI_ARTIFACTS_BUILD_YAML")
    if env_path:
        p = Path(env_path)
        if not p.is_file():
            raise pytest.UsageError(
                f"OPCLI_ARTIFACTS_BUILD_YAML={env_path!r} does not exist or is not a file."
            )
        return p

    cli_path: str | None = config.getoption("--artifacts-build-yaml", default=None)
    if cli_path:
        p = Path(cli_path)
        if not p.is_file():
            raise pytest.UsageError(
                f"--artifacts-build-yaml={cli_path!r} does not exist or is not a file."
            )
        return p

    directory = Path(config.rootpath)
    while True:
        candidate = directory / ARTIFACTS_BUILD_YAML
        if candidate.is_file():
            return candidate
        parent = directory.parent
        if parent == directory:
            break
        directory = parent

    raise pytest.UsageError(
        f"{ARTIFACTS_BUILD_YAML!r} not found (searched up from {config.rootpath!r}). "
        "Run 'opcli artifacts build' first, or set OPCLI_ARTIFACTS_BUILD_YAML."
    )


def _select_arch_builds_charm(
    builds: list[CharmOutput],
    arch: str,
    artifact_name: str,
) -> list[CharmOutput]:
    """Return charm builds matching *arch*, or all builds if none match."""
    matching = [b for b in builds if b.arch == arch]
    if matching:
        return matching
    if builds:
        logger.warning(
            "No charm build for '%s' matches arch '%s'; using all available: %s",
            artifact_name,
            arch,
            [b.arch for b in builds],
        )
    return builds


def _select_arch_builds_rock(
    builds: list[RockOutput],
    arch: str,
    artifact_name: str,
) -> list[RockOutput]:
    """Return rock builds matching *arch*, or all builds if none match."""
    matching = [b for b in builds if b.arch == arch]
    if matching:
        return matching
    if builds:
        logger.warning(
            "No rock build for '%s' matches arch '%s'; using all available: %s",
            artifact_name,
            arch,
            [b.arch for b in builds],
        )
    return builds


def _build_charm_path(artifacts: ArtifactsGenerated) -> str:
    """Core logic for the ``charm_path`` fixture."""
    from opcli.core.env import current_arch

    charms = artifacts.charms
    if not charms:
        pytest.fail("charm_path: no charms found in artifacts.build.yaml")
    if len(charms) > 1:
        names = [c.name for c in charms]
        pytest.fail(
            f"charm_path: multiple charms found ({names!r}); use charm_paths for multi-charm repos"
        )

    charm = charms[0]
    arch = current_arch()
    arch_builds = _select_arch_builds_charm(charm.builds, arch, charm.name)

    if len(arch_builds) > 1:
        bases = [b.base for b in arch_builds]
        pytest.fail(
            f"charm_path: charm '{charm.name}' has {len(arch_builds)} builds for "
            f"arch '{arch}' (bases: {bases!r}); use charm_paths to get all paths"
        )

    build = arch_builds[0]
    if not build.path:
        pytest.fail(
            f"charm_path: charm '{charm.name}' build for arch '{arch}' has no local "
            "path (CI artifact). Run 'opcli artifacts localize' first."
        )
    return build.path


def _build_charm_paths(artifacts: ArtifactsGenerated) -> dict[str, list[str]]:
    """Core logic for the ``charm_paths`` fixture."""
    from opcli.core.env import current_arch

    arch = current_arch()
    result: dict[str, list[str]] = {}
    for charm in artifacts.charms:
        arch_builds = _select_arch_builds_charm(charm.builds, arch, charm.name)
        result[charm.name] = [b.path for b in arch_builds if b.path]
    return result


def _build_rock_images(artifacts: ArtifactsGenerated) -> dict[str, str]:
    """Core logic for the ``rock_images`` fixture."""
    from opcli.core.env import current_arch

    arch = current_arch()
    result: dict[str, str] = {}
    for rock in artifacts.rocks:
        arch_builds = _select_arch_builds_rock(rock.builds, arch, rock.name)
        for build in arch_builds:
            value = build.image or build.file
            if value:
                result[rock.name] = value
                break
    return result


def _build_charm_resource_images(
    artifacts: ArtifactsGenerated,
    rock_imgs: dict[str, str],
) -> dict[str, dict[str, str]]:
    """Core logic for the ``charm_resource_images`` fixture."""
    result: dict[str, dict[str, str]] = {}
    for charm in artifacts.charms:
        resources: dict[str, str] = {}
        for res_name, res in (charm.resources or {}).items():
            if res.rock and res.rock in rock_imgs:
                resources[res_name] = rock_imgs[res.rock]
        result[charm.name] = resources
    return result


def _build_resource_images(
    artifacts: ArtifactsGenerated,
    charm_res_images: dict[str, dict[str, str]],
) -> dict[str, str]:
    """Core logic for the ``resource_images`` fixture."""
    charms = artifacts.charms
    if not charms:
        pytest.fail("resource_images: no charms found in artifacts.build.yaml")
    if len(charms) > 1:
        names = [c.name for c in charms]
        pytest.fail(
            f"resource_images: multiple charms found ({names!r}); "
            "use charm_resource_images for multi-charm repos"
        )
    return charm_res_images[charms[0].name]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def opcli_artifacts(request: pytest.FixtureRequest) -> ArtifactsGenerated:
    """The full ``ArtifactsGenerated`` model from ``artifacts.build.yaml``."""
    from opcli.core.yaml_io import load_artifacts_build

    path = _discover_artifacts_build(request.config)
    return load_artifacts_build(path)


@pytest.fixture(scope="session")
def charm_path(opcli_artifacts: ArtifactsGenerated) -> str:
    """Path to the single built charm for the current architecture."""
    return _build_charm_path(opcli_artifacts)


@pytest.fixture(scope="session")
def charm_paths(opcli_artifacts: ArtifactsGenerated) -> dict[str, list[str]]:
    """Paths for every charm keyed by charm name, filtered to the current arch."""
    return _build_charm_paths(opcli_artifacts)


@pytest.fixture(scope="session")
def rock_images(opcli_artifacts: ArtifactsGenerated) -> dict[str, str]:
    """Image reference (or local file path) for each rock, keyed by rock name."""
    return _build_rock_images(opcli_artifacts)


@pytest.fixture(scope="session")
def charm_resource_images(
    opcli_artifacts: ArtifactsGenerated,
    rock_images: dict[str, str],
) -> dict[str, dict[str, str]]:
    """OCI-image resources for every charm, keyed by charm name then resource name.

    Returns ``{charm_name: {resource_name: image_ref}}``.

    Example usage with ``pytest-jubilant`` in a multi-charm repo::

        def test_deploy(juju, charm_paths, charm_resource_images):
            for path in charm_paths["operator"]:
                juju.deploy(path, resources=charm_resource_images["operator"])
    """
    return _build_charm_resource_images(opcli_artifacts, rock_images)


@pytest.fixture(scope="session")
def resource_images(
    opcli_artifacts: ArtifactsGenerated,
    charm_resource_images: dict[str, dict[str, str]],
) -> dict[str, str]:
    """OCI-image resources for the single charm, keyed by resource name.

    A convenience shortcut for single-charm repos::

        def test_deploy(juju, charm_path, resource_images):
            juju.deploy(charm_path, resources=resource_images)
            juju.wait(jubilant.all_active)

    Fails (``pytest.fail``) if there are zero or more than one charm.
    Use ``charm_resource_images`` for multi-charm repos.
    """
    return _build_resource_images(opcli_artifacts, charm_resource_images)


__all__ = [
    "charm_path",
    "charm_paths",
    "charm_resource_images",
    "opcli_artifacts",
    "resource_images",
    "rock_images",
]
