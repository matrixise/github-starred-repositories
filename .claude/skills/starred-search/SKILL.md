---
name: starred-search
version: 1.0.0
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
   - the vocabulary the analyses would use (`self-hosted`, `local-first`,
     `alternative`).

   A single broad keyword set on its own returns hundreds of rows: a `rag`-centred
   sweep once matched 368 repos, almost all noise. Ranking (step 3) is what makes the
   result usable.

2. **Query the four text fields** with this block:

   ```bash
   uv run python - <<'EOF'
   from starred.db import open_db

   KEYWORDS = ["notebooklm", "podcast", "rag", "citation"]   # <- the expanded list

   with open_db() as c:
       rows = c.execute("""
           SELECT r.name_with_owner, r.description, r.url, r.primary_language,
                  r.stargazer_count, r.is_archived, substr(r.pushed_at, 1, 10) AS pushed,
                  a.score, a.summary,
                  (SELECT GROUP_CONCAT(tag, ', ') FROM analysis_tags WHERE repo_id = r.id) AS tags,
                  (SELECT GROUP_CONCAT(topic_name, ', ') FROM topics WHERE repo_id = r.id) AS topics
           FROM repositories r
           LEFT JOIN analysis a ON a.repo_id = r.id
       """).fetchall()

   hits = []
   for r in rows:
       blob = " ".join(
           str(r[k] or "").lower()
           for k in ("name_with_owner", "description", "summary", "tags", "topics")
       )
       matched = [k for k in KEYWORDS if k in blob]
       if matched:
           hits.append((len(matched), r, matched))

   hits.sort(key=lambda h: (-h[0], -(h[1]["score"] or 0), -h[1]["stargazer_count"]))
   for n, r, matched in hits[:25]:
       flag = " [ARCHIVED]" if r["is_archived"] else ""
       print(f"[{n}] {r['score']}/5 {r['name_with_owner']} ({r['primary_language']}, "
             f"{r['stargazer_count']}*, pushed {r['pushed']}){flag} {matched}")
       print(f"    {r['summary']}")
       print(f"    tags: {r['tags']}")
   EOF
   ```

   Use subqueries for tags and topics, as above. Joining both tables and relying on
   `GROUP_CONCAT(DISTINCT …)` multiplies rows across the two dimensions.

3. **Rank by keywords matched first**, then score, then stars. A repo hitting two
   keywords is more relevant than a 5/5 hitting one: that is how the single real
   NotebookLM replacement surfaced above a wall of popular RAG frameworks.

4. **Check liveness before recommending.** Read `is_archived` and `pushed_at` on every
   candidate you are about to name. The catalogue contains repos archived since 2016
   and abandoned since 2022 that still carry a good score, because nothing
   re-analyzes an archived repo.

5. **Answer in three blocks.**
   - **The direct replacement**, if one exists: name it, give score, language, stars,
     URL, and what makes it the answer.
   - **Partial coverage, grouped by function** (ingestion, audio generation, storage):
     say which piece each repo covers, so the user can assemble.
   - **What to rule out**: archived repos, scores of 1 or 2, and lookalikes. An
     `awesome-*` list or a prompt collection *for* the tool is not a substitute for
     it, and saying so explicitly is part of the answer.

6. **Close with a recommendation**, one or two lines: the turnkey option versus the
   assemble-it-yourself option, and which one fits what the user described.

## Anti-patterns

- Do not dump the first 30 rows of the query as the answer. The query is your input,
  the three blocks are the output.
- Do not recommend a repo whose `summary` you did not read: the name is misleading
  often enough (a `tunnel` in a name can be an SSH tunnel manager, an API client
  feature, or a VPN).
- Do not present an `awesome-*` list as a tool.
- Do not invent a repo that is not in the catalogue, and do not silently widen the
  answer to projects the user has not starred. If the honest answer is "nothing here
  does that", say it, then offer to search outside.
