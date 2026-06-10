# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration test: machine-charm deploys and reaches active/idle."""

import os

import jubilant

from opcli.pytest_plugin import CharmPathList


def test_spread_job_forwarded_to_pytest() -> None:
    """SPREAD_JOB set by spread is forwarded into pytest via pytest-environment-template.

    This validates the full env-forwarding chain:
    spread sets SPREAD_JOB → opcli pytest expand renders it via
    pytest-environment-template → tox passenv forwards it → pytest receives it.
    """
    assert "SPREAD_JOB" in os.environ, (
        "SPREAD_JOB not found in pytest environment. "
        "Expected it to be forwarded via pytest-environment-template in spread.yaml."
    )
    assert os.environ["SPREAD_JOB"], "SPREAD_JOB is empty — spread should always set it."


def test_machine_charm_active(
    juju: jubilant.Juju,
    charm_paths: dict[str, CharmPathList],
) -> None:
    """Deploy machine-charm and assert it reaches active/idle within 5 minutes."""
    juju.deploy(charm_paths["machine-charm"].path, app="machine-charm")
    status = juju.wait(jubilant.all_active, timeout=300)
    assert status.apps["machine-charm"].units["machine-charm/0"].workload_status.current == "active"
