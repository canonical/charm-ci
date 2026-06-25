# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for ``opcli install`` commands and core install helpers."""

from unittest.mock import call

from typer.testing import CliRunner

from opcli.commands.install import app as install_app
from opcli.core.install import install_concierge, install_gh, install_local, install_spread

_RUNNER = CliRunner()


# ---------------------------------------------------------------------------
# install_gh
# ---------------------------------------------------------------------------


def test_install_gh_skips_when_already_present(mocker):
    mocker.patch("shutil.which", return_value="/snap/bin/gh")
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_gh()
    mock_run.assert_not_called()


def test_install_gh_installs_via_snap_as_root(mocker):
    mocker.patch("shutil.which", return_value=None)
    mocker.patch("os.getuid", return_value=0)
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_gh()
    mock_run.assert_called_once_with(["snap", "install", "gh", "--classic"])


def test_install_gh_uses_sudo_as_non_root(mocker):
    mocker.patch("shutil.which", return_value=None)
    mocker.patch("os.getuid", return_value=1000)
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_gh()
    mock_run.assert_called_once_with(["sudo", "snap", "install", "gh", "--classic"])


# ---------------------------------------------------------------------------
# install_spread (non-root path)
# ---------------------------------------------------------------------------


def test_install_spread_skips_when_already_present(mocker):
    mocker.patch("shutil.which", return_value="/usr/local/bin/spread")
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_spread()
    mock_run.assert_not_called()


def test_install_spread_as_root(mocker):
    mocker.patch("shutil.which", return_value=None)
    mocker.patch("os.getuid", return_value=0)
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_spread()
    calls = mock_run.call_args_list
    assert calls[0] == call(["snap", "install", "go", "--classic"])
    assert calls[1] == call(["go", "install", "github.com/canonical/spread/cmd/spread@latest"])
    assert calls[2] == call(["ln", "-sf", "/root/go/bin/spread", "/usr/local/bin/spread"])


def test_install_spread_as_non_root_uses_sudo_and_local_bin(mocker, tmp_path):
    mocker.patch("shutil.which", return_value=None)
    mocker.patch("os.getuid", return_value=1000)
    mocker.patch("pathlib.Path.home", return_value=tmp_path)
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_spread()
    calls = mock_run.call_args_list
    assert calls[0] == call(["sudo", "snap", "install", "go", "--classic"])
    assert calls[1] == call(["go", "install", "github.com/canonical/spread/cmd/spread@latest"])
    go_bin = str(tmp_path / "go" / "bin" / "spread")
    local_bin = str(tmp_path / ".local" / "bin" / "spread")
    assert calls[2] == call(["ln", "-sf", go_bin, local_bin])


# ---------------------------------------------------------------------------
# install_concierge (non-root path)
# ---------------------------------------------------------------------------


def test_install_concierge_skips_when_already_present(mocker):
    mocker.patch("shutil.which", return_value="/snap/bin/concierge")
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_concierge()
    mock_run.assert_not_called()


def test_install_concierge_as_root(mocker):
    mocker.patch("shutil.which", return_value=None)
    mocker.patch("os.getuid", return_value=0)
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_concierge()
    mock_run.assert_called_once_with(["snap", "install", "concierge", "--classic"])


def test_install_concierge_uses_sudo_as_non_root(mocker):
    mocker.patch("shutil.which", return_value=None)
    mocker.patch("os.getuid", return_value=1000)
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_concierge()
    mock_run.assert_called_once_with(["sudo", "snap", "install", "concierge", "--classic"])


# ---------------------------------------------------------------------------
# install_local
# ---------------------------------------------------------------------------


def test_install_local_calls_all_installers(mocker):
    mock_gh = mocker.patch("opcli.core.install.install_gh")
    mock_spread = mocker.patch("opcli.core.install.install_spread")
    mock_concierge = mocker.patch("opcli.core.install.install_concierge")
    mock_tox = mocker.patch("opcli.core.install.install_tox")
    install_local()
    mock_gh.assert_called_once()
    mock_spread.assert_called_once()
    mock_concierge.assert_called_once()
    mock_tox.assert_called_once()


# ---------------------------------------------------------------------------
# CLI: opcli install local
# ---------------------------------------------------------------------------


def test_cli_install_local(mocker):
    mocker.patch("opcli.core.install.install_gh")
    mocker.patch("opcli.core.install.install_spread")
    mocker.patch("opcli.core.install.install_concierge")
    mocker.patch("opcli.core.install.install_tox")
    result = _RUNNER.invoke(install_app, ["local"])
    assert result.exit_code == 0
    assert "All local tools are available" in result.output


def test_cli_install_gh(mocker):
    mocker.patch("shutil.which", return_value=None)
    mocker.patch("os.getuid", return_value=0)
    mocker.patch("opcli.core.install.run_command")
    result = _RUNNER.invoke(install_app, ["gh"])
    assert result.exit_code == 0
    assert "gh is available" in result.output
