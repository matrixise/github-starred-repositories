---
name: starred-search
version: 1.1.0
description: "Answer questions about what the starred catalogue already contains. Use when the user asks 'do I have a project that...', 'ai-je un projet qui...', 'what could replace X', 'an open source equivalent of Y', 'which of my repos do Z', or names a commercial tool and wants a self-hosted alternative from their own stars."
metadata:
  requires:
    bins: [uv]
---

# starred-search

**Read [`starred-shared`](../starred-shared/SKILL.md) first.**

## When to use

- "Do I have something that replaces NotebookLM?", "an open source ngrok?"
- "Which of my repos deal with vector search?"
- Not for listing recent stars (`starred-digest`), not for fetching new ones
  (`starred-update`).

The answer must come **only** from the local catalogue. If nothing matches, say so and
offer to look outside it; never fill the gap with a repo the user has not starred.

## Steps

1. **Expand the intent into keywords.** Five to fifteen, covering three angles:
   - the product named by the user (`notebooklm`, `ngrok`, `notion`);
   - its functions (`podcast`, `tunnel`, `rag`, `citation`, `ocr`);
   - the vocabulary the analyses would use (`self-hosted`, `local-first`, `alternative`).

   Multi-word keywords are supported and often sharper than single tokens
   (`"notebook lm"`, `"expose local"`).

2. **Run the command.** Do not hand-write SQL: the query, the ranking and the liveness
   fields live in `search_repos()` (`starred/db.py`) and are covered by tests.

   ```bash
   uv run starred search notebooklm "notebook lm" podcast rag citation
   uv run starred search ngrok tunnel "expose local" --limit 10
   uv run starred search rag --min-score 4 --no-archived
   task search -- ngrok tunnel
   ```

   Options: `--limit` (default 25), `--min-score`, `--no-archived`, `--db`.

3. **Read the ranking.** The leading `[n]` is how many distinct keywords the repo
   matched, across name, description, Claude summary, tags and topics. Rows are sorted
   by that count, then score, then stars, so a repo hitting two keywords outranks a
   150 000-star platform hitting one generic term. Trust the top of the list over the
   star counts.

4. **Check liveness in the output.** Each line carries the archived marker and
   `poussé <date>`. A 5/5 score means nothing if the repo was archived or last pushed
   in 2022: scores are never recomputed after a repo dies.

5. **Answer in three blocks.**
   - **The direct replacement**, if one exists: name it, give score, language, stars,
     URL, and what makes it the answer.
   - **Partial coverage, grouped by function** (ingestion, audio generation, storage):
     say which piece each repo covers, so the user can assemble.
   - **What to rule out**: archived repos, scores of 1 or 2, and lookalikes. An
     `awesome-*` list or a prompt collection *for* the tool is not a substitute for it,
     and saying so explicitly is part of the answer.

6. **Close with a recommendation**, one or two lines: the turnkey option versus the
   assemble-it-yourself option, and which one fits what the user described.

## Widening the search

If the first run returns nothing useful, change the keywords rather than the tool:

- drop the product name, keep the functions (a repo may solve the problem without ever
  naming the commercial tool);
- add the implementation vocabulary (`webrtc`, `wireguard`, `embeddings`);
- raise `--limit` before concluding the catalogue has nothing.

Only add a bespoke query when the question is not keyword-shaped at all (aggregates,
date windows, cross-table counts). In that case use `open_db()` and the helpers from
`starred/db.py`, and mind the date-comparison trap in `starred-shared`.

## Anti-patterns

- Do not paste the raw command output as the answer. It is your input; the three blocks
  are the output.
- Do not recommend a repo whose summary you did not read: names mislead often enough
  (a `tunnel` in a name can be an SSH tunnel manager, an API client feature, or a VPN).
- Do not present an `awesome-*` list as a tool.
- Do not invent a repo that is not in the catalogue, and do not silently widen the
  answer to projects the user has not starred. If the honest answer is "nothing here
  does that", say it, then offer to search outside.
