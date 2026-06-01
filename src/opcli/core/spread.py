# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Core logic for ``opcli spread init``, ``expand``, and ``run``.

``init`` discovers integration test modules and generates ``spread.yaml``
plus ``tests/integration/run/task.yaml``.

``expand`` reads ``spread.yaml``, finds backends whose ``type:`` field is a
recognized virtual type (``integration-test``), replaces them with concrete
``<name>-local`` or ``<name>-ci`` backends, and returns the expanded YAML.
The original file is **never** modified.

``run`` creates a temporary directory inside the project root containing
the expanded ``spread.yaml`` with ``reroot: ..`` and runs ``spread`` from
that directory.  Spread discovers ``spread.yaml`` in the temp dir and
uses ``reroot`` to locate the actual project tree one level up.
"""

import json
import logging
import posixpath
import re
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

from ruamel.yaml.error import YAMLError

from opcli.core.env import is_ci as _is_ci
from opcli.core.exceptions import ConfigurationError, ValidationError
from opcli.core.progress import status
from opcli.core.secrets import load_secrets_env as _load_secrets_env
from opcli.core.subprocess import run_command
from opcli.core.yaml_io import dump_yaml, dumps_yaml, literalize, load_yaml

logger = logging.getLogger(__name__)

_SPREAD_YAML = "spread.yaml"
_TASK_YAML_REL = "tests/integration/run/task.yaml"
_BUILD_DIR = "build"
_VIRTUAL_BACKEND = "integration-test"
_INTEGRATION_SUITES_KEY = "integration-suites"


# ---------------------------------------------------------------------------
#  spread init
# ---------------------------------------------------------------------------


_TASK_YAML_CONTENT = (
    "summary: integration tests\n"
    "\n"
    "execute: |\n"
    '    cd "${SPREAD_PATH}"\n'
    '    PYTEST_CMD=$(opcli pytest expand -e "${TOX_ENV:-integration}"'
    ' -- --model testing --keep-models "$MODULE") || exit 1\n'
    "    runuser -l ubuntu -c \"cd '${SPREAD_PATH}' && ${PYTEST_CMD}\"\n"
)

_TASK_YAML_CONTENT_SUITE = (
    "summary: integration tests\n"
    "\n"
    "execute: |\n"
    '    cd "${SPREAD_PATH}"\n'
    '    PYTEST_CMD=$(opcli pytest expand --suite "$OPCLI_SUITE"'
    ' -e "${TOX_ENV:-integration}"'
    ' -- --model testing --keep-models "$MODULE") || exit 1\n'
    "    runuser -l ubuntu -c \"cd '${SPREAD_PATH}/${OPCLI_CWD}' && ${PYTEST_CMD}\"\n"
)


def spread_init(root: Path, *, force: bool = False) -> tuple[Path, Path | None]:
    """Generate ``spread.yaml`` with ``integration-suites``.

    No ``task.yaml`` is generated — it is materialized at expand/run time
    into the ``build/`` directory.

    Returns:
        Tuple of (spread.yaml path, None).

    Raises:
        ConfigurationError: If files exist and *force* is ``False``.
    """
    spread_path = root / _SPREAD_YAML

    if not force and spread_path.exists():
        msg = f"{spread_path.name} already exists. Use --force to overwrite."
        raise ConfigurationError(msg)

    project_name = root.resolve().name

    spread_content = _generate_spread_yaml(project_name)
    spread_path.write_text(spread_content)
    logger.info("Wrote %s", spread_path)

    return spread_path, None


def spread_expand(
    root: Path,
    *,
    ci: bool | None = None,
) -> str:
    """Read ``spread.yaml`` and return the expanded content as a string.

    The output is for display / debugging; it does **not** include the
    ``reroot`` field that ``spread_run`` injects.

    Also prints generated task.yaml paths and content to stderr so the
    user can see (and reproduce) the full spread setup.

    Raises:
        ConfigurationError: If ``spread.yaml`` is missing or malformed.
    """
    expanded = _expand(root, ci=ci)
    _emit_task_yaml_info(expanded)
    return dumps_yaml(literalize(expanded))


def _emit_task_yaml_info(expanded: dict[str, object]) -> None:
    """Print a brief note about runtime-generated task.yaml files to stderr."""
    suites = expanded.get("suites")
    if not isinstance(suites, dict):
        return

    paths: list[str] = []
    for suite_path, suite_cfg in suites.items():
        if not isinstance(suite_cfg, dict):
            continue
        env = suite_cfg.get("environment")
        if not isinstance(env, dict):
            continue
        if "OPCLI_SUITE" in env:
            paths.append(f"{suite_path}run/task.yaml")

    if not paths:
        return

    sys.stderr.write(
        f"Note: {len(paths)} task.yaml file(s) generated at runtime: {', '.join(paths)}\n"
    )


# ---------------------------------------------------------------------------
#  spread run
# ---------------------------------------------------------------------------


def spread_run(
    root: Path,
    *,
    extra_args: list[str] | None = None,
    ci: bool | None = None,
) -> None:
    """Expand ``spread.yaml`` and run ``spread``.

    The expanded YAML is written to a temporary subdirectory inside *root*
    with a ``reroot: ..`` field so spread resolves the project tree from the
    parent directory.  Spread is invoked from that subdirectory; the original
    ``spread.yaml`` is never modified.

    In local mode, secrets from ``.secrets.env`` (if present) are loaded and
    passed as environment variables to the spread subprocess.  In CI mode the
    variables are expected to already be in the environment.

    Raises:
        ConfigurationError: If ``spread.yaml`` is missing or malformed.
        SubprocessError: If spread exits non-zero.
    """
    is_ci = ci if ci is not None else _is_ci()
    expanded = _expand(root, ci=ci)
    expanded["reroot"] = _compose_reroot(expanded.get("reroot"))

    # Load secrets env overlay for local runs only
    secrets_env: dict[str, str] | None = None
    if not is_ci:
        loaded = _load_secrets_env(root)
        if loaded:
            secrets_env = loaded

    with _build_dir_context(root, expanded) as build_dir:
        _materialize_task_files(root, build_dir)
        cmd = ["spread"]
        if extra_args:
            cmd.extend(extra_args)
        status(f"Running spread ({'CI' if is_ci else 'local'} mode)")
        run_command(cmd, cwd=str(build_dir), interactive=True, env=secrets_env)


def spread_jobs(root: Path) -> list[dict[str, str]]:
    """Return all CI spread task selectors as a list of GitHub Actions matrix entries.

    Calls ``spread -list`` on the expanded (CI-mode) ``spread.yaml``, restricted
    to virtual backends (``integration-test``).  Non-virtual
    spread-native backends are excluded.

    Each entry has:

    - ``name``: display name — the full spread selector as returned by
      ``spread -list``.
    - ``selector``: full spread selector as returned by ``spread -list``.
    - ``runs-on``: GitHub Actions runner label (JSON-encoded, from the
      ``runner:`` field on the system entry, or ``"ubuntu-latest"`` if absent).
    - ``arch``: architecture string — taken from the explicit ``arch:`` field
      on the system entry when present, otherwise derived from the runner label.

    Raises:
        ConfigurationError: If ``spread.yaml`` is missing, malformed, or
            contains no virtual backend.
        SubprocessError: If ``spread -list`` fails.
    """
    raw = _load_spread_yaml(root)
    runner_map, arch_map, ci_backend_names = _virtual_runner_map(raw)

    # _expand_backend raises ConfigurationError when no virtual backends are found.
    expanded = _expand_integration_suites(raw, root)
    expanded = _expand_backend(expanded, ci=True)
    expanded["reroot"] = _compose_reroot(expanded.get("reroot"))

    entries: list[dict[str, str]] = []
    with _expanded_spread_yaml_dir(root, expanded, prefix=".spread-jobs-") as tmp_dir:
        _materialize_task_files(root, tmp_dir)
        selectors = [f"{name}:" for name in ci_backend_names]
        result = run_command(
            ["spread", "-list", *selectors],
            cwd=str(tmp_dir),
            stream=False,
        )

        for raw_line in result.stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split(":")
            _MIN_PARTS = 3  # noqa: N806
            if len(parts) < _MIN_PARTS:
                continue
            system = parts[1]
            runner = runner_map.get(system, json.dumps(_DEFAULT_RUNNER))
            explicit_arch = arch_map.get(system)
            arch = explicit_arch if explicit_arch is not None else _arch_from_runner(runner)
            entries.append(
                {
                    "name": line,
                    "selector": line,
                    "runs-on": runner,
                    "arch": arch,
                }
            )

    return entries


def _materialize_task_files(root: Path, build_dir: Path) -> None:
    """Generate ``task.yaml`` files for integration-suites inside the build dir.

    Suite paths in the expanded YAML are prefixed with the build directory name
    (e.g. ``build/tests/integration/``).  Spread resolves these relative to the
    project root (via ``reroot: ..``), so writing to ``<root>/<suite-path>/run/``
    places files at ``<root>/build/<logical-suite>/run/task.yaml``.

    The build directory is NOT excluded from spread's file sync, so these files
    are available on the remote machine.  They persist for inspection and are
    overwritten on next run.
    """
    spread_yaml = build_dir / _SPREAD_YAML
    if not spread_yaml.exists():
        return

    data = load_yaml(spread_yaml)
    suites = data.get("suites")
    if not isinstance(suites, dict):
        return

    for suite_path, suite_cfg in suites.items():
        if not isinstance(suite_cfg, dict):
            continue
        env = suite_cfg.get("environment")
        if not isinstance(env, dict):
            continue
        # Only generate task.yaml for opcli-managed suites
        if "OPCLI_SUITE" not in env:
            continue

        task_dir = root / suite_path / "run"
        task_dir.mkdir(parents=True, exist_ok=True)
        task_file = task_dir / "task.yaml"
        task_file.write_text(_TASK_YAML_CONTENT_SUITE)


def _generate_spread_yaml(
    project_name: str,
) -> str:
    """Build the default ``spread.yaml`` content with ``integration-suites``."""
    # Root environment: project-wide vars (CONCIERGE, standard vars)
    root_env: dict[str, str] = {
        "CONCIERGE": '$(HOST: echo "${CONCIERGE:-concierge.yaml}")',
        # Defaults to "main"; override on the host with OPCLI_GIT_REF=<branch>
        # before running spread to install opcli from a specific branch.
        "OPCLI_GIT_REF": '$(HOST: echo "${OPCLI_GIT_REF:-main}")',
    }

    data: dict[str, object] = {
        "project": project_name,
        "path": "/home/ubuntu/proj",
        "kill-timeout": "60m",
        "warn-timeout": "1m",
        "backends": {
            _VIRTUAL_BACKEND: {
                "type": _VIRTUAL_BACKEND,
                "systems": ["ubuntu-24.04"],
            },
        },
        "environment": root_env,
        "exclude": [".git", ".tox", ".venv", ".*_cache"],
        _INTEGRATION_SUITES_KEY: {
            "tests/integration/": {
                "summary": "integration tests",
                "cwd": "./",
                "backends": [_VIRTUAL_BACKEND],
            },
        },
    }

    return dumps_yaml(data)


def _expand(root: Path, *, ci: bool | None = None) -> dict[str, Any]:
    """Load ``spread.yaml``, expand integration-suites then virtual backend."""
    data = _load_spread_yaml(root)

    if "reroot" in data:
        msg = (
            "'reroot' in spread.yaml is incompatible with opcli. "
            "opcli manages reroot internally during expansion."
        )
        raise ConfigurationError(msg)

    # Expand integration-suites first so the generated suites: entries
    # are present when _expand_backend renames backend references.
    data = _expand_integration_suites(data, root)
    data = _expand_backend(data, ci=ci)
    return data


def _load_spread_yaml(root: Path) -> dict[str, Any]:
    """Load and validate ``spread.yaml`` from *root*.

    Returns:
        Parsed YAML mapping.

    Raises:
        ConfigurationError: If the file is missing or not a YAML mapping.
    """
    spread_path = root / _SPREAD_YAML
    if not spread_path.exists():
        msg = f"{_SPREAD_YAML} not found. Run 'opcli spread init' first."
        raise ConfigurationError(msg)

    try:
        data = load_yaml(spread_path)
    except ValueError:
        msg = f"{_SPREAD_YAML} does not contain a YAML mapping"
        raise ConfigurationError(msg) from None

    return data


def _expand_backend(
    spread_data: dict[str, object],
    *,
    ci: bool | None = None,
) -> dict[str, object]:
    """Replace all virtual-typed backends with concrete ones.

    Scans all backends in ``spread.yaml`` for a ``type:`` field whose value
    is a recognised virtual type (``integration-test``).
    Each such backend is removed and replaced with a concrete counterpart
    named ``{backend_name}-local`` or ``{backend_name}-ci``.  The ``type:``
    field is consumed by opcli and replaced with ``type: adhoc``.  All other
    user-defined fields (``systems``, ``environment``, ``prepare-each``,
    ``kill-timeout``, etc.) are preserved.

    Multiple virtual backends may coexist in a single ``spread.yaml`` — each
    is expanded independently, so users can declare e.g.
    ``integration-test:`` and ``integration-test-arm:`` (both with
    ``type: integration-test``) with different system lists.

    Suite-level ``backends:`` lists are also updated so that any reference to
    a virtual backend name is replaced with the corresponding concrete name.

    Args:
        spread_data: Parsed spread.yaml (mutated in place on a deep copy).
        ci: Force CI mode if True, local if False, auto-detect if None.

    Returns:
        New dict with the backends replaced.

    Raises:
        ConfigurationError: If no backend with a recognised virtual type is
            found.
    """
    data = deepcopy(spread_data)
    backends = data.get("backends")
    if not isinstance(backends, dict):
        msg = "spread.yaml has no 'backends' section"
        raise ConfigurationError(msg)

    use_ci = ci if ci is not None else _is_ci()
    found_any = False

    for backend_name in list(backends.keys()):
        backend_entry = backends.get(backend_name)
        if not isinstance(backend_entry, dict):
            continue
        # If type: is absent, fall back to the backend name as the type —
        # mirroring spread's own convention where the backend name implies
        # the driver type when type: is not explicitly set.
        raw_type = backend_entry.get("type")
        backend_type = raw_type if isinstance(raw_type, str) else backend_name
        if backend_type not in _BACKEND_CONFIGS:
            continue
        found_any = True

        prepare_parts = _BACKEND_CONFIGS[backend_type]
        # Strip opcli-only fields; _build_concrete_backend sets type: adhoc
        virtual = {k: v for k, v in backend_entry.items() if k != "type"}
        del backends[backend_name]

        concrete_name = f"{backend_name}-ci" if use_ci else f"{backend_name}-local"
        if concrete_name in backends:
            msg = (
                f"Cannot expand virtual backend '{backend_name}': the concrete "
                f"name '{concrete_name}' already exists in spread.yaml. "
                "Rename or remove the conflicting backend."
            )
            raise ConfigurationError(msg)
        backends[concrete_name] = _build_concrete_backend(
            virtual,
            use_ci=use_ci,
            prepare_parts=prepare_parts,
        )
        _replace_suite_backend_name(data, backend_name, concrete_name)

    if not found_any:
        known = ", ".join(f"'{n}'" for n in _BACKEND_CONFIGS)
        msg = (
            f"spread.yaml contains no backend with a recognised virtual type "
            f"({known}). Nothing to expand."
        )
        raise ConfigurationError(msg)

    data["backends"] = backends
    return data


# ---------------------------------------------------------------------------
#  integration-suites expansion
# ---------------------------------------------------------------------------

# opcli-only keys that are consumed during expansion and not passed to spread.
_SUITE_OPCLI_KEYS = frozenset(
    {
        "auto-discover",
        "discover-pattern",
        "cwd",
        "pytest-arguments-template",
        "pytest-environment-template",
    }
)


def _expand_integration_suites(
    data: dict[str, object],
    root: Path,
) -> dict[str, Any]:
    """Expand ``integration-suites`` into native ``suites:`` entries.

    For each entry in ``integration-suites``:
    - Discovers test modules (or uses explicit variants)
    - Injects ``OPCLI_SUITE`` and ``OPCLI_CWD`` into environment
    - Generates MODULE variants
    - Merges into the ``suites:`` block

    The ``integration-suites`` key is stripped from the output.

    Returns the mutated data dict.
    """
    raw_integration_suites = data.pop(_INTEGRATION_SUITES_KEY, None)
    if raw_integration_suites is None:
        return data

    if not isinstance(raw_integration_suites, dict):
        msg = f"'{_INTEGRATION_SUITES_KEY}' must be a mapping"
        raise ConfigurationError(msg)

    suites: dict[str, object] = data.get("suites", {}) or {}  # type: ignore[assignment]
    if not isinstance(suites, dict):
        suites = {}

    for suite_path, suite_cfg_raw in raw_integration_suites.items():
        if not isinstance(suite_path, str):
            continue
        normalized_path, suite_cfg = _build_suite_entry(suite_path, suite_cfg_raw, root)
        # Prefix with build dir so spread finds task.yaml inside build/
        build_suite_path = f"{_BUILD_DIR}/{normalized_path}"
        suites[build_suite_path] = suite_cfg

    data["suites"] = suites
    return data


def _validate_safe_path(path: str, label: str) -> None:
    """Reject paths that escape the project root via traversal or absolutes."""
    if path.startswith("/"):
        msg = f"Absolute path not allowed in {label}: '{path}'"
        raise ConfigurationError(msg)
    # Normalize and check for traversal
    normalized = posixpath.normpath(path)
    if normalized.startswith("..") or "/../" in f"/{normalized}/":
        msg = f"Path traversal not allowed in {label}: '{path}'"
        raise ConfigurationError(msg)
    # Reject shell-dangerous characters in cwd (used in runuser -c)
    dangerous = set("'\"$`\\;|&(){}")
    found = dangerous.intersection(path)
    if found:
        msg = f"Unsafe characters {found} in {label}: '{path}'"
        raise ConfigurationError(msg)


def _build_suite_entry(
    suite_path: str,
    suite_cfg_raw: object,
    root: Path,
) -> tuple[str, dict[str, object]]:
    """Build a single expanded suite entry from an integration-suites config."""
    _validate_safe_path(suite_path, "integration-suites key")

    suite_cfg: dict[str, object] = dict(suite_cfg_raw) if isinstance(suite_cfg_raw, dict) else {}

    # Extract opcli-only keys
    auto_discover = suite_cfg.pop("auto-discover", True)
    discover_pattern = suite_cfg.pop("discover-pattern", "test_*.py")
    cwd = suite_cfg.pop("cwd", "./")
    suite_cfg.pop("pytest-arguments-template", None)
    suite_cfg.pop("pytest-environment-template", None)
    if not isinstance(cwd, str):
        cwd = "./"
    if not isinstance(discover_pattern, str):
        discover_pattern = "test_*.py"

    _validate_safe_path(cwd, "cwd")

    # Discover or use explicit variants
    env: dict[str, str] = {}
    existing_env = suite_cfg.get("environment")
    if isinstance(existing_env, dict):
        env.update(existing_env)

    if auto_discover:
        discover_dir = root / suite_path.rstrip("/")
        modules = _discover_modules_in(discover_dir, discover_pattern)
        # MODULE values must be relative to OPCLI_CWD so pytest can resolve them
        # from the directory it is invoked in (SPREAD_PATH/OPCLI_CWD).
        cwd_clean = cwd.rstrip("/") or "."
        suite_rel_to_cwd = posixpath.relpath(suite_path.rstrip("/"), cwd_clean)
        for mod_path in modules:
            key = _module_key(mod_path)
            full_key = f"MODULE/{key}"
            module_value = posixpath.join(suite_rel_to_cwd, mod_path)
            if full_key in env and env[full_key] != module_value:
                raise ConfigurationError(
                    f"MODULE key collision in suite '{suite_path}': "
                    f"'{mod_path}' and '{posixpath.relpath(env[full_key], suite_rel_to_cwd)}' "
                    f"both map to key '{full_key}'. "
                    "Rename one of the files or directories to avoid the conflict."
                )
            env[full_key] = module_value
        if not modules:
            logger.warning(
                "auto-discover found no test modules in '%s' (pattern: %s). "
                "Add test files or set auto-discover: false with explicit MODULE/ variants.",
                suite_path,
                discover_pattern,
            )
    else:
        # Validate that explicit suites have MODULE/ variants
        has_module = any(k.startswith("MODULE/") for k in env if isinstance(k, str))
        if not has_module:
            msg = (
                f"Suite '{suite_path}' has auto-discover: false but no MODULE/ "
                f"entries in environment. Add MODULE/<name>: <value> entries or "
                f"set auto-discover: true."
            )
            raise ConfigurationError(msg)

    # Inject opcli suite context variables
    env["OPCLI_SUITE"] = suite_path
    env["OPCLI_CWD"] = cwd
    suite_cfg["environment"] = env

    # Ensure trailing slash on suite path (spread convention)
    normalized_path = suite_path if suite_path.endswith("/") else suite_path + "/"
    return normalized_path, suite_cfg


def _discover_modules_in(directory: Path, pattern: str) -> list[str]:
    """Find test modules matching *pattern* recursively under *directory*.

    Returns relative paths with the ``.py`` extension (e.g. ``subdir/test_foo.py``),
    relative to *directory*.  Callers are responsible for prepending the suite
    path (relative to ``OPCLI_CWD``) before passing to pytest.
    """
    if not directory.is_dir():
        return []
    return sorted(str(p.relative_to(directory)) for p in directory.rglob(pattern) if p.is_file())


def _module_key(module_path: str) -> str:
    """Derive a spread variant key from a module relative path.

    Strips the ``.py`` suffix and replaces any character that is not
    ``[a-zA-Z0-9]`` with ``_`` to produce a valid spread environment variable
    name (the spread varname regex only allows ``[a-zA-Z0-9_]`` after the
    variant separator):
    ``test_foo.py``             → ``test_foo``
    ``subdir/test_foo.py``      → ``subdir_test_foo``
    ``k8s-charm/test-foo.py``   → ``k8s_charm_test_foo``
    """
    stem = module_path[:-3] if module_path.endswith(".py") else module_path
    return re.sub(r"[^a-zA-Z0-9]", "_", stem)


def get_suite_config(root: Path, suite: str | None = None) -> dict[str, str | None]:
    """Read suite configuration for ``opcli pytest``.

    Returns a dict with at least ``cwd`` (default ``"./"``), and optionally
    ``pytest-arguments-template`` and ``pytest-environment-template``.

    Resolution order:
    1. If *suite* given: look up in ``integration-suites``, then ``suites:``.
    2. If *suite* is None: auto-detect if single ``integration-suites`` entry.
    3. Fall back to default (cwd=``"./"``).

    Raises:
        ConfigurationError: If multiple suites exist and none is specified,
            or if the specified suite is not found.
    """
    spread_path = root / _SPREAD_YAML
    if not spread_path.exists():
        return {"cwd": "./"}

    try:
        data = load_yaml(spread_path)
    except (ValueError, YAMLError) as exc:
        msg = f"Failed to parse {_SPREAD_YAML}: {exc}"
        raise ConfigurationError(msg) from exc

    integration_suites = data.get(_INTEGRATION_SUITES_KEY)
    native_suites = data.get("suites")

    if suite is not None:
        # Explicit suite requested — look it up
        return _lookup_suite(suite, integration_suites, native_suites)

    # Auto-detect: only works with a single integration-suites entry
    if isinstance(integration_suites, dict):
        if len(integration_suites) == 1:
            key = next(iter(integration_suites))
            cfg = integration_suites[key]
            return _extract_suite_config(cfg)
        if len(integration_suites) > 1:
            available = ", ".join(integration_suites.keys())
            msg = f"Multiple integration-suites found. Use --suite to specify one of: {available}"
            raise ConfigurationError(msg)

    # No integration-suites or empty — default behavior
    return {"cwd": "./"}


def _extract_suite_config(cfg: object) -> dict[str, str | None]:
    """Extract cwd and template keys from a suite config mapping."""
    if not isinstance(cfg, dict):
        return {"cwd": "./"}
    cwd = cfg.get("cwd", "./")
    result: dict[str, str | None] = {"cwd": cwd if isinstance(cwd, str) else "./"}
    args_tmpl = cfg.get("pytest-arguments-template")
    if isinstance(args_tmpl, str):
        result["pytest-arguments-template"] = args_tmpl
    env_tmpl = cfg.get("pytest-environment-template")
    if isinstance(env_tmpl, str):
        result["pytest-environment-template"] = env_tmpl
    return result


def _lookup_suite(
    suite: str,
    integration_suites: object,
    native_suites: object,
) -> dict[str, str | None]:
    """Find *suite* in integration-suites or native suites."""
    # Normalize: try with and without trailing slash
    candidates = [suite, suite.rstrip("/") + "/", suite.rstrip("/")]

    # Check integration-suites first
    if isinstance(integration_suites, dict):
        for candidate in candidates:
            if candidate in integration_suites:
                return _extract_suite_config(integration_suites[candidate])

    # Fall back to native suites (best-effort: cwd defaults to ./)
    if isinstance(native_suites, dict):
        for candidate in candidates:
            if candidate in native_suites:
                return {"cwd": "./"}

    available: list[str] = []
    if isinstance(integration_suites, dict):
        available.extend(integration_suites.keys())
    if isinstance(native_suites, dict):
        available.extend(native_suites.keys())

    msg = f"Suite '{suite}' not found. Available: {', '.join(available) or '(none)'}"
    raise ConfigurationError(msg)


def _build_concrete_backend(
    virtual: object,
    *,
    use_ci: bool,
    prepare_parts: tuple[str, str, str, str],
) -> dict[str, object]:
    """Return a concrete adhoc backend dict built from a virtual backend entry.

    *prepare_parts* is ``(local_before, local_after, ci_before, ci_after)``.
    If the user's virtual backend contains a ``prepare`` key its content is
    spliced between the *before* and *after* sections of the appropriate mode.
    """
    backend_def: dict[str, object] = deepcopy(virtual) if isinstance(virtual, dict) else {}
    backend_def["type"] = "adhoc"

    # Extract user-defined prepare before we overwrite it.
    user_prepare = backend_def.pop("prepare", None)
    user_prepare_str = str(user_prepare).rstrip("\n") + "\n" if user_prepare else ""

    local_prepare_before, local_prepare_after, ci_prepare_before, ci_prepare_after = prepare_parts

    systems = backend_def.get("systems")

    if use_ci:
        backend_def["allocate"] = _CI_ALLOCATE
        prepare = ci_prepare_before + user_prepare_str + ci_prepare_after
        if prepare:
            backend_def["prepare"] = prepare
        # GitHub Actions vars are only needed for the CI backend so that
        # _CI_PREPARE can authenticate and download build artifacts via gh.
        # Scoping them here keeps the root spread.yaml clean for local runs.
        existing_env = backend_def.get("environment")
        existing_env = dict(existing_env) if isinstance(existing_env, dict) else {}
        backend_def["environment"] = {
            # SUDO_USER=ubuntu makes juju store controller data in the ubuntu
            # user's home directory so tests running as ubuntu can find the
            # credentials.
            "SUDO_USER": "ubuntu",
            "GITHUB_TOKEN": '$(HOST: echo "${GITHUB_TOKEN:-}")',
            "GITHUB_RUN_ID": '$(HOST: echo "${GITHUB_RUN_ID:-}")',
            "GITHUB_REPOSITORY": '$(HOST: echo "${GITHUB_REPOSITORY:-}")',
            "GITHUB_WORKSPACE": '$(HOST: echo "${GITHUB_WORKSPACE:-}")',
            "DOCKERHUB_MIRROR": '$(HOST: echo "${DOCKERHUB_MIRROR:-}")',
            **existing_env,
        }
        if isinstance(systems, list):
            backend_def["systems"] = _transform_systems(
                systems, strip_keys=_SYSTEM_STRIP_KEYS, inject_username="root"
            )
    else:
        # Extract per-system resource overrides before stripping the fields.
        resources: dict[str, dict[str, int]] = {}
        if isinstance(systems, list):
            resources = _extract_system_resources(systems)

        preamble = _make_resource_preamble(resources)
        backend_def["allocate"] = preamble + _LOCAL_ALLOCATE
        backend_def["discard"] = _LOCAL_DISCARD
        existing_env = backend_def.get("environment")
        existing_env = dict(existing_env) if isinstance(existing_env, dict) else {}
        backend_def["environment"] = {
            # SUDO_USER=ubuntu makes juju store controller data in the ubuntu
            # user's home directory so tests running as ubuntu can find the
            # credentials.
            "SUDO_USER": "ubuntu",
            **existing_env,
        }
        prepare = local_prepare_before + user_prepare_str + local_prepare_after
        if prepare:
            backend_def["prepare"] = prepare

        if isinstance(systems, list):
            backend_def["systems"] = _transform_systems(
                systems, strip_keys=_SYSTEM_STRIP_KEYS, inject_username="ubuntu"
            )

    return backend_def


# ---------------------------------------------------------------------------
#  spread expand
# ---------------------------------------------------------------------------

# -- Inline shell scripts for adhoc backends --------------------------------
#
# Spread prepends ``set -eu`` and defines helper functions (``ADDRESS``,
# ``FATAL``, ``ERROR``) in every script it runs.  Scripts must call
# ``ADDRESS <ip>`` to tell spread where to SSH.
#
# The local allocate script mirrors craft-application's .extension but is
# fully self-contained (no external script file).

_LOCAL_ALLOCATE = """\
DISTRO=$(echo "$SPREAD_SYSTEM" | cut -d- -f1)
SERIES=$(echo "$SPREAD_SYSTEM" | cut -d- -f2)
VM_NAME="spread-${DISTRO}-${SERIES}-$$-${RANDOM}"
VM_NAME=$(echo "$VM_NAME" | tr . -)

DISK="${DISK:-20}"
CPU="${CPU:-4}"
MEM="${MEM:-8}"

CLOUD_CONFIG=$(mktemp)
cat > "$CLOUD_CONFIG" <<'ENDCLOUD'
#cloud-config
ssh_pwauth: true
users:
  - name: ubuntu
    lock_passwd: false
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
ENDCLOUD

CLEANUP_VM=true
cleanup() {
  rm -f "$CLOUD_CONFIG" 2>/dev/null || true
  if [ "$CLEANUP_VM" = true ] && [ -n "${VM_NAME:-}" ]; then
    lxc delete --force "${VM_NAME}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

lxc launch --vm \\
  "${DISTRO}:${SERIES}" \\
  "${VM_NAME}" \\
  --config "user.user-data=$(cat "$CLOUD_CONFIG")" \\
  --config "limits.cpu=${CPU}" \\
  --config "limits.memory=${MEM}GiB" \\
  --device "root,size=${DISK}GiB" >&2

# Wait for LXD agent to be ready inside the VM
while ! lxc exec "${VM_NAME}" -- true &>/dev/null; do sleep 0.5; done

# Wait for cloud-init and snap seeding
lxc exec "${VM_NAME}" -- cloud-init status --wait >&2
lxc exec "${VM_NAME}" -- snap wait system seed.loaded >&2

# Set ubuntu user password (using lxc exec to avoid YAML escaping issues)
lxc exec "${VM_NAME}" -- bash -c "echo ubuntu:${SPREAD_PASSWORD} | chpasswd"

# Enable SSH password authentication
lxc exec "${VM_NAME}" -- bash -c \\
  'if [ -d /etc/ssh/sshd_config.d ]; then
     printf "PasswordAuthentication yes\\n" \\
       > /etc/ssh/sshd_config.d/00-spread.conf
   fi'
lxc exec "${VM_NAME}" -- sed -i \\
  's/^\\s*#\\?\\s*PasswordAuthentication\\>.*/PasswordAuthentication yes/' \\
  /etc/ssh/sshd_config
lxc exec "${VM_NAME}" -- killall -HUP sshd 2>/dev/null || true

# Get and report the VM's IPv4 address
while true; do
  RAW_ADDR=$(lxc ls --format csv --columns 4 "name=${VM_NAME}" | head -1)
  ADDR=$(echo "$RAW_ADDR" | awk '{print $1}')
  if [ -n "$ADDR" ]; then
    CLEANUP_VM=false
    ADDRESS "$ADDR"
    break
  fi
  sleep 0.5
done
"""

_LOCAL_DISCARD = """\
instance_name=$(lxc ls --format json \
  | jq -r --arg a "$SPREAD_SYSTEM_ADDRESS" \
    '.[] | select(any(
      .state.network[]?.addresses[]?; .address == $a
    )) | .name' | head -1)
if [ -n "$instance_name" ]; then
  lxc delete --force "$instance_name"
fi
"""

_LOCAL_PREPARE_BEFORE_USER = """\
loginctl enable-linger ubuntu
snap install astral-uv --classic
export UV_TOOL_BIN_DIR=/usr/local/bin
export UV_TOOL_DIR=/usr/local/share/uv-tools
if grep -q 'name = "opcli"' "${SPREAD_PATH}/pyproject.toml" 2>/dev/null; then
  uv tool install "${SPREAD_PATH}" --quiet
else
  uv tool install \
      "git+https://github.com/canonical/charm-ci@${OPCLI_GIT_REF:-main}" \
      --quiet
fi
opcli install spread
opcli install tox
opcli install concierge
opcli env provision -c "$CONCIERGE"
chown -R ubuntu:ubuntu /home/ubuntu
usermod -aG lxd ubuntu || true
opcli artifacts push-images --missing-registry deploy
"""

_LOCAL_PREPARE_AFTER_USER = """\
chown -R ubuntu:ubuntu "${SPREAD_PATH}"
"""

_CI_PREPARE_BEFORE_USER = """\
loginctl enable-linger ubuntu
chown -R ubuntu:ubuntu "${SPREAD_PATH}"
snap install astral-uv --classic
export UV_TOOL_BIN_DIR=/usr/local/bin
export UV_TOOL_DIR=/usr/local/share/uv-tools
if grep -q 'name = "opcli"' "${GITHUB_WORKSPACE}/pyproject.toml" 2>/dev/null; then
  uv tool install "${GITHUB_WORKSPACE}" --quiet
else
  uv tool install \
      "git+https://github.com/canonical/charm-ci@${OPCLI_GIT_REF:-main}" \
      --quiet
fi
opcli install spread
opcli install tox
opcli install concierge
usermod -aG lxd ubuntu || true
export HOME=/home/ubuntu
opcli env provision -c "$CONCIERGE" --image-registry "${DOCKERHUB_MIRROR:-}"
chown -R ubuntu:ubuntu /home/ubuntu
"""

_CI_PREPARE_AFTER_USER = """\
export HOME=/root
if [ -n "${GITHUB_RUN_ID:-}" ]; then
  export GH_TOKEN="${GITHUB_TOKEN}"
  cd "${SPREAD_PATH}" && opcli artifacts fetch \
    --run-id "${GITHUB_RUN_ID}" \
    --repo "${GITHUB_REPOSITORY}" \
    --wait
fi
opcli artifacts push-images --missing-registry deploy
chown -R ubuntu:ubuntu "${SPREAD_PATH}"
"""

_CI_ALLOCATE = """\
id ubuntu &>/dev/null || sudo useradd -m -s /bin/bash ubuntu
echo "ubuntu ALL=(ALL) NOPASSWD:ALL" | sudo install -m 0440 /dev/stdin /etc/sudoers.d/ubuntu
sudo sed -i 's/^[[:space:]]*#\\?[[:space:]]*\\(PermitRootLogin\\|PasswordAuthentication\\).*/\\1 yes/' \
    /etc/ssh/sshd_config
if [ -d /etc/ssh/sshd_config.d ]; then
  printf 'PermitRootLogin yes\\nPasswordAuthentication yes\\n' | \
      sudo tee /etc/ssh/sshd_config.d/00-spread.conf > /dev/null
fi
sudo systemctl restart ssh
echo "root:${SPREAD_PASSWORD}" | sudo chpasswd

ADDRESS localhost
"""

# Map each virtual backend type value to:
#   (local_prepare_before, local_prepare_after, ci_prepare_before, ci_prepare_after)
# Backends in spread.yaml declare their virtual type via the ``type:`` field
# (e.g. ``type: integration-test``).  Concrete names are derived as
# ``"{backend_name}-local"`` / ``"{backend_name}-ci"`` from the user-defined
# backend name.
_BACKEND_CONFIGS: dict[str, tuple[str, str, str, str]] = {
    _VIRTUAL_BACKEND: (
        _LOCAL_PREPARE_BEFORE_USER,
        _LOCAL_PREPARE_AFTER_USER,
        _CI_PREPARE_BEFORE_USER,
        _CI_PREPARE_AFTER_USER,
    ),
}

# Keys in system entries that are opcli-specific and must be stripped before
# passing to spread.  ``runner`` is a GitHub Actions runner-label field only
# meaningful to the CI backend; resource fields are used by the local allocate
# script and have no meaning in spread's own backend model.
_SYSTEM_STRIP_KEYS: frozenset[str] = frozenset({"cpu", "memory", "disk", "runner", "arch"})

# Names of resource fields and their corresponding shell variable names.
_RESOURCE_FIELDS: dict[str, str] = {"cpu": "CPU", "memory": "MEM", "disk": "DISK"}


def _extract_system_resources(
    systems: list[object],
) -> dict[str, dict[str, int]]:
    """Return ``{system_name: {cpu/memory/disk: value}}`` from system entries.

    Only positive integer values are accepted.

    Raises:
        ValidationError: If a resource value is not a positive integer.
    """
    result: dict[str, dict[str, int]] = {}
    for entry in systems:
        if not isinstance(entry, dict):
            continue
        for name, props in entry.items():
            if not isinstance(props, dict):
                continue
            res: dict[str, int] = {}
            for field in _RESOURCE_FIELDS:
                val = props.get(field)
                if val is None:
                    continue
                if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
                    msg = f"System '{name}': '{field}' must be a positive integer, got {val!r}"
                    raise ValidationError(msg)
                res[field] = val
            if res:
                result[name] = res
    return result


def _make_resource_preamble(resources: dict[str, dict[str, int]]) -> str:
    """Return a bash ``case`` snippet that sets CPU/MEM/DISK per ``$SPREAD_SYSTEM``.

    Each arm uses ``${VAR:-N}`` so that an explicit env-var override still wins.
    Returns an empty string when *resources* is empty.
    """
    if not resources:
        return ""
    lines = ['case "$SPREAD_SYSTEM" in\n']
    for sys_name, res in resources.items():
        parts = [
            f'{shell_var}="${{{shell_var}:-{res[field]}}}"'
            for field, shell_var in _RESOURCE_FIELDS.items()
            if field in res
        ]
        if parts:
            # Quote the pattern to prevent shell glob expansion (e.g. ubuntu-*)
            lines.append(f'  "{sys_name}") {"; ".join(parts)} ;;\n')
    lines.append("esac\n\n")
    return "".join(lines)


def _transform_systems(
    systems: list[object],
    *,
    strip_keys: frozenset[str],
    inject_username: str | None = None,
) -> list[object]:
    """Strip opcli-specific keys from system entries and optionally inject ``username``.

    For each system entry:
    - Plain strings: converted to a dict if username injection is needed.
    - Dict entries: ``strip_keys`` are removed from the props mapping; ``username``
      is set via ``setdefault`` if *inject_username* is given.
    - If all props are removed and no username is injected, collapses back to a
      plain string (avoids ``{"ubuntu-24.04": {}}`` noise in the output).
    """
    result: list[object] = []
    for entry in systems:
        if isinstance(entry, str):
            if inject_username:
                result.append({entry: {"username": inject_username}})
            else:
                result.append(entry)
        elif isinstance(entry, dict):
            merged: dict[str, object] = {
                name: _transform_system_props(
                    name, props, strip_keys=strip_keys, inject_username=inject_username
                )
                for name, props in entry.items()
            }
            # If a single-key mapping collapsed to {name: None}, emit plain string
            if len(merged) == 1 and next(iter(merged.values())) is None and not inject_username:
                result.append(next(iter(merged)))
            else:
                result.append(merged)
        else:
            result.append(entry)
    return result


def _transform_system_props(
    name: str,
    props: object,
    *,
    strip_keys: frozenset[str],
    inject_username: str | None,
) -> object:
    """Return the transformed props for a single system name→props pair."""
    if isinstance(props, dict):
        new_props = {k: v for k, v in props.items() if k not in strip_keys}
        if inject_username:
            new_props.setdefault("username", inject_username)
        return new_props if new_props else None
    if props is None:
        if inject_username:
            return {"username": inject_username}
        return None
    return props


def _replace_suite_backend_name(
    data: dict[str, object],
    virtual_name: str,
    concrete_name: str,
) -> None:
    """Replace *virtual_name* with *concrete_name* in all suite ``backends:`` lists."""
    suites = data.get("suites")
    if not isinstance(suites, dict):
        return
    for suite_cfg in suites.values():
        if isinstance(suite_cfg, dict):
            suite_backends = suite_cfg.get("backends")
            if isinstance(suite_backends, list):
                suite_cfg["backends"] = [
                    concrete_name if b == virtual_name else b for b in suite_backends
                ]


def _compose_reroot(existing_reroot: object | None) -> str:
    """Return a ``reroot`` value that accounts for the temp sub-directory.

    The expanded ``spread.yaml`` lives one directory below the project root,
    so we need ``..`` to point back.  If the user already specified a
    ``reroot`` in their original ``spread.yaml``, we compose ``../`` with
    that existing value (normalised).

    Raises:
        ConfigurationError: If *existing_reroot* is not a string or is absolute.
    """
    if existing_reroot is None:
        return ".."

    if not isinstance(existing_reroot, str):
        msg = f"'reroot' in spread.yaml must be a string, got {type(existing_reroot).__name__}"
        raise ConfigurationError(msg)

    if posixpath.isabs(existing_reroot):
        msg = f"'reroot' in spread.yaml must be a relative path, got '{existing_reroot}'"
        raise ConfigurationError(msg)

    return posixpath.normpath(posixpath.join("..", existing_reroot))


@contextmanager
def _expanded_spread_yaml_dir(
    root: Path,
    expanded: object,
    *,
    prefix: str,
) -> Iterator[Path]:
    """Yield a temp directory containing the expanded ``spread.yaml``."""
    with tempfile.TemporaryDirectory(prefix=prefix, dir=root) as tmp_dir:
        tmp_dir_path = Path(tmp_dir)
        dump_yaml(literalize(expanded), tmp_dir_path / _SPREAD_YAML)
        yield tmp_dir_path


@contextmanager
def _build_dir_context(
    root: Path,
    expanded: object,
) -> Iterator[Path]:
    """Yield the ``build/`` directory containing expanded ``spread.yaml`` and task files.

    Unlike ``_expanded_spread_yaml_dir`` (which uses random temp dirs), this
    writes to a deterministic ``build/`` path for debuggability.  The directory
    persists after the run so users can inspect generated files.
    """
    build_dir = root / _BUILD_DIR
    build_dir.mkdir(parents=True, exist_ok=True)
    dump_yaml(literalize(expanded), build_dir / _SPREAD_YAML)
    yield build_dir


def _virtual_runner_map(  # noqa: C901
    raw: dict[str, object],
) -> tuple[dict[str, str], dict[str, str | None], list[str]]:
    """Extract runner labels, explicit arches and CI backend names from virtual backends.

    Reads only the virtual backend sections (those whose ``type:`` is a
    recognised virtual type).  The ``runner:`` and ``arch:`` fields are
    opcli-owned and are stripped before spread ever sees the YAML.

    Returns:
        Tuple of:
        - system_runner_map: ``{system_name: runner_json}`` built from virtual
          backend system entries.  The runner value is JSON-encoded so that
          the workflow can use ``fromJSON(matrix.runs-on)``.
        - system_arch_map: ``{system_name: arch}`` for systems that declare an
          explicit ``arch:`` field.  Value is ``None`` when no explicit arch is
          set; callers should fall back to ``_arch_from_runner`` in that case.
        - ci_backend_names: concrete CI names derived from the virtual backend
          names (e.g. ``["integration-test-ci"]``).
    """
    runner_map: dict[str, str] = {}
    arch_map: dict[str, str | None] = {}
    ci_names: list[str] = []
    raw_backends = raw.get("backends") or {}
    if not isinstance(raw_backends, dict):
        return runner_map, arch_map, ci_names
    for backend_name, backend_cfg in raw_backends.items():
        if not isinstance(backend_cfg, dict):
            continue
        raw_type = backend_cfg.get("type")
        backend_type = raw_type if isinstance(raw_type, str) else backend_name
        if backend_type not in _BACKEND_CONFIGS:
            continue
        ci_names.append(f"{backend_name}-ci")
        for system_entry in backend_cfg.get("systems") or []:
            if isinstance(system_entry, str):
                runner_map.setdefault(system_entry, json.dumps(_DEFAULT_RUNNER))
                arch_map.setdefault(system_entry, None)
            elif isinstance(system_entry, dict):
                for sys_name, sys_props in system_entry.items():
                    raw_runner: object = _DEFAULT_RUNNER
                    explicit_arch: str | None = None
                    if isinstance(sys_props, dict):
                        raw_runner = sys_props.get("runner", _DEFAULT_RUNNER)
                        raw_arch = sys_props.get("arch")
                        if isinstance(raw_arch, str):
                            explicit_arch = raw_arch
                    runner_map.setdefault(str(sys_name), json.dumps(raw_runner))
                    arch_map.setdefault(str(sys_name), explicit_arch)
    return runner_map, arch_map, ci_names


# ---------------------------------------------------------------------------
#  spread jobs — enumerate CI test selectors for GitHub Actions matrix
# ---------------------------------------------------------------------------

_DEFAULT_RUNNER = "ubuntu-latest"


def _arch_from_runner(runner_json: str) -> str:
    """Derive a display architecture string from a JSON-encoded runner label.

    Checks for ``arm64`` in the runner labels; falls back to ``amd64``.
    """
    try:
        labels = json.loads(runner_json)
    except json.JSONDecodeError:
        return "amd64"
    if isinstance(labels, str):
        labels = [labels]
    if isinstance(labels, list) and any("arm64" in str(lbl).lower() for lbl in labels):
        return "arm64"
    return "amd64"
