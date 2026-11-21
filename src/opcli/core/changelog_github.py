# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Bounded GitHub API access for changelog data."""

import http.client
import json
import re
import time
from http import HTTPStatus
from urllib.parse import quote, urlsplit

from opcli.core.exceptions import ValidationError
from opcli.models.changelog import Boundary

_HOST = "api.github.com"
_PAGE_SIZE = 100
_MAX_PAGES = 100
_MAX_READ = 24 * 1024 * 1024
_TRANSIENT = {429, 500, 502, 503, 504}
_ATTEMPTS = (1, 2, 4)
_TAG_DEPTH = 8
_MAX_BACKOFF = 60
_SHA = re.compile(r"[a-f0-9]{40}")


class APIError(ValidationError):
    """GitHub did not return a usable response."""


class GitHub:
    """Use a fixed HTTPS endpoint; never send credentials to a continuation host."""

    def __init__(self, token: str) -> None:
        self._headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "charm-ci-changelog",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    def request(
        self, method: str, path: str, data: dict[str, object] | None = None
    ) -> tuple[int, object, dict[str, str]]:
        """Return response status, decoded data and normalized headers."""
        _check_path(path)
        payload = json.dumps(data).encode() if data is not None else None
        headers = self._headers | ({"Content-Type": "application/json"} if payload else {})
        for delay in (*_ATTEMPTS, 0):
            connection = http.client.HTTPSConnection(_HOST, timeout=30)
            try:
                connection.request(method, path, body=payload, headers=headers)
                response = connection.getresponse()
                status = response.status
                metadata = {k.lower(): v for k, v in response.getheaders()}
                raw = response.read(_MAX_READ + 1)
            except (OSError, http.client.HTTPException):
                if method == "GET" and delay:
                    time.sleep(delay)
                    continue
                raise APIError("GitHub API transport failed") from None
            finally:
                connection.close()
            if method == "GET" and delay and _try_again(status, metadata):
                time.sleep(_wait(delay, metadata))
                continue
            if len(raw) > _MAX_READ:
                raise APIError("GitHub API response exceeds the permitted size")
            try:
                decoded: object = json.loads(raw) if raw else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise APIError(f"GitHub API returned invalid JSON (HTTP {status})") from None
            return status, decoded, metadata
        raise AssertionError("unreachable")

    def get(self, path: str) -> object:
        """Read a successful API response."""
        status, value, _ = self.request("GET", path)
        if status != HTTPStatus.OK:
            raise APIError(f"GitHub API read failed (HTTP {status})")
        return value

    def pages(self, path: str, *, key: str | None = None) -> list[object]:
        """Retrieve every page or reject the response."""
        result: list[object] = []
        for data in self.page_records(path, key=key):
            rows = sequence(mapping(data).get(key)) if key is not None else sequence(data)
            result.extend(rows)
        return result

    def page_records(self, path: str, *, key: str | None = None) -> list[object]:
        """Return page envelopes, checking order, continuations and limits."""
        pages: list[object] = []
        seen: set[str] = set()
        separator = "&" if "?" in path else "?"
        for page in range(1, _MAX_PAGES + 1):
            next_path = f"{path}{separator}per_page={_PAGE_SIZE}&page={page}"
            status, data, headers = self.request("GET", next_path)
            if status != HTTPStatus.OK:
                raise APIError(f"GitHub API page failed (HTTP {status})")
            rows = sequence(mapping(data).get(key)) if key is not None else sequence(data)
            fingerprint = json.dumps(rows, sort_keys=True)
            if rows and fingerprint in seen:
                raise APIError("GitHub API repeated a page")
            seen.add(fingerprint)
            pages.append(data)
            continuation = _continuation(headers.get("link", ""), path, page + 1)
            if not continuation and len(rows) < _PAGE_SIZE:
                return pages
        raise APIError("GitHub API pagination did not complete within the page limit")

    def boundary(self, repository: str, ref: str) -> Boundary:
        """Resolve a literal ref, including the original annotated tag object."""
        Boundary(ref=ref, object_id="0" * 40, commit="0" * 40)
        path = f"/repos/{repository}/git"
        response = mapping(self.get(f"{path}/ref/{quote(ref.removeprefix('refs/'))}"))
        if response.get("ref") != ref:
            raise APIError("GitHub did not resolve the exact requested ref")
        target = mapping(response.get("object"))
        original = text(target.get("sha"))
        for _ in range(_TAG_DEPTH):
            sha = text(target.get("sha"))
            if not _SHA.fullmatch(sha):
                break
            if target.get("type") == "commit":
                return Boundary(ref=ref, object_id=original, commit=sha)
            if target.get("type") != "tag":
                break
            target = mapping(mapping(self.get(f"{path}/tags/{sha}")).get("object"))
        raise APIError("GitHub ref does not resolve to a commit")


def verify_comparison(
    record: dict[str, object], base: str, head: str, commits: tuple[str, ...]
) -> tuple[str, ...]:
    """Require a complete Compare commit list with a verified ancestor base."""
    merge_base = mapping(record.get("merge_base_commit"))
    expected_status = "identical" if base == head else "ahead"
    if (
        merge_base.get("sha") != base
        or record.get("status") != expected_status
        or record.get("total_commits") != len(commits)
        or record.get("ahead_by") != len(commits)
        or (commits[-1] if commits else base) != head
        or len(set(commits)) != len(commits)
        or base in commits
        or not all(_SHA.fullmatch(sha) for sha in commits)
    ):
        raise APIError("GitHub Compare result does not prove a complete ancestor range")
    return commits


def mapping(value: object) -> dict[str, object]:
    """Require a GitHub JSON object."""
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise APIError("invalid GitHub API object")
    return value


def sequence(value: object) -> list[object]:
    """Require a GitHub JSON array."""
    if not isinstance(value, list):
        raise APIError("invalid GitHub API array")
    return value


def text(value: object) -> str:
    """Require a GitHub JSON string."""
    if not isinstance(value, str):
        raise APIError("invalid GitHub API text")
    return value


def _check_path(path: str) -> None:
    url = urlsplit(path)
    if url.scheme or url.netloc or not url.path.startswith("/") or url.fragment:
        raise APIError("invalid GitHub API path")


def _continuation(header: str, path: str, next_page: int) -> bool:
    for link in header.split(","):
        if 'rel="next"' not in link:
            continue
        match = re.fullmatch(r'\s*<([^>]+)>;\s*rel="next"\s*', link)
        if not match:
            raise APIError("invalid GitHub API continuation")
        parsed = urlsplit(match[1])
        # GitHub sometimes returns a different canonical path (e.g. /repositories/ID/).
        # Fetch the original endpoint with the page number rather than following it.
        if (
            parsed.scheme != "https"
            or parsed.netloc != _HOST
            or parsed.fragment
            or not re.search(rf"(?:^|&)page={next_page}(?:&|$)", parsed.query)
        ):
            raise APIError(f"invalid GitHub API continuation for {urlsplit(path).path}")
        return True
    return False


def _try_again(status: int, headers: dict[str, str]) -> bool:
    return status in _TRANSIENT or (
        status == HTTPStatus.FORBIDDEN
        and (headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers)
    )


def _wait(delay: int, headers: dict[str, str]) -> int:
    retry = headers.get("retry-after", "")
    reset = headers.get("x-ratelimit-reset", "")
    if retry.isdecimal():
        delay = max(delay, int(retry))
    elif headers.get("x-ratelimit-remaining") == "0" and reset.isdecimal():
        delay = max(delay, int(reset) - int(time.time()) + 1)
    if delay > _MAX_BACKOFF:
        raise APIError("GitHub requested a retry beyond the permitted backoff")
    return delay
