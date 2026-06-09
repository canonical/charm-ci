# opcli

A **local-first CLI tool** for Canonical operator developers to build charms, rocks, and snaps; manage test environments; and run integration tests — identically on a developer laptop and inside a CI job.

`opcli` replaces the monolithic [`operator-workflows`](https://github.com/canonical/operator-workflows) approach with a modular pipeline based on explicit build plans (`artifacts.yaml`), stable build output (`artifacts.build.yaml`), and [spread](https://github.com/canonical/spread)-based test execution.

## Contents

- [Documentation](#documentation)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Commands](#commands)
- [`artifacts.yaml` schema](#artifactsyaml-schema)
- [`spread.yaml` virtual backends](#spreadyaml-virtual-backends)
- [`integration-suites` in `spread.yaml`](#integration-suites-in-spreadyaml)
- [pytest-opcli plugin](#pytest-opcli-plugin)
- [Tutorial testing](#tutorial-testing)
- [CI vs local](#ci-vs-local)
- [GitHub Actions reusable workflows](#github-actions-reusable-workflows)
- [Secrets for integration tests](#secrets-for-integration-tests)
- [Development](#development)

## Documentation

| Document | Purpose |
|---|---|
| [docs/ISD283.md](docs/ISD283.md) | Functional specification |
| [AGENTS.md](AGENTS.md) | Developer guide for AI coding agents |
| [examples/](examples/) | Example project layout with `artifacts.yaml`, `spread.yaml`, and `concierge.yaml` |

## Installation

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (`sudo snap install astral-uv --classic`)
- [charmcraft](https://charmcraft.io/) (`sudo snap install charmcraft --classic`)
- [rockcraft](https://rockcraft.io/) (`sudo snap install rockcraft --classic`) — if building rocks
- [LXD](https://canonical.com/lxd) (`sudo lxd init --auto && sudo usermod -aG lxd $USER`)
- [spread](https://github.com/canonical/spread) — installed via `opcli install spread` after opcli is set up
- [concierge](https://github.com/canonical/concierge/) (`sudo snap install concierge --classic`) — for env provisioning

### Install opcli

```bash
sudo snap install astral-uv --classic
uv tool install git+https://github.com/canonical/charm-ci.git
export PATH="$HOME/.local/bin:$PATH"  # or: uv tool update-shell && exec $SHELL
opcli --help
```

## Quick start

### Local testing with spread

```bash
opcli artifacts init     # discover charms/rocks/snaps → artifacts.yaml
opcli artifacts build    # build all → artifacts.build.yaml
opcli spread init        # generate spread.yaml with integration-suites
opcli spread expand      # preview expanded spread config
opcli spread run         # run integration tests (LXD backend)

# Target a specific test:
opcli spread run -- integration-test-local:ubuntu-24.04:tests/integration/run:test_charm

# Run a specific suite without spread (monorepo):
opcli pytest run --suite k8s-charm/tests/integration/
```

### Local testing without spread

```bash
opcli artifacts init
opcli artifacts build
opcli install tox                                      # install tox + tox-uv
opcli env provision                                    # concierge (auto-elevates with sudo)
opcli artifacts push-images --missing-registry deploy  # push rocks to local registry (k8s only)
opcli pytest run                                       # run all integration tests via tox
```

### Publishing to CharmHub

```bash
# After a successful build (local or CI):
opcli artifacts publish --channel latest/edge

# If every charm declares its own channel in artifacts.yaml, the flag can be omitted:
opcli artifacts publish

# Dry-run to preview what would be uploaded:
opcli artifacts publish --channel latest/edge --dry-run

# Publish only specific charms:
opcli artifacts publish --channel latest/stable --charm my-charm
```

Requires `charmcraft` credentials: run `charmcraft login` interactively or set `CHARMCRAFT_AUTH` in CI.
The command reads `artifacts.build.yaml` to resolve charm files and resource→rock mappings, then:
1. Uploads OCI-image resources (rocks from registry or local file, external images from `upstream-source`)
2. Uploads each `.charm` file and releases it to the channel with bound resource revisions

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
| `publish` | Upload charms and OCI resources to CharmHub. `--channel` (optional; per-charm channels supported), `--charm` (filter), `--dry-run`. |
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
| `init` | Generate `spread.yaml` with `integration-suites`. `--force` to overwrite. |
| `expand` | Print fully expanded `spread.yaml` to stdout. |
| `run` | Expand virtual backend and run spread. Args after `--` forwarded verbatim. |
| `jobs` | Print CI test matrix JSON (one entry per spread task/variant). `--exclude <pattern>` to filter by selector (repeatable, fnmatch glob, e.g. `--exclude 'my-docs-ci:*'`). |

### `opcli pytest`

| Command | Description |
|---|---|
| `run` | Assemble and execute the tox integration test command. `-e` for env, `--suite` for suite, `--` forwards args. |
| `expand` | Print full `tox -e integration -- <flags>` command. `-e` for env, `--suite` for suite, `--` forwards args. |

By default, `opcli pytest run/expand` invokes `tox -e integration` with no extra flags. Artifact fixtures are injected automatically by the pytest-opcli plugin — no CLI flag plumbing needed.

To pass Juju-specific options or other pytest flags, use `pytest-arguments-template` on the suite entry in `spread.yaml`:

```yaml
integration-suites:
  tests/integration/:
    pytest-arguments-template: |
      --model testing
      --keep-models
```

To pass artifacts as environment variables instead of fixtures:

```yaml
integration-suites:
  tests/integration/:
    pytest-environment-template: |
      {% for build in artifacts.charms[0].builds if build.arch == arch %}
      CHARM_PATH={{ build.path }}
      {% endfor %}
```

The `--suite` flag selects a specific integration suite (useful in monorepos with multiple test directories):

```bash
opcli pytest run --suite k8s-charm/tests/integration/
opcli pytest expand --suite machine-charm/tests/integration/
```

When a single `integration-suites` entry exists, `--suite` is auto-detected. With multiple suites, it's required.

### `opcli tutorial`

| Command | Description |
|---|---|
| `expand <file>` | Extract shell commands from a Markdown (.md) or RST (.rst) tutorial file and print them to stdout as a shell script. |

Typical usage in a spread `task.yaml` backed by the `opcli-minimal` backend:

```bash
runuser -l ubuntu -s /bin/bash -c 'set -ex; . <(opcli tutorial expand -- "$1")' _ "${SPREAD_PATH}${TUTORIAL}"
```

**What gets extracted:**

| File type | Included | Excluded |
|---|---|---|
| `.md` | 3-backtick code fences (all languages except `{…}` tags like `{terminal}`) | 4+-backtick fences; `<!-- SPREAD SKIP --> … <!-- SPREAD SKIP END -->` ranges |
| `.md` | `<!-- SPREAD … -->` HTML comment blocks (always) | — |
| `.rst` | `.. code-block::` directives (directive options like `:caption:` are skipped) | `.. SPREAD SKIP … .. SPREAD SKIP END` ranges |
| `.rst` | `.. SPREAD … .. SPREAD END` blocks (always) | — |

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

opcli recognises two virtual backend types and expands them into concrete spread backends at runtime.

### `integration-test` — full integration backend

For standard charm integration tests with Juju and concierge provisioning:

```yaml
backends:
  integration-test:
    type: integration-test
    systems:
      - ubuntu-24.04:
          runner: [self-hosted, noble]   # CI runner labels
          cpu: 4                         # local LXD VM vCPUs
          memory: 8                      # local LXD VM RAM (GiB)
          disk: 20                       # local LXD VM disk (GiB)
```

- Locally (`CI` unset): expands to `integration-test-local` with an LXD backend; installs concierge, Juju, and opcli, then runs `opcli env provision`.
- In CI (`CI=true`): expands to `integration-test-ci` with an adhoc backend targeting the current runner.

### `opcli-minimal` — lightweight backend

For tests that only need opcli itself (e.g. tutorial runs, doc validation), with no Juju or concierge:

```yaml
backends:
  my-docs:
    type: opcli-minimal
    systems:
      - ubuntu-24.04:
          runner: [self-hosted, noble]
```

- Locally: expands to `my-docs-local`; installs uv and opcli only.
- In CI: expands to `my-docs-ci`; prepare is empty (the CI workflow installs opcli before spread runs).

Users write their own `task.yaml` for this backend (or use `opcli tutorial expand` — see [Tutorial testing](#tutorial-testing)).

---

The `runner`, `cpu`, `memory`, and `disk` fields are opcli-only metadata — they are stripped before spread sees the YAML.

## `integration-suites` in `spread.yaml`

Instead of committing boilerplate `task.yaml` files, declare test suites declaratively:

```yaml
integration-suites:
  tests/integration/:
    working-dir: ./
    summary: top-level integration tests
    backends:
      - integration-test
    environment:
      CONCIERGE/test_k8s_charm: concierge-microk8s.yaml

  # Monorepo pattern — sub-charm with its own tests
  k8s-charm/tests/integration/:
    working-dir: k8s-charm/
    summary: k8s-charm sub-charm tests
    backends:
      - integration-test

  # Explicit variants (no auto-discovery)
  machine-charm/tests/integration/:
    working-dir: machine-charm/
    auto-discover: false
    summary: machine-charm tests
    backends:
      - integration-test
    environment:
      MODULE/test_charm: test_charm
```

At expand time, `integration-suites` entries are converted into native spread `suites:` entries with:
- **Auto-discovery** (default): scans the suite directory for `test_*.py` files and generates `MODULE/<name>` spread variants.
- **`working-dir`**: tells `opcli pytest` which directory to run pytest from. Defaults to `./` (project root).
- **`task.yaml` generation**: written into the `build/` directory at runtime (e.g. `build/tests/integration/run/task.yaml`). Files persist for inspection and are overwritten on next run. Add `build/` to your `.gitignore`.
- **`discover-pattern`**: customize the glob for auto-discovery (e.g., `discover-pattern: "test_*.py"` is the default; use `"*_test.py"` if your project uses that convention).

> **Migrating from native suites:** Replace your `suites:` block and committed `task.yaml` with an `integration-suites:` entry. Delete the `task.yaml` file — opcli generates it at runtime. Existing native `suites:` entries coexist and are passed through unchanged.

> **Note:** `reroot` in `spread.yaml` is incompatible with opcli. opcli manages `reroot` internally during expansion (to resolve paths from the `build/` directory back to the project root).

### Suite-specific keys

| Key | Default | Description |
|---|---|---|
| `working-dir` | `./` | Working directory for pytest invocation (opcli-only, stripped from spread output) |
| `auto-discover` | `true` | Scan for `test_*.py` and generate `MODULE/` variants |
| `discover-pattern` | `test_*.py` | Glob pattern for auto-discovery |
| `pytest-arguments-template` | — | Jinja2 template for pytest CLI args (opcli-only, stripped) |
| `pytest-environment-template` | — | Jinja2 template for env vars (opcli-only, stripped) |
| `backends` | (required) | Which virtual backends to run this suite on |
| `summary` | — | Spread suite summary |
| `environment` | — | Additional environment variables (merged with auto-discovered modules) |

### Pytest invocation templates

Controls how `opcli pytest run` and `opcli pytest expand` pass extra flags to the test framework. These keys live **per-suite inside `integration-suites`** — they are opcli-only and stripped from the spread output:

| Key | Effect |
|---|---|
| `pytest-arguments-template` | Jinja2 template rendered into CLI args passed to tox/pytest |
| `pytest-environment-template` | Jinja2 template rendered into `KEY=VALUE` env vars |

When no template is specified, `opcli pytest` runs bare `tox -e integration` with no extra flags. Artifact fixtures are provided by the pytest-opcli plugin automatically. Use `pytest-arguments-template` to pass additional options (Juju model name, test selection flags, etc.):

**Template context:** `artifacts` (full `ArtifactsGenerated` model) and `arch` (current architecture string).

For projects with multiple suites, use a YAML anchor to avoid repetition:

```yaml
x-pytest-args: &pytest-args
  pytest-arguments-template: |
    --model testing
    --keep-models

integration-suites:
  tests/integration/:
    <<: *pytest-args
    working-dir: ./
  k8s-charm/tests/integration/:
    <<: *pytest-args
    working-dir: k8s-charm/
    pytest-arguments-template: |   # override for this suite
      --model testing-k8s
      --keep-models
```

The `x-pytest-args` key at the top level is ignored by both spread and opcli — it exists only for the YAML anchor.

## pytest-opcli plugin

`opcli` ships a [pytest plugin](https://docs.pytest.org/en/stable/explanation/plugins.html) that auto-discovers `artifacts.build.yaml` and injects built artifacts as session-scoped fixtures. Integration tests stop needing manual `--charm-file` / `--resource-image` CLI flags in `conftest.py`.

### Installation

The plugin is bundled inside `opcli` and activates automatically whenever `opcli` is installed in the same Python environment as pytest. Add it as a test dependency alongside pytest-jubilant and your other test packages.

**With uv — add to your project:**

```bash
uv add --group integration "opcli @ git+https://github.com/canonical/charm-ci.git"
```

Or in `pyproject.toml` directly:

```toml
[dependency-groups]
integration = [
    "opcli @ git+https://github.com/canonical/charm-ci.git",
    "pytest-jubilant",
]
```

**With tox — add to the integration test env:**

```ini
[testenv:integration]
deps =
    opcli @ git+https://github.com/canonical/charm-ci.git
    pytest-jubilant
```

No further configuration is required. The `pytest11` entry point registers the plugin as soon as the package is installed.

### Fixtures

All fixtures are session-scoped and architecture-aware (they filter builds to the machine's current CPU architecture).

| Fixture / Helper | Return type | Description |
|---|---|---|
| `opcli_build_yaml_path` | `Path` | Resolved path to `artifacts.build.yaml`. Use as a dependency in custom conftest fixtures. |
| `opcli_artifacts` | `ArtifactsGenerated` | Full model parsed from `artifacts.build.yaml`. Always requires yaml; not available in CLI-flag mode. |
| `charm_path` | `str` | Path to the single built `.charm`. Fails if the repo contains more than one charm, or if the single charm has more than one build for the current arch (use `charm_paths` instead). |
| `charm_paths` | `dict[str, CharmPathList]` | All `.charm` paths per charm name. Use `.path` for the single-base shortcut, or `['ubuntu@X']` for base-keyed access. |
| `resource_images` | `dict[str, str]` | `{resource_name: image_ref}`. In yaml mode: resolves each OCI-image resource to its rock image for the single charm. In CLI-flag mode: uses `--resource-image` values directly. Fails if the repo contains zero or more than one charm (yaml mode only). |
| `build_rock_images(artifacts, root)` | `dict[str, str]` | Helper function (not a fixture) — returns `{rock_name: image_ref}` for the current arch. Use in a conftest `rock_images` fixture for multi-charm repos. |

### Usage examples

**Single charm with OCI resources (most common):**

```python
def test_deploy(juju, charm_path, resource_images):
    juju.deploy(charm_path, resources=resource_images)
    juju.wait(jubilant.all_active)
```

**Single charm built for multiple bases:**

```python
def test_deploy(juju, charm_paths):
    # single base — use .path shortcut
    juju.deploy(charm_paths["my-charm"].path)
    juju.wait(jubilant.all_active)
```

Or to target a specific base explicitly:

```python
def test_deploy(juju, charm_paths):
    juju.deploy(charm_paths["my-charm"]["ubuntu@24.04"])
    juju.wait(jubilant.all_active)
```

**Multi-charm repo (define `rock_images` in `conftest.py`):**

```python
# conftest.py
from pathlib import Path
import pytest
from opcli.models.artifacts_build import ArtifactsGenerated
from opcli.pytest_plugin import build_rock_images

@pytest.fixture(scope="session")
def rock_images(opcli_artifacts: ArtifactsGenerated, opcli_build_yaml_path: Path) -> dict[str, str]:
    return build_rock_images(opcli_artifacts, opcli_build_yaml_path.parent)
```

```python
# test_deploy.py
def test_deploy(juju, charm_paths, rock_images):
    juju.deploy(charm_paths["operator"].path, resources={"backend": rock_images["backend-rock"]})
    juju.deploy(charm_paths["agent"].path)
    juju.wait(jubilant.all_active)
```

### Discovery

The plugin locates `artifacts.build.yaml` in this order:

1. `--artifacts-build-yaml` pytest CLI option.
2. `OPCLI_ARTIFACTS_BUILD_YAML` environment variable (absolute path).
3. Walk up from pytest's rootdir until `artifacts.build.yaml` is found (stops at git root).
4. `pytest.UsageError` if none of the above succeed — run `opcli artifacts build` first.

### CLI-flag mode (no build step)

If you already have charm files and OCI image references (for example, from a previous CI stage), you can pass them directly as pytest CLI flags without needing `artifacts.build.yaml`:

```bash
pytest \
  --charm-file my-charm=./my-charm.charm \
  --resource-image oci-image=ghcr.io/org/rock:sha256-abc
```

Both flags are repeatable. Use `NAME=VALUE` format where `NAME` is the charm name (for `--charm-file`) or the Juju resource name (for `--resource-image`).

| Flag | Format | Maps to |
|---|---|---|
| `--charm-file` | `NAME=PATH` | `charm_path` / `charm_paths` |
| `--resource-image` | `NAME=REF` | `resource_images` |

Each fixture independently checks its own CLI flags first, then falls back to yaml discovery. Mixing modes per-fixture is supported (for example, pass `--charm-file` but let `resource_images` come from yaml).

## Tutorial testing

`opcli tutorial expand` extracts shell commands from a Markdown or RST tutorial file and prints them as a shell script. Combined with the `opcli-minimal` backend, this lets you gate docs PRs by running the tutorial in CI.

### Example setup

1. **Declare the backend and suite in `spread.yaml`:**

```yaml
backends:
  docs:
    type: opcli-minimal
    systems:
      - ubuntu-24.04:
          runner: [ubuntu-latest]

suites:
  docs/tutorial/:
    summary: Tutorial smoke test
    systems:
      - ubuntu-24.04

environment:
  TUTORIAL: docs/tutorial/getting-started.md
```

2. **Write `docs/tutorial/run/task.yaml`:**

```yaml
summary: Run getting-started tutorial

execute: |
  runuser -l ubuntu -s /bin/bash -c \
    'set -ex; . <(opcli tutorial expand -- "$1")' \
    _ "${SPREAD_PATH}${TUTORIAL}"
```

3. **Exclude tutorial jobs from the integration-test matrix** (they run in a separate job):

```bash
opcli spread jobs --exclude 'docs-ci:*'
```

### Marker syntax

Inline `SPREAD` markers let you include commands that are not in code fences, or skip blocks that shouldn't run:

```markdown
<!-- SPREAD
sudo snap install my-charm --classic
-->

Normal prose here. The next shell block is skipped:

<!-- SPREAD SKIP -->
```bash
$ interactive-command  # won't be extracted
```
<!-- SPREAD SKIP END -->
```

RST equivalent uses `.. SPREAD`, `.. SPREAD END`, `.. SPREAD SKIP`, and `.. SPREAD SKIP END` directives.

## CI vs local

| Env var | Controls | Local | CI |
|---|---|---|---|
| `CI` | Spread backend expansion | `*-local` (LXD VM) | `*-ci` (current runner) |
| `GITHUB_ACTIONS` | Artifact output format | Local file paths | GHCR images + artifact refs |
| `OPCLI_ROCK_UPLOAD` | Rock build output mode | — (not set) | `registry` (push to GHCR) or `artifact` (upload `.rock` as GH artifact, for fork PRs) |
| `OPCLI_GIT_REF` | opcli version inside spread VM | defaults to `main` | set by workflow |

## GitHub Actions reusable workflows

Three reusable workflows are available for operator repositories:

| Workflow | Purpose |
|---|---|
| `build-artifacts.yml` | Build matrix generation, parallel artifact builds, merged `artifacts.build.yaml`; debug builds open a detached tmate session when `runner.debug == 1` |
| `integration-test.yml` | Download artifacts, generate spread task matrix, run integration tests |
| `publish-artifacts.yml` | Publish validated artifacts to CharmHub; `channel` is optional and falls back to per-charm channels in `artifacts.yaml` |
| `doc-test.yml` | Generate spread task matrix, run documentation/tutorial tests (no artifact build) |

Example usage for integration tests:

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

Example usage for documentation tests:

```yaml
jobs:
  doc-test:
    uses: canonical/charm-ci/.github/workflows/doc-test.yml@main
    permissions:
      contents: read
      actions: read
    with:
      working-directory: .
      # spread-jobs-include: "docs-ci:*"  # optional: restrict to matching jobs
```

Example usage for publishing:

```yaml
jobs:
  publish:
    uses: canonical/charm-ci/.github/workflows/publish-artifacts.yml@main
    permissions:
      contents: write
      actions: read
    secrets:
      CHARMHUB_TOKEN: ${{ secrets.CHARMHUB_TOKEN }}
    with:
      # channel: latest/edge  # optional
      working-directory: .
```

Pinning to a SHA or tag automatically installs the matching `opcli` version via `canonical/get-workflow-version-action`.

### Fork PR support

When a pull request comes from a fork, the `GITHUB_TOKEN` is read-only and cannot push OCI images to GHCR. The `build-artifacts.yml` workflow handles this automatically:

1. **Fork detection** — checks `github.event.pull_request.head.repo.fork` and sets `OPCLI_ROCK_UPLOAD=artifact`.
2. **Artifact mode** — the `.rock` file is uploaded as a GitHub Actions artifact instead of being pushed to GHCR.
3. **Test phase** — `opcli artifacts fetch` downloads the `.rock` artifact, `opcli artifacts localize` rewrites paths, and `opcli artifacts push-images --missing-registry deploy` provisions a local registry and pushes the rock there.
4. **Debugging** — when GitHub Actions debug logging is enabled, the build job opens a detached tmate session before installing build tools.

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
