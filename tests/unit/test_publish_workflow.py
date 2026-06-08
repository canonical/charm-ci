# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the publish-artifacts reusable workflow contract."""

from pathlib import Path
from typing import cast

from ruamel.yaml import YAML


def test_publish_workflow_channel_input_is_optional() -> None:
    workflow = _load_workflow()
    channel_input = cast(
        dict[str, object],
        cast(
            dict[str, object],
            cast(dict[str, object], workflow["on"])["workflow_call"],
        )["inputs"],
    )["channel"]
    assert channel_input["required"] is False


def test_publish_workflow_conditionally_passes_channel() -> None:
    workflow = _load_workflow()
    jobs = cast(dict[str, object], workflow["jobs"])
    publish_job = cast(dict[str, object], jobs["publish"])
    steps = cast(list[dict[str, object]], publish_job["steps"])
    publish_step = next(step for step in steps if step.get("name") == "Publish to CharmHub")

    assert publish_step["env"]["INPUT_CHANNEL"] == "${{ inputs.channel }}"
    assert "CHANNEL_ARGS=()" in publish_step["run"]
    assert 'if [ -n "${INPUT_CHANNEL}" ]; then' in publish_step["run"]
    assert "opcli artifacts publish \\" in publish_step["run"]
    assert '"${CHANNEL_ARGS[@]}"' in publish_step["run"]
    assert '"${DRY_RUN_ARGS[@]}" > publish-results.json' in publish_step["run"]


def test_example_publish_workflow_channel_input_is_optional() -> None:
    workflow = _load_workflow(Path("examples/.github/workflows/publish.yaml"))
    channel_input = cast(
        dict[str, object],
        cast(
            dict[str, object],
            cast(dict[str, object], workflow["on"])["workflow_dispatch"],
        )["inputs"],
    )["channel"]
    assert channel_input["required"] is False


def _load_workflow(path: Path | None = None) -> dict[str, object]:
    yaml = YAML(typ="safe")
    yaml.version = (1, 2)
    workflow_path = path or Path(".github/workflows/publish-artifacts.yml")
    if not workflow_path.is_absolute():
        workflow_path = Path(__file__).resolve().parents[2] / workflow_path
    with workflow_path.open() as file:
        data = yaml.load(file)
    assert isinstance(data, dict)
    return data
