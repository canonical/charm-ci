# opcli

A **local-first CLI tool** for Canonical operator developers to build charms, rocks, and snaps; manage test environments; and run integration tests — identically on a developer laptop and inside a CI job.

`opcli` replaces the monolithic [`operator-workflows`](https://github.com/canonical/operator-workflows) approach with a modular pipeline based on explicit build plans (`artifacts.yaml`), stable build output (`artifacts.build.yaml`), and [spread](https://github.com/canonical/spread)-based test execution.

## Documentation

| Document | Purpose |
|---|---|
| [docs/ISD283.md](docs/ISD283.md) | Functional specification |
| [AGENTS.md](AGENTS.md) | Developer guide for AI coding agents |
| [examples/](examples/) | Example project layout with `artifacts.yaml`, `spread.yaml`, and `concierge.yaml` |

## Installation

```bash
# With uv (recommended)
uv tool install git+https://github.com/canonical/charm-ci.git

# Or from a local clone
git clone https://github.com/canonical/charm-ci.git
cd charm-ci && uv tool install .

# Verify
opcli --help
```

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (package manager)
- [LXD](https://canonical.com/lxd) (for local spread testing)
- [spread](https://github.com/canonical/spread) (installed via `opcli install spread`)
- [concierge](https://github.com/canonical/concierge/) (for environment provisioning)

## Quick start

### Local testing with spread

```bash
opcli artifacts init     # discover charms/rocks/snaps → artifacts.yaml
opcli artifacts build    # build all → artifacts.build.yaml
opcli spread init        # generate spread.yaml + tests/integration/run/task.yaml
opcli spread expand      # preview expanded spread config
opcli spread run         # run integration tests (LXD backend)

# Target a specific test:
opcli spread run -- integration-test-local:ubuntu-24.04:tests/integration/run:test_charm
```

### Local testing without spread

```bash
opcli artifacts init
opcli artifacts build
opcli install tox              # install tox + tox-uv (if not already present)
sudo opcli env provision       # provision with concierge
opcli artifacts push-images --missing-registry deploy  # deploy registry + push rocks
opcli pytest run -- -k test_charm   # run tests via tox
```

## Commands

### `opcli artifacts`

| Command | Description |
|---|---|
| `init` | Discover charms/rocks/snaps and generate `artifacts.yaml`. `--force` to overwrite. |
| `build` | Build artifacts → `artifacts.build.yaml`. Filter: `--charm`, `--rock`, `--snap`. |
| `matrix` | Print JSON build matrix for GitHub Actions. |
| `collect <partial>...` | Merge partial `artifacts.build.yaml` from parallel jobs. |
| `fetch` | Download CI artifacts and rewrite to local paths. `--run-id` (required), `--repo`, `--wait`. |
| `localize` | Rewrite CI artifact refs to local paths (after manual download). |
| `push-images` | Load rock OCI images into a local registry. `-r` for registry (default: `localhost:32000`). `--missing-registry`: `skip` (default), `deploy` (auto-provision), or `fail`. |
| `path` | Print absolute path(s) to built artifacts. Optional `NAME` arg, `--type`, `--arch`. |

### `opcli install`

| Command | Description |
|---|---|
| `spread` | Install the spread test runner (no-op if already present). |
| `tox` | Install tox with tox-uv for running integration tests. |
| `concierge` | Install the concierge snap (no-op if already present). |

### `opcli env`

| Command | Description |
|---|---|
| `provision` | Run `concierge prepare` to provision the test environment. `-c` for concierge path. |
| `deploy-registry` | Deploy local OCI registry at `localhost:32000` (auto-detects k8s provider). |

### `opcli spread`

| Command | Description |
|---|---|
| `init` | Generate `spread.yaml` + `tests/integration/run/task.yaml`. `--force` to overwrite. |
| `expand` | Print fully expanded `spread.yaml` to stdout. |
| `run` | Expand virtual backend and run spread. Args after `--` forwarded verbatim. |
| `jobs` | Print CI test matrix JSON (one entry per spread task/variant). |

### `opcli pytest`

| Command | Description |
|---|---|
| `run` | Assemble and execute the tox integration test command. `-e` for env, `-m` for invocation mode, `--` forwards args. |
| `expand` | Print full `tox -e integration -- <flags>` command. `-e` for env, `-m` for invocation mode, `--` forwards args. |

Both commands accept `--invocation-mode` (`-m`) to override the pytest invocation mode without needing a `spread.yaml`:

```bash
opcli pytest run -m observability        # force observability mode
opcli pytest expand -m pfe -- -k test_x  # force pfe mode, filter tests
```

Precedence: `--invocation-mode` flag → `pytest-invocation-mode` in `spread.yaml` → default `pfe`.

### `opcli tutorial`

| Command | Description |
|---|---|
| `expand <file>` | Extract shell commands from a tutorial (`.md`/`.rst`) and print as a shell script for `eval`. |

## `artifacts.yaml` schema

```yaml
version: 1
rocks:
  - name: my-rock
    rockcraft-yaml: rocks/my-rock/rockcraft.yaml
    platforms:
      - arch: amd64
      - arch: arm64
        runner: [self-hosted, arm64]
charms:
  - name: my-charm
    charmcraft-yaml: charmcraft.yaml
    resources:
      my-rock-image:
        type: oci-image
        rock: my-rock
snaps:
  - name: my-snap
    snapcraft-yaml: snap/snapcraft.yaml
    pack-dir: .
```

Key fields:
- **`*-yaml`**: explicit path to the craft YAML file (not a directory).
- **`pack-dir`**: working directory for the build tool (defaults to the YAML's parent dir).
- **`platforms[].runner`**: GitHub Actions runner labels (used by `opcli artifacts matrix`; defaults to `["ubuntu-latest"]` at matrix generation time when omitted).

## `spread.yaml` virtual backends

opcli recognises virtual backend types (`integration-test`, `tutorial`) and expands them into concrete spread backends at runtime:

```yaml
backends:
  integration-test:
    type: integration-test
    pytest-invocation-mode: observability  # optional; default: pfe
    systems:
      - ubuntu-24.04:
          runner: [self-hosted, noble]   # CI runner labels
          cpu: 4                         # local LXD VM vCPUs
          memory: 8                      # local LXD VM RAM (GiB)
          disk: 20                       # local LXD VM disk (GiB)
```

- Locally (`CI` unset): expands to `integration-test-local` with an LXD backend.
- In CI (`CI=true`): expands to `integration-test-ci` with an adhoc backend targeting the current runner.

The `runner`, `cpu`, `memory`, `disk`, and `pytest-invocation-mode` fields are opcli-only metadata — they are stripped before spread sees the YAML.

### `pytest-invocation-mode`

Controls how `opcli pytest run` and `opcli pytest expand` pass built artifacts to the test framework:

| Value | Behaviour |
|---|---|
| `pfe` (default) | Passes `--charm-file=<path>` and `--<rock>-image=<ref>` as CLI flags. Supports multi-charm, multi-rock repos. |
| `observability` | Sets `CHARM_PATH` environment variable. Assumes single charm, no rocks. Errors if >1 charm found. |

When absent, defaults to `pfe`. This key is read at runtime by `opcli pytest run/expand`, so it works both inside spread and when running directly on the developer's machine. It can also be overridden via the `--invocation-mode` (`-m`) CLI flag, which takes precedence over the `spread.yaml` value.

## CI vs local

| Env var | Controls | Local | CI |
|---|---|---|---|
| `CI` | Spread backend expansion | `*-local` (LXD VM) | `*-ci` (current runner) |
| `GITHUB_ACTIONS` | Artifact output format | Local file paths | GHCR images + artifact refs |
| `OPCLI_ROCK_UPLOAD` | Rock build output mode | — (not set) | `registry` (push to GHCR) or `artifact` (upload `.rock` as GH artifact, for fork PRs) |
| `OPCLI_GIT_REF` | opcli version inside spread VM | defaults to `main` | set by workflow |

## GitHub Actions reusable workflows

Two reusable workflows are available for operator repositories:

| Workflow | Purpose |
|---|---|
| `build-artifacts.yml` | Build matrix generation, parallel artifact builds, merged `artifacts.build.yaml` |
| `integration-test.yml` | Download artifacts, generate spread task matrix, run integration tests |

Example usage:

```yaml
jobs:
  build:
    uses: canonical/charm-ci/.github/workflows/build-artifacts.yml@main
    permissions:
      contents: read
      packages: write
      actions: read
    with:
      working-directory: .
      # upload-image: artifact  # uncomment for fork PRs (no GHCR push)

  test:
    needs: build
    uses: canonical/charm-ci/.github/workflows/integration-test.yml@main
    secrets: inherit
    with:
      working-directory: .
```

Pinning to a SHA or tag automatically installs the matching `opcli` version via `canonical/get-workflow-version-action`.

### Fork PR support

When a pull request comes from a fork, the `GITHUB_TOKEN` is read-only and cannot push OCI images to GHCR. The `build-artifacts.yml` workflow handles this automatically:

1. **Fork detection** — checks `github.event.pull_request.head.repo.fork` and sets `OPCLI_ROCK_UPLOAD=artifact`.
2. **Artifact mode** — the `.rock` file is uploaded as a GitHub Actions artifact instead of being pushed to GHCR.
3. **Test phase** — `opcli artifacts fetch` downloads the `.rock` artifact, `opcli artifacts localize` rewrites paths, and `opcli artifacts push-images --missing-registry deploy` provisions a local registry and pushes the rock there.

To manually test the fork path, pass `upload-image: artifact` to `build-artifacts.yml` (or use `workflow_dispatch` if configured).

## Secrets for integration tests

Integration tests often need secrets (cloud credentials, API tokens, etc.).
opcli supports this identically locally and in CI.

### Locally: `.secrets.env`

Create a `.secrets.env` file in your repo root (gitignored) with plain `KEY=VALUE` pairs:

```env
# .secrets.env — never commit this file
S3_ACCESS_KEY=AKIAIOSFODNN7EXAMPLE
DATABASE_URL=postgres://user:pass@host/db
```

opcli auto-loads this file before running spread (local mode only), so no manual `export` is needed.

### In `spread.yaml`

Declare the secrets as spread environment variables using the `$(HOST: echo ...)` pattern:

```yaml
environment:
  S3_ACCESS_KEY: '$(HOST: echo "${S3_ACCESS_KEY:-}")'
  DATABASE_URL: '$(HOST: echo "${DATABASE_URL:-}")'
```

This self-documents what secrets your test suite requires.

### In CI: workflow inputs

Pass secret names to the reusable workflow via `test-secret-{1..5}-name` inputs:

```yaml
jobs:
  integration-test:
    uses: canonical/charm-ci/.github/workflows/integration-test.yml@main
    secrets: inherit
    with:
      test-secret-1-name: S3_ACCESS_KEY
      test-secret-2-name: DATABASE_URL
```

The workflow resolves values from your repository's GitHub Secrets, masks them with `::add-mask::`, and exports them to the environment before spread runs.

> **Note:** Running `opcli spread run -- -vv` locally will print secret values to the terminal (spread's verbose mode). This is acceptable for a local dev environment. In CI, GitHub Actions log masking covers all output.

## Development

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                    # install deps
uv run opcli --help                        # run the tool
uv run ruff check src/ tests/              # lint
uv run ruff format --check src/ tests/     # format check
uv run mypy src/                           # type check
uv run pytest tests/unit/                  # unit tests
```

### Project structure

```
src/opcli/
  commands/    # CLI layer (Typer) — parses args, delegates to core/
  core/        # All business logic
  models/      # Pydantic V2 models (artifacts.yaml, artifacts.build.yaml)
  data/        # Bundled static files (e.g. registry.yaml manifest)
tests/
  unit/        # Fast tests — mock external processes
  integration/ # Requires LXD/spread — skip-guarded
docs/          # Spec
examples/      # Example project layout
```

## License

Apache License 2.0 — see [LICENSE](LICENSE).

