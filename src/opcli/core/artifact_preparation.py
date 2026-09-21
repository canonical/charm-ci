# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""On-demand preparation of the current CI run's integration-test artifacts."""

import os
import re
from pathlib import Path

from opcli.core.artifacts import artifacts_fetch
from opcli.core.constants import ARTIFACTS_YAML
from opcli.core.exceptions import ConfigurationError
from opcli.core.provision import provision_load
from opcli.core.yaml_io import load_artifacts_build


def deferred_artifacts_enabled() -> bool:
    """Return whether artifact preparation is deferred in this CI process.

    Local testing continues to use prebuilt artifacts, even when a shared
    Spread backend enables the opt-in.
    """
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return False
    value = os.environ.get("OPCLI_DEFER_ARTIFACTS", "")
    if value not in ("", "0", "1"):
        raise ConfigurationError("OPCLI_DEFER_ARTIFACTS must be 0 or 1.")
    return value == "1"


def prepare_deferred_artifacts(start: Path) -> Path:
    """Fetch and prepare artifacts before any automatic manifest discovery.

    Run in the pytest user's context so downloaded files remain writable by
    that user. The normal provision helper handles privileged registry/image
    operations. No existing build manifest counts as evidence of readiness.
    """
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if not run_id.isascii() or not run_id.isdecimal() or int(run_id) <= 0:
        raise ConfigurationError(
            "Deferred artifacts require GITHUB_RUN_ID to be a positive integer."
        )
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ConfigurationError("Deferred artifacts require GITHUB_REPOSITORY=owner/repo.")
    raw_timeout = os.environ.get("OPCLI_FETCH_WAIT_TIMEOUT", "")
    timeout = None
    if raw_timeout:
        if not raw_timeout.isascii() or not raw_timeout.isdecimal():
            raise ConfigurationError("OPCLI_FETCH_WAIT_TIMEOUT must be a non-negative integer.")
        timeout = int(raw_timeout)

    root = _find_artifacts_root(start)
    manifest = artifacts_fetch(root, run_id, repo, wait=True, wait_timeout=timeout)
    pushed = provision_load(root, missing_registry="deploy")
    artifacts = load_artifacts_build(manifest)
    if any(
        build.file and build.image not in pushed
        for rock in artifacts.rocks
        for build in rock.builds
    ):
        raise ConfigurationError(
            "Deferred artifact preparation could not push local rock images. "
            "Ensure a Kubernetes provider is available for the local registry."
        )
    return manifest


def _find_artifacts_root(start: Path) -> Path:
    """Find the build plan independently of any missing or stale build output."""
    directory = start.resolve()
    while True:
        if (directory / ARTIFACTS_YAML).is_file():
            return directory
        if (directory / ".git").exists() or directory.parent == directory:
            break
        directory = directory.parent
    raise ConfigurationError(
        f"Deferred artifacts require {ARTIFACTS_YAML} under {start} or an ancestor "
        "within the project."
    )
