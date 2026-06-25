# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for ``opcli install`` commands and core install helpers."""

import os
from unittest.mock import MagicMock, call

import pytest
from typer.testing import CliRunner

from opcli.commands.install import app as install_app
from opcli.core.exceptions import ConfigurationError
from opcli.core.install import (
    _check_os_prerequisites,
    _lxd_is_initialised,
    _warn_if_local_bin_not_on_path,
    install_all,
    install_charmcraft,
    install_concierge,
    install_doctor,
    install_gh,
    install_lxd,
    install_rockcraft,
    install_snapcraft,
    install_spread,
    install_tox,
    install_uv,
)

_RUNNER = CliRunner()


# ---------------------------------------------------------------------------
# OS precheck
# ---------------------------------------------------------------------------


def test_os_precheck_passes_on_linux(mocker):
    mocker.patch("platform.system", return_value="Linux")
    mocker.patch("shutil.which", return_value="/usr/bin/snap")
    _check_os_prerequisites()


def test_os_precheck_raises_on_non_linux(mocker):
    mocker.patch("platform.system", return_value="Darwin")
    with pytest.raises(ConfigurationError, match="Linux"):
        _check_os_prerequisites()


def test_os_precheck_raises_if_snap_missing(mocker):
    mocker.patch("platform.system", return_value="Linux")
    mocker.patch("shutil.which", return_value=None)
    with pytest.raises(ConfigurationError, match="snapd"):
        _check_os_prerequisites()


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
    assert calls[2] == call(["cp", "/root/go/bin/spread", "/usr/local/bin/spread"])


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
# install_uv
# ---------------------------------------------------------------------------


def test_install_uv_skips_when_already_present(mocker):
    mocker.patch("shutil.which", return_value="/snap/bin/uv")
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_uv()
    mock_run.assert_not_called()


def test_install_uv_as_root(mocker):
    mocker.patch("shutil.which", return_value=None)
    mocker.patch("os.getuid", return_value=0)
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_uv()
    mock_run.assert_called_once_with(["snap", "install", "astral-uv", "--classic"])


def test_install_uv_as_non_root(mocker):
    mocker.patch("shutil.which", return_value=None)
    mocker.patch("os.getuid", return_value=1000)
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_uv()
    mock_run.assert_called_once_with(["sudo", "snap", "install", "astral-uv", "--classic"])


# ---------------------------------------------------------------------------
# install_tox
# ---------------------------------------------------------------------------


def test_install_tox_installs_uv_first_if_missing(mocker):
    mocker.patch("shutil.which", return_value=None)
    mocker.patch("os.getuid", return_value=1000)
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_tox()
    assert mock_run.call_args_list[0] == call(
        ["sudo", "snap", "install", "astral-uv", "--classic"]
    )


def test_install_tox_installs_as_non_root_with_upgrade(mocker):
    mocker.patch("shutil.which", side_effect=lambda t: "/snap/bin/uv" if t == "uv" else None)
    mocker.patch("os.getuid", return_value=1000)
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_tox()
    mock_run.assert_called_once_with(
        ["/snap/bin/uv", "tool", "install", "tox", "--with", "tox-uv", "--upgrade", "--quiet"],
        env=None,
    )


def test_install_tox_installs_to_system_dirs_as_root(mocker):
    mocker.patch("shutil.which", side_effect=lambda t: "/snap/bin/uv" if t == "uv" else None)
    mocker.patch("os.getuid", return_value=0)
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_tox()
    mock_run.assert_called_once_with(
        ["/snap/bin/uv", "tool", "install", "tox", "--with", "tox-uv", "--upgrade", "--quiet"],
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
# _lxd_is_initialised
# ---------------------------------------------------------------------------


def test_lxd_is_initialised_returns_true_when_storage_pools_present(mocker):
    mock_result = MagicMock()
    mock_result.stdout = "config:\n  user.user-data: ...\ndevices:\n  root:\n    path: /\n    pool: default\n    type: disk\n"
    mocker.patch("opcli.core.install.run_command", return_value=mock_result)
    assert _lxd_is_initialised([]) is True


def test_lxd_is_initialised_returns_false_when_no_storage_pools(mocker):
    mock_result = MagicMock()
    mock_result.stdout = "config: {}\ndevices: {}\n"
    mocker.patch("opcli.core.install.run_command", return_value=mock_result)
    assert _lxd_is_initialised([]) is False


# ---------------------------------------------------------------------------
# install_lxd
# ---------------------------------------------------------------------------


def test_install_lxd_skips_install_when_already_present(mocker):
    mocker.patch("shutil.which", return_value="/snap/bin/lxd")
    mocker.patch("os.getuid", return_value=1000)
    mocker.patch.dict(os.environ, {}, clear=True)
    mocker.patch("grp.getgrnam", side_effect=KeyError("lxd"))
    mocker.patch("opcli.core.install._lxd_is_initialised", return_value=True)
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_lxd()
    assert call(["sudo", "snap", "install", "lxd"]) not in mock_run.call_args_list
    assert call(["sudo", "lxd", "init", "--auto"]) not in mock_run.call_args_list


def test_install_lxd_inits_preinstalled_but_uninitialised_lxd(mocker):
    """LXD pre-installed (e.g. Ubuntu 24.04 multipass) but not yet initialised."""
    mocker.patch("shutil.which", return_value="/usr/sbin/lxd")
    mocker.patch("os.getuid", return_value=1000)
    mocker.patch.dict(os.environ, {"USER": "ubuntu"}, clear=False)
    mocker.patch("grp.getgrnam", side_effect=KeyError("lxd"))
    mocker.patch("opcli.core.install._lxd_is_initialised", return_value=False)
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_lxd()
    assert call(["sudo", "snap", "install", "lxd"]) not in mock_run.call_args_list
    assert call(["sudo", "lxd", "init", "--auto"]) in mock_run.call_args_list


def test_install_lxd_installs_and_inits_as_root(mocker):
    mocker.patch("shutil.which", return_value=None)
    mocker.patch("os.getuid", return_value=0)
    mocker.patch.dict(os.environ, {"USER": "root"}, clear=False)
    mocker.patch("grp.getgrnam", side_effect=KeyError("lxd"))
    mocker.patch("opcli.core.install._lxd_is_initialised", return_value=False)
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_lxd()
    assert call(["snap", "install", "lxd"]) in mock_run.call_args_list
    assert call(["lxd", "init", "--auto"]) in mock_run.call_args_list


def test_install_lxd_uses_sudo_as_non_root(mocker):
    mocker.patch("shutil.which", return_value=None)
    mocker.patch("os.getuid", return_value=1000)
    mocker.patch.dict(os.environ, {"USER": "ubuntu"}, clear=False)
    mocker.patch("grp.getgrnam", side_effect=KeyError("lxd"))
    mocker.patch("opcli.core.install._lxd_is_initialised", return_value=False)
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
    mocker.patch("opcli.core.install._lxd_is_initialised", return_value=False)
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
    mocker.patch("opcli.core.install._lxd_is_initialised", return_value=False)
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_lxd()
    assert call(["sudo", "usermod", "-aG", "lxd", "ubuntu"]) not in mock_run.call_args_list


# ---------------------------------------------------------------------------
# install_charmcraft
# ---------------------------------------------------------------------------


def test_install_charmcraft_skips_when_already_present(mocker):
    mocker.patch("shutil.which", return_value="/snap/bin/charmcraft")
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_charmcraft()
    mock_run.assert_not_called()


def test_install_charmcraft_as_root(mocker):
    mocker.patch("shutil.which", return_value=None)
    mocker.patch("os.getuid", return_value=0)
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_charmcraft()
    mock_run.assert_called_once_with(["snap", "install", "charmcraft", "--classic"])


def test_install_charmcraft_uses_sudo_as_non_root(mocker):
    mocker.patch("shutil.which", return_value=None)
    mocker.patch("os.getuid", return_value=1000)
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_charmcraft()
    mock_run.assert_called_once_with(["sudo", "snap", "install", "charmcraft", "--classic"])


# ---------------------------------------------------------------------------
# install_rockcraft
# ---------------------------------------------------------------------------


def test_install_rockcraft_skips_when_already_present(mocker):
    mocker.patch("shutil.which", return_value="/snap/bin/rockcraft")
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_rockcraft()
    mock_run.assert_not_called()


def test_install_rockcraft_as_root(mocker):
    mocker.patch("shutil.which", return_value=None)
    mocker.patch("os.getuid", return_value=0)
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_rockcraft()
    mock_run.assert_called_once_with(["snap", "install", "rockcraft", "--classic"])


def test_install_rockcraft_uses_sudo_as_non_root(mocker):
    mocker.patch("shutil.which", return_value=None)
    mocker.patch("os.getuid", return_value=1000)
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_rockcraft()
    mock_run.assert_called_once_with(["sudo", "snap", "install", "rockcraft", "--classic"])


# ---------------------------------------------------------------------------
# install_snapcraft
# ---------------------------------------------------------------------------


def test_install_snapcraft_skips_when_already_present(mocker):
    mocker.patch("shutil.which", return_value="/snap/bin/snapcraft")
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_snapcraft()
    mock_run.assert_not_called()


def test_install_snapcraft_as_root(mocker):
    mocker.patch("shutil.which", return_value=None)
    mocker.patch("os.getuid", return_value=0)
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_snapcraft()
    mock_run.assert_called_once_with(["snap", "install", "snapcraft", "--classic"])


def test_install_snapcraft_uses_sudo_as_non_root(mocker):
    mocker.patch("shutil.which", return_value=None)
    mocker.patch("os.getuid", return_value=1000)
    mock_run = mocker.patch("opcli.core.install.run_command")
    install_snapcraft()
    mock_run.assert_called_once_with(["sudo", "snap", "install", "snapcraft", "--classic"])


# ---------------------------------------------------------------------------
# install_all
# ---------------------------------------------------------------------------


def test_install_all_calls_all_installers_as_root(mocker):
    mocker.patch("platform.system", return_value="Linux")
    mocker.patch("shutil.which", return_value="/usr/bin/snap")
    mocker.patch("os.getuid", return_value=0)
    mock_gh = mocker.patch("opcli.core.install.install_gh")
    mock_spread = mocker.patch("opcli.core.install.install_spread")
    mock_concierge = mocker.patch("opcli.core.install.install_concierge")
    mock_uv = mocker.patch("opcli.core.install.install_uv")
    mock_tox = mocker.patch("opcli.core.install.install_tox")
    mock_lxd = mocker.patch("opcli.core.install.install_lxd")
    mock_charmcraft = mocker.patch("opcli.core.install.install_charmcraft")
    mock_rockcraft = mocker.patch("opcli.core.install.install_rockcraft")
    mock_snapcraft = mocker.patch("opcli.core.install.install_snapcraft")
    install_all()
    mock_gh.assert_called_once()
    mock_spread.assert_called_once()
    mock_concierge.assert_called_once()
    mock_uv.assert_called_once()
    mock_tox.assert_called_once()
    mock_lxd.assert_called_once()
    mock_charmcraft.assert_called_once()
    mock_rockcraft.assert_called_once()
    mock_snapcraft.assert_called_once()


def test_install_all_warns_path_for_non_root(mocker, tmp_path, capsys):
    mocker.patch("platform.system", return_value="Linux")
    mocker.patch("shutil.which", return_value="/usr/bin/snap")
    mocker.patch("os.getuid", return_value=1000)
    mocker.patch("pathlib.Path.home", return_value=tmp_path)
    mocker.patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=False)
    mocker.patch("opcli.core.install.install_gh")
    mocker.patch("opcli.core.install.install_spread")
    mocker.patch("opcli.core.install.install_concierge")
    mocker.patch("opcli.core.install.install_uv")
    mocker.patch("opcli.core.install.install_tox")
    mocker.patch("opcli.core.install.install_lxd")
    mocker.patch("opcli.core.install.install_charmcraft")
    mocker.patch("opcli.core.install.install_rockcraft")
    mocker.patch("opcli.core.install.install_snapcraft")
    install_all()
    captured = capsys.readouterr()
    assert "export PATH" in captured.out


def test_install_all_raises_on_non_linux(mocker):
    mocker.patch("platform.system", return_value="Darwin")
    mocker.patch("shutil.which", return_value="/usr/bin/snap")
    with pytest.raises(ConfigurationError, match="Linux"):
        install_all()


# ---------------------------------------------------------------------------
# install_doctor
# ---------------------------------------------------------------------------


def test_install_doctor_returns_path_and_version(mocker):
    mocker.patch("shutil.which", side_effect=lambda t: f"/usr/bin/{t}")
    mocker.patch("opcli.core.install._get_version", return_value="v1.2.3")
    result = install_doctor()
    assert result["gh"] == ("/usr/bin/gh", "v1.2.3")
    assert result["uv"] == ("/usr/bin/uv", "v1.2.3")


def test_install_doctor_returns_none_for_missing(mocker):
    mocker.patch("shutil.which", return_value=None)
    result = install_doctor()
    assert all(v == (None, None) for v in result.values())


# ---------------------------------------------------------------------------
# CLI: opcli install all
# ---------------------------------------------------------------------------


def test_cli_install_all(mocker):
    mocker.patch("platform.system", return_value="Linux")
    mocker.patch("shutil.which", return_value="/usr/bin/snap")
    mocker.patch("os.getuid", return_value=0)
    mocker.patch("opcli.core.install.install_gh")
    mocker.patch("opcli.core.install.install_spread")
    mocker.patch("opcli.core.install.install_concierge")
    mocker.patch("opcli.core.install.install_tox")
    mocker.patch("opcli.core.install.install_lxd")
    result = _RUNNER.invoke(install_app, ["all"])
    assert result.exit_code == 0
    assert "All tools installed" in result.output
    assert "Next steps" in result.output


def test_cli_install_doctor_all_present(mocker):
    mocker.patch("shutil.which", side_effect=lambda t: f"/usr/bin/{t}")
    mocker.patch("opcli.core.install._get_version", return_value="1.0.0")
    result = _RUNNER.invoke(install_app, ["doctor"])
    assert result.exit_code == 0
    assert "\u2713" in result.output


def test_cli_install_doctor_missing_tool(mocker):
    mocker.patch("shutil.which", return_value=None)
    result = _RUNNER.invoke(install_app, ["doctor"])
    assert result.exit_code == 1
    assert "\u2717" in result.output
    assert "opcli install all" in result.output


# ---------------------------------------------------------------------------
# PATH warning
# ---------------------------------------------------------------------------


def test_path_warning_printed_when_local_bin_missing(mocker, tmp_path, capsys):
    mocker.patch("pathlib.Path.home", return_value=tmp_path)
    mocker.patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "SHELL": "/bin/bash"}, clear=False)
    _warn_if_local_bin_not_on_path(prefix=True)
    captured = capsys.readouterr()
    assert ".local/bin" in captured.out
    assert "export PATH" in captured.out
    assert ".bashrc" in captured.out


def test_path_warning_uses_zshrc_for_zsh(mocker, tmp_path, capsys):
    mocker.patch("pathlib.Path.home", return_value=tmp_path)
    mocker.patch.dict(os.environ, {"PATH": "/usr/bin", "SHELL": "/usr/bin/zsh"}, clear=False)
    _warn_if_local_bin_not_on_path(prefix=True)
    captured = capsys.readouterr()
    assert ".zshrc" in captured.out


def test_path_warning_silent_when_local_bin_present(mocker, tmp_path, capsys):
    local_bin = str(tmp_path / ".local" / "bin")
    mocker.patch("pathlib.Path.home", return_value=tmp_path)
    mocker.patch.dict(os.environ, {"PATH": f"{local_bin}:/usr/bin"}, clear=False)
    _warn_if_local_bin_not_on_path(prefix=False)
    captured = capsys.readouterr()
    assert captured.out == ""


# ---------------------------------------------------------------------------
# Per-tool commands visible and callable
# ---------------------------------------------------------------------------


def test_per_tool_commands_visible_in_help():
    result = _RUNNER.invoke(install_app, ["--help"])
    assert "spread" in result.output
    assert "gh" in result.output
    assert "tox" in result.output


def test_per_tool_spread_command_works(mocker):
    mocker.patch("shutil.which", return_value="/usr/local/bin/spread")
    mock_run = mocker.patch("opcli.core.install.run_command")
    result = _RUNNER.invoke(install_app, ["spread"])
    assert result.exit_code == 0
    mock_run.assert_not_called()


def test_per_tool_gh_command_works(mocker):
    mocker.patch("shutil.which", return_value="/snap/bin/gh")
    mock_run = mocker.patch("opcli.core.install.run_command")
    result = _RUNNER.invoke(install_app, ["gh"])
    assert result.exit_code == 0
    mock_run.assert_not_called()
