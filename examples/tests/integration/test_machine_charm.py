# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration test: machine-charm deploys and reaches active/idle."""


import jubilant

from opcli.pytest_plugin import CharmPathList


def test_machine_charm_active(
    juju: jubilant.Juju,
    charm_paths: dict[str, CharmPathList],
) -> None:
    """Deploy machine-charm and assert it reaches active/idle within 5 minutes."""
    juju.deploy(charm_paths["machine-charm"].path, app="machine-charm")
    status = juju.wait(jubilant.all_active, timeout=300)
    assert status.apps["machine-charm"].units["machine-charm/0"].workload_status.current == "active"
