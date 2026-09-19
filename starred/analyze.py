import asyncio
import json
import re
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, NamedTuple

from claude_agent_sdk import ClaudeAgentOptions, query
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

SYSTEM_PROMPT = (
    "You are a senior software developer evaluating GitHub repositories. "
    "You respond ONLY with valid JSON, no markdown, no extra text."
)

README_MAX_CHARS = 3000


class AnalysisResult(NamedTuple):
    repo_id: int
    score: int
    summary: str
    tags: list[str]


PROMPT_TEMPLATE = """\
Rate the interest of this GitHub repository for an active software developer.

Repository: {name}
Description: {description}
Language: {language}
Topics: {topics}
Last push: {pushed_at}
Stars: {stars}
Archived: {archived}
{readme_section}
Respond with ONLY a JSON object (no markdown):
{{"score": <integer 1-5>, "summary": "<one sentence in English>", "tags": [<3 to 5 short kebab-case tags>]}}

Score guide:
1 = Not interesting (abandoned, trivial, superseded)
2 = Low interest
3 = Moderate interest
4 = High interest
5 = Excellent (actively maintained, widely useful, innovative)

Tags guide:
3 to 5 short kebab-case tags (English) describing the repository's domain or
purpose (e.g. "web-framework", "cli-tool", "data-visualization"). Do not repeat
the programming language as a tag.
"""


def _build_prompt(row: sqlite3.Row) -> str:
    pushed = row["pushed_at"][:10] if row["pushed_at"] else "unknown"

    readme_section = ""
    readme_path = row["readme_path"]
    if readme_path and Path(readme_path).exists():
        content = Path(readme_path).read_text(encoding="utf-8", errors="replace")
        if len(content) > README_MAX_CHARS:
            content = content[:README_MAX_CHARS] + "\n...(truncated)"
        readme_section = f"\nREADME (excerpt):\n{content}\n"

    return PROMPT_TEMPLATE.format(
        name=row["name_with_owner"],
        description=row["description"] or "(no description)",
        language=row["primary_language"] or "unknown",
        topics=row["topics"] or "(none)",
        pushed_at=pushed,
        stars=row["stargazer_count"],
        archived="yes" if row["is_archived"] else "no",
        readme_section=readme_section,
    )


def _extract_json(text: str) -> dict[str, Any]:
    # Strip markdown code fences if present
    text = re.sub(r"```(?:json)?\s*", "", text).strip()
    return json.loads(text)


def _normalize_tag(tag: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", tag.strip().lower()).strip("-")


def _is_rate_limit(exc: BaseException) -> bool:
    return "rate_limit" in str(exc).lower()


@retry(
    retry=retry_if_exception(_is_rate_limit),
    wait=wait_exponential(multiplier=2, min=30, max=120),
    stop=stop_after_attempt(4),
    reraise=True,
)
async def _analyze_one(row: sqlite3.Row) -> AnalysisResult:
    prompt = _build_prompt(row)
    raw = ""

    async for message in query(
        prompt=f"{SYSTEM_PROMPT}\n\n---\n\n{prompt}",
        options=ClaudeAgentOptions(
            allowed_tools=[],
            permission_mode="bypassPermissions",
        ),
    ):
        if hasattr(message, "result"):
            raw = message.result or ""
    data = _extract_json(str(raw))
    if not isinstance(data.get("score"), int | float) or "summary" not in data:
        raise ValueError(f"Unexpected Claude response format: {raw!r}")
    if not isinstance(data.get("tags"), list):
        raise ValueError(f"Unexpected Claude response format: {raw!r}")
    score = max(1, min(5, int(data["score"])))
    summary = str(data["summary"])
    tags = [tag for t in data["tags"] if (tag := _normalize_tag(str(t)))]
    return AnalysisResult(row["id"], score, summary, tags)


async def analyze_all_async(
    rows: list[sqlite3.Row],
    concurrency: int = 3,
    delay: float = 0.0,
) -> AsyncIterator[tuple[sqlite3.Row, AnalysisResult | None, Exception | None]]:
    """
    Analyze rows concurrently, yielding (row, result, error) as each completes.

    `delay` staggers the start of each request to spread the load on the API.
    """
    semaphore = asyncio.Semaphore(concurrency)

    async def run_one(index: int, row: sqlite3.Row):
        async with semaphore:
            if delay:
                await asyncio.sleep(delay * index)
            try:
                return row, await _analyze_one(row), None
            except Exception as e:  # noqa: BLE001 - reported per repo, never fatal
                return row, None, e

    tasks = [asyncio.create_task(run_one(i, row)) for i, row in enumerate(rows)]
    for coro in asyncio.as_completed(tasks):
        yield await coro
