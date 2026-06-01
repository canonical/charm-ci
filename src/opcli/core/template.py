# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Jinja2 template rendering for pytest invocation customization.

When a suite in ``integration-suites`` declares ``pytest-arguments-template``
or ``pytest-environment-template``, this module renders the template against
the full ``artifacts.build.yaml`` data.

Template context:
    artifacts: The full ``ArtifactsGenerated`` model (all charms, rocks, snaps
        with all builds across all architectures and bases).
    arch: The current machine architecture (e.g. "amd64", "arm64") — a
        convenience variable for filtering builds in Jinja2 expressions.

The rendered output is parsed into:
    - CLI arguments (whitespace-split tokens) for ``pytest-arguments-template``
    - Environment variables (KEY=VALUE lines) for ``pytest-environment-template``
"""

import shlex
from pathlib import Path

from jinja2 import BaseLoader, Environment, TemplateSyntaxError, UndefinedError

from opcli.core.constants import ARTIFACTS_BUILD_YAML
from opcli.core.env import current_arch
from opcli.core.exceptions import ConfigurationError
from opcli.core.yaml_io import load_artifacts_build
from opcli.models.artifacts_build import ArtifactsGenerated


def render_arguments_template(
    root: Path,
    template_str: str,
) -> list[str]:
    """Render a ``pytest-arguments-template`` into a list of CLI tokens.

    The rendered text is split on whitespace; each non-empty token becomes
    a CLI argument passed to tox/pytest.

    Raises:
        ConfigurationError: If ``artifacts.build.yaml`` is missing or the
            template fails to render.
    """
    rendered = _render_template(root, template_str)
    return shlex.split(rendered)


def render_environment_template(
    root: Path,
    template_str: str,
) -> dict[str, str]:
    """Render a ``pytest-environment-template`` into a dict of env vars.

    Each non-empty line in the rendered output must be ``KEY=VALUE``.
    Lines starting with ``#`` are treated as comments and skipped.

    Raises:
        ConfigurationError: If ``artifacts.build.yaml`` is missing, the
            template fails to render, or a line is malformed.
    """
    rendered = _render_template(root, template_str)
    env: dict[str, str] = {}
    for lineno, line in enumerate(rendered.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            msg = (
                f"pytest-environment-template: line {lineno} is not a valid "
                f"KEY=VALUE pair: {stripped!r}"
            )
            raise ConfigurationError(msg)
        key, _, value = stripped.partition("=")
        env[key.strip()] = value.strip()
    return env


# ---------------------------------------------------------------------------
#  Private helpers
# ---------------------------------------------------------------------------


def _render_template(root: Path, template_str: str) -> str:
    """Render a Jinja2 template string against the artifacts context.

    Returns the rendered string (may contain leading/trailing whitespace).

    Raises:
        ConfigurationError: On missing artifacts file, syntax errors, or
            undefined variable references.
    """
    artifacts = _load_artifacts(root)
    context = _build_context(artifacts)

    env = Environment(  # noqa: S701
        loader=BaseLoader(),
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )

    try:
        template = env.from_string(template_str)
    except TemplateSyntaxError as exc:
        msg = f"Jinja2 syntax error in template: {exc}"
        raise ConfigurationError(msg) from exc

    try:
        return template.render(context)
    except UndefinedError as exc:
        msg = f"Undefined variable in template: {exc}"
        raise ConfigurationError(msg) from exc


def _load_artifacts(root: Path) -> ArtifactsGenerated:
    """Load ``artifacts.build.yaml`` from *root*.

    Raises:
        ConfigurationError: If the file does not exist.
    """
    gen_path = root / ARTIFACTS_BUILD_YAML
    if not gen_path.exists():
        msg = f"{ARTIFACTS_BUILD_YAML} not found. Run 'opcli artifacts build' first."
        raise ConfigurationError(msg)
    return load_artifacts_build(gen_path)


def _build_context(artifacts: ArtifactsGenerated) -> dict[str, object]:
    """Build the Jinja2 template context dictionary."""
    return {
        "artifacts": artifacts,
        "arch": current_arch(),
    }
