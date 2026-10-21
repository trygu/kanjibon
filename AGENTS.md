# AGENTS.md

## Project Overview

**kanjibon** converts the KANJIDIC2 XML file (`data/kanjidic2.xml`, kept in-repo — no
downloads, no LFS) into a normalized SQLite database. It is the data foundation for a
kanji learning app: the shipped artifact is `output/kanjidic2.sqlite`, and apps consume
it through the **app contract views** (`kanji_seed`, `distractor_pool`, `kanji_priority`)
— not by reading the XML or scraping tables.

- `src/kanjibon/builder.py` — streaming XML parser → SQLite (schema, inserts, finalize)
- `src/kanjibon/cli.py` — `kanjibon build|export|artifacts` commands
- `packages/oboeru/` — the **oboeru** kanji quiz, an independent PyPI package
  (uv workspace member; depends only on the DB contract views, not on kanjibon)
- `tests/` — pytest, runs against `tests/fixtures/sample_kanjidic2.xml`
- `.github/workflows/build-database.yml` — CI: test → build → artifacts → release assets

## Behavioral Guidelines

These guidelines bias toward caution over speed. For trivial tasks, use judgment.

### 1. Think Before Coding

Don't assume. Don't hide confusion. Surface tradeoffs.

Before implementing:

- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity First

Minimum code that solves the problem. Nothing speculative.

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.
- Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

### 3. Surgical Changes

Touch only what you must. Clean up only your own mess.

When editing existing code:

- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.

When your changes create orphans:

- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

### 4. Goal-Driven Execution

Define success criteria. Loop until verified.

Transform tasks into verifiable goals:

- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:

1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]

Strong success criteria let you loop independently. Weak criteria ("make it work")
require constant clarification.

These guidelines are working if: fewer unnecessary changes in diffs, fewer rewrites due
to overcomplication, and clarifying questions come before implementation rather than
after mistakes.

## Project Instructions

### Tooling (uv-based)

- Use **uv** for everything Python: `uv sync` sets up `.venv` (incl. dev tools),
  `uv run <cmd>` runs inside it. Never use `pip`/`python -m venv` directly.
- `make build` / `make artifacts` / `make test` / `make quiz` wrap the corresponding
  `uv run` commands.
- `uv.lock` is committed — update it via `uv lock` when dependencies change.
- Tests: `uv run pytest` (or `make test`). Lint/format: `uv run ruff check src/ tests/ packages/`,
  `uv run black src/ tests/ packages/`.
- **Workspace**: this is a uv workspace — `kanjibon` at the root, `oboeru` in
  `packages/oboeru`. `uv sync` installs both. Runtime deps of **kanjibon** stay
  empty; oboeru's deps (textual, pillow) belong to its own pyproject. Run the quiz
  via `make quiz` (`uv run --package oboeru oboeru`).

### Coding Style

- **Standard library only at runtime.** Do not add runtime dependencies — the parser is
  intentionally dependency-free. Dev tools (pytest, ruff, black) go in
  `[dependency-groups] dev` in `pyproject.toml`.
- Keep the streaming (`iterparse`) architecture; the full 15MB XML must never be loaded
  into memory.

### Database Schema Rules

- **Schema version**: bump `PRAGMA user_version` in `_finalize_db()` whenever
  tables/views change, and update the "Schema Version" section in README.md.
- **KANJIDIC2 semantics** — do not regress these:
  - `seq` columns preserve XML source order: first meaning = primary, first reading =
    most common. Views depend on this (`ORDER BY seq`).
  - `stroke_count`: the FIRST value is the accepted count; later ones are miscounts.
  - `jlpt` is a documented heuristic estimate (legacy 1-4 mapping + grade/freq
    fallback), not official JLPT data. README must stay honest about this.
  - SKIP codes with a `skip_misclass` attribute are stored as `qc_type='skip_misclass'`.
- **App contract**: the queries in README.md / `manifest.json`
  (`kanji_seed` by `lvl`, `distractor_pool`) must keep working. Changing view columns is
  a breaking change for downstream apps — treat it accordingly.

### Documentation

- Keep README.md in sync with reality: generated files, CLI flags, schema, and level
  counts. Don't document features that don't exist.
- If you change anything listed in this file, update AGENTS.md too.
