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

### Docs

- Documented the `TOX_ENV` environment variable for overriding the default
  tox environment. (#87)
- Clarified `AI agents must never merge PRs` policy in `AGENTS.md`.

## [v0.0.1-alpha.9] - 2026-07-13

See [GitHub Releases](https://github.com/canonical/charm-ci/releases/tag/v0.0.1-alpha.9).

[Unreleased]: https://github.com/canonical/charm-ci/compare/v0.0.1-alpha.9...HEAD
[v0.0.1-alpha.9]: https://github.com/canonical/charm-ci/releases/tag/v0.0.1-alpha.9
