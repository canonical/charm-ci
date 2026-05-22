# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration test: machine-charm deploys and reaches active/idle."""


import pytest
from pytest_operator.plugin import OpsTest


@pytest.mark.asyncio
async def test_machine_charm_active(
    ops_test: OpsTest,
    machine_charm_file: str,
) -> None:
    """Deploy machine-charm and assert it reaches active/idle within 5 minutes."""
    app = await ops_test.model.deploy(
        machine_charm_file,
        application_name="machine-charm",
    )
    await ops_test.model.wait_for_idle(
        apps=[app.name],
        status="active",
        timeout=300,
    )
    assert app.units[0].workload_status == "active"
