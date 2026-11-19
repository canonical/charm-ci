# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest

from opcli.core.changelog_github import GitHub, verify_comparison
from opcli.core.exceptions import ValidationError


def test_repeated_api_page_is_not_treated_as_complete(mocker):
    api = GitHub("")
    mocker.patch.object(api, "request", return_value=(200, [{"id": 1}] * 100, {}))
    with pytest.raises(ValidationError, match="repeated"):
        api.pages("/repos/example/charm/pulls")


@pytest.mark.parametrize("sha_list", [("b" * 40,), ("a" * 40, "a" * 40), ("b" * 40, "a" * 40)])
def test_compare_rejects_incomplete_or_inconsistent_history(sha_list):
    base = "0" * 40
    head = "b" * 40
    record = {
        "status": "ahead",
        "merge_base_commit": {"sha": base},
        "total_commits": 2,
        "ahead_by": 2,
    }
    with pytest.raises(ValidationError, match="Compare"):
        verify_comparison(record, base, head, sha_list)


def test_compare_can_prove_the_exact_identity_and_count():
    base, head = "a" * 40, "b" * 40
    assert verify_comparison(
        {"status": "ahead", "merge_base_commit": {"sha": base}, "total_commits": 1, "ahead_by": 1},
        base,
        head,
        (head,),
    ) == (head,)


def test_annotated_tag_keeps_both_object_ids(mocker):
    api = GitHub("")
    ref = "refs/tags/v1"
    raw = "a" * 40
    commit = "b" * 40
    mocker.patch.object(
        api,
        "request",
        side_effect=[
            (200, {"ref": ref, "object": {"sha": raw, "type": "tag"}}, {}),
            (200, {"object": {"sha": commit, "type": "commit"}}, {}),
        ],
    )
    result = api.boundary("canonical/example", ref)
    assert result.object_id == raw
    assert result.commit == commit


def test_transient_api_failure_retries_without_redirecting_credentials(mocker):
    mocker.patch("opcli.core.changelog_github.time.sleep")
    connection = mocker.patch("opcli.core.changelog_github.http.client.HTTPSConnection")
    fake = connection.return_value
    failure = mocker.Mock(status=429)
    failure.getheaders.return_value = [("Retry-After", "1")]
    failure.read.return_value = b"{}"
    success = mocker.Mock(status=200)
    success.getheaders.return_value = []
    success.read.return_value = b'{"ok": true}'
    fake.getresponse.side_effect = [failure, success]
    assert GitHub("fixture").get("/repos/canonical/example") == {"ok": True}
    assert connection.call_args_list == [mocker.call("api.github.com", timeout=30)] * 2


def test_pagination_refuses_external_continuation(mocker):
    api = GitHub("")
    request = mocker.patch.object(
        api,
        "request",
        return_value=(
            200,
            [{"id": 1}],
            {"link": '<https://example.invalid/?page=2>; rel="next"'},
        ),
    )
    with pytest.raises(ValidationError, match="invalid GitHub API continuation"):
        api.pages("/repos/canonical/example/pulls")
    assert request.call_count == 1
