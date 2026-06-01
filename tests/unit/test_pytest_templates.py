# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Tests for pytest Jinja2 template rendering and integration."""

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from opcli.commands.pytest_cmd import app as pytest_app
from opcli.core.exceptions import ConfigurationError
from opcli.core.pytest_args import assemble_tox_argv, pytest_run
from opcli.core.spread import get_suite_config, spread_expand
from opcli.core.template import render_arguments_template, render_environment_template
from tests.conftest import write_file

_SINGLE_CHARM_BUILD = """\
version: 1
rocks: []
charms:
- name: traefik-k8s
  charmcraft-yaml: charmcraft.yaml
  builds:
  - arch: amd64
    path: traefik-k8s_ubuntu-22.04-amd64.charm
  - arch: arm64
    path: traefik-k8s_ubuntu-24.04-arm64.charm
snaps: []
"""

_MULTI_ARTIFACT_BUILD = """\
version: 1
rocks:
- name: my-rock
  rockcraft-yaml: rockcraft.yaml
  builds:
  - arch: amd64
    file: my-rock_amd64.rock
    image: ghcr.io/canonical/my-rock:latest
  - arch: arm64
    file: my-rock_arm64.rock
    image: ghcr.io/canonical/my-rock:arm64
charms:
- name: my-charm
  charmcraft-yaml: charmcraft.yaml
  builds:
  - arch: amd64
    path: my-charm_ubuntu-22.04-amd64.charm
  - arch: amd64
    path: my-charm_ubuntu-24.04-amd64.charm
  - arch: arm64
    path: my-charm_ubuntu-22.04-arm64.charm
snaps: []
"""

_SPREAD_YAML_WITH_ARGS_TEMPLATE = """\
project: myproject
path: /home/ubuntu/proj
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-24.04
integration-suites:
  tests/integration/:
    pytest-arguments-template: |
      {{% for charm in artifacts.charms %}}
        {{% for build in charm.builds if build.arch == arch %}}
          --charm-file={{{{ build.path }}}}
        {{% endfor %}}
      {{% endfor %}}
"""

_SPREAD_YAML_WITH_ENV_TEMPLATE = """\
project: myproject
path: /home/ubuntu/proj
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-24.04
integration-suites:
  tests/integration/:
    pytest-environment-template: |
      CHARM_PATH={{ artifacts.charms[0].builds[0].path }}
"""

_SPREAD_YAML_NO_TEMPLATE = """\
project: myproject
path: /home/ubuntu/proj
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-24.04
integration-suites:
  tests/integration/:
    summary: integration tests
"""


class TestRenderArgumentsTemplate:
    """Tests for render_arguments_template()."""

    def test_renders_charm_file_flags(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)

        template = (
            "{% for charm in artifacts.charms %}"
            "{% for build in charm.builds if build.arch == arch %}"
            " --charm-file={{ build.path }}"
            "{% endfor %}"
            "{% endfor %}"
        )

        with patch("opcli.core.template.current_arch", return_value="amd64"):
            result = render_arguments_template(tmp_path, template)

        assert result == ["--charm-file=traefik-k8s_ubuntu-22.04-amd64.charm"]

    def test_renders_multiple_artifacts(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _MULTI_ARTIFACT_BUILD)

        template = (
            "{% for charm in artifacts.charms %}"
            "{% for build in charm.builds if build.arch == arch %}"
            " --charm-file={{ build.path }}"
            "{% endfor %}"
            "{% endfor %}"
            "{% for rock in artifacts.rocks %}"
            "{% for build in rock.builds if build.arch == arch %}"
            " --{{ rock.name }}-image={{ build.image }}"
            "{% endfor %}"
            "{% endfor %}"
        )

        with patch("opcli.core.template.current_arch", return_value="amd64"):
            result = render_arguments_template(tmp_path, template)

        assert "--charm-file=my-charm_ubuntu-22.04-amd64.charm" in result
        assert "--charm-file=my-charm_ubuntu-24.04-amd64.charm" in result
        assert "--my-rock-image=ghcr.io/canonical/my-rock:latest" in result
        # arm64 entries should NOT be present
        assert "--charm-file=my-charm_ubuntu-22.04-arm64.charm" not in result

    def test_missing_artifacts_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match=r"artifacts\.build\.yaml not found"):
            render_arguments_template(tmp_path, "{{ artifacts }}")

    def test_syntax_error_raises(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)

        with pytest.raises(ConfigurationError, match="Jinja2 syntax error"):
            render_arguments_template(tmp_path, "{% invalid %}")

    def test_undefined_variable_raises(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)

        with (
            patch("opcli.core.template.current_arch", return_value="amd64"),
            pytest.raises(ConfigurationError, match="Undefined variable"),
        ):
            render_arguments_template(tmp_path, "{{ nonexistent.var }}")

    def test_type_error_raises_configuration_error(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)

        with (
            patch("opcli.core.template.current_arch", return_value="amd64"),
            pytest.raises(ConfigurationError, match="Error evaluating template"),
        ):
            render_arguments_template(tmp_path, "{{ artifacts.charms + 1 }}")

    def test_index_error_raises_configuration_error(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)

        with (
            patch("opcli.core.template.current_arch", return_value="amd64"),
            pytest.raises(ConfigurationError, match="(Error evaluating|Undefined variable)"),
        ):
            render_arguments_template(tmp_path, "{{ artifacts.charms[99].builds[0].path }}")


class TestRenderEnvironmentTemplate:
    """Tests for render_environment_template()."""

    def test_renders_key_value_pairs(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)

        template = "CHARM_PATH={{ artifacts.charms[0].builds[0].path }}"

        with patch("opcli.core.template.current_arch", return_value="amd64"):
            result = render_environment_template(tmp_path, template)

        assert result == {"CHARM_PATH": "traefik-k8s_ubuntu-22.04-amd64.charm"}

    def test_skips_comments_and_blank_lines(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)

        template = "# This is a comment\n\nFOO=bar\n\n"

        with patch("opcli.core.template.current_arch", return_value="amd64"):
            result = render_environment_template(tmp_path, template)

        assert result == {"FOO": "bar"}

    def test_malformed_line_raises(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)

        template = "NOT_A_VALID_LINE"

        with (
            patch("opcli.core.template.current_arch", return_value="amd64"),
            pytest.raises(ConfigurationError, match="not a valid KEY=VALUE"),
        ):
            render_environment_template(tmp_path, template)

    def test_value_with_equals_sign(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)

        template = "IMAGE=ghcr.io/org/img:tag=latest"

        with patch("opcli.core.template.current_arch", return_value="amd64"):
            result = render_environment_template(tmp_path, template)

        assert result == {"IMAGE": "ghcr.io/org/img:tag=latest"}


class TestAssembleToxArgvWithTemplate:
    """Tests for assemble_tox_argv with suite_config templates."""

    def test_default_pfe_when_no_template(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)

        with patch("opcli.core.pytest_args.current_arch", return_value="amd64"):
            argv = assemble_tox_argv(tmp_path)

        joined = " ".join(argv)
        assert "--charm-file=" in joined

    def test_arguments_template_overrides_pfe(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)

        suite_config: dict[str, str | None] = {
            "cwd": "./",
            "pytest-arguments-template": "--custom-flag={{ artifacts.charms[0].builds[0].path }}",
        }

        with patch("opcli.core.template.current_arch", return_value="amd64"):
            argv = assemble_tox_argv(tmp_path, suite_config=suite_config)

        assert "--custom-flag=traefik-k8s_ubuntu-22.04-amd64.charm" in argv
        assert "--charm-file=" not in " ".join(argv)

    def test_extra_args_appended_with_template(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)

        suite_config: dict[str, str | None] = {
            "cwd": "./",
            "pytest-arguments-template": "--custom=value",
        }

        with patch("opcli.core.template.current_arch", return_value="amd64"):
            argv = assemble_tox_argv(
                tmp_path, suite_config=suite_config, extra_args=["-k", "test_foo"]
            )

        assert argv == ["tox", "-e", "integration", "--", "--custom=value", "-k", "test_foo"]


class TestPytestRunWithTemplate:
    """Tests for pytest_run with template-based env vars."""

    def test_env_template_sets_vars(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)

        suite_config: dict[str, str | None] = {
            "cwd": "./",
            "pytest-environment-template": ("CHARM_PATH={{ artifacts.charms[0].builds[0].path }}"),
        }

        with (
            patch("opcli.core.template.current_arch", return_value="amd64"),
            patch("opcli.core.pytest_args.run_command") as mock_run,
        ):
            pytest_run(tmp_path, ci=True, suite_config=suite_config)

        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args
        env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
        assert env is not None
        assert env["CHARM_PATH"] == "traefik-k8s_ubuntu-22.04-amd64.charm"


class TestGetSuiteConfigTemplates:
    """Tests for get_suite_config returning template keys."""

    def test_returns_arguments_template(self, tmp_path: Path) -> None:
        spread = (
            "project: x\npath: /x\n"
            "backends:\n  it:\n    type: integration-test\n    systems: [u]\n"
            "integration-suites:\n"
            "  tests/integration/:\n"
            "    pytest-arguments-template: |\n"
            "      --flag=value\n"
        )
        write_file(tmp_path / "spread.yaml", spread)
        cfg = get_suite_config(tmp_path)
        assert "pytest-arguments-template" in cfg
        assert "--flag=value" in str(cfg["pytest-arguments-template"])

    def test_returns_environment_template(self, tmp_path: Path) -> None:
        spread = (
            "project: x\npath: /x\n"
            "backends:\n  it:\n    type: integration-test\n    systems: [u]\n"
            "integration-suites:\n"
            "  tests/integration/:\n"
            "    pytest-environment-template: |\n"
            "      KEY=val\n"
        )
        write_file(tmp_path / "spread.yaml", spread)
        cfg = get_suite_config(tmp_path)
        assert "pytest-environment-template" in cfg
        assert "KEY=val" in str(cfg["pytest-environment-template"])

    def test_no_template_returns_none(self, tmp_path: Path) -> None:
        write_file(tmp_path / "spread.yaml", _SPREAD_YAML_NO_TEMPLATE)
        cfg = get_suite_config(tmp_path)
        assert cfg.get("pytest-arguments-template") is None
        assert cfg.get("pytest-environment-template") is None

    def test_no_spread_yaml_returns_default(self, tmp_path: Path) -> None:
        cfg = get_suite_config(tmp_path)
        assert cfg == {"cwd": "./"}


class TestCLIRunnerTemplates:
    """CliRunner tests for opcli pytest expand with templates."""

    def test_expand_default_pfe(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        with patch("opcli.core.pytest_args.current_arch", return_value="amd64"):
            result = runner.invoke(pytest_app, ["expand"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert "--charm-file=" in result.output

    def test_expand_with_env_template(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)
        write_file(tmp_path / "spread.yaml", _SPREAD_YAML_WITH_ENV_TEMPLATE)
        (tmp_path / "tests" / "integration").mkdir(parents=True)
        (tmp_path / "tests" / "integration" / "test_charm.py").touch()
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        with patch("opcli.core.template.current_arch", return_value="amd64"):
            result = runner.invoke(pytest_app, ["expand"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert "CHARM_PATH=" in result.output
        assert "traefik-k8s_ubuntu-22.04-amd64.charm" in result.output

    def test_expand_with_extra_args(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        with patch("opcli.core.pytest_args.current_arch", return_value="amd64"):
            result = runner.invoke(
                pytest_app,
                ["expand", "--", "-k", "test_foo"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert "-k" in result.output
        assert "test_foo" in result.output


class TestSpreadExpandStripsTemplateKeys:
    """Test that template keys are stripped during spread expansion."""

    def test_template_keys_stripped(self, tmp_path: Path) -> None:
        write_file(tmp_path / "spread.yaml", _SPREAD_YAML_WITH_ENV_TEMPLATE)
        (tmp_path / "tests" / "integration").mkdir(parents=True)
        (tmp_path / "tests" / "integration" / "test_charm.py").touch()

        expanded = spread_expand(tmp_path)
        assert "pytest-arguments-template" not in expanded
        assert "pytest-environment-template" not in expanded
