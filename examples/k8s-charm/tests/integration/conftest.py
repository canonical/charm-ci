# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for k8s-charm sub-charm integration tests.

This demonstrates a monorepo layout where a sub-charm has its own
integration tests directory with its own conftest, independent of the
top-level tests/integration/.

Artifact fixtures (charm_path, resource_images, etc.) are provided
automatically by the pytest-opcli plugin — no flag plumbing needed.
"""

from collections.abc import Generator

import jubilant
import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
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
