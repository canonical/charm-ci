# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for machine-charm sub-charm integration tests."""

from collections.abc import Generator

import jubilant
import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--charm-file", action="append", default=[], dest="charm_files")
    parser.addoption("--model", action="store", default=None)
    parser.addoption("--keep-models", action="store_true", default=False)
    # TODO(#33): Accept (and ignore) rock image flags passed by opcli for other charms.
    # Once #33 lands, per-suite argument templating will make this unnecessary.
    parser.addoption(
        "--k8s-rock-image", action="store", default=None, dest="k8s_rock_image"
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
    files: list[str] = request.config.getoption("charm_files")
    if not files:
        pytest.fail("No --charm-file provided.")
    # Pick the machine-charm file from the list
    for f in files:
        if "machine-charm" in f:
            return f
    return files[0]
