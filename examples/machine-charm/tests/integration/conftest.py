# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for machine-charm sub-charm integration tests.

Artifact fixtures (charm_path, etc.) are provided automatically by the
pytest-opcli plugin — no flag plumbing needed.
"""

from collections.abc import Generator

import jubilant
import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--model", action="store", default=None)
    parser.addoption("--keep-models", action="store_true", default=False)


@pytest.fixture(scope="module")
def juju(request: pytest.FixtureRequest) -> Generator[jubilant.Juju]:
    model = request.config.getoption("--model")
    if model:
        yield jubilant.Juju(model=model)
        return
    keep = request.config.getoption("--keep-models")
    with jubilant.temp_model(keep=keep) as j:
        yield j
