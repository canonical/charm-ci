# Changelog

All notable changes to `opcli` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Until version `1.0.0`, this project does not yet follow strict [Semantic
Versioning](https://semver.org/) — see [Versioning policy](README.md#versioning-policy)
in the README for what "alpha" currently guarantees (and doesn't).

Entries for releases prior to `v0.0.1-alpha.9` are not backfilled here; see the
[GitHub Releases](https://github.com/canonical/charm-ci/releases) page and
`git log` for that history.

## [Unreleased]

## [v1.0.0] - 2026-08-20

The first official stable release of `opcli`, and its first publication to
PyPI.

### Added

- `opcli` is now published to PyPI as
  [`charm-opcli`](https://pypi.org/project/charm-opcli/) (the plain `opcli`
  name was already taken by an unrelated package) via a new,
  manually-triggered `.github/workflows/publish-opcli-pypi.yml` workflow
  (run against a release tag after the GitHub Release is cut), using PyPI
  Trusted Publishing (OIDC) — no API token is stored in this repository.
  `pip install`/`uv tool install "charm-opcli[cli]"` now works without a
  `git+https://...` source; the installed command and `import opcli` are
  unchanged.

### Changed

- **Breaking:** Project status is now `Development Status :: 5 -
  Production/Stable` (was `3 - Alpha`). From this release onward, breaking
  changes to the CLI, `artifacts.yaml`/`artifacts.build.yaml` schemas,
  `spread.yaml` virtual-backend keys, or reusable-workflow inputs require a
  major version bump per the [Versioning policy](README.md#versioning-policy).

## [v0.0.1-alpha.10] - 2026-08-10

This is expected to be the last `alpha` release before `1.0.0`, pending no
new issues surfacing. See [Versioning policy](README.md#versioning-policy).

### Changed

- **Breaking:** `artifacts.build.yaml` and the expanded `spread.yaml`/`task.yaml`
  tree now live under `build/` instead of the project root. Update any
  external tooling/scripts that read `artifacts.build.yaml` directly from the
  project root. (#118)
- GitHub Release creation on `opcli artifacts publish` is now optional
  (previously always created); per-arch/base fan-out in publish output was
  also reduced. (#122, #129)
- `opcli spread jobs` output is now sorted for deterministic CI matrix
  ordering. (#119)
- Renamed the `concierge-microk8s.yaml` example to `concierge-k8s.yaml` and
  switched it to the canonical `k8s` provider. (#124)
- CI's `test` job now runs against an explicit Python `3.12`/`3.13` matrix
  instead of relying on `uv`'s default interpreter resolution.

### Fixed

- Fixed a false-positive transport-prefix detection in `upload-resource` for
  `host:port`-style OCI references. (#120)
- `opcli artifacts publish` now retries known-transient CharmHub upload
  failures (connection resets, upload-status polling timeouts) with
  exponential backoff. (#115)
- `base: null` no longer appears in `artifacts.build.yaml` for charms that use
  `charmcraft.yaml`'s `base:` field with arch-only `platforms:` keys (e.g.
  `platforms: {amd64:}`); the base is now read from `charmcraft.yaml` as a
  fallback when it cannot be parsed from the packed filename. (#107)
- Fixed `pyproject.toml`/`__init__.py`'s hardcoded version string (stuck at
  `0.0.1-alpha.2` since alpha.2 despite 8 subsequent tagged releases);
  `__version__` is now derived from installed package metadata.

### Added

- Support for GitHub Environment-scoped secrets via an optional `environment`
  input on `integration-test.yml` and `publish-artifacts.yml`, forwarded to
  the relevant jobs. (#123)
- `opcli --version` flag.
- `--verbose`/`-v` global flag to surface internal `logger.info()` output
  (previously invisible by default).
- Complete PyPI-style package metadata (`readme`, `license`, `authors`,
  `keywords`, `classifiers`) in `pyproject.toml`. (#131)

### Docs

- Documented the `TOX_ENV` environment variable for overriding the default
  tox environment. (#87)
- Clarified `AI agents must never merge PRs` policy in `AGENTS.md`.
- Added a [Versioning policy](README.md#versioning-policy) section to
  `README.md` and this `CHANGELOG.md`.

## [v0.0.1-alpha.9] - 2026-07-13

See [GitHub Releases](https://github.com/canonical/charm-ci/releases/tag/v0.0.1-alpha.9).

[Unreleased]: https://github.com/canonical/charm-ci/compare/v1.0.0...HEAD
[v1.0.0]: https://github.com/canonical/charm-ci/releases/tag/v1.0.0
[v0.0.1-alpha.10]: https://github.com/canonical/charm-ci/releases/tag/v0.0.1-alpha.10
[v0.0.1-alpha.9]: https://github.com/canonical/charm-ci/releases/tag/v0.0.1-alpha.9
