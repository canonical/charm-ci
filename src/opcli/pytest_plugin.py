# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""pytest-opcli: pytest plugin for automatic artifact injection.

Auto-discovers ``artifacts.build.yaml`` and exposes built artifacts as
session-scoped fixtures, eliminating the need for CLI flag plumbing in
``conftest.py``.

Discovery order:

1. ``--artifacts-build-yaml`` pytest CLI option.
2. ``OPCLI_ARTIFACTS_BUILD_YAML`` environment variable (absolute path).
3. Walk up from pytest's ``rootdir`` until ``artifacts.build.yaml`` is found
   (stops at the git root or filesystem root).
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
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _opcli_build_yaml_path(request: pytest.FixtureRequest) -> Path:
    """Internal: resolved path to artifacts.build.yaml."""
    return _discover_artifacts_build(request.config)


@pytest.fixture(scope="session")
def opcli_artifacts(_opcli_build_yaml_path: Path) -> ArtifactsGenerated:
    """The full ``ArtifactsGenerated`` model from ``artifacts.build.yaml``."""
    from opcli.core.yaml_io import load_artifacts_build

    return load_artifacts_build(_opcli_build_yaml_path)


@pytest.fixture(scope="session")
def charm_path(
    opcli_artifacts: ArtifactsGenerated,
    _opcli_build_yaml_path: Path,
) -> str:
    """Path to the single built charm for the current architecture."""
    return _build_charm_path(opcli_artifacts, _opcli_build_yaml_path.parent)


@pytest.fixture(scope="session")
def charm_paths(
    opcli_artifacts: ArtifactsGenerated,
    _opcli_build_yaml_path: Path,
) -> dict[str, list[str]]:
    """Paths for every charm keyed by charm name, filtered to the current arch."""
    return _build_charm_paths(opcli_artifacts, _opcli_build_yaml_path.parent)


@pytest.fixture(scope="session")
def rock_images(
    opcli_artifacts: ArtifactsGenerated,
    _opcli_build_yaml_path: Path,
) -> dict[str, str]:
    """Image reference (or local file path) for each rock, keyed by rock name."""
    return _build_rock_images(opcli_artifacts, _opcli_build_yaml_path.parent)


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


# ---------------------------------------------------------------------------
# Internal helpers -- tested directly by unit tests
# ---------------------------------------------------------------------------


def _resolve_path(path: str, artifacts_root: Path) -> str:
    """Return *path* as an absolute string, resolved against *artifacts_root*."""
    p = Path(path)
    if p.is_absolute():
        return str(p)
    return str((artifacts_root / p).resolve())


def _build_charm_path(artifacts: ArtifactsGenerated, artifacts_root: Path) -> str:
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
    arch_builds = _select_arch_builds_charm(charm.builds, arch)

    if not arch_builds:
        available = sorted({b.arch for b in charm.builds})
        pytest.fail(
            f"charm_path: no build for charm '{charm.name}' matches arch '{arch}'; "
            f"available arches: {available!r}"
        )
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
    return _resolve_path(build.path, artifacts_root)


def _build_charm_paths(
    artifacts: ArtifactsGenerated, artifacts_root: Path
) -> dict[str, list[str]]:
    """Core logic for the ``charm_paths`` fixture."""
    from opcli.core.env import current_arch

    arch = current_arch()
    result: dict[str, list[str]] = {}
    for charm in artifacts.charms:
        arch_builds = _select_arch_builds_charm(charm.builds, arch)
        if not arch_builds:
            available = sorted({b.arch for b in charm.builds})
            pytest.fail(
                f"charm_paths: no build for charm '{charm.name}' matches arch '{arch}'; "
                f"available arches: {available!r}"
            )
        result[charm.name] = [_resolve_path(b.path, artifacts_root) for b in arch_builds if b.path]
    return result


def _build_rock_images(artifacts: ArtifactsGenerated, artifacts_root: Path) -> dict[str, str]:
    """Core logic for the ``rock_images`` fixture."""
    from opcli.core.env import current_arch

    arch = current_arch()
    result: dict[str, str] = {}
    for rock in artifacts.rocks:
        arch_builds = _select_arch_builds_rock(rock.builds, arch)
        if not arch_builds:
            available = sorted({b.arch for b in rock.builds})
            pytest.fail(
                f"rock_images: no build for rock '{rock.name}' matches arch '{arch}'; "
                f"available arches: {available!r}"
            )
        for build in arch_builds:
            if build.image:
                result[rock.name] = build.image
                break
            if build.file:
                result[rock.name] = _resolve_path(build.file, artifacts_root)
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


def _select_arch_builds_charm(
    builds: list[CharmOutput],
    arch: str,
) -> list[CharmOutput]:
    """Return charm builds matching *arch*."""
    return [b for b in builds if b.arch == arch]


def _select_arch_builds_rock(
    builds: list[RockOutput],
    arch: str,
) -> list[RockOutput]:
    """Return rock builds matching *arch*."""
    return [b for b in builds if b.arch == arch]


def _discover_artifacts_build(config: pytest.Config) -> Path:
    """Resolve the path to ``artifacts.build.yaml``.

    Checks, in order:

    1. ``--artifacts-build-yaml`` pytest CLI option.
    2. ``OPCLI_ARTIFACTS_BUILD_YAML`` environment variable.
    3. Walk up from ``config.rootpath`` until the file is found (stops at git root).

    Raises:
        pytest.UsageError: If the file cannot be found.
    """
    from opcli.core.constants import ARTIFACTS_BUILD_YAML

    cli_path: str | None = config.getoption("--artifacts-build-yaml", default=None)
    if cli_path:
        p = Path(cli_path)
        if not p.is_file():
            raise pytest.UsageError(
                f"--artifacts-build-yaml={cli_path!r} does not exist or is not a file."
            )
        return p

    env_path = os.environ.get("OPCLI_ARTIFACTS_BUILD_YAML")
    if env_path:
        p = Path(env_path)
        if not p.is_file():
            raise pytest.UsageError(
                f"OPCLI_ARTIFACTS_BUILD_YAML={env_path!r} does not exist or is not a file."
            )
        return p

    directory = Path(config.rootpath)
    while True:
        candidate = directory / ARTIFACTS_BUILD_YAML
        if candidate.is_file():
            return candidate
        if (directory / ".git").exists():
            break
        parent = directory.parent
        if parent == directory:
            break
        directory = parent

    raise pytest.UsageError(
        f"{ARTIFACTS_BUILD_YAML!r} not found (searched up from {config.rootpath!r}). "
        "Run 'opcli artifacts build' first, or set OPCLI_ARTIFACTS_BUILD_YAML."
    )


__all__ = [
    "charm_path",
    "charm_paths",
    "charm_resource_images",
    "opcli_artifacts",
    "resource_images",
    "rock_images",
]
