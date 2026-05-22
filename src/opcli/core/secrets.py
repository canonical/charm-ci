# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared secrets-env loading for local test execution."""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_SECRETS_ENV_FILE = ".secrets.env"


def load_secrets_env(root: Path) -> dict[str, str]:
    """Load secrets from ``.secrets.env`` in the project root.

    The file uses plain ``KEY=VALUE`` format (one per line).  Blank lines and
    lines starting with ``#`` are ignored.  Values may be optionally quoted
    with single or double quotes.

    Returns an empty dict if the file does not exist.
    """
    secrets_path = root / _SECRETS_ENV_FILE
    if not secrets_path.is_file():
        return {}

    env: dict[str, str] = {}
    for lineno, raw_line in enumerate(secrets_path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            logger.warning(
                "%s:%d: skipping malformed line (no '=' found)",
                _SECRETS_ENV_FILE,
                lineno,
            )
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip optional surrounding quotes
        if (
            len(value) >= 2  # noqa: PLR2004
            and value[0] == value[-1]
            and value[0] in ("'", '"')
        ):
            value = value[1:-1]
        env[key] = value

    if env:
        logger.info("Loaded %d secret(s) from %s", len(env), _SECRETS_ENV_FILE)
    return env
