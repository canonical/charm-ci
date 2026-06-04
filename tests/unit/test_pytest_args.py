# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Tests for ``opcli pytest expand`` and ``opcli pytest run``."""

from pathlib import Path

from pytest_mock import MockerFixture

from opcli.core.pytest_args import assemble_tox_argv, pytest_run


class TestAssembleToxArgv:
    """Tests for assemble_tox_argv()."""

    def test_no_flags_no_extra_omits_separator(self, tmp_path: Path) -> None:
        argv = assemble_tox_argv(tmp_path)

        assert argv == ["tox", "-e", "integration"]
        assert "--" not in argv

    def test_artifacts_yaml_not_required_without_template(self, tmp_path: Path) -> None:
        """Without a template, assemble_tox_argv does not read artifacts.build.yaml."""
        # No artifacts.build.yaml present — should not raise
        argv = assemble_tox_argv(tmp_path)

        assert argv == ["tox", "-e", "integration"]

    def test_extra_args_only_include_separator(self, tmp_path: Path) -> None:
        argv = assemble_tox_argv(tmp_path, extra_args=["-k", "test_foo"])

        assert "--" in argv
        assert "-k" in argv
        assert "test_foo" in argv

    def test_custom_tox_env(self, tmp_path: Path) -> None:
        argv = assemble_tox_argv(tmp_path, tox_env="e2e")

        assert argv[2] == "e2e"

    def test_extra_args_forwarded(self, tmp_path: Path) -> None:
        argv = assemble_tox_argv(tmp_path, extra_args=["-v", "-k", "test_charm"])

        sep_idx = argv.index("--")
        tail = argv[sep_idx + 1 :]
        assert "-v" in tail
        assert "-k" in tail
        assert "test_charm" in tail


class TestPytestRun:
    """Tests for ``pytest_run`` — executes tox interactively."""

    def test_runs_tox_interactively(self, tmp_path: Path, mocker: MockerFixture) -> None:
        mock_run = mocker.patch("opcli.core.pytest_args.run_command")
        mocker.patch("opcli.core.pytest_args.is_ci", return_value=False)
        mocker.patch("opcli.core.pytest_args.load_secrets_env", return_value={})

        pytest_run(tmp_path)

        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args
        assert call_kwargs.kwargs["interactive"] is True
        assert call_kwargs.kwargs["cwd"] == str(tmp_path)
        cmd = call_kwargs.args[0]
        assert cmd[:3] == ["tox", "-e", "integration"]

    def test_forwards_extra_args(self, tmp_path: Path, mocker: MockerFixture) -> None:
        mock_run = mocker.patch("opcli.core.pytest_args.run_command")
        mocker.patch("opcli.core.pytest_args.is_ci", return_value=False)
        mocker.patch("opcli.core.pytest_args.load_secrets_env", return_value={})

        pytest_run(tmp_path, extra_args=["-k", "test_charm"])

        cmd = mock_run.call_args.args[0]
        assert "-k" in cmd
        assert "test_charm" in cmd

    def test_custom_tox_env(self, tmp_path: Path, mocker: MockerFixture) -> None:
        mock_run = mocker.patch("opcli.core.pytest_args.run_command")
        mocker.patch("opcli.core.pytest_args.is_ci", return_value=False)
        mocker.patch("opcli.core.pytest_args.load_secrets_env", return_value={})

        pytest_run(tmp_path, tox_env="e2e")

        cmd = mock_run.call_args.args[0]
        assert cmd[2] == "e2e"

    def test_loads_secrets_env_locally(self, tmp_path: Path, mocker: MockerFixture) -> None:
        mock_run = mocker.patch("opcli.core.pytest_args.run_command")
        mocker.patch("opcli.core.pytest_args.is_ci", return_value=False)
        mocker.patch(
            "opcli.core.pytest_args.load_secrets_env",
            return_value={"SECRET_KEY": "val"},
        )

        pytest_run(tmp_path)

        assert mock_run.call_args.kwargs["env"] == {"SECRET_KEY": "val"}

    def test_skips_secrets_in_ci(self, tmp_path: Path, mocker: MockerFixture) -> None:
        mock_run = mocker.patch("opcli.core.pytest_args.run_command")
        mocker.patch("opcli.core.pytest_args.is_ci", return_value=True)
        mock_load = mocker.patch("opcli.core.pytest_args.load_secrets_env")

        pytest_run(tmp_path)

        mock_load.assert_not_called()
        assert mock_run.call_args.kwargs["env"] is None
