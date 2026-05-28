# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the --json flag on opcli artifacts publish."""

import json
from unittest.mock import patch

from typer.testing import CliRunner

from opcli.commands.artifacts import app
from opcli.core.publish import PublishResult, ReleaseEntry

runner = CliRunner()


def _fake_publish(*_args: object, **_kwargs: object) -> list[PublishResult]:
    return [
        PublishResult(
            charm_name="my-charm",
            channel="latest/edge",
            releases=[ReleaseEntry(revision=12, base="ubuntu@22.04", arch="amd64")],
            resources={"redis-image": 5},
        ),
    ]


class TestPublishJsonFlag:
    """Tests for --json output mode on the publish command."""

    @patch("opcli.commands.artifacts.artifacts_publish", side_effect=_fake_publish)
    def test_json_flag_outputs_json(self, mock_publish: object) -> None:
        result = runner.invoke(app, ["publish", "--channel", "latest/edge", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["charm_name"] == "my-charm"
        assert data[0]["channel"] == "latest/edge"
        assert data[0]["releases"][0]["revision"] == _fake_publish()[0].releases[0].revision
        assert data[0]["resources"] == {"redis-image": 5}

    @patch("opcli.commands.artifacts.artifacts_publish", side_effect=_fake_publish)
    def test_no_json_flag_outputs_human(self, mock_publish: object) -> None:
        result = runner.invoke(app, ["publish", "--channel", "latest/edge"])
        assert result.exit_code == 0
        assert "Published my-charm to latest/edge:" in result.output
        assert "rev 12" in result.output
        # Should NOT be valid JSON
        try:
            json.loads(result.output)
            msg = "Expected non-JSON output without --json flag"
            raise AssertionError(msg)
        except json.JSONDecodeError:
            pass

    @patch("opcli.commands.artifacts.artifacts_publish", return_value=[])
    def test_json_flag_empty_results(self, mock_publish: object) -> None:
        result = runner.invoke(app, ["publish", "--channel", "latest/edge", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.output) == []

    @patch("opcli.commands.artifacts.artifacts_publish", side_effect=_fake_publish)
    def test_json_flag_with_dry_run(self, mock_publish: object) -> None:
        result = runner.invoke(app, ["publish", "--channel", "latest/edge", "--json", "--dry-run"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["charm_name"] == "my-charm"
