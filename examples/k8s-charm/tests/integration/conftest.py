# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for k8s-charm sub-charm integration tests.

This demonstrates a monorepo layout where a sub-charm has its own
integration tests directory with its own conftest, independent of the
top-level tests/integration/.
"""

from collections.abc import Generator

import jubilant
import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--charm-file",
        action="append",
        default=[],
        dest="charm_files",
        help="Path to a built charm file.",
    )
    parser.addoption(
        "--k8s-rock-image",
        action="store",
        default=None,
        help="OCI image reference for the k8s-rock-image resource.",
    )
    parser.addoption(
        "--model",
        action="store",
        default=None,
        help="Juju model name.",
    )
    parser.addoption(
        "--keep-models",
        action="store_true",
        default=False,
        help="Keep models after tests.",
    )


@pytest.fixture(scope="module")
def juju(request: pytest.FixtureRequest) -> Generator[jubilant.Juju]:
    model = request.config.getoption("--model")
    if model:
        yield jubilant.Juju(model=model)
        return

    keep = request.config.getoption("--keep-models")
    with jubilant.temp_model(keep=keep) as j:
        yield j


@pytest.fixture(scope="module")
def charm_file(request: pytest.FixtureRequest) -> str:
    """The single charm file for this sub-charm."""
    files: list[str] = request.config.getoption("charm_files")
    if not files:
        pytest.fail("No --charm-file provided.")
    return files[0]


@pytest.fixture(scope="module")
def rock_image(request: pytest.FixtureRequest) -> str:
    image: str | None = request.config.getoption("--k8s-rock-image")
    if not image:
        pytest.fail("--k8s-rock-image not provided.")
    return image
