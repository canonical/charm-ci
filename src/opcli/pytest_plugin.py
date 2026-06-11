# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

r"""pytest-opcli: pytest plugin for automatic artifact injection.

Two input modes — use whichever fits your workflow:

**CLI-flag mode** (pfe-compatible, no build step needed)::

    pytest --charm-file k8s-charm=./k8s-charm.charm \
           --resource-image oci-image=ghcr.io/org/rock:sha

**yaml mode** (zero-config, auto-discovers ``artifacts.build.yaml``)::

    pytest   # file discovered via --artifacts-build-yaml, env var, or walk-up

Each fixture independently checks its own CLI flags first, then falls back
to yaml discovery. Mixing modes per-fixture is supported.

``artifacts.build.yaml`` discovery order (yaml mode):

1. ``--artifacts-build-yaml`` pytest CLI option.
2. ``OPCLI_ARTIFACTS_BUILD_YAML`` environment variable (absolute path).
3. Walk up from pytest's ``rootdir`` until ``artifacts.build.yaml`` is found
   (stops at the git root or filesystem root).
4. ``pytest.UsageError`` if not found.

Fixtures
--------
opcli_build_yaml_path
    Resolved ``Path`` to ``artifacts.build.yaml``.
    For use as a fixture dependency in custom conftest fixtures.
opcli_artifacts
    The full :class:`~opcli.models.artifacts_build.ArtifactsGenerated` model.
    Always requires ``artifacts.build.yaml``; not available in CLI-flag mode.
charm_path
    Path string for a single charm with a single base.
    CLI: ``--charm-file name=path`` (single entry, path validated to exist).
    yaml: single charm from ``artifacts.build.yaml``.
charm_paths
    ``{charm_name: CharmPathList}`` -- all charm paths for the current arch,
    keyed by charm name.  See :class:`CharmPathList` for the access API.
    CLI: all ``--charm-file name=path`` entries (paths validated to exist).
    yaml: all charms from ``artifacts.build.yaml``.

    Example (single base)::

        def test_deploy(juju, charm_paths):
            juju.deploy(charm_paths['my-charm'].path)

    Example (multi-base)::

        def test_deploy(juju, charm_paths):
            juju.deploy(charm_paths['my-charm']['ubuntu@24.04'])

resource_images
    ``{resource_name: image_ref}`` -- OCI resource images for the single charm.
    CLI: all ``--resource-image name=ref`` entries.
    yaml: resolves resource -> rock -> image for the single charm.

    Example::

        def test_deploy(juju, charm_path, resource_images):
            juju.deploy(charm_path, resources=resource_images)
            juju.wait(jubilant.all_active)

charm_resource_images
    ``{charm_name: {resource_name: image_ref}}`` -- OCI resource images for every
    charm, keyed first by charm name then by resource name.
    yaml mode only (no CLI-flag equivalent); requires ``artifacts.build.yaml``.

    Example::

        def test_deploy(juju, charm_paths, charm_resource_images):
            juju.deploy(charm_paths['my-charm'].path,
                        resources=charm_resource_images['my-charm'])
            juju.wait(jubilant.all_active)

Helpers (for multi-charm conftest patterns)
-------------------------------------------
build_rock_images
    ``{rock_name: image_ref}`` for the current arch.  Use this in a conftest
    ``rock_images`` fixture for multi-charm repos (see example in docstring).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

import pytest

if TYPE_CHECKING:
    from opcli.models.artifacts_build import ArtifactsGenerated, CharmOutput, RockOutput

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CharmPathList
# ---------------------------------------------------------------------------


class CharmPathList:
    """Ordered list of charm paths for a single charm name, with optional base info.

    Stores ``(base, path)`` pairs where *base* may be ``None`` when base
    information is unavailable (e.g. CLI-flag mode).

    Access patterns::

        # Single-base shortcut
        paths.path                  # str; fails if there is more than one build

        # Base-keyed lookup (requires base info)
        paths['ubuntu@22.04']       # str
        paths['ubuntu@24.04']       # str

        # Iterate all paths
        for p in paths: ...         # yields str

        # Introspection
        len(paths)                  # int
        paths.bases                 # list[str | None]

    Raises ``KeyError`` when using string indexing on a list that has no base
    information (CLI-flag mode), with a helpful message explaining why.
    """

    def __init__(self, entries: list[tuple[str | None, str]]) -> None:
        self._entries = entries

    # -- single-path shortcut ------------------------------------------------

    @property
    def path(self) -> str:
        """Return the single charm path.

        Raises ``pytest.fail`` when there is more than one build so the test
        produces a clear, actionable message.
        """
        if len(self._entries) == 1:
            return self._entries[0][1]
        bases = [b for b, _ in self._entries]
        pytest.fail(
            f"charm_paths: .path is ambiguous — {len(self._entries)} builds available "
            f"(bases: {bases!r}). Use ['<base>'] to select one."
        )

    # -- base-keyed lookup ---------------------------------------------------

    def __getitem__(self, base: str) -> str:
        """Return the path for the given *base* string."""
        if not isinstance(base, str):
            raise TypeError(f"CharmPathList indices must be str, not {type(base).__name__!r}")
        # Check whether any entry has base info at all
        if all(b is None for b, _ in self._entries):
            raise KeyError(
                f"{base!r}: no base information is available for this charm "
                "(base info is only present in yaml mode, not CLI-flag mode)"
            )
        for b, p in self._entries:
            if b == base:
                return p
        available = [b for b, _ in self._entries if b is not None]
        raise KeyError(f"{base!r} not found; available bases: {available!r}")

    # -- sequence interface --------------------------------------------------

    def __iter__(self) -> Iterator[str]:
        """Iterate over all path strings."""
        return (p for _, p in self._entries)

    def __len__(self) -> int:
        """Return the number of builds."""
        return len(self._entries)

    # -- introspection -------------------------------------------------------

    @property
    def bases(self) -> list[str | None]:
        """Return the list of base strings (``None`` when unavailable)."""
        return [b for b, _ in self._entries]

    # -- repr ----------------------------------------------------------------

    def __repr__(self) -> str:
        """Return a developer-friendly representation."""
        return f"CharmPathList({self._entries!r})"

    def __eq__(self, other: object) -> bool:
        """Return True if *other* is a CharmPathList with the same entries."""
        if isinstance(other, CharmPathList):
            return self._entries == other._entries
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]  # mutable; explicitly unhashable


# ---------------------------------------------------------------------------
# pytest hooks
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:  # pragma: no cover
    """Register opcli options."""
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
    group.addoption(
        "--charm-file",
        action="append",
        default=None,
        metavar="NAME=FILE",
        help=(
            "Built charm file as NAME=PATH (repeatable).  "
            "Bypasses artifacts.build.yaml for charm_path and charm_paths."
        ),
    )
    group.addoption(
        "--resource-image",
        action="append",
        default=None,
        metavar="NAME=REF",
        help=(
            "OCI image reference as NAME=REF (repeatable).  "
            "Bypasses artifacts.build.yaml for resource_images."
        ),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def opcli_build_yaml_path(request: pytest.FixtureRequest) -> Path:
    """Resolved path to ``artifacts.build.yaml``.

    Useful as a dependency in conftest fixtures that call
    :func:`build_rock_images` directly (e.g. multi-charm repos).
    """
    return _discover_artifacts_build(request.config)


@pytest.fixture(scope="session")
def opcli_artifacts(opcli_build_yaml_path: Path) -> ArtifactsGenerated:
    """The full ``ArtifactsGenerated`` model from ``artifacts.build.yaml``."""
    from opcli.core.yaml_io import load_artifacts_build

    return load_artifacts_build(opcli_build_yaml_path)


@pytest.fixture(scope="session")
def charm_path(request: pytest.FixtureRequest) -> str:
    """Path to the single built charm for the current architecture.

    CLI mode: uses ``--charm-file name=path`` (exactly one entry required).
    yaml mode: single charm from ``artifacts.build.yaml``.
    """
    charm_files = request.config.getoption("--charm-file", default=None)
    if charm_files is not None:
        pairs = _parse_kv_flags(charm_files, "--charm-file")
        if len(pairs) > 1:
            names = list(pairs)
            pytest.fail(
                f"charm_path: multiple --charm-file entries ({names!r}); "
                "use charm_paths for multi-charm invocations"
            )
        path = Path(next(iter(pairs.values()))).resolve()
        if not path.is_file():
            raise pytest.UsageError(f"charm_path: --charm-file path does not exist: {path}")
        return str(path)

    yaml_path = _discover_artifacts_build(
        request.config,
        hint="or pass --charm-file NAME=PATH to bypass artifacts.build.yaml",
    )
    from opcli.core.yaml_io import load_artifacts_build

    return _build_charm_path(load_artifacts_build(yaml_path), yaml_path.parent)


@pytest.fixture(scope="session")
def charm_paths(request: pytest.FixtureRequest) -> dict[str, CharmPathList]:
    """Paths for every charm keyed by charm name, filtered to the current arch.

    CLI mode: all ``--charm-file name=path`` entries as ``{name: CharmPathList}``.
    yaml mode: all charms from ``artifacts.build.yaml``.
    """
    charm_files = request.config.getoption("--charm-file", default=None)
    if charm_files is not None:
        pairs = _parse_kv_flags(charm_files, "--charm-file")
        result: dict[str, CharmPathList] = {}
        for name, raw_path in pairs.items():
            path = Path(raw_path).resolve()
            if not path.is_file():
                raise pytest.UsageError(f"charm_paths: --charm-file path does not exist: {path}")
            result[name] = CharmPathList([(None, str(path))])
        return result

    yaml_path = _discover_artifacts_build(
        request.config,
        hint="or pass --charm-file NAME=PATH to bypass artifacts.build.yaml",
    )
    from opcli.core.yaml_io import load_artifacts_build

    return _build_charm_paths(load_artifacts_build(yaml_path), yaml_path.parent)


@pytest.fixture(scope="session")
def resource_images(request: pytest.FixtureRequest) -> dict[str, str]:
    """OCI-image resources keyed by resource name.

    CLI mode: all ``--resource-image name=ref`` entries as ``{name: ref}``.
    yaml mode: resolves resource → rock → image for the single charm.

    Single-charm only; for multi-charm repos use :func:`charm_resource_images`.

    Example::

        def test_deploy(juju, charm_path, resource_images):
            juju.deploy(charm_path, resources=resource_images)
            juju.wait(jubilant.all_active)

    In yaml mode, fails if there are zero or more than one charm.
    """
    resource_imgs = request.config.getoption("--resource-image", default=None)
    if resource_imgs is not None:
        return _parse_kv_flags(resource_imgs, "--resource-image")

    charm_files = request.config.getoption("--charm-file", default=None)
    yaml_path = _discover_artifacts_build(
        request.config,
        hint=(
            "pass --resource-image NAME=REF for each OCI resource, "
            "or run 'opcli artifacts build' to create artifacts.build.yaml"
        )
        if charm_files
        else None,
    )
    from opcli.core.yaml_io import load_artifacts_build

    artifacts = load_artifacts_build(yaml_path)
    rock_imgs = build_rock_images(artifacts, yaml_path.parent)
    return _build_resource_images(artifacts, rock_imgs)


@pytest.fixture(scope="session")
def charm_resource_images(request: pytest.FixtureRequest) -> dict[str, dict[str, str]]:
    """OCI-image resources keyed by charm name, then by resource name.

    yaml mode only — always requires ``artifacts.build.yaml``.
    No CLI-flag equivalent; use :func:`resource_images` for single-charm CLI workflows.
    Returns ``{charm_name: {resource_name: image_ref}}`` for every charm.

    Example::

        def test_deploy(juju, charm_paths, charm_resource_images):
            juju.deploy(charm_paths['my-charm'].path,
                        resources=charm_resource_images['my-charm'])
            juju.wait(jubilant.all_active)
    """
    yaml_path = _discover_artifacts_build(
        request.config,
        hint="run 'opcli artifacts build' to create artifacts.build.yaml",
    )
    from opcli.core.yaml_io import load_artifacts_build

    artifacts = load_artifacts_build(yaml_path)
    rock_imgs = build_rock_images(artifacts, yaml_path.parent)
    return _build_charm_resource_images(artifacts, rock_imgs)


# ---------------------------------------------------------------------------
# Internal helpers -- tested directly by unit tests
# ---------------------------------------------------------------------------


def _parse_kv_flags(values: list[str], flag: str) -> dict[str, str]:
    """Parse a list of ``NAME=VALUE`` strings into a dict.

    Splits on the first ``=`` only, so values containing ``=`` are preserved.
    Raises ``pytest.UsageError`` on malformed entries (missing ``=``, empty
    name, empty value, or duplicate name).
    """
    result: dict[str, str] = {}
    for entry in values:
        if "=" not in entry:
            raise pytest.UsageError(f"{flag}: expected NAME=VALUE, got {entry!r}")
        name, _, value = entry.partition("=")
        if not name:
            raise pytest.UsageError(f"{flag}: NAME must not be empty, got {entry!r}")
        if not value:
            raise pytest.UsageError(f"{flag}: VALUE must not be empty, got {entry!r}")
        if name in result:
            raise pytest.UsageError(
                f"{flag}: duplicate NAME {name!r}; each name may only appear once"
            )
        result[name] = value
    return result


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
) -> dict[str, CharmPathList]:
    """Core logic for the ``charm_paths`` fixture."""
    from opcli.core.env import current_arch

    arch = current_arch()
    result: dict[str, CharmPathList] = {}
    for charm in artifacts.charms:
        arch_builds = _select_arch_builds_charm(charm.builds, arch)
        if not arch_builds:
            available = sorted({b.arch for b in charm.builds})
            pytest.fail(
                f"charm_paths: no build for charm '{charm.name}' matches arch '{arch}'; "
                f"available arches: {available!r}"
            )
        entries = [(b.base, _resolve_path(b.path, artifacts_root)) for b in arch_builds if b.path]
        if not entries:
            pytest.fail(
                f"charm_paths: charm '{charm.name}' arch-{arch!r} builds have no local "
                "path (CI artifacts). Run 'opcli artifacts localize' first."
            )
        result[charm.name] = CharmPathList(entries)
    return result


def build_rock_images(artifacts: ArtifactsGenerated, artifacts_root: Path) -> dict[str, str]:
    """Return a ``{rock_name: image_ref}`` mapping for the current architecture.

    For each rock in *artifacts*, resolves the image reference (registry push)
    or local ``.rock`` file path, filtered to the current machine's CPU arch.

    Designed for use in ``conftest.py`` for multi-charm repos where
    :fixture:`resource_images` is unavailable::

        from opcli.models.artifacts_build import ArtifactsGenerated
        from opcli.pytest_plugin import build_rock_images

        @pytest.fixture(scope="session")
        def rock_images(opcli_artifacts: ArtifactsGenerated, opcli_build_yaml_path) -> dict[str, str]:
            return build_rock_images(opcli_artifacts, opcli_build_yaml_path.parent)
    """
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


def _build_resource_images(
    artifacts: ArtifactsGenerated,
    rock_imgs: dict[str, str],
) -> dict[str, str]:
    """Core logic for the ``resource_images`` fixture."""
    charms = artifacts.charms
    if not charms:
        pytest.fail("resource_images: no charms found in artifacts.build.yaml")
    if len(charms) > 1:
        names = [c.name for c in charms]
        pytest.fail(
            f"resource_images: multiple charms found ({names!r}); "
            "use the charm_resource_images fixture for multi-charm repos"
        )
    charm = charms[0]
    result: dict[str, str] = {}
    for res_name, res in (charm.resources or {}).items():
        if not res.rock:
            continue
        if res.rock not in rock_imgs:
            available = sorted(rock_imgs)
            pytest.fail(
                f"resource_images: resource '{res_name}' references rock '{res.rock}' "
                f"which is not in rock_imgs (available: {available!r})"
            )
        result[res_name] = rock_imgs[res.rock]
    return result


def _build_charm_resource_images(
    artifacts: ArtifactsGenerated,
    rock_imgs: dict[str, str],
) -> dict[str, dict[str, str]]:
    """Core logic for the ``charm_resource_images`` fixture."""
    charms = artifacts.charms
    if not charms:
        pytest.fail("charm_resource_images: no charms found in artifacts.build.yaml")
    result: dict[str, dict[str, str]] = {}
    for charm in charms:
        charm_result: dict[str, str] = {}
        for res_name, res in (charm.resources or {}).items():
            if not res.rock:
                continue
            if res.rock not in rock_imgs:
                available = sorted(rock_imgs)
                pytest.fail(
                    f"charm_resource_images: charm '{charm.name}' resource '{res_name}' "
                    f"references rock '{res.rock}' "
                    f"which is not in rock_imgs (available: {available!r})"
                )
            charm_result[res_name] = rock_imgs[res.rock]
        result[charm.name] = charm_result
    return result


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


def _discover_artifacts_build(
    config: pytest.Config,
    hint: str | None = None,
) -> Path:
    """Resolve the path to ``artifacts.build.yaml``.

    Checks, in order:

    1. ``--artifacts-build-yaml`` pytest CLI option.
    2. ``OPCLI_ARTIFACTS_BUILD_YAML`` environment variable.
    3. Walk up from ``config.rootpath`` until the file is found (stops at git root).

    Args:
        config: The pytest config object.
        hint: Optional extra hint appended to the not-found error message.

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

    suffix = f"  {hint}" if hint else ""
    raise pytest.UsageError(
        f"{ARTIFACTS_BUILD_YAML!r} not found (searched up from {config.rootpath!r}). "
        f"Run 'opcli artifacts build' first, or set OPCLI_ARTIFACTS_BUILD_YAML.{suffix}"
    )


__all__ = [
    "CharmPathList",
    "build_rock_images",
    "charm_path",
    "charm_paths",
    "charm_resource_images",
    "opcli_artifacts",
    "opcli_build_yaml_path",
    "resource_images",
]
