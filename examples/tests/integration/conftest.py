# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared pytest fixtures and CLI options for examples integration tests.

opcli pytest expand assembles flags of the form:
  --charm-file=./machine-charm.charm
  --charm-file=./k8s-charm.charm
  --k8s-rock-image=localhost:32000/k8s-rock:latest

These fixtures parse those flags and expose per-charm fixtures.
"""


from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--charm-file",
        action="append",
        default=[],
        dest="charm_files",
        help="Path to a built charm file (may be repeated for multiple charms).",
    )
    parser.addoption(
        "--k8s-rock-image",
        action="store",
        default=None,
        help="OCI image reference for the k8s-rock-image resource.",
    )


def _find_charm(request: pytest.FixtureRequest, name: str) -> str:
    """Return the charm file whose path contains *name*, or fail the test."""
    files: list[str] = request.config.getoption("charm_files")
    matches = [f for f in files if name in Path(f).name]
    if not matches:
        pytest.fail(
            f"No --charm-file matching '{name}' was provided. "
            f"Received: {files or ['(none)']}"
        )
    if len(matches) > 1:
        pytest.fail(
            f"Multiple --charm-file values match '{name}': {matches}. "
            "Provide exactly one charm file per charm."
        )
    return matches[0]


@pytest.fixture(scope="module")
def machine_charm_file(request: pytest.FixtureRequest) -> str:
    """Absolute or relative path to the built machine-charm .charm file."""
    return _find_charm(request, "machine-charm")


@pytest.fixture(scope="module")
def k8s_charm_file(request: pytest.FixtureRequest) -> str:
    """Absolute or relative path to the built k8s-charm .charm file."""
    return _find_charm(request, "k8s-charm")


@pytest.fixture(scope="module")
def k8s_rock_image(request: pytest.FixtureRequest) -> str:
    """OCI image reference for the k8s-rock-image resource."""
    image: str | None = request.config.getoption("--k8s-rock-image")
    if not image:
        pytest.fail(
            "--k8s-rock-image was not provided. "
            "Run 'opcli artifacts push-images' before running tests locally, "
            "or ensure the build workflow has pushed the rock to GHCR."
        )
    return image
