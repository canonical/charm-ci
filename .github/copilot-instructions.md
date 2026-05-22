# opcli — Copilot Instructions

Read **`AGENTS.md`** (repository root) before starting any task. It is the single source of truth for project conventions, architecture, data models, command specs, build tool invariants, and the git workflow.

Key reminders:
- Never push to `main` directly — always branch → PR → CI → squash merge.
- All git commits must include the trailer:
  `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`
- Run `uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/ && uv run mypy src/ && uv run pytest tests/unit/` before pushing.
