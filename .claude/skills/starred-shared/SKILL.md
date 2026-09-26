---
name: starred-shared
version: 1.0.0
description: "First read before any operation on the starred repositories catalogue. Command inventory, SQL schema, known pitfalls, output conventions. Use when the user mentions their starred repos for the first time in a session, or when any other starred-* skill is invoked."
metadata:
  requires:
    bins: [task, uv]
---

# starred-shared

**CRITICAL — read this before running any `starred` command.** Applies to every other
`starred-*` skill.

All commands assume the working directory is the repository root. The database
(`starred.db`), the downloaded READMEs (`readmes/`) and `.env` are local and
gitignored: never commit them, never paste `GITHUB_TOKEN` into output.

## Command inventory

`task` loads `.env` automatically, so prefer it over the bare CLI.

| Task | CLI | Effect |
|---|---|---|
| `task sync` | `starred sync` | Incremental sync, stops at the newest `starred_at` already stored |
| `task sync:full` | `starred sync --full` | Walks every starred page; the only way to backfill columns on old rows |
| `task refresh-stars` | `starred refresh-stars` | Batched GraphQL refresh of `stargazer_count`, `is_archived`, `created_at`, `pushed_at` for the whole catalogue |
| `task fetch-readme` | `starred fetch-readme` | Async README download for rows where `readme_path IS NULL` |
| `task analyze` | `starred analyze` | Claude scoring + summary + tags, concurrent (default 3) |
| `task digest` | `starred digest --since 24h` | Markdown summary of a recent window |
| `task export` | `starred export-obsidian` | Writes Obsidian notes for high scores |
| `task export:prune` | `starred export-obsidian --prune` | Same, plus deletes notes that no longer match |
| `task update` | — | Pipeline: `sync` → `fetch-readme` → `analyze` |
| `task secrets` | — | gitleaks scan over git history |

## SQL schema

```
repositories(id, name_with_owner, description, url, is_archived, pushed_at,
             starred_at, primary_language, stargazer_count, synced_at,
             readme_path, created_at)
topics(repo_id, topic_name)              -- GitHub topics
analysis(repo_id, score, summary, analyzed_at)
analysis_tags(repo_id, tag)              -- Claude-generated tags
meta(key, value)
```

Query it through the project's own layer so migrations and row factory apply:

```bash
uv run python -c "
from starred.db import open_db
with open_db() as c:
    print(c.execute('SELECT COUNT(*) FROM repositories').fetchone()[0])
"
```

Useful helpers already exist in `starred/db.py` — reuse them instead of rewriting SQL:
`get_repos_starred_since()`, `get_repos_for_export()`, `get_repos_to_analyze()`,
`get_tags()`, `get_last_starred_at()`.

## Pitfalls

These cost real time when rediscovered. Read them before improvising.

**`analyze` stops after 20 repos.** The CLI default is `--limit 20` and `task update`
inherits it, so a sync of 24 new repos leaves 4 unanalyzed and still exits 0. Always
count what remains and loop. See `starred-update`.

**Never compare `starred_at` against `datetime('now', ...)` in SQL.** Timestamps are
stored as ISO strings with a `T` separator; SQLite's `datetime()` uses a space. The
lexicographic comparison then puts `2026-09-19T04:46` *after* `2026-09-19 14:06`, so a
"last 24h" filter silently returns older rows. Use `starred digest --since` or
`get_repos_starred_since()`, which formats the bound with `.isoformat()`.

**Four repos are permanently unresolvable.** They were unstarred, renamed, made
private or deleted upstream. They keep `created_at IS NULL` and show up as `missing`
in `refresh-stars`. This is expected, not a bug to fix, and there is no prune command.

**`analysis.prompt_id` is an orphan column.** Present in the live database, absent
from the schema and from `_migrate()`. Do not read it, do not be surprised by it.

**Migrations run on first `open_db()`.** Including the one that moved the old CSV
`analysis.tags` column into `analysis_tags` and dropped it. Any command touching the
database upgrades it in place.

**`fetch-readme` and `analyze` exit 1 on partial failure.** They print a
`N done, M failed` line and return non-zero. In a chain, check the exit code rather
than only reading the last line.

**Scores predate archiving.** A repo archived upstream keeps the score it earned while
active, because nothing re-analyzes it. Check `is_archived` before recommending.

## Output conventions

- Scores as `N/5`; never invent one for a repo with no `analysis` row, say
  "not analyzed".
- Star counts with a space separator: `13 264 ★`.
- Dates as `YYYY-MM-DD`.
- Always give the repository URL when recommending something.
- Long runs (`update`, `refresh-stars`, `analyze`) go to the background: output is
  verbose and tqdm-buffered. Report the final counters, not the progress bars.

## Which sibling skill to follow

| User intent | Skill |
|---|---|
| "Reload my repos", "I starred new things", "refresh the stars" | `starred-update` |
| "Digest", "what's new since yesterday", "summarize my new stars" | `starred-digest` |
| "Do I have a project that…", "what replaces X", "an open source equivalent of Y" | `starred-search` |
