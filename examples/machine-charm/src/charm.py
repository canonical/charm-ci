#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Trivial machine charm used to validate the opcli CI build pipeline."""

import ops


class MachineCharm(ops.CharmBase):
    """A minimal machine charm that immediately goes active."""

    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        self.framework.observe(self.on.install, self._on_install)

    def _on_install(self, event: ops.InstallEvent) -> None:
        self.unit.status = ops.ActiveStatus()


if __name__ == "__main__":
    ops.main(MachineCharm)
