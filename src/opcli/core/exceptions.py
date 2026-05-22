# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""opcli exception hierarchy.

All user-facing errors inherit from OpcliError so that CLI commands can
catch a single base type and produce friendly output.
"""

import shlex


class OpcliError(Exception):
    """Base exception for all opcli errors.

    Attributes:
        hint: Optional actionable suggestion displayed after the error message.
    """

    hint: str | None = None

    def __init__(self, *args: object, hint: str | None = None) -> None:
        super().__init__(*args)
        if hint is not None:
            self.hint = hint


class SubprocessError(OpcliError):
    """An external command exited with a non-zero status."""

    def __init__(self, cmd: list[str], returncode: int, stderr: str) -> None:
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"Command {shlex.join(cmd)} failed with exit code {returncode}:\n{stderr}"
        )


class ValidationError(OpcliError):
    """A YAML file failed schema validation."""


class DiscoveryError(OpcliError):
    """Artifact discovery found nothing or encountered conflicting markers."""


class ConfigurationError(OpcliError):
    """A required configuration file is missing or invalid."""
