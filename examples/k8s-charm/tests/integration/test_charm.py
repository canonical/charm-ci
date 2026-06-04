# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration test for the k8s-charm sub-charm (monorepo pattern).

This test runs in its own suite with cwd set to k8s-charm/, demonstrating
that a sub-charm can have independent integration tests that receive only
the artifacts relevant to it.
"""

import jubilant


def test_deploy(
    juju: jubilant.Juju,
    charm_path: str,
    resource_images: dict[str, str],
) -> None:
    """Deploy the k8s-charm and verify active/idle."""
    juju.deploy(
        charm_path,
        app="k8s-charm",
        resources=resource_images,
    )
    status = juju.wait(jubilant.all_active, timeout=300)
    assert status.apps["k8s-charm"].units["k8s-charm/0"].workload_status.current == "active"
