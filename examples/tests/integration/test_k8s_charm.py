# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration test: k8s-charm deploys to a k8s model with its OCI container."""


import jubilant


def test_k8s_charm_active(
    juju: jubilant.Juju,
    charm_paths: dict[str, list[str]],
    rock_images: dict[str, str],
) -> None:
    """Deploy k8s-charm to a k8s model with its rock container and assert active/idle."""
    juju.deploy(
        charm_paths["k8s-charm"][0],
        app="k8s-charm",
        resources={"k8s-rock-image": rock_images["k8s-rock"]},