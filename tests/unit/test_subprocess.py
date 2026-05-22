# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for the central subprocess wrapper."""

import pytest

from opcli.core.exceptions import SubprocessError
from opcli.core.subprocess import run_command


class TestStdinCaptured:
    """stdin forwarding in captured (non-streaming) mode."""

    def test_stdin_piped_to_process(self) -> None:
        result = run_command(
            ["cat"],
            stream=False,
            stdin="hello from stdin\n",
        )
        assert result.stdout == "hello from stdin\n"
        assert result.returncode == 0

    def test_stdin_none_is_harmless(self) -> None:
        result = run_command(["echo", "ok"], stream=False, stdin=None)
        assert "ok" in result.stdout
        assert result.returncode == 0


class TestStdinStreaming:
    """stdin forwarding in streaming (real-time) mode."""

    def test_stdin_piped_to_process(self) -> None:
        result = run_command(
            ["cat"],
            stream=True,
            stdin="streamed input\n",
        )
        assert result.stdout == "streamed input\n"
        assert result.returncode == 0

    def test_stdin_none_is_harmless(self) -> None:
        result = run_command(["echo", "ok"], stream=True, stdin=None)
        assert "ok" in result.stdout
        assert result.returncode == 0

    def test_broken_pipe_does_not_raise_from_writer_thread(self) -> None:
        """Commands that close stdin early must not crash the wrapper."""
        # `true` exits immediately without reading stdin — triggers BrokenPipeError.
        result = run_command(["true"], stream=True, stdin="ignored input")
        assert result.returncode == 0

    def test_failed_command_raises_subprocess_error(self) -> None:
        with pytest.raises(SubprocessError):
            run_command(["false"], stream=True, stdin="some input")


class TestEnvCaptured:
    """env overlay in captured (non-streaming) mode."""

    def test_extra_env_var_visible_to_process(self) -> None:
        result = run_command(
            ["sh", "-c", "echo $MY_TEST_VAR"],
            stream=False,
            env={"MY_TEST_VAR": "hello"},
        )
        assert result.stdout.strip() == "hello"

    def test_env_none_inherits_parent_env(self) -> None:
        """When env is None the subprocess still sees PATH (so sh works)."""
        result = run_command(["sh", "-c", "echo ok"], stream=False, env=None)
        assert "ok" in result.stdout

    def test_env_overrides_existing_var(self) -> None:
        result = run_command(
            ["sh", "-c", "echo $HOME"],
            stream=False,
            env={"HOME": "/overridden"},
        )
        assert result.stdout.strip() == "/overridden"


class TestEnvStreaming:
    """env overlay in streaming (real-time) mode."""

    def test_extra_env_var_visible_to_process(self) -> None:
        result = run_command(
            ["sh", "-c", "echo $MY_TEST_VAR"],
            stream=True,
            env={"MY_TEST_VAR": "streamed"},
        )
        assert result.stdout.strip() == "streamed"


class TestInteractiveMutualExclusion:
    """interactive and stdin are mutually exclusive."""

    def test_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            run_command(["echo", "hi"], interactive=True, stdin="hello")
