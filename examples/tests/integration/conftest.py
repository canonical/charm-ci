# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared pytest fixtures and CLI options for examples integration tests.

Artifact fixtures (charm_paths, charm_resource_images, etc.) are provided
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
        help="Juju model name to use (creates a temp model if not provided).",
    )
    parser.addoption(
        "--keep-models",
        action="store_true",
        default=False,
        help="Keep temporarily-created models after the test run.",
    )


@pytest.fixture(scope="module")
def juju(request: pytest.FixtureRequest) -> Generator[jubilant.Juju]:
    """Create or connect to a Juju model for the test module."""
    model = request.config.getoption("--model")
    if model:
        yield jubilant.Juju(model=model)
        return

    keep = request.config.getoption("--keep-models")
    with jubilant.temp_model(keep=keep) as j:
        yield j
