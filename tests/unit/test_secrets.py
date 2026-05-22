# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for ``opcli.core.secrets``."""

from pathlib import Path

import pytest

from opcli.core.secrets import load_secrets_env


class TestLoadSecretsEnv:
    """Tests for .secrets.env loading."""

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_secrets_env(tmp_path) == {}

    def test_basic_key_value(self, tmp_path: Path) -> None:
        (tmp_path / ".secrets.env").write_text("FOO=bar\nBAZ=qux\n")
        assert load_secrets_env(tmp_path) == {"FOO": "bar", "BAZ": "qux"}

    def test_strips_quotes(self, tmp_path: Path) -> None:
        (tmp_path / ".secrets.env").write_text("A='single'\nB=\"double\"\n")
        assert load_secrets_env(tmp_path) == {"A": "single", "B": "double"}

    def test_ignores_comments_and_blanks(self, tmp_path: Path) -> None:
        (tmp_path / ".secrets.env").write_text("# comment\n\nKEY=val\n")
        assert load_secrets_env(tmp_path) == {"KEY": "val"}

    def test_malformed_line_warns(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        (tmp_path / ".secrets.env").write_text("NOEQUALS\nGOOD=val\n")
        with caplog.at_level("WARNING"):
            result = load_secrets_env(tmp_path)
        assert result == {"GOOD": "val"}
        assert "malformed" in caplog.text
