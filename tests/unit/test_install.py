# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for ``opcli install`` commands and core install helpers."""

import os
from unittest.mock import call

import pytest
from typer.testing import CliRunner

from opcli.commands.install import app as install_app
from opcli.core.exceptions import ConfigurationError
from opcli.core.install import (
    _warn_if_local_bin_not_on_path,
    install_bootstrap,
    install_check,
    install_concierge,
    install_gh,
    install_lxd,
    install_spread,
    install_tox,
)

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
# install_spread
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
# install_tox
# ---------------------------------------------------------------------------


def test_install_tox_skips_when_already_present(mocker):
    mocker.patch("shutil.which", side_effect=lambda t: "/usr/bin/" + t)
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_tox()
    mock_run.assert_not_called()


def test_install_tox_raises_if_uv_missing(mocker):
    mocker.patch("shutil.which", return_value=None)
    with pytest.raises(ConfigurationError, match="uv not found"):
        install_tox()


def test_install_tox_installs_as_non_root(mocker):
    mocker.patch("shutil.which", side_effect=lambda t: "/snap/bin/uv" if t == "uv" else None)
    mocker.patch("os.getuid", return_value=1000)
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_tox()
    mock_run.assert_called_once_with(
        ["uv", "tool", "install", "tox", "--with", "tox-uv", "--quiet"],
        env=None,
    )


def test_install_tox_installs_to_system_dirs_as_root(mocker):
    mocker.patch("shutil.which", side_effect=lambda t: "/snap/bin/uv" if t == "uv" else None)
    mocker.patch("os.getuid", return_value=0)
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_tox()
    mock_run.assert_called_once_with(
        ["uv", "tool", "install", "tox", "--with", "tox-uv", "--quiet"],
        env={"UV_TOOL_BIN_DIR": "/usr/local/bin", "UV_TOOL_DIR": "/usr/local/share/uv-tools"},
    )


# ---------------------------------------------------------------------------
# install_concierge
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
# install_lxd
# ---------------------------------------------------------------------------


def test_install_lxd_skips_install_when_already_present(mocker):
    mocker.patch("shutil.which", return_value="/snap/bin/lxd")
    mocker.patch("os.getuid", return_value=1000)
    mocker.patch("os.environ.get", return_value=None)
    mocker.patch("grp.getgrnam", side_effect=KeyError("lxd"))
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_lxd()
    mock_run.assert_not_called()


def test_install_lxd_installs_and_inits_as_root(mocker):
    mocker.patch("shutil.which", return_value=None)
    mocker.patch("os.getuid", return_value=0)
    mocker.patch.dict(os.environ, {"USER": "root"}, clear=False)
    mocker.patch("grp.getgrnam", side_effect=KeyError("lxd"))
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_lxd()
    assert call(["snap", "install", "lxd"]) in mock_run.call_args_list
    assert call(["lxd", "init", "--auto"]) in mock_run.call_args_list


def test_install_lxd_uses_sudo_as_non_root(mocker):
    mocker.patch("shutil.which", return_value=None)
    mocker.patch("os.getuid", return_value=1000)
    mocker.patch.dict(os.environ, {"USER": "ubuntu"}, clear=False)
    mocker.patch("grp.getgrnam", side_effect=KeyError("lxd"))
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_lxd()
    assert call(["sudo", "snap", "install", "lxd"]) in mock_run.call_args_list
    assert call(["sudo", "lxd", "init", "--auto"]) in mock_run.call_args_list


def test_install_lxd_adds_user_to_lxd_group(mocker):
    mocker.patch("shutil.which", return_value=None)
    mocker.patch("os.getuid", return_value=1000)
    mocker.patch.dict(os.environ, {"USER": "ubuntu"}, clear=False)

    mock_group = mocker.MagicMock()
    mock_group.gr_mem = []
    mocker.patch("grp.getgrnam", return_value=mock_group)
    mock_run = mocker.patch("opcli.core.install.run_command")

    install_lxd()
    assert call(["sudo", "usermod", "-aG", "lxd", "ubuntu"]) in mock_run.call_args_list


def test_install_lxd_skips_group_add_if_already_member(mocker):
    mocker.patch("shutil.which", return_value=None)
    mocker.patch("os.getuid", return_value=1000)
    mocker.patch.dict(os.environ, {"USER": "ubuntu"}, clear=False)

    mock_group = mocker.MagicMock()
    mock_group.gr_mem = ["ubuntu"]
    mocker.patch("grp.getgrnam", return_value=mock_group)
    mock_run = mocker.patch("opcli.core.install.run_command")

    install_lxd()
    assert call(["sudo", "usermod", "-aG", "lxd", "ubuntu"]) not in mock_run.call_args_list


# ---------------------------------------------------------------------------
# install_bootstrap
# ---------------------------------------------------------------------------


def test_install_bootstrap_calls_all_installers(mocker):
    mock_gh = mocker.patch("opcli.core.install.install_gh")
    mock_spread = mocker.patch("opcli.core.install.install_spread")
    mock_concierge = mocker.patch("opcli.core.install.install_concierge")
    mock_tox = mocker.patch("opcli.core.install.install_tox")
    mock_lxd = mocker.patch("opcli.core.install.install_lxd")
    mocker.patch("opcli.core.install._warn_if_local_bin_not_on_path")
    install_bootstrap()
    mock_gh.assert_called_once()
    mock_spread.assert_called_once()
    mock_concierge.assert_called_once()
    mock_tox.assert_called_once()
    mock_lxd.assert_called_once()


# ---------------------------------------------------------------------------
# install_check
# ---------------------------------------------------------------------------


def test_install_check_returns_found_tools(mocker):
    mocker.patch("shutil.which", side_effect=lambda t: f"/usr/bin/{t}")
    result = install_check()
    assert result["gh"] == "/usr/bin/gh"
    assert result["spread"] == "/usr/bin/spread"
    assert result["lxd"] == "/usr/bin/lxd"
    assert result["uv"] == "/usr/bin/uv"


def test_install_check_returns_none_for_missing(mocker):
    mocker.patch("shutil.which", return_value=None)
    result = install_check()
    assert all(v is None for v in result.values())


# ---------------------------------------------------------------------------
# CLI: opcli install bootstrap
# ---------------------------------------------------------------------------


def test_cli_install_bootstrap(mocker):
    mocker.patch("opcli.core.install.install_gh")
    mocker.patch("opcli.core.install.install_spread")
    mocker.patch("opcli.core.install.install_concierge")
    mocker.patch("opcli.core.install.install_tox")
    mocker.patch("opcli.core.install.install_lxd")
    mocker.patch("opcli.core.install._warn_if_local_bin_not_on_path")
    result = _RUNNER.invoke(install_app, ["bootstrap"])
    assert result.exit_code == 0
    assert "Bootstrap complete" in result.output


def test_cli_install_check_all_present(mocker):
    mocker.patch("shutil.which", side_effect=lambda t: f"/usr/bin/{t}")
    result = _RUNNER.invoke(install_app, ["check"])
    assert result.exit_code == 0
    assert "✓" in result.output


def test_cli_install_check_missing_tool(mocker):
    mocker.patch("shutil.which", return_value=None)
    result = _RUNNER.invoke(install_app, ["check"])
    assert result.exit_code == 1
    assert "✗" in result.output
    assert "opcli install bootstrap" in result.output


# ---------------------------------------------------------------------------
# PATH warning
# ---------------------------------------------------------------------------


def test_path_warning_printed_when_local_bin_missing(mocker, tmp_path, capsys):
    mocker.patch("pathlib.Path.home", return_value=tmp_path)
    mocker.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=False)
    _warn_if_local_bin_not_on_path()
    captured = capsys.readouterr()
    assert ".local/bin" in captured.out
    assert "export PATH" in captured.out


def test_path_warning_silent_when_local_bin_present(mocker, tmp_path, capsys):
    local_bin = str(tmp_path / ".local" / "bin")
    mocker.patch("pathlib.Path.home", return_value=tmp_path)
    mocker.patch.dict(os.environ, {"PATH": f"{local_bin}:/usr/bin"}, clear=False)
    _warn_if_local_bin_not_on_path()
    captured = capsys.readouterr()
    assert captured.out == ""


# ---------------------------------------------------------------------------
# Hidden commands still callable
# ---------------------------------------------------------------------------


def test_hidden_spread_command_still_works(mocker):
    mocker.patch("shutil.which", return_value="/usr/local/bin/spread")
    mock_run = mocker.patch("opcli.core.install.run_command")
    result = _RUNNER.invoke(install_app, ["spread"])
    assert result.exit_code == 0
    mock_run.assert_not_called()


def test_hidden_gh_command_still_works(mocker):
    mocker.patch("shutil.which", return_value="/snap/bin/gh")
    mock_run = mocker.patch("opcli.core.install.run_command")
    result = _RUNNER.invoke(install_app, ["gh"])
    assert result.exit_code == 0
    mock_run.assert_not_called()
