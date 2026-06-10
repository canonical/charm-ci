# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration test: env template forwards spread env vars into pytest.

``examples/spread.yaml`` sets::

    pytest-environment-template: |
      SPREAD_JOB={{ env.get("SPREAD_JOB", "") }}

This causes ``opcli pytest expand`` to prefix the tox command with
``SPREAD_JOB=<value>``.  This test asserts that the variable actually
reaches the pytest process, proving the end-to-end env-forwarding flow.
"""

import os


def test_spread_job_forwarded_to_pytest() -> None:
    """SPREAD_JOB set by spread is forwarded into the pytest process via env template."""
    assert "SPREAD_JOB" in os.environ, (
        "SPREAD_JOB not found in pytest environment. "
        "Expected it to be forwarded via pytest-environment-template in spread.yaml."
    )
    assert os.environ["SPREAD_JOB"], "SPREAD_JOB is empty — spread should always set it."
