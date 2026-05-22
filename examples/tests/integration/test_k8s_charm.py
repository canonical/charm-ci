# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration test: k8s-charm deploys to a k8s model with its OCI container."""


import pytest
from pytest_operator.plugin import OpsTest


@pytest.mark.asyncio
async def test_k8s_charm_active(
    ops_test: OpsTest,
    k8s_charm_file: str,
    k8s_rock_image: str,
) -> None:
    """Deploy k8s-charm to a k8s model with its rock container and assert active/idle."""
    app = await ops_test.model.deploy(
        k8s_charm_file,
        application_name="k8s-charm",
        resources={"k8s-rock-image": k8s_rock_image},
    )
    await ops_test.model.wait_for_idle(
        apps=[app.name],
        status="active",
        timeout=300,
    )
    assert app.units[0].workload_status == "active"
