# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Exercise lazy fixture preparation through the real fetch/push implementation."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from opcli import pytest_plugin
from opcli.core import artifacts as artifacts_core
from opcli.core.constants import artifacts_build_path
from opcli.core.exceptions import ConfigurationError, SubprocessError
from opcli.core.spread import (
    _CI_PREPARE_AFTER_USER,
    _LOCAL_PREPARE_BEFORE_USER,
    _TASK_YAML_CONTENT,
    _TASK_YAML_CONTENT_SUITE,
    spread_expand,
)
from opcli.core.subprocess import SubprocessResult, run_command
from opcli.core.template import render_arguments_template, render_environment_template
from opcli.core.yaml_io import loads_yaml
from tests.conftest import write_file

_PREPARATION_COMMANDS = 5

_PLAN = """\
version: 1
charms:
  - name: app
    charmcraft-yaml: charmcraft.yaml
    platforms:
      - arch: amd64
    resources:
      app-image:
        type: oci-image
        rock: app
rocks:
  - name: app
    rockcraft-yaml: rockcraft.yaml
    platforms:
      - arch: amd64
"""
_CHARM = """\
version: 1
charms:
  - name: app
    charmcraft-yaml: charmcraft.yaml
    resources:
      app-image:
        type: oci-image
        rock: app
    builds:
      - arch: amd64
        artifact: app-charm
        run-id: '123'
"""
_ROCK = """\
version: 1
rocks:
  - name: app
    rockcraft-yaml: rockcraft.yaml
    builds:
      - arch: amd64
        artifact: app-rock
        run-id: '123'
        file: app.rock
"""


@pytest.fixture
def deferred_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "OPCLI_DEFER_ARTIFACTS": "1",
        "GITHUB_ACTIONS": "true",
        "GITHUB_RUN_ID": "123",
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_TOKEN": "test-token-not-a-secret",
        "OPCLI_FETCH_WAIT_TIMEOUT": "60",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("OPCLI_ARTIFACTS_BUILD_YAML", raising=False)


@pytest.fixture
def artifact_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    write_file(tmp_path / "artifacts.yaml", _PLAN)
    write_file(tmp_path / ".git", "gitdir: irrelevant\n")
    monkeypatch.setattr("opcli.core.env.platform.machine", lambda: "x86_64")
    return tmp_path


def _config(root: Path, **options: object) -> pytest.Config:
    config = MagicMock(spec=pytest.Config)
    config.rootpath = root
    config.stash = pytest.Stash()
    config.getoption.side_effect = lambda name, default=None: options.get(name, default)
    return config


def _request(config: pytest.Config) -> pytest.FixtureRequest:
    request = MagicMock(spec=pytest.FixtureRequest)
    request.config = config
    return request


def _fake_commands(
    monkeypatch: pytest.MonkeyPatch, *, remote_image: bool = False
) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(cmd: list[str], **kwargs: object) -> SubprocessResult:
        calls.append(cmd)
        if cmd[:3] == ["gh", "run", "download"]:
            assert cmd[3] == "123"
            assert cmd[cmd.index("--repo") + 1] == "owner/repo"
            dest = Path(cmd[cmd.index("--dir") + 1])
            name = cmd[cmd.index("--name") + 1]
            if name.startswith("artifacts-build-"):
                content = _ROCK if "-rock-" in name else _CHARM
                if remote_image and "-rock-" in name:
                    content = content.replace("        artifact: app-rock\n", "").replace(
                        "        file: app.rock", "        image: ghcr.io/owner/app:current"
                    )
                write_file(dest / "artifacts.build.yaml", content)
            else:
                write_file(dest / ("app.rock" if name == "app-rock" else "app.charm"), "built")
        else:
            assert "copy" in cmd
            assert cmd[0] == "sudo"
        return SubprocessResult("", "", 0)

    monkeypatch.setattr("opcli.core.artifacts.run_command", run)
    monkeypatch.setattr("opcli.core.provision.run_command", run)
    monkeypatch.setattr("opcli.core.provision._is_port_open", lambda *a, **kw: True)
    monkeypatch.setattr("opcli.core.provision._skopeo_binary", lambda: "skopeo")
    return calls


@pytest.mark.usefixtures("deferred_env")
class TestDeferredFixtures:
    @pytest.mark.parametrize(
        "entry",
        [
            "opcli_build_yaml_path",
            "opcli_artifacts",
            "charm_path",
            "charm_paths",
            "resource_images",
            "charm_resource_images",
            "rock_images",
        ],
    )
    def test_every_entry_prepares_before_loading(
        self, artifact_project: Path, monkeypatch: pytest.MonkeyPatch, entry: str
    ) -> None:
        calls = _fake_commands(monkeypatch)
        config = _config(artifact_project)
        request = _request(config)
        assert not artifacts_build_path(artifact_project).exists()
        if entry in {"opcli_artifacts", "rock_images"}:
            path = pytest_plugin.opcli_build_yaml_path.__wrapped__(request)
            artifacts = pytest_plugin.opcli_artifacts.__wrapped__(path)
            result = (
                pytest_plugin.build_rock_images(artifacts, artifact_project)
                if entry == "rock_images"
                else artifacts
            )
        else:
            result = getattr(pytest_plugin, entry).__wrapped__(request)
        assert result
        assert len(calls) == _PREPARATION_COMMANDS
        assert "localhost:32000/app:" in artifacts_build_path(artifact_project).read_text()
        pytest_plugin.charm_paths.__wrapped__(request)
        images = pytest_plugin.charm_resource_images.__wrapped__(request)
        assert images["app"]["app-image"].startswith("localhost:32000/app:")
        assert len(calls) == _PREPARATION_COMMANDS

    def test_stale_manifest_is_not_readiness(
        self, artifact_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_file(artifacts_build_path(artifact_project), "not valid yaml: [")
        calls = _fake_commands(monkeypatch)
        paths = pytest_plugin.charm_paths.__wrapped__(_request(_config(artifact_project)))
        assert paths["app"].path == str(artifact_project / "app-charm" / "app.charm")
        assert len(calls) == _PREPARATION_COMMANDS

    def test_registry_images_do_not_require_local_push(
        self, artifact_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _fake_commands(monkeypatch, remote_image=True)
        images = pytest_plugin.charm_resource_images.__wrapped__(
            _request(_config(artifact_project))
        )
        assert images == {"app": {"app-image": "ghcr.io/owner/app:current"}}
        assert all(cmd[:3] == ["gh", "run", "download"] for cmd in calls)
        assert not any("app-rock" in cmd for cmd in calls)

    def test_nested_root_without_manifest(
        self, artifact_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_commands(monkeypatch)
        nested = artifact_project / "tests" / "integration"
        nested.mkdir(parents=True)
        assert pytest_plugin._discover_artifacts_build(_config(nested)) == artifacts_build_path(
            artifact_project
        )

    def test_new_pytest_session_fetches_again(
        self, artifact_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _fake_commands(monkeypatch)
        pytest_plugin._discover_artifacts_build(_config(artifact_project))
        pytest_plugin._discover_artifacts_build(_config(artifact_project))
        assert len(calls) == 2 * _PREPARATION_COMMANDS

    def test_missing_registry_is_error_not_readiness(
        self, artifact_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_commands(monkeypatch)
        monkeypatch.setattr("opcli.core.provision._is_port_open", lambda *a, **kw: False)
        monkeypatch.setattr("opcli.core.provision.shutil.which", lambda _: None)
        config = _config(artifact_project)
        with pytest.raises(pytest.UsageError, match="could not push"):
            pytest_plugin._discover_artifacts_build(config)
        _fake_commands(monkeypatch)
        pytest_plugin._discover_artifacts_build(config)

    def test_malformed_plan_can_be_corrected_and_retried(
        self, artifact_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _fake_commands(monkeypatch)
        config = _config(artifact_project)
        write_file(artifact_project / "artifacts.yaml", "version: 99\n")
        with pytest.raises(pytest.UsageError, match=r"artifacts.yaml"):
            pytest_plugin._discover_artifacts_build(config)
        assert not calls
        write_file(artifact_project / "artifacts.yaml", _PLAN)
        pytest_plugin._discover_artifacts_build(config)
        assert len(calls) == _PREPARATION_COMMANDS

    def test_archive_download_failure_propagates(
        self, artifact_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_commands(monkeypatch)
        original = artifacts_core.run_command

        def run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            if "app-charm" in cmd:
                raise SubprocessError(cmd=cmd, returncode=1, stderr="archive download failed")
            return original(cmd, **kwargs)

        monkeypatch.setattr("opcli.core.artifacts.run_command", run)
        with pytest.raises(pytest.UsageError, match="archive download failed"):
            pytest_plugin._discover_artifacts_build(_config(artifact_project))

    @pytest.mark.parametrize("override", ["cli", "env"])
    def test_explicit_manifest_bypasses_preparation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, override: str
    ) -> None:
        manifest = tmp_path / "custom.yaml"
        write_file(manifest, "version: 1\n")
        options = {"--artifacts-build-yaml": str(manifest)} if override == "cli" else {}
        if override == "env":
            monkeypatch.setenv("OPCLI_ARTIFACTS_BUILD_YAML", str(manifest))
        assert pytest_plugin._discover_artifacts_build(_config(tmp_path, **options)) == manifest

    def test_cli_artifacts_bypass_preparation(self, tmp_path: Path) -> None:
        charm = tmp_path / "app.charm"
        charm.touch()
        config = _config(
            tmp_path,
            **{"--charm-file": [f"app={charm}"], "--resource-image": ["img=registry/ref"]},
        )
        request = _request(config)
        assert pytest_plugin.charm_path.__wrapped__(request) == str(charm)
        assert pytest_plugin.charm_paths.__wrapped__(request)["app"].path == str(charm)
        assert pytest_plugin.resource_images.__wrapped__(request) == {"img": "registry/ref"}

    @pytest.mark.parametrize(
        ("variable", "value"),
        [
            ("GITHUB_RUN_ID", ""),
            ("GITHUB_RUN_ID", "not-a-run"),
            ("GITHUB_REPOSITORY", ""),
            ("GITHUB_REPOSITORY", "bad"),
            ("OPCLI_FETCH_WAIT_TIMEOUT", "-1"),
            ("OPCLI_FETCH_WAIT_TIMEOUT", "oops"),
            ("OPCLI_DEFER_ARTIFACTS", "yes"),
        ],
    )
    def test_invalid_configuration(
        self, artifact_project: Path, monkeypatch: pytest.MonkeyPatch, variable: str, value: str
    ) -> None:
        monkeypatch.setenv(variable, value)
        with pytest.raises(pytest.UsageError, match=variable):
            pytest_plugin._discover_artifacts_build(_config(artifact_project))

    def test_missing_plan_does_not_use_stale_manifest(self, tmp_path: Path) -> None:
        write_file(tmp_path / ".git", "gitdir: unused")
        write_file(artifacts_build_path(tmp_path), "version: 1\n")
        with pytest.raises(pytest.UsageError, match=r"artifacts.yaml"):
            pytest_plugin._discover_artifacts_build(_config(tmp_path))

    @pytest.mark.parametrize("stage", ["download", "push"])
    def test_failure_not_cached(
        self, artifact_project: Path, monkeypatch: pytest.MonkeyPatch, stage: str
    ) -> None:
        calls = _fake_commands(monkeypatch)
        target = (
            "opcli.core.artifacts.run_command"
            if stage == "download"
            else "opcli.core.provision.run_command"
        )

        def fail(cmd: list[str], **kwargs: object) -> SubprocessResult:
            raise SubprocessError(
                cmd=cmd, returncode=1, stderr="HTTP 403" if stage == "download" else "push failed"
            )

        monkeypatch.setattr(target, fail)
        config = _config(artifact_project)
        with pytest.raises(pytest.UsageError, match=r"HTTP 403|push failed"):
            pytest_plugin._discover_artifacts_build(config)
        if stage == "push":
            assert calls
        fresh_calls = _fake_commands(monkeypatch)
        pytest_plugin._discover_artifacts_build(config)
        assert len(fresh_calls) == _PREPARATION_COMMANDS

    @pytest.mark.parametrize("conclusion", ["failure", "cancelled", None])
    def test_fetch_failure_cancel_timeout(
        self, artifact_project: Path, monkeypatch: pytest.MonkeyPatch, conclusion: str | None
    ) -> None:
        monkeypatch.setenv("OPCLI_FETCH_WAIT_TIMEOUT", "1")
        monkeypatch.setattr("opcli.core.artifacts.time.sleep", lambda _: None)
        calls: list[list[str]] = []

        def run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            calls.append(cmd)
            if cmd[:3] == ["gh", "run", "view"]:
                jobs = [
                    {
                        "name": "Build rock app (amd64)",
                        "conclusion": conclusion,
                        "status": "completed" if conclusion else "in_progress",
                    }
                ]
                return SubprocessResult(json.dumps({"jobs": jobs}), "", 0)
            raise SubprocessError(cmd=cmd, returncode=1, stderr="no artifact matches")

        monkeypatch.setattr("opcli.core.artifacts.run_command", run)
        expected = conclusion or "Timed out"
        with pytest.raises(pytest.UsageError, match=expected):
            pytest_plugin._discover_artifacts_build(_config(artifact_project))
        assert any(cmd[:3] == ["gh", "run", "view"] for cmd in calls)

    def test_configured_wait_budget_reaches_existing_poller(
        self, artifact_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPCLI_FETCH_WAIT_TIMEOUT", "75")
        monkeypatch.setattr("opcli.core.artifacts.time.monotonic", lambda: 100.0)
        monkeypatch.setattr("opcli.core.artifacts.time.sleep", lambda _: None)
        downloads = []

        def run(cmd: list[str], **kwargs: object) -> SubprocessResult:
            if cmd[:3] == ["gh", "run", "view"]:
                return SubprocessResult('{"jobs": []}', "", 0)
            downloads.append(cmd)
            raise SubprocessError(cmd=cmd, returncode=1, stderr="not available yet")

        monkeypatch.setattr("opcli.core.artifacts.run_command", run)
        with pytest.raises(pytest.UsageError, match="after 75s"):
            pytest_plugin._discover_artifacts_build(_config(artifact_project))
        expected_attempts = 3
        assert len(downloads) == expected_attempts

    @pytest.mark.parametrize("timeout", [None, ""])
    def test_default_wait_budget(
        self, artifact_project: Path, monkeypatch: pytest.MonkeyPatch, timeout: str | None
    ) -> None:
        if timeout is None:
            monkeypatch.delenv("OPCLI_FETCH_WAIT_TIMEOUT")
        else:
            monkeypatch.setenv("OPCLI_FETCH_WAIT_TIMEOUT", timeout)
        _fake_commands(monkeypatch)
        pytest_plugin._discover_artifacts_build(_config(artifact_project))


@pytest.mark.parametrize(
    ("flag", "github_actions"), [("", "true"), ("0", "true"), ("1", ""), ("1", "false")]
)
def test_eager_and_local_discovery_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str, github_actions: str
) -> None:
    monkeypatch.setenv("OPCLI_DEFER_ARTIFACTS", flag)
    monkeypatch.setenv("GITHUB_ACTIONS", github_actions)
    monkeypatch.delenv("OPCLI_ARTIFACTS_BUILD_YAML", raising=False)
    manifest = artifacts_build_path(tmp_path)
    write_file(manifest, "version: 1\n")
    assert pytest_plugin._discover_artifacts_build(_config(tmp_path)) == manifest


@pytest.mark.usefixtures("deferred_env")
def test_env_only_templates_need_no_artifacts(tmp_path: Path) -> None:
    assert render_arguments_template(tmp_path, "--run={{ env.GITHUB_RUN_ID }}") == ["--run=123"]
    assert render_environment_template(tmp_path, "RUN={{ env.GITHUB_RUN_ID }}") == {"RUN": "123"}
    assert render_arguments_template(tmp_path, "--label=artifacts") == ["--label=artifacts"]
    assert render_arguments_template(
        tmp_path, "{% set artifacts = 'local variable' %}--label='{{ artifacts }}'"
    ) == ["--label=local variable"]


@pytest.mark.usefixtures("deferred_env")
@pytest.mark.parametrize("stale", [False, True])
def test_artifact_templates_rejected_even_with_stale_manifest(tmp_path: Path, stale: bool) -> None:
    if stale:
        write_file(artifacts_build_path(tmp_path), "version: 1\n")
    with pytest.raises(ConfigurationError, match="OPCLI_DEFER_ARTIFACTS"):
        render_arguments_template(tmp_path, "{{ artifacts.charms }}")
    with pytest.raises(ConfigurationError, match="OPCLI_DEFER_ARTIFACTS"):
        render_environment_template(tmp_path, "CHARM={{ artifacts.charms }}")


@pytest.mark.parametrize(
    ("flag", "github_actions", "expected"),
    [("", "true", True), ("0", "true", True), ("1", "true", False), ("1", "", True)],
)
def test_ci_prepare_eager_default_and_deferred_skip(
    tmp_path: Path, flag: str, github_actions: str, expected: bool
) -> None:
    script = (
        """\
opcli() { printf '%s\\n' "$*"; }
chown() { printf 'ownership\\n'; }
"""
        + _CI_PREPARE_AFTER_USER
    )
    result = run_command(
        ["bash", "-eu"],
        stdin=script,
        stream=False,
        env={
            "OPCLI_DEFER_ARTIFACTS": flag,
            "GITHUB_ACTIONS": github_actions,
            "GITHUB_RUN_ID": "123",
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_TOKEN": "",
            "OPCLI_FETCH_WAIT_TIMEOUT": "2700",
            "SPREAD_PATH": str(tmp_path),
        },
    )
    assert ("artifacts fetch" in result.stdout) == expected
    assert ("push-images" in result.stdout) == expected
    assert "ownership" in result.stdout
    if expected:
        assert "--wait-timeout 2700" in result.stdout
    assert "OPCLI_DEFER_ARTIFACTS" not in _LOCAL_PREPARE_BEFORE_USER
    assert "push-images" in _LOCAL_PREPARE_BEFORE_USER


@pytest.mark.parametrize("enabled", [False, True])
def test_ci_backend_injects_marker_only_for_opt_in(tmp_path: Path, enabled: bool) -> None:
    write_file(
        tmp_path / "spread.yaml",
        """\
project: test
path: /home/ubuntu/test
backends:
  tests:
    type: integration-test
    systems: [ubuntu-24.04]
    environment:
      OPCLI_DEFER_ARTIFACTS: '1'
suites:
  tests/:
    summary: test
""",
    )
    if not enabled:
        source = tmp_path / "spread.yaml"
        source.write_text(source.read_text().replace("OPCLI_DEFER_ARTIFACTS: '1'", "OTHER: value"))
    ci = loads_yaml(spread_expand(tmp_path, ci=True))
    local = loads_yaml(spread_expand(tmp_path, ci=False))
    assert ci["backends"]["tests-ci"]["environment"].get("GITHUB_ACTIONS") == (
        "true" if enabled else None
    )
    assert "GITHUB_ACTIONS" not in local["backends"]["tests-local"]["environment"]


@pytest.mark.parametrize("task_yaml", [_TASK_YAML_CONTENT, _TASK_YAML_CONTENT_SUITE])
def test_generated_task_preserves_allowlist_without_token_in_command(
    tmp_path: Path, task_yaml: str, capsys: pytest.CaptureFixture[str]
) -> None:
    token = """synthetic-'token;$(touch forbidden)-"$value"""
    tox = tmp_path / "tox"
    write_file(
        tox,
        """#!/usr/bin/env python3
import os
assert os.environ["GITHUB_TOKEN"] == """
        + repr(token)
        + """
assert os.environ["HOME"] == "/home/ubuntu"
assert os.environ["OPCLI_DEFER_ARTIFACTS"] == "1"
assert os.environ["GITHUB_ACTIONS"] == "true"
assert os.environ["GITHUB_RUN_ID"] == "123"
assert os.environ["GITHUB_REPOSITORY"] == "owner/repo"
assert os.environ["OPCLI_FETCH_WAIT_TIMEOUT"] == "2700"
assert os.environ["OPCLI_PACKAGE"] == "opcli"
assert "UNRELATED_SECRET" not in os.environ
print("pytest started with correct environment")
""",
    )
    tox.chmod(0o755)
    # Emulate login's environment clearing, retaining only runuser's allowlist.
    script = """\
opcli() { printf 'tox -e integration\\n'; }
runuser() {
  test "$1" = -l && test "$2" = ubuntu
  test "$4" = -c
  case "$*" in *"$GITHUB_TOKEN"*) exit 20;; esac
  whitelist="${3#--whitelist-environment=}"
  IFS=, read -ra names <<< "$whitelist"
  forwarded=("PATH=$PATH" "HOME=/home/ubuntu")
  for name in "${names[@]}"; do
    if value=$(printenv "$name"); then
      forwarded+=("$name=$value")
    fi
  done
  env -i "${forwarded[@]}" bash -c "$5"
}
"""
    script += loads_yaml(task_yaml)["execute"]
    result = run_command(
        ["bash", "-eu"],
        stdin=script,
        stream=False,
        cwd=str(tmp_path),
        env={
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "GITHUB_TOKEN": token,
            "OPCLI_DEFER_ARTIFACTS": "1",
            "GITHUB_ACTIONS": "true",
            "GITHUB_RUN_ID": "123",
            "GITHUB_REPOSITORY": "owner/repo",
            "OPCLI_FETCH_WAIT_TIMEOUT": "2700",
            "UNRELATED_SECRET": "must-not-forward",
            "SPREAD_PATH": str(tmp_path),
            "OPCLI_SUITE": "tests/",
            "MODULE": "test_app.py",
        },
    )
    assert "pytest started with correct environment" in result.stdout
    assert token not in result.stdout + result.stderr + capsys.readouterr().err
    assert not (tmp_path / "forbidden").exists()
