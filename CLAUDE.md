# github-starred-repositories — Claude Code Instructions

## Project Overview

CLI tool (`starred`) that manages a curated list of GitHub starred repositories:

1. Syncs repos via the **GitHub GraphQL API** into a local SQLite database (`starred.db`)
2. Downloads READMEs via the **GitHub REST API** (async, concurrent with httpx)
3. Analyzes each repo with **Claude** (claude-code-sdk) to produce a 1-5 interest score
4. Lists repos with filters (language, topic, score, archived)
5. Exports top-scored repos to an **Obsidian vault** as Markdown notes

## Architecture

```
starred/
  cli.py       # Click commands: sync, fetch-readme, analyze, list, export-obsidian
  client.py    # GitHub GraphQL client — fetch_starred() generator (httpx sync)
  readme.py    # GitHub REST client — fetch_all_async() (httpx async, semaphore)
  analyze.py   # Claude analysis via claude-code-sdk — analyze_repo() / _analyze_one()
  db.py        # SQLite layer — open_db(), upsert_repo(), upsert_analysis(), queries
  models.py    # StarredRepo dataclass
```

Entry point: `starred.cli:main` (registered in `pyproject.toml`).

## Tech Stack

- **Python 3.11+** with `uv` for dependency and venv management
- **click** for CLI, **rich** for terminal output, **tqdm** for progress bars
- **httpx** (sync for GraphQL, async for REST bulk requests)
- **claude-code-sdk** for AI analysis (`query()` async generator)
- **tenacity** for retry logic on Claude rate-limit errors
- **SQLite** with `sqlite3` stdlib, no ORM

## Useful Dev Commands

```bash
# Run commands
uv run starred sync
uv run starred sync --full
uv run starred fetch-readme --limit 10 --concurrency 5
uv run starred analyze --limit 5
uv run starred list --min-score 4 --description
uv run starred export-obsidian --vault ~/Documents/MyVault --min-score 4

# Inspect the database
sqlite3 starred.db ".tables"
sqlite3 starred.db "SELECT name_with_owner, score, summary FROM repositories JOIN analysis ON analysis.repo_id = repositories.id ORDER BY score DESC LIMIT 20;"
sqlite3 starred.db "SELECT COUNT(*) FROM repositories WHERE readme_path IS NOT NULL;"
sqlite3 starred.db "SELECT COUNT(*) FROM analysis;"

# Check schema
sqlite3 starred.db ".schema"
```

## Code Conventions

- No unnecessary docstrings. Only add a docstring if it genuinely explains non-obvious behavior.
- All SQLite writes use `INSERT ... ON CONFLICT DO UPDATE` (upsert pattern). Never use separate SELECT + INSERT/UPDATE sequences.
- HTTP I/O for bulk operations (READMEs) is async using `httpx.AsyncClient` with a `asyncio.Semaphore` for concurrency control.
- The GraphQL sync (`client.py`) is synchronous — one page at a time, not bulk.
- Claude analysis runs synchronously from the CLI but the underlying `_analyze_one()` is async; it is called via `asyncio.run()` inside `analyze_repo()`.
- SQLite connections are managed with the `open_db()` context manager (commits on success, rolls back on error).
- Schema migrations are applied via `_migrate()` in `db.py` using `PRAGMA table_info`.
- Score is always clamped to [1, 5] with `max(1, min(5, ...))` after parsing Claude's JSON response.
- The CLI uses `Path` objects consistently; string paths are only used when writing to SQLite.

## Do Not Commit

The following must not be committed (already in `.gitignore`):

- `starred.db` — local database, user-specific
- `.env` — contains `GITHUB_TOKEN`
- `readmes/` — downloaded README files, can be large
- `.venv/`, `__pycache__/`, `*.pyc`, `dist/`, `build/`
