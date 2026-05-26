# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Core logic for ``opcli env provision``, ``opcli env load``,
and ``opcli env deploy-registry``.

``provision_prepare`` invokes concierge to provision the test environment.

``load`` reads ``artifacts.build.yaml`` and pushes locally-built rock
OCI images into a container image registry so that Juju / MicroK8s can
pull them during integration tests.

``registry`` deploys a local OCI registry at ``localhost:32000`` using a
Kubernetes manifest (``src/opcli/data/registry.yaml``).  The manifest works
identically on both canonical k8s and MicroK8s — it creates a
``container-registry`` namespace, a ``registry:2`` deployment, and a
NodePort Service on port 32000.  This is a local-only operation — in CI
images are served from GHCR.
"""

import logging
import os
import shutil
import socket
from pathlib import Path

from opcli.core.exceptions import ConfigurationError
from opcli.core.progress import status
from opcli.core.subprocess import run_command
from opcli.core.yaml_io import dump_artifacts_build, dump_yaml, load_artifacts_build, load_yaml

logger = logging.getLogger(__name__)

_CONCIERGE_YAML = "concierge.yaml"
_ARTIFACTS_GENERATED_YAML = "artifacts.build.yaml"
_DEFAULT_REGISTRY = "localhost:32000"
_REGISTRY_PORT = 32000

_REGISTRY_YAML = Path(__file__).parent.parent / "data" / "registry.yaml"
_REGISTRY_DEPLOYMENT = "deployment/registry"
_REGISTRY_NAMESPACE = "container-registry"

# Concierge providers that support the image-registry config (container runtimes
# that pull from Docker Hub).  LXD doesn't use OCI images from Docker Hub.
_IMAGE_REGISTRY_PROVIDERS: frozenset[str] = frozenset({"microk8s", "k8s"})


def provision_prepare(
    root: Path,
    *,
    concierge_file: str = _CONCIERGE_YAML,
    image_registry: str = "",
) -> None:
    """Run ``concierge prepare`` to provision the test environment.

    When *image_registry* is non-empty, patches the concierge file to inject
    ``image-registry: {url: <value>}`` into each enabled provider section
    before invoking concierge.  This configures a Docker Hub mirror (e.g. on
    self-hosted runners) without requiring manual edits to ``concierge.yaml``.

    When not running as root, automatically invokes concierge via ``sudo``.

    Raises:
        ConfigurationError: If the concierge file does not exist or
            concierge is not installed.
        SubprocessError: If concierge exits non-zero.
    """
    concierge_path = root / concierge_file
    if not concierge_path.exists():
        msg = f"{concierge_file} not found. Create a concierge.yaml in the repository root."
        raise ConfigurationError(msg)

    if not shutil.which("concierge"):
        msg = "concierge is not installed. Install with: sudo snap install concierge --classic"
        raise ConfigurationError(msg)

    if image_registry:
        _patch_concierge_image_registry(concierge_path, image_registry)

    cmd = ["concierge", "prepare", "-c", str(concierge_path)]
    if os.getuid() != 0:
        cmd = ["sudo", *cmd]

    run_command(cmd, cwd=str(root))
    status("Provisioning complete")


def _patch_concierge_image_registry(concierge_path: Path, url: str) -> None:
    """Inject ``image-registry: {url: <url>}`` into container-runtime providers.

    Only patches ``microk8s`` and ``k8s`` providers that declare ``enable: true``.
    LXD and other providers are left untouched — they don't pull OCI images from
    Docker Hub.  The file is rewritten in place using ruamel.yaml to preserve
    comments.
    """
    data = load_yaml(concierge_path)
    providers = data.get("providers")
    if not isinstance(providers, dict):
        logger.info(
            "No 'providers' section in %s — skipping image-registry patch.", concierge_path
        )
        return

    patched = False
    for name, provider_cfg in providers.items():
        if not isinstance(provider_cfg, dict):
            continue
        if name not in _IMAGE_REGISTRY_PROVIDERS:
            continue
        if not provider_cfg.get("enable", False):
            continue
        provider_cfg["image-registry"] = {"url": url}
        patched = True

    if patched:
        dump_yaml(data, concierge_path)
        logger.info("Patched %s with image-registry url: %s", concierge_path, url)


def provision_load(
    root: Path,
    *,
    registry: str = _DEFAULT_REGISTRY,
    missing_registry: str = "skip",
) -> list[str]:
    """Push locally-built rock images to *registry*.

    Reads ``artifacts.build.yaml`` and for each rock with a local
    ``file`` output, converts the ``.rock`` archive to an OCI image and
    pushes it to the target registry using ``skopeo``.

    Args:
        root: Project root directory.
        registry: Target image registry address.
        missing_registry: Policy when registry is unreachable:
            ``"skip"`` — silently return (default, backward-compatible).
            ``"deploy"`` — call :func:`provision_registry` first, then push.
            ``"fail"`` — raise :class:`ConfigurationError`.

    Returns:
        List of image references that were pushed.

    Raises:
        ConfigurationError: If ``artifacts.build.yaml`` is missing, if
            *missing_registry* is ``"fail"`` and registry is unreachable,
            or if ``"deploy"`` is used with a non-local registry.
        SubprocessError: If a push command fails.
    """
    gen_path = root / _ARTIFACTS_GENERATED_YAML
    if not gen_path.exists():
        logger.info("No %s found — nothing to load.", _ARTIFACTS_GENERATED_YAML)
        return []

    generated = load_artifacts_build(gen_path)

    has_pushable_rocks = any(build.file for rock in generated.rocks for build in rock.builds)
    if not has_pushable_rocks:
        logger.info("No rocks with local file output — nothing to push.")
        return []

    if not _ensure_registry_available(root, registry, missing_registry):
        return []

    pushed: list[str] = []

    for rock in generated.rocks:
        for build in rock.builds:
            if not build.file:
                continue

            rock_path = Path(build.file)
            image_ref = f"{registry}/{rock.name}:{build.arch}"

            if build.image == image_ref:
                logger.info("Already loaded %s, skipping", image_ref)
                continue

            # Push directly from .rock archive to registry in one step — no Docker
            # daemon needed (avoids failures in MicroK8s-only environments).
            status(f"Pushing '{rock.name}' ({build.arch}) → {image_ref}")
            run_command(
                [
                    "sudo",
                    "rockcraft.skopeo",
                    "--insecure-policy",
                    "copy",
                    "--dest-tls-verify=false",
                    f"oci-archive:{rock_path}",
                    f"docker://{image_ref}",
                ],
                cwd=str(root),
            )

            build.image = image_ref

            pushed.append(image_ref)
            logger.info("Pushed %s", image_ref)

    if pushed:
        dump_artifacts_build(generated, gen_path)

    return pushed


def _ensure_registry_available(root: Path, registry: str, missing_registry: str) -> bool:
    """Ensure the registry is reachable, applying the *missing_registry* policy.

    Returns:
        ``True`` if the registry is available and pushing can proceed.
        ``False`` if the policy is ``"skip"`` and the registry is unreachable.

    Raises:
        ConfigurationError: If the policy is ``"fail"`` or if ``"deploy"``
            cannot resolve the situation.
    """
    if _is_port_open("localhost", _REGISTRY_PORT):
        return True

    if missing_registry == "skip":
        logger.info(
            "Registry not reachable at localhost:%d — skipping load.",
            _REGISTRY_PORT,
        )
        return False

    if missing_registry == "deploy":
        if registry != _DEFAULT_REGISTRY:
            msg = (
                f"--missing-registry=deploy only works with the managed local "
                f"registry ({_DEFAULT_REGISTRY}), not '{registry}'."
            )
            raise ConfigurationError(msg)
        status("Registry not reachable — deploying local registry")
        result = provision_registry(root)
        if result == "skipped":
            logger.info("Registry deployment skipped (no k8s provider).")
            return False
        if not _is_port_open("localhost", _REGISTRY_PORT):
            msg = (
                "Registry was deployed but is still not reachable at "
                f"localhost:{_REGISTRY_PORT}. Check k8s cluster health."
            )
            raise ConfigurationError(msg)
        return True

    # missing_registry == "fail"
    msg = (
        f"Registry not reachable at localhost:{_REGISTRY_PORT} and "
        "--missing-registry=fail was specified."
    )
    raise ConfigurationError(msg)


def provision_registry(
    root: Path,
) -> str:
    """Deploy a local OCI registry at ``localhost:32000``.

    Auto-detects the active k8s provider (microk8s, canonical k8s, or
    standalone kubectl) and applies ``src/opcli/data/registry.yaml``.
    The same manifest works on all providers.

    Returns:
        ``"deployed"``       — the registry was just provisioned.
        ``"already_running"``— a service is already listening on port 32000;
                               nothing was changed.
        ``"skipped"``        — no k8s provider found or no rocks to push.

    Raises:
        SubprocessError: If the underlying kubectl command fails.
    """
    # Skip if there are no rocks to push — the registry is only needed to serve
    # locally-built rock images.
    gen_path = root / _ARTIFACTS_GENERATED_YAML
    if gen_path.exists():
        generated = load_artifacts_build(gen_path)
        if not generated.rocks:
            logger.info("No rocks in %s, skipping registry setup.", _ARTIFACTS_GENERATED_YAML)
            return "skipped"

    # Quick TCP probe — skip if something is already listening.
    if _is_port_open("localhost", _REGISTRY_PORT):
        logger.info("Registry already running at localhost:%d.", _REGISTRY_PORT)
        return "already_running"

    kubectl = _detect_kubectl()
    if kubectl is None:
        logger.info("No k8s provider found on PATH, skipping registry setup.")
        return "skipped"

    # Wait for at least one node to be Ready before deploying — freshly
    # bootstrapped clusters (e.g. in nested LXD) can take a while.
    run_command([*kubectl, "wait", "--for=condition=Ready", "node", "--all", "--timeout=300s"])
    run_command([*kubectl, "apply", "-f", "-"], stdin=_REGISTRY_YAML.read_text())
    run_command(
        [
            *kubectl,
            "rollout",
            "status",
            _REGISTRY_DEPLOYMENT,
            "-n",
            _REGISTRY_NAMESPACE,
            "--timeout=300s",
        ]
    )

    return "deployed"


def _is_port_open(host: str, port: int, *, timeout: float = 2.0) -> bool:
    """Return ``True`` if a TCP connection to *host*:*port* succeeds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _detect_kubectl() -> list[str] | None:
    """Auto-detect the kubectl command based on installed k8s providers.

    Detection order: microk8s → k8s → standalone kubectl.
    Returns the command prefix (e.g. ``["sudo", "microk8s", "kubectl"]``)
    or ``None`` if no k8s tooling is found.
    """
    if shutil.which("microk8s"):
        return ["sudo", "microk8s", "kubectl"]
    if shutil.which("k8s"):
        return ["sudo", "k8s", "kubectl"]
    if shutil.which("kubectl"):
        return ["sudo", "kubectl"]
    return None
