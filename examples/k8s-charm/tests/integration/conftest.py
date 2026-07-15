# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for k8s-charm sub-charm integration tests.

This demonstrates a monorepo layout where a sub-charm has its own
integration tests directory with its own conftest, independent of the
top-level tests/integration/.

Artifact fixtures (charm_path, resource_images, etc.) are provided
automatically by the pytest-opcli plugin — no flag plumbing needed.

Because the root ``artifacts.build.yaml`` covers multiple charms,
``resource_images`` is unavailable here. Instead we provide a local
``rock_images`` fixture for the multi-charm pattern.
"""

from collections.abc import Generator
from pathlib import Path

import jubilant
import pytest

from opcli.models.artifacts_build import ArtifactsGenerated
from opcli.pytest_plugin import artifacts_root_from_yaml_path, build_rock_images


@pytest.fixture(scope="session")
def rock_images(
    opcli_artifacts: ArtifactsGenerated,
    opcli_build_yaml_path: Path,
) -> dict[str, str]:
    """Rock name → image ref for the current arch (multi-charm repo pattern)."""
    return build_rock_images(opcli_artifacts, artifacts_root_from_yaml_path(opcli_build_yaml_path))


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
