---
name: starred-update
version: 1.0.0
description: "Reload the starred repositories catalogue from GitHub. Use when the user says 'reload the repos', 'reprends les derniers repos', 'I starred new projects', 'update the tool', 'refresh the stars', or asks to fetch recently starred repositories."
metadata:
  requires:
    bins: [task, uv]
---

# starred-update

**Read [`starred-shared`](../starred-shared/SKILL.md) first.**

## When to use

- New repositories were starred on GitHub and the local catalogue must catch up.
- The user wants star counts and metadata refreshed on repos already stored
  (see *Refreshing metadata* below, it is a different command).
- Not for reading the result: once the catalogue is current, hand over to
  `starred-digest`.

## Steps

1. **Run the pipeline in the background.**

   ```bash
   task update
   ```

   `sync` → `fetch-readme` → `analyze`. Output is long and tqdm-buffered, so run it
   detached and read the output file when it completes rather than polling.

2. **Catch up on the analyze cap.** This is the step that gets forgotten. `analyze`
   stopped at 20 repos even though the sync may have brought more:

   ```bash
   uv run python -c "
   from starred.db import open_db
   with open_db() as c:
       print(c.execute('''SELECT COUNT(*) FROM repositories r
                          LEFT JOIN analysis a ON a.repo_id = r.id
                          WHERE a.repo_id IS NULL''').fetchone()[0])
   "
   ```

   If the count is not zero, run `task analyze LIMIT=<count>` and repeat until it is.

3. **Verify the end state** in one query:

   ```bash
   uv run python -c "
   from starred.db import open_db
   with open_db() as c:
       q = lambda s: c.execute(s).fetchone()[0]
       print('repos:', q('SELECT COUNT(*) FROM repositories'))
       print('analyses:', q('SELECT COUNT(*) FROM analysis'))
       print('no analysis:', q('SELECT COUNT(*) FROM repositories r LEFT JOIN analysis a ON a.repo_id=r.id WHERE a.repo_id IS NULL'))
       print('no readme:', q('SELECT COUNT(*) FROM repositories WHERE readme_path IS NULL'))
       print('empty tags:', q('SELECT COUNT(*) FROM analysis a WHERE NOT EXISTS (SELECT 1 FROM analysis_tags WHERE repo_id = a.repo_id)'))
   "
   ```

4. **Report, then show.** State how many repos arrived, how many failed, and whether
   anything is still pending. Then follow `starred-digest` on the same window: the
   user asked to reload the repos because they want to know what landed, not to read
   counters.

## Refreshing metadata

When the request is about the stars themselves ("go fetch the latest star counts")
rather than about new repositories:

```bash
task refresh-stars
```

One batched GraphQL request per 100 repos, rewriting `stargazer_count`,
`is_archived`, `created_at` and `pushed_at` across the whole catalogue. Roughly 40
seconds for 1400 repos. It reports `N updated, M missing`; the missing ones are the
four permanently unresolvable repos from `starred-shared`.

Because this is the only moment `is_archived` gets refreshed, surface what it reveals:

```bash
uv run python -c "
from starred.db import open_db
with open_db() as c:
    for r in c.execute('''SELECT r.name_with_owner, r.stargazer_count, a.score, substr(r.pushed_at,1,10)
                          FROM repositories r JOIN analysis a ON a.repo_id = r.id
                          WHERE r.is_archived = 1 AND a.score >= 4
                          ORDER BY a.score DESC, r.stargazer_count DESC LIMIT 10'''):
        print(r[2], r[0], r[1], r[3])
"
```

Archived repos holding a score of 4 or 5 earned it while still active. List them as
candidates for a decision, without changing their score.

## Export to Obsidian (optional)

Only when explicitly asked. Never implicitly: the `export` step is deliberately
commented out in the local `update` task.

```bash
uv run starred export-obsidian --vault <vault> --min-score 5 --dry-run   # always first
uv run starred export-obsidian --vault <vault> --min-score 5 --prune     # then, if approved
```

`--prune` only deletes notes carrying the export tag in their frontmatter, so
hand-written notes in the same folder survive. Expect every note to be reported as
`updated` the first time after a frontmatter format change.

## Anti-patterns

- Do not run `task sync:full` for a routine update. It re-walks every page for no
  gain; it is for backfilling a newly added column.
- Do not report success on the `Done.` line alone: `fetch-readme` and `analyze` exit 1
  on partial failure.
- Do not stop at "N repos synced" without telling the user what those repos are.
- Do not re-analyze already-scored repos to "refresh" them; `--force` exists only for
  analyses missing tags.
