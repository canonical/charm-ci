# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration test for the k8s-charm sub-charm (monorepo pattern).

This test runs in its own suite with working-dir set to k8s-charm/. Because
the root artifacts.build.yaml covers multiple charms, tests use the
multi-charm fixtures charm_paths and rock_images, scoped by charm name.
"""

import jubilant


def test_deploy(
    juju: jubilant.Juju,
    charm_paths: dict[str, list[str]],
    rock_images: dict[str, str],
) -> None:
    """Deploy the k8s-charm and verify active/idle."""
    juju.deploy(
        charm_paths["k8s-charm"][0],
        app="k8s-charm",
        resources={"k8s-rock": rock_images["k8s-rock"]},
    )
    status = juju.wait(jubilant.all_active, timeout=300)
    assert status.apps["k8s-charm"].units["k8s-charm/0"].workload_status.current == "active"
