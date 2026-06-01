# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration test for the machine-charm sub-charm (monorepo pattern).

Demonstrates auto-discover: false — variants are listed explicitly.
"""

import jubilant


def test_deploy(juju: jubilant.Juju, charm_file: str) -> None:
    """Deploy the machine-charm and verify active/idle."""
    juju.deploy(charm_file, app="machine-charm")
    status = juju.wait(jubilant.all_active, timeout=300)
    assert status.apps["machine-charm"].units["machine-charm/0"].workload_status.current == "active"


def test_status(juju: jubilant.Juju) -> None:
    """Verify the charm reports meaningful status."""
    status = juju.status()
    assert "machine-charm" in status.apps
