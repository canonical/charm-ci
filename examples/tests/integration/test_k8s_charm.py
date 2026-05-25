# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration test: k8s-charm deploys to a k8s model with its OCI container."""


import jubilant


def test_k8s_charm_active(
    juju: jubilant.Juju,
    k8s_charm_file: str,
    k8s_rock_image: str,
) -> None:
    """Deploy k8s-charm to a k8s model with its rock container and assert active/idle."""
    juju.deploy(
        k8s_charm_file,
        app="k8s-charm",
        resources={"k8s-rock-image": k8s_rock_image},
    )
    status = juju.wait(jubilant.all_active, timeout=300)
    assert status.apps["k8s-charm"].units["k8s-charm/0"].workload_status.current == "active"
