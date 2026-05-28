# AGENTS.md

Instructions for AI coding agents working on this repository.

---

## Project overview

`opcli` — local-first CLI for Canonical operator developers to build charms/rocks/snaps, manage test environments, and run integration tests.

- **Spec:** [`docs/ISD283.md`](docs/ISD283.md) — read before implementing new features.
- **opcli owns:** file-based contracts, artifact discovery, subprocess execution, YAML transforms, artifact download (`gh run download`), CI job status queries (`gh api`), publishing to CharmHub (`charmcraft upload`/`upload-resource`).
- **opcli does NOT own:** GitHub workflow orchestration, artifact upload, runner selection.

---

## Quick-start

```bash
uv sync                                    # install deps
uv run opcli --help                        # run the tool
uv run ruff check src/ tests/              # lint
uv run ruff format --check src/ tests/     # format check
uv run mypy src/                           # type check
uv run pytest tests/unit/                  # unit tests
```

Never use `pip install`. All dependency management goes through `uv` and `pyproject.toml`.

> **Note:** These commands mirror the CI workflow (`.github/workflows/ci.yml`). If you change one, update the other.

---

## Architecture rules

### Repository layout

```
src/opcli/
  commands/    # CLI layer ONLY — parses args, calls core/. No business logic.
  core/        # All business logic lives here.
  models/      # Pydantic V2 models (artifacts.yaml, artifacts.build.yaml)
  data/        # Bundled static files (e.g. registry.yaml manifest)
tests/
  unit/        # Fast tests — mock external processes
  integration/ # Requires LXD/spread — skip-guarded with @pytest.mark.integration
docs/          # Spec (ISD283)
examples/      # Example project layout (artifacts.yaml, spread.yaml, concierge.yaml)
```

### Key constraints

1. **`commands/` is presentation only.** Never put logic in Typer callbacks. Tests validate `core/` directly.
2. **Subprocess rule.** All external binary calls go through `core/subprocess.py:run_command`. This is the mock boundary in tests.
3. **Never overwrite `spread.yaml`.** Always produce a transformed copy in a temp file.
4. **Avoid `Any`.** Prefer specific types; `mypy --strict` must pass. Legacy `Any` in YAML-handling helpers is tolerated but should not spread.
5. **CLI consistency: `run` / `expand` pairs.** Commands that execute a subprocess (`run`) and commands that print the equivalent command (`expand`) must be aligned in arguments, flags, and semantics. If `opcli foo run --bar baz` executes something, then `opcli foo expand --bar baz` must print the equivalent command with the same flags accepted. This applies to `spread`, `pytest`, and any future command groups with this pattern.
6. **Stepdown rule.** Within each module, order functions so callers appear before callees. Public API at the top, then private helpers below in call-order. Read top-to-bottom like a narrative.

---

## Tech stack

| Concern | Choice |
|---|---|
| Language | Python 3.12+, strict typing |
| Packaging | `uv` |
| CLI | `Typer` |
| Data models | `Pydantic V2` |
| Lint/format | `Ruff` (rules: `E F W I UP B SIM PL RUF`; ignores: `B008` globally, `E501` in `spread.py`) |
| YAML (user files) | `ruamel.yaml` (preserves comments) |
| Testing | `pytest` + `pytest-mock` |

---

## CLI output model

opcli follows a two-tier output convention:

- **Data commands** (`artifacts matrix`, `spread jobs`, `spread expand`, `artifacts path`) — always emit structured output (JSON/YAML) to stdout. These exist solely to produce machine-readable data.
- **Action commands** (`artifacts publish`, `artifacts build`, `spread run`) — print human-readable status to stdout by default. Use `--json` to opt into structured JSON output for CI consumption.

This mirrors the `gh` CLI pattern: action commands are human-first; `--json` switches to machine-parseable output.

---

## Spread privilege model

Spread **always** runs prepare/execute/restore scripts as root, regardless of the `username` field in the backend config. From spread docs: "In all cases the end result is the same: a system that executes scripts as root."

- **Local backend** (`username: ubuntu`): spread SSHes as ubuntu, then uses passwordless sudo to run scripts as root. `SUDO_USER=ubuntu` is set by the sudo mechanism.
- **CI backend** (`ADDRESS localhost`): spread runs natively as root. No sudo involved, so `SUDO_USER` comes from the backend `environment:` block in spread.yaml.

Concierge respects `SUDO_USER` (via its `realUser()` function) to write configs (kubeconfig, juju data) to the correct user's home directory and set proper ownership. Do NOT wrap `opcli env provision` in `runuser` or similar — it fights spread's design.

---

## CI detection

| Variable | Controls | Where checked |
|---|---|---|
| `CI` | Spread backend expansion (`-local` vs `-ci`) | `core/spread.py` |
| `GITHUB_ACTIONS=true` | CI-format artifact output (GHCR + artifact refs) | `core/artifacts.py` |
| `OPCLI_ROCK_UPLOAD` | Rock upload mode: `registry` (default, push to GHCR) or `artifact` (keep local `.rock`, upload as GH artifact — used for fork PRs) | `core/artifacts.py` |

---

## Spread virtual backend keys

The virtual backend in `spread.yaml` accepts opcli-only keys that are stripped during expansion:

| Key | Values | Default | Effect |
|---|---|---|---|
| `type` | `integration-test`, `tutorial` | (required) | Selects the backend template |
| `pytest-invocation-mode` | `pfe`, `observability` | `pfe` | Controls how `opcli pytest run/expand` passes artifacts to the test framework |
| `runner` | JSON array of labels | — | CI runner labels for GitHub Actions matrix |
| `cpu`, `memory`, `disk` | integer | 4, 8, 20 | Local LXD VM resource allocation |

### `pytest-invocation-mode` details

- **`pfe`**: Passes `--charm-file=<path>` and `--<rock>-image=<ref>` as CLI flags to tox/pytest. Used by Platform Engineering repos. Supports multi-charm, multi-rock.
- **`observability`**: Sets `CHARM_PATH` environment variable when running tox. Assumes single charm, no rocks. Errors if >1 charm found. Used by Observability repos that read `CHARM_PATH` in conftest.

When absent (or no `spread.yaml` exists), defaults to `pfe`.

---

## Data model: Pydantic vs ruamel.yaml

| File | Approach |
|---|---|
| `artifacts.yaml`, `artifacts.build.yaml` | **Pydantic V2** — validated at load |
| `spread.yaml`, `concierge.yaml`, `task.yaml` | **ruamel.yaml dict** — preserve comments/unknown keys |

Pydantic conventions:
- YAML-facing models: lax mode. Internal-only: `strict=True`.
- Field aliases: `alias=` + `populate_by_name=True` (e.g. `charmcraft-yaml` → `charmcraft_yaml`).

---

## Build tool invariants

These encode hard-won correctness lessons — do not violate.

1. **Symlinks for non-standard filenames.** When `artifacts.yaml` points to e.g. `charmcraft-my-charm.yaml`, a temp symlink is created in `pack_dir`. If a real file with *different* content exists at that path → `ConfigurationError`. Cleanup checks `.is_symlink()` not `.exists()`.

2. **Output attribution.** `attributed: set[str]` tracks claimed output paths across builds. Prevents two artifacts in a shared pack-dir from claiming the same file.

3. **`after - before` for output detection.** Never use `sorted(after)` alone. The set difference identifies files produced by *this* specific build invocation.

4. **CI artifact download.** `artifacts_fetch` downloads to `root/{artifact-name}/` subdirectories to prevent filename collisions. All artifact names are validated via `_safe_artifact_dir()` to ensure they resolve under the project root (path traversal prevention).

5. **Fork PR rock handling.** Fork PRs get read-only `GITHUB_TOKEN` and cannot push to GHCR. The workflow sets `OPCLI_ROCK_UPLOAD=artifact`; `artifacts_build` keeps the `.rock` file local and writes `artifact:` + `run-id:` metadata. After `artifacts_fetch` downloads the `.rock`, `push-images --missing-registry deploy` auto-deploys a local registry and pushes there. This converges with the local development path.

---

## Error hierarchy

```
OpcliError (base)
├── SubprocessError    — external command failed
├── ValidationError    — YAML schema validation failed
├── DiscoveryError     — discovery found nothing / conflicts
└── ConfigurationError — missing or invalid config
```

All Typer callbacks catch `OpcliError` and emit user-friendly messages. No raw tracebacks.

---

## Testing conventions

- **TDD:** write unit tests before implementation for non-trivial features.
- **Mock boundary:** mock at `run_command`. Never run real charmcraft/rockcraft/spread in unit tests.
- **`pre_existing_before/after` pattern:** simulate build tool output by writing files inside the `fake_run` side-effect, not before it.

---

## What CI enforces

The table below distinguishes *mechanically enforced* rules from *advisory* ones. Enforced rules block merge (or will, once branch protection is enabled). Advisory rules depend on developer/agent discipline.

| Rule | Enforcement | How |
|---|---|---|
| Lint (`ruff check`) | ✅ CI blocks | `ci.yml` step |
| Format (`ruff format --check`) | ✅ CI blocks | `ci.yml` step |
| Type safety (`mypy --strict`) | ✅ CI blocks | `ci.yml` step |
| Unit tests pass | ✅ CI blocks | `ci.yml` step |
| Coverage ≥ 85% | ✅ CI blocks | `pytest --cov-fail-under=85` |
| No push to main | ⚠️ Advisory | Branch protection (enable on canonical/charm-ci) |
| CI green before merge | ⚠️ Advisory | Branch protection (enable on canonical/charm-ci) |
| Docs updated with code | ⚠️ Advisory | PR review discipline |
| Mock at `run_command` only | ⚠️ Advisory | Code review |
| Avoid `Any` | ✅ CI blocks | `mypy --strict` rejects new `Any` |

---

## Git workflow

**Never push to `main`.** Always: branch → PR → CI green → squash merge.

```bash
git checkout -b fix/my-fix
# make changes
git push --set-upstream origin fix/my-fix
gh pr create --title "..." --body "..."
gh pr checks <number> --watch   # WAIT for CI workflow green
gh pr merge <number> --squash
```

**CI must be green before merging. No exceptions.**

**If a CI check fails, fix it.** Never dismiss a failure as "pre-existing" or "unrelated to this PR". If a workflow is broken, investigate and fix it in the same PR (or a preceding one) before merging. The goal is to keep `main` green at all times.

**Every PR must update docs.** If a PR changes CLI behavior, adds/removes commands, modifies flags, or alters workflows, the corresponding documentation must be updated in the same PR. This includes `docs/ISD283.md` (spec), `README.md`, and `AGENTS.md` as applicable. No code-only PRs that leave docs stale.

All commits must include:
```
Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
```
