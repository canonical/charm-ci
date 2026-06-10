# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Tests for pytest Jinja2 template rendering and integration."""

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from opcli.commands.pytest_cmd import _cd_prefix
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

        with pytest.raises(ConfigurationError, match="Jinja2 syntax error in pytest-arguments"):
            render_arguments_template(tmp_path, "{% invalid %}")

    def test_undefined_variable_raises(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)

        with (
            patch("opcli.core.template.current_arch", return_value="amd64"),
            pytest.raises(ConfigurationError, match="Undefined variable in pytest-arguments"),
        ):
            render_arguments_template(tmp_path, "{{ nonexistent.var }}")

    def test_undefined_attribute_raises(self, tmp_path: Path) -> None:
        """StrictUndefined catches typos like artifacts.charm (missing 's')."""
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)

        with (
            patch("opcli.core.template.current_arch", return_value="amd64"),
            pytest.raises(ConfigurationError, match="Undefined variable"),
        ):
            render_arguments_template(tmp_path, "{{ artifacts.nonexistent_field }}")

    def test_type_error_raises_configuration_error(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)

        with (
            patch("opcli.core.template.current_arch", return_value="amd64"),
            pytest.raises(ConfigurationError, match="Error evaluating pytest-arguments"),
        ):
            render_arguments_template(tmp_path, "{{ artifacts.charms + 1 }}")

    def test_index_error_raises_configuration_error(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)

        with (
            patch("opcli.core.template.current_arch", return_value="amd64"),
            pytest.raises(ConfigurationError, match=r"(Error evaluating|Undefined variable)"),
        ):
            render_arguments_template(tmp_path, "{{ artifacts.charms[99].builds[0].path }}")

    def test_ssti_attack_blocked(self, tmp_path: Path) -> None:
        """SandboxedEnvironment prevents template injection attacks."""
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)

        malicious = "{{ artifacts.__class__.__mro__ }}"
        with (
            patch("opcli.core.template.current_arch", return_value="amd64"),
            pytest.raises(ConfigurationError, match="Unsafe operation"),
        ):
            render_arguments_template(tmp_path, malicious)


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

    def test_env_context_renders_existing_var(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)

        template = "MY_VAR={{ env.MY_TEST_VAR_OPCLI }}"
        with (
            patch("opcli.core.template.current_arch", return_value="amd64"),
            patch.dict("os.environ", {"MY_TEST_VAR_OPCLI": "hello-world"}),
        ):
            result = render_environment_template(tmp_path, template)

        assert result == {"MY_VAR": "hello-world"}

    def test_env_context_get_with_default(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)

        template = 'OPTIONAL={{ env.get("SURELY_ABSENT_OPCLI_VAR", "fallback") }}'
        with patch("opcli.core.template.current_arch", return_value="amd64"):
            result = render_environment_template(tmp_path, template)

        assert result == {"OPTIONAL": "fallback"}

    def test_env_context_missing_var_strict_raises(self, tmp_path: Path) -> None:
        """Accessing a missing env key via attribute notation raises ConfigurationError."""
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)

        template = "BAD={{ env.SURELY_ABSENT_OPCLI_VAR }}"
        with (
            patch("opcli.core.template.current_arch", return_value="amd64"),
            pytest.raises(ConfigurationError, match="Undefined variable"),
        ):
            render_environment_template(tmp_path, template)


class TestAssembleToxArgvWithTemplate:
    """Tests for assemble_tox_argv with suite_config templates."""

    def test_default_no_template_produces_no_flags(self, tmp_path: Path) -> None:
        """Without a template, no artifact flags are generated — plugin handles injection."""
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)

        argv = assemble_tox_argv(tmp_path)

        assert argv == ["tox", "-e", "integration"]
        assert "--" not in " ".join(argv)

    def test_arguments_template_overrides_pfe(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)

        suite_config: dict[str, str | None] = {
            "working-dir": "./",
            "pytest-arguments-template": "--custom-flag={{ artifacts.charms[0].builds[0].path }}",
        }

        with patch("opcli.core.template.current_arch", return_value="amd64"):
            argv = assemble_tox_argv(tmp_path, suite_config=suite_config)

        assert "--custom-flag=traefik-k8s_ubuntu-22.04-amd64.charm" in argv
        assert "--charm-file=" not in " ".join(argv)

    def test_extra_args_appended_with_template(self, tmp_path: Path) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)

        suite_config: dict[str, str | None] = {
            "working-dir": "./",
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
            "working-dir": "./",
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
        assert cfg == {"working-dir": "./"}


class TestCLIRunnerTemplates:
    """CliRunner tests for opcli pytest expand with templates."""

    def test_expand_no_template_produces_bare_tox(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without a template, expand emits just 'tox -e integration' — no artifact flags."""
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(pytest_app, ["expand"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert "--charm-file=" not in result.output
        assert "tox" in result.output
        assert "-e integration" in result.output

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SPREAD_YAML_WITH_WORKING_DIR = """\
project: myproject
path: /home/ubuntu/proj
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-24.04
integration-suites:
  sub-charm/tests/integration/:
    working-dir: sub-charm/
    summary: integration tests
"""

_SPREAD_YAML_WITH_WORKING_DIR_AND_ENV_TEMPLATE = """\
project: myproject
path: /home/ubuntu/proj
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-24.04
integration-suites:
  sub-charm/tests/integration/:
    working-dir: sub-charm/
    pytest-environment-template: |
      CHARM_PATH={{ artifacts.charms[0].builds[0].path }}
"""


class TestCdPrefix:
    """Unit tests for the _cd_prefix() helper."""

    def test_same_dir_returns_empty(self, tmp_path: Path) -> None:
        assert _cd_prefix(tmp_path, tmp_path) == ""

    def test_same_dir_trailing_slash_ignored(self, tmp_path: Path) -> None:
        sub = tmp_path / "./"
        assert _cd_prefix(tmp_path, sub) == ""

    def test_subdirectory_returns_relative_prefix(self, tmp_path: Path) -> None:
        sub = tmp_path / "sub-charm"
        assert _cd_prefix(tmp_path, sub) == "cd sub-charm && "

    def test_nested_subdirectory(self, tmp_path: Path) -> None:
        sub = tmp_path / "a" / "b"
        assert _cd_prefix(tmp_path, sub) == "cd a/b && "

    def test_outside_root_falls_back_to_absolute(self, tmp_path: Path) -> None:
        other = tmp_path.parent / "other"
        prefix = _cd_prefix(tmp_path, other)
        assert prefix == f"cd {other} && "


class TestExpandCdPrefix:
    """Integration tests: pytest expand emits cd prefix when working-dir != root."""

    def test_expand_no_cd_when_root_working_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(pytest_app, ["expand"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert not result.output.startswith("cd ")

    def test_expand_emits_cd_prefix_for_subdir_working_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "sub-charm").mkdir()
        (tmp_path / "sub-charm" / "tests" / "integration").mkdir(parents=True)
        (tmp_path / "sub-charm" / "tests" / "integration" / "test_charm.py").touch()
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)
        write_file(tmp_path / "spread.yaml", _SPREAD_YAML_WITH_WORKING_DIR)
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            pytest_app,
            ["expand", "--suite", "sub-charm/tests/integration/"],
            catch_exceptions=False,
        )

        assert result.exit_code == 0, result.output
        assert result.output.startswith("cd sub-charm && ")
        assert "tox" in result.output

    def test_expand_env_template_includes_cd_prefix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "sub-charm").mkdir()
        (tmp_path / "sub-charm" / "tests" / "integration").mkdir(parents=True)
        (tmp_path / "sub-charm" / "tests" / "integration" / "test_charm.py").touch()
        write_file(tmp_path / "artifacts.build.yaml", _SINGLE_CHARM_BUILD)
        write_file(tmp_path / "spread.yaml", _SPREAD_YAML_WITH_WORKING_DIR_AND_ENV_TEMPLATE)
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        with patch("opcli.core.template.current_arch", return_value="amd64"):
            result = runner.invoke(
                pytest_app,
                ["expand", "--suite", "sub-charm/tests/integration/"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert result.output.startswith("cd sub-charm && ")
        assert "CHARM_PATH=" in result.output


# ---------------------------------------------------------------------------
# Examples project — smoke test the real examples/spread.yaml env template
# ---------------------------------------------------------------------------

_EXAMPLES_DIR = Path(__file__).parent.parent.parent / "examples"

_EXAMPLES_ARTIFACTS_BUILD = """\
version: 1
rocks:
- name: k8s-rock
  rockcraft-yaml: k8s-rock/rockcraft.yaml
  builds:
  - arch: amd64
    file: k8s-rock_amd64.rock
    image: ghcr.io/canonical/k8s-rock:latest
charms:
- name: machine-charm
  charmcraft-yaml: machine-charm/charmcraft.yaml
  builds:
  - arch: amd64
    path: machine-charm_ubuntu-24.04-amd64.charm
- name: k8s-charm
  charmcraft-yaml: k8s-charm/charmcraft.yaml
  builds:
  - arch: amd64
    path: k8s-charm_ubuntu-24.04-amd64.charm
snaps: []
"""


class TestExamplesEnvTemplate:
    """Verify that examples/spread.yaml's pytest-environment-template actually renders env vars.

    Uses the real examples/spread.yaml so that any change to the examples file
    is reflected here without having to update a separate test fixture.
    """

    def test_spread_job_rendered_from_examples_spread_yaml(self, tmp_path: Path) -> None:
        """SPREAD_JOB from env is rendered into KEY=VALUE by the examples template."""
        shutil.copy(_EXAMPLES_DIR / "spread.yaml", tmp_path / "spread.yaml")
        write_file(tmp_path / "artifacts.build.yaml", _EXAMPLES_ARTIFACTS_BUILD)
        (tmp_path / "tests" / "integration").mkdir(parents=True)
        (tmp_path / "tests" / "integration" / "test_k8s_charm.py").touch()
        (tmp_path / "tests" / "integration" / "test_machine_charm.py").touch()

        suite_cfg = get_suite_config(tmp_path, suite="tests/integration/")
        env_template = suite_cfg.get("pytest-environment-template")
        assert isinstance(env_template, str), (
            "examples/spread.yaml missing pytest-environment-template"
        )

        with patch.dict(
            "os.environ",
            {"SPREAD_JOB": "integration-test:ubuntu-24.04:tests/integration/run:test_k8s_charm"},
        ):
            result = render_environment_template(tmp_path, env_template)

        assert "SPREAD_JOB" in result
        assert (
            result["SPREAD_JOB"]
            == "integration-test:ubuntu-24.04:tests/integration/run:test_k8s_charm"
        )
