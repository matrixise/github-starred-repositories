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

<!-- rtk-instructions v2 -->
# RTK (Rust Token Killer) - Token-Optimized Commands

## Golden Rule

**Always prefix commands with `rtk`**. If RTK has a dedicated filter, it uses it. If not, it passes through unchanged. This means RTK is always safe to use.

**Important**: Even in command chains with `&&`, use `rtk`:
```bash
# ❌ Wrong
git add . && git commit -m "msg" && git push

# ✅ Correct
rtk git add . && rtk git commit -m "msg" && rtk git push
```

## RTK Commands by Workflow

### Build & Compile (80-90% savings)
```bash
rtk cargo build         # Cargo build output
rtk cargo check         # Cargo check output
rtk cargo clippy        # Clippy warnings grouped by file (80%)
rtk tsc                 # TypeScript errors grouped by file/code (83%)
rtk lint                # ESLint/Biome violations grouped (84%)
rtk prettier --check    # Files needing format only (70%)
rtk next build          # Next.js build with route metrics (87%)
```

### Test (90-99% savings)
```bash
rtk cargo test          # Cargo test failures only (90%)
rtk vitest run          # Vitest failures only (99.5%)
rtk playwright test     # Playwright failures only (94%)
rtk test <cmd>          # Generic test wrapper - failures only
```

### Git (59-80% savings)
```bash
rtk git status          # Compact status
rtk git log             # Compact log (works with all git flags)
rtk git diff            # Compact diff (80%)
rtk git show            # Compact show (80%)
rtk git add             # Ultra-compact confirmations (59%)
rtk git commit          # Ultra-compact confirmations (59%)
rtk git push            # Ultra-compact confirmations
rtk git pull            # Ultra-compact confirmations
rtk git branch          # Compact branch list
rtk git fetch           # Compact fetch
rtk git stash           # Compact stash
rtk git worktree        # Compact worktree
```

Note: Git passthrough works for ALL subcommands, even those not explicitly listed.

### GitHub (26-87% savings)
```bash
rtk gh pr view <num>    # Compact PR view (87%)
rtk gh pr checks        # Compact PR checks (79%)
rtk gh run list         # Compact workflow runs (82%)
rtk gh issue list       # Compact issue list (80%)
rtk gh api              # Compact API responses (26%)
```

### JavaScript/TypeScript Tooling (70-90% savings)
```bash
rtk pnpm list           # Compact dependency tree (70%)
rtk pnpm outdated       # Compact outdated packages (80%)
rtk pnpm install        # Compact install output (90%)
rtk npm run <script>    # Compact npm script output
rtk npx <cmd>           # Compact npx command output
rtk prisma              # Prisma without ASCII art (88%)
```

### Files & Search (60-75% savings)
```bash
rtk ls <path>           # Tree format, compact (65%)
rtk read <file>         # Code reading with filtering (60%)
rtk grep <pattern>      # Search grouped by file (75%)
rtk find <pattern>      # Find grouped by directory (70%)
```

### Analysis & Debug (70-90% savings)
```bash
rtk err <cmd>           # Filter errors only from any command
rtk log <file>          # Deduplicated logs with counts
rtk json <file>         # JSON structure without values
rtk deps                # Dependency overview
rtk env                 # Environment variables compact
rtk summary <cmd>       # Smart summary of command output
rtk diff                # Ultra-compact diffs
```

### Infrastructure (85% savings)
```bash
rtk docker ps           # Compact container list
rtk docker images       # Compact image list
rtk docker logs <c>     # Deduplicated logs
rtk kubectl get         # Compact resource list
rtk kubectl logs        # Deduplicated pod logs
```

### Network (65-70% savings)
```bash
rtk curl <url>          # Compact HTTP responses (70%)
rtk wget <url>          # Compact download output (65%)
```

### Meta Commands
```bash
rtk gain                # View token savings statistics
rtk gain --history      # View command history with savings
rtk discover            # Analyze Claude Code sessions for missed RTK usage
rtk proxy <cmd>         # Run command without filtering (for debugging)
rtk init                # Add RTK instructions to CLAUDE.md
rtk init --global       # Add RTK to ~/.claude/CLAUDE.md
```

## Token Savings Overview

| Category | Commands | Typical Savings |
|----------|----------|-----------------|
| Tests | vitest, playwright, cargo test | 90-99% |
| Build | next, tsc, lint, prettier | 70-87% |
| Git | status, log, diff, add, commit | 59-80% |
| GitHub | gh pr, gh run, gh issue | 26-87% |
| Package Managers | pnpm, npm, npx | 70-90% |
| Files | ls, read, grep, find | 60-75% |
| Infrastructure | docker, kubectl | 85% |
| Network | curl, wget | 65-70% |

Overall average: **60-90% token reduction** on common development operations.
<!-- /rtk-instructions -->