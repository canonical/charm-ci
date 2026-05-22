#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Trivial sidecar k8s charm used to validate the opcli CI build pipeline.

Deploys to a k8s model with a rock container. Goes active once pebble is ready.
"""

import ops


class K8sCharm(ops.CharmBase):
    """A minimal sidecar k8s charm that goes active when pebble is ready."""

    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        self.framework.observe(
            self.on.k8s_rock_pebble_ready, self._on_pebble_ready
        )

    def _on_pebble_ready(self, event: ops.PebbleReadyEvent) -> None:
        self.unit.status = ops.ActiveStatus()


if __name__ == "__main__":
    ops.main(K8sCharm)
