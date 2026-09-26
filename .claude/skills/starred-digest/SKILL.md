---
name: starred-digest
version: 1.0.0
description: "Summarize recently starred repositories into a readable digest. Use when the user says 'digest', 'montre le digest', 'what's new since yesterday', 'summarize my new stars', 'what did I star this week', or asks for an email about recent repos."
metadata:
  requires:
    bins: [task, uv]
---

# starred-digest

**Read [`starred-shared`](../starred-shared/SKILL.md) first.**

## When to use

- The user wants to know what landed in the catalogue recently.
- Right after `starred-update`, to show what the sync brought in.
- Not for finding one specific project or a capability: that is `starred-search`.

## Steps

1. **Run the command.** Do not hand-roll SQL for a time window, the date comparison
   trap in `starred-shared` applies.

   ```bash
   uv run starred digest --since 24h        # default window
   uv run starred digest --since 7d         # also 2w, 90m, or an ISO date
   uv run starred digest --since 7d --min-score 5
   task digest SINCE=2d                     # shorthand
   ```

2. **Reuse the output as-is.** It is Markdown already grouped by shared tag, themes
   ordered by size with `autres` last, repos sorted by score then stars, each one
   dated from `created_at`. Reformatting it into a table loses the grouping and the
   summaries. Quote it, do not rewrite it.

3. **Add the cross-cutting read.** Three to five lines on top of the raw output, which
   is the part the command cannot produce:
   - the dominant theme, and whether it continues a trend from previous windows;
   - repos **created the same day they were starred** (the `créé aujourd'hui` marker),
     which means the user is watching an emerging niche;
   - a language or an author that stands out (several repos from one org or one
     maintainer in the same window);
   - the weakest repo of the batch and why it scored low.

4. **Mind the window.** The default 24h bound is strict: a repo starred yesterday
   morning falls outside it. If a digest looks truncated right after an update, widen
   to `2d` before suspecting a bug, and say which window you used.

## Email variant

When the user asks for a mail rather than a digest:

- Add a subject line naming the most interesting fact, not the count
  ("5 nouveaux stars, dont 2 créés le jour même" beats "Digest du 20/09").
- Regroup by business theme rather than by raw tag: the automatic grouping puts 12
  repos under `cli-tool`, which is accurate but not informative. Split on what they
  actually do (encryption, terminal tooling, agent orchestration).
- One line per repo: what it is, why it is worth the user's time, and the one caveat.
- Close with the aggregate: score distribution, and what the batch says about their
  current focus.

## Anti-patterns

- Do not run `analyze` from this skill. A repo with no score is reported as
  "not analyzed", the digest is a read-only view.
- Do not pad the digest with repos outside the requested window.
- Do not restate every tag line in prose; the raw block already carries them.
- Do not claim a trend from a single repo.
