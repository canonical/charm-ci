# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Smoke tests for the opcli scaffold."""

import io
import subprocess as sp
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from opcli.app import typer_app as app
from opcli.core.exceptions import (
    ConfigurationError,
    DiscoveryError,
    OpcliError,
    SubprocessError,
    ValidationError,
)
from opcli.core.subprocess import SubprocessResult, run_command

runner = CliRunner()


class TestCLIEntryPoint:
    """Verify the Typer app is wired correctly."""

    def test_help_exits_zero(self) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "opcli" in result.output.lower()

    def test_artifacts_help(self) -> None:
        result = runner.invoke(app, ["artifacts", "--help"])
        assert result.exit_code == 0
        assert "artifacts" in result.output.lower()

    def test_env_help(self) -> None:
        result = runner.invoke(app, ["env", "--help"])
        assert result.exit_code == 0

    def test_spread_help(self) -> None:
        result = runner.invoke(app, ["spread", "--help"])
        assert result.exit_code == 0

    def test_pytest_help(self) -> None:
        result = runner.invoke(app, ["pytest", "--help"])
        assert result.exit_code == 0


class TestExceptionHierarchy:
    """Verify exception types are catchable as OpcliError."""

    def test_subprocess_error_formats_with_shlex(self) -> None:
        err = SubprocessError(cmd=["echo", "hello world"], returncode=1, stderr="fail")
        assert "echo 'hello world'" in str(err)

    def test_subprocess_error_is_opcli_error(self) -> None:
        err = SubprocessError(cmd=["false"], returncode=1, stderr="fail")
        assert isinstance(err, OpcliError)
        assert err.returncode == 1
        assert err.stderr == "fail"
        assert "false" in str(err)

    @pytest.mark.parametrize(
        "exc_cls",
        [
            SubprocessError,
            ValidationError,
            DiscoveryError,
            ConfigurationError,
        ],
    )
    def test_all_exceptions_inherit_from_base(self, exc_cls: type) -> None:
        assert issubclass(exc_cls, OpcliError)


class TestGlobalErrorHandler:
    """Verify OpcliError produces friendly output without tracebacks."""

    def test_opcli_error_shows_message_on_stderr(self) -> None:
        with patch(
            "opcli.commands.artifacts.artifacts_build",
            side_effect=ConfigurationError("something went wrong"),
        ):
            result = runner.invoke(app, ["artifacts", "build"])
            assert result.exit_code == 1
            assert "error: something went wrong" in result.output

    def test_opcli_error_no_traceback(self) -> None:
        with patch(
            "opcli.commands.spread.spread_expand",
            side_effect=ConfigurationError("spread.yaml not found"),
        ):
            result = runner.invoke(app, ["spread", "expand"])
            assert result.exit_code == 1
            assert "Traceback" not in result.output
            assert "error: spread.yaml not found" in result.output

    def test_subprocess_error_handled(self) -> None:
        with patch(
            "opcli.commands.spread.spread_run",
            side_effect=SubprocessError(cmd=["spread"], returncode=1, stderr="task failed"),
        ):
            result = runner.invoke(app, ["spread", "run"])
            assert result.exit_code == 1
            assert "error:" in result.output
            assert "Traceback" not in result.output


class TestSubprocessWrapper:
    """Verify the subprocess wrapper mocking pattern works."""

    # --- Captured mode (stream=False) ---

    def test_captured_success(self) -> None:
        with patch("opcli.core.subprocess.subprocess.run") as mock_run:
            mock_run.return_value.stdout = "ok\n"
            mock_run.return_value.stderr = ""
            mock_run.return_value.returncode = 0

            result = run_command(["echo", "hello"], stream=False)

            assert isinstance(result, SubprocessResult)
            assert result.stdout == "ok\n"
            assert result.returncode == 0
            mock_run.assert_called_once()

    def test_captured_failure_raises(self) -> None:
        with patch("opcli.core.subprocess.subprocess.run") as mock_run:
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = "not found"
            mock_run.return_value.returncode = 127

            with pytest.raises(SubprocessError, match="not found"):
                run_command(["bad-cmd"], stream=False)

    def test_captured_no_check(self) -> None:
        with patch("opcli.core.subprocess.subprocess.run") as mock_run:
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = "warn"
            mock_run.return_value.returncode = 1

            result = run_command(["cmd"], check=False, stream=False)
            assert result.returncode == 1

    def test_captured_file_not_found(self) -> None:
        with patch("opcli.core.subprocess.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("No such file")

            with pytest.raises(SubprocessError) as exc_info:
                run_command(["nonexistent-binary"], stream=False)

            assert exc_info.value.returncode == 127  # noqa: PLR2004
            assert "'nonexistent-binary' not found" in exc_info.value.stderr

    def test_captured_timeout(self) -> None:
        with patch("opcli.core.subprocess.subprocess.run") as mock_run:
            mock_run.side_effect = sp.TimeoutExpired(cmd=["slow"], timeout=5)

            with pytest.raises(SubprocessError, match="timed out"):
                run_command(["slow"], timeout=5, stream=False)

    # --- Streaming mode (stream=True, the default) ---

    def test_streaming_success(self) -> None:
        with patch("opcli.core.subprocess.subprocess.Popen") as mock_popen:
            proc = mock_popen.return_value
            proc.stdout = io.StringIO("line1\nline2\n")
            proc.stderr = io.StringIO("")
            proc.returncode = 0
            proc.wait.return_value = 0

            result = run_command(["build", "thing"])

            assert result.stdout == "line1\nline2\n"
            assert result.returncode == 0

    def test_streaming_failure_raises(self) -> None:
        with patch("opcli.core.subprocess.subprocess.Popen") as mock_popen:
            proc = mock_popen.return_value
            proc.stdout = io.StringIO("")
            proc.stderr = io.StringIO("error output\n")
            proc.returncode = 1
            proc.wait.return_value = 1

            with pytest.raises(SubprocessError, match="error output"):
                run_command(["bad-build"])

    def test_streaming_file_not_found(self) -> None:
        with patch("opcli.core.subprocess.subprocess.Popen") as mock_popen:
            mock_popen.side_effect = FileNotFoundError("No such file")

            with pytest.raises(SubprocessError) as exc_info:
                run_command(["missing-tool"])

            assert exc_info.value.returncode == 127  # noqa: PLR2004

    def test_logs_command_and_cwd(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("opcli.core.subprocess.subprocess.Popen") as mock_popen:
            proc = mock_popen.return_value
            proc.stdout = io.StringIO("")
            proc.stderr = io.StringIO("")
            proc.returncode = 0
            proc.wait.return_value = 0

            run_command(["charmcraft", "pack"], cwd="/some/dir")

        captured = capsys.readouterr()
        assert "$ charmcraft pack" in captured.err
        assert "cwd: /some/dir" in captured.err
        assert "$ charmcraft pack" not in captured.out

    def test_logs_command_without_cwd(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("opcli.core.subprocess.subprocess.run") as mock_run:
            mock_run.return_value.stdout = "ok"
            mock_run.return_value.stderr = ""
            mock_run.return_value.returncode = 0

            run_command(["echo", "hello"], stream=False)

        captured = capsys.readouterr()
        # Captured mode logs to stderr so stdout stays clean for programmatic use
        assert "$ echo hello" in captured.err
        assert "$ echo hello" not in captured.out
        assert "cwd:" not in captured.err

    # --- Interactive mode ---

    def test_interactive_success(self) -> None:
        with patch("opcli.core.subprocess.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0

            result = run_command(["spread", "-shell"], interactive=True)

            assert result.returncode == 0
            assert result.stdout == ""
            assert result.stderr == ""
            mock_run.assert_called_once_with(["spread", "-shell"], cwd=None, check=False, env=None)

    def test_interactive_failure_raises(self) -> None:
        with patch("opcli.core.subprocess.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1

            with pytest.raises(SubprocessError) as exc_info:
                run_command(["spread", "-shell"], interactive=True)

            assert exc_info.value.returncode == 1
            assert "interactive mode" in exc_info.value.stderr

    def test_interactive_no_check(self) -> None:
        with patch("opcli.core.subprocess.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1

            result = run_command(["spread"], check=False, interactive=True)
            assert result.returncode == 1

    def test_interactive_file_not_found(self) -> None:
        with patch("opcli.core.subprocess.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("No such file")

            with pytest.raises(SubprocessError) as exc_info:
                run_command(["spread"], interactive=True)

            assert exc_info.value.returncode == 127  # noqa: PLR2004
            assert "'spread' not found" in exc_info.value.stderr
            assert "opcli install spread" in exc_info.value.stderr

    def test_interactive_other_oserror(self) -> None:
        with patch("opcli.core.subprocess.subprocess.run") as mock_run:
            mock_run.side_effect = OSError("permission denied")

            with pytest.raises(SubprocessError) as exc_info:
                run_command(["spread"], interactive=True)

            assert exc_info.value.returncode == -1
            assert "permission denied" in exc_info.value.stderr
