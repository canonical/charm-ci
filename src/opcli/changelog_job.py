# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""GitHub Actions entry point for generating a read-only release draft."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from pydantic import ValidationError as ModelError

from opcli.core.changelog_collect import collect
from opcli.core.changelog_git import GitHistory
from opcli.core.changelog_github import GitHub
from opcli.core.changelog_manifest import canonical, digest, idempotency_key, verify_range
from opcli.core.changelog_normalize import normalize
from opcli.core.changelog_render import Format, render
from opcli.core.exceptions import OpcliError, ValidationError
from opcli.models.changelog import Boundary, ReleaseRange, RunManifest


class RangeInputError(ValidationError):
    """A fixed diagnostic safe to show in workflow logs."""


def main() -> int:
    """Run only against the repository and generator identity set by Actions."""
    out = Path(os.environ["CHANGELOG_OUTPUT_DIR"])
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise ValidationError("changelog output directory must be empty")
    release = None
    result_url = (
        f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/"
        f"{os.environ['GITHUB_RUN_ID']}"
    )
    try:
        if os.environ.get("OPEN_PR") == "true":
            raise ValidationError("PR creation is disabled in this dry-run pilot")
        api = GitHub(os.environ.get("GH_TOKEN", ""))
        raw_format = os.environ.get("OUTPUT_FORMAT", "md")
        if raw_format not in ("md", "rst"):
            raise ValidationError("output_format must be md or rst")
        output_format = cast("Format", raw_format)
        release = _load_range(api, output_format)
        generate(
            release,
            api,
            out,
            output_format,
            result_url,
            token=os.environ.get("GH_TOKEN", ""),
            allow_full=os.environ.get("ALLOW_FULL_HISTORY") == "true",
        )
    except (OpcliError, ModelError, ValueError, OSError) as exc:
        error = (
            str(exc)
            if isinstance(exc, RangeInputError)
            else "Changelog generation stopped; no release page was published"
        )
        # Exceptions can contain raw API data, so keep the public diagnostic bounded.
        failure: dict[str, object] = {
            "retrieval": "failed",
            "warnings": [error],
            "output_hash": None,
            "result_url": result_url,
            "generator": os.environ.get("GENERATOR_SHA"),
            "schema_version": 1,
            "renderer_version": 1,
        }
        if release is not None:
            failure.update(
                {"range_hash": digest(release), "idempotency_key": idempotency_key(release)}
            )
        (out / "run.json").write_bytes(canonical(failure))
        print(error)
        return 1
    return 0


def generate(  # noqa: PLR0913
    release: ReleaseRange,
    api: GitHub,
    out: Path,
    output_format: Format,
    result_url: str,
    *,
    token: str = "",
    allow_full: bool = False,
) -> None:
    """Persist the range, generate against SHAs, then recheck the remote state."""
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (out / "range.json").open("xb") as handle:
        handle.write(canonical(release))
    expected_hash = digest(release)

    def resolve(ref: str) -> Boundary:
        return api.boundary(release.repository, ref)

    verify_range(release, expected_hash, resolve)
    with TemporaryDirectory(prefix="changelog-history-") as path:
        snapshot = collect(release, api, GitHistory(Path(path), token), allow_full=allow_full)
    entries, exclusions = normalize(release, snapshot)
    body = render(release, entries, output_format).encode()
    verify_range(release, expected_hash, resolve)
    entry_payload: dict[str, object] = {
        "entries": [entry.model_dump(mode="json") for entry in entries]
    }
    report = RunManifest(
        range_hash=expected_hash,
        idempotency_key=idempotency_key(release),
        generator=release.generator,
        retrieval=snapshot.retrieval,
        commits=len(snapshot.sources),
        pull_requests=len({pr.number for source in snapshot.sources for pr in source.prs}),
        unclassified=sum(entry.category == "Unclassified" for entry in entries),
        warnings=snapshot.warnings
        + tuple(warning for entry in entries for warning in entry.warnings),
        exclusions=exclusions,
        metadata_hash=digest(snapshot),
        entries_hash=digest(entry_payload),
        output_hash=digest(body),
        result_url=result_url,
    )
    (out / "run.json").write_bytes(canonical(report))
    (out / f"release.{output_format}").write_bytes(body)


def _load_range(api: GitHub, output_format: Format) -> ReleaseRange:
    repo = os.environ["GITHUB_REPOSITORY"]
    repository_id = int(os.environ["GITHUB_REPOSITORY_ID"])
    generator = os.environ["GENERATOR_SHA"]
    raw = os.environ.get("RANGE_MANIFEST", "")
    manifest_hash = os.environ.get("RANGE_MANIFEST_HASH", "")
    from_ref, to_ref, source_branch, release_version, release_date = ref_inputs = tuple(
        os.environ.get(key, "")
        for key in ("FROM_REF", "TO_REF", "SOURCE_BRANCH", "RELEASE_VERSION", "RELEASE_DATE")
    )
    config_hash = digest({"format": output_format, "date_heading": False})
    if raw or manifest_hash:
        if not (raw and manifest_hash) or any(ref_inputs):
            raise RangeInputError(
                "Provide both range_manifest and range_manifest_hash without any ref-mode inputs"
            )
        release = ReleaseRange.model_validate_json(raw)
        if (
            release.repository != repo
            or release.repository_id != repository_id
            or release.generator != generator
            or release.config_hash != config_hash
            or digest(release) != manifest_hash
        ):
            raise ValidationError("provided release manifest does not match the workflow inputs")
        return release

    if not all(ref_inputs):
        raise RangeInputError(
            "Ref mode requires from_ref, to_ref, source_branch, release_version, and release_date"
        )
    return ReleaseRange(
        repository=repo,
        repository_id=repository_id,
        previous=api.boundary(repo, from_ref),
        current=api.boundary(repo, to_ref),
        source_tip=api.boundary(repo, source_branch),
        release=release_version,
        release_date=release_date,
        generator=generator,
        config_hash=config_hash,
    )


if __name__ == "__main__":
    raise SystemExit(main())
