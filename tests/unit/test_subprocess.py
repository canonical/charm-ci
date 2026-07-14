# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for the central subprocess wrapper."""

from pathlib import Path

import pytest

from opcli.core import subprocess as subprocess_module
from opcli.core.exceptions import SubprocessError
from opcli.core.subprocess import _is_retryable, run_command


def _fail_n_times_then_succeed_cmd(counter_file: Path, failures: int, message: str) -> list[str]:
    """Build a shell command that fails *failures* times (printing *message*
    to stderr) then succeeds, tracking attempts via *counter_file*.
    """
    return [
        "sh",
        "-c",
        f'count=$(cat "{counter_file}" 2>/dev/null || echo 0); '
        f'count=$((count + 1)); echo "$count" > "{counter_file}"; '
        f'if [ "$count" -le {failures} ]; then echo "{message}" >&2; exit 1; '
        f"fi; echo ok",
    ]


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


class TestRetry:
    """retries / retry_on behavior in run_command."""

    def test_succeeds_after_transient_failures_with_correct_backoff(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleep_calls: list[float] = []
        monkeypatch.setattr(subprocess_module, "_sleep", sleep_calls.append)

        counter_file = tmp_path / "attempts"
        cmd = _fail_n_times_then_succeed_cmd(
            counter_file, failures=2, message="RemoteDisconnected"
        )

        result = run_command(
            cmd,
            stream=False,
            retries=2,
            retry_on=["RemoteDisconnected"],
        )

        assert result.returncode == 0
        assert "ok" in result.stdout
        assert counter_file.read_text().strip() == "3"
        assert sleep_calls == [5.0, 15.0]

    def test_non_retryable_failure_fails_immediately(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleep_calls: list[float] = []
        monkeypatch.setattr(subprocess_module, "_sleep", sleep_calls.append)

        counter_file = tmp_path / "attempts"
        cmd = _fail_n_times_then_succeed_cmd(
            counter_file, failures=99, message="permission-required: no access"
        )

        with pytest.raises(SubprocessError, match="permission-required"):
            run_command(cmd, stream=False, retries=2, retry_on=["RemoteDisconnected"])

        assert counter_file.read_text().strip() == "1"
        assert sleep_calls == []

    def test_retries_exhausted_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleep_calls: list[float] = []
        monkeypatch.setattr(subprocess_module, "_sleep", sleep_calls.append)

        counter_file = tmp_path / "attempts"
        cmd = _fail_n_times_then_succeed_cmd(counter_file, failures=99, message="Connection reset")

        with pytest.raises(SubprocessError, match="Connection reset"):
            run_command(cmd, stream=False, retries=2, retry_on=["Connection reset"])

        # 3 total attempts (1 initial + 2 retries), 2 sleeps in between.
        assert counter_file.read_text().strip() == "3"
        assert sleep_calls == [5.0, 15.0]

    def test_default_retries_zero_is_single_attempt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleep_calls: list[float] = []
        monkeypatch.setattr(subprocess_module, "_sleep", sleep_calls.append)

        counter_file = tmp_path / "attempts"
        cmd = _fail_n_times_then_succeed_cmd(
            counter_file, failures=99, message="RemoteDisconnected"
        )

        with pytest.raises(SubprocessError):
            run_command(cmd, stream=False, retry_on=["RemoteDisconnected"])

        assert counter_file.read_text().strip() == "1"
        assert sleep_calls == []


class TestIsRetryableRealisticOutput:
    """_is_retryable against realistic charmcraft-shaped stdout/stderr splits.

    charmcraft writes progress/error text to stderr and, on success, a JSON
    payload to stdout — the two streams are captured separately by
    ``run_command`` and concatenated (``stdout + stderr``) before matching.
    These tests exercise ``_is_retryable`` directly against that shape,
    rather than only through generic ``sh -c`` fixtures, so a regression in
    how the concatenation or matching is done (e.g. checking only one stream,
    or matching against ``cmd`` instead of output) would be caught.
    """

    _RETRY_ON = (
        "Timeout polling Charmhub for upload status",
        "RemoteDisconnected",
        "Connection aborted",
        "Connection reset",
    )

    def test_matches_when_pattern_is_in_stderr_only(self) -> None:
        stdout = ""
        stderr = (
            "Uploading bytes ended, id db6c7cda-760e-43bf-9312-0b09f0778454\n"
            "Timeout polling Charmhub for upload status (after 60.0s).\n"
        )
        assert _is_retryable(stdout + stderr, self._RETRY_ON) is True

    def test_matches_when_pattern_is_in_stdout_only(self) -> None:
        stdout = "('Connection aborted.', RemoteDisconnected('Remote end closed connection'))\n"
        stderr = ""
        assert _is_retryable(stdout + stderr, self._RETRY_ON) is True

    def test_does_not_match_permission_required_charmcraft_output(self) -> None:
        stdout = '{"errors": [{"code": "permission-required", "message": "no access"}]}\n'
        stderr = (
            "Store operation failed:\n"
            "- permission-required: No publisher or collaborator permission "
            "for the haproxy charm package\n"
        )
        assert _is_retryable(stdout + stderr, self._RETRY_ON) is False

    def test_no_retry_on_patterns_never_matches(self) -> None:
        assert _is_retryable("RemoteDisconnected", None) is False
        assert _is_retryable("RemoteDisconnected", []) is False
