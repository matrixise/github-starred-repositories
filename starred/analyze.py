import asyncio
import json
import re
import sqlite3
from pathlib import Path

from claude_code_sdk import ClaudeCodeOptions, query
from claude_code_sdk.types import AssistantMessage, TextBlock
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

SYSTEM_PROMPT = (
    "You are a senior software developer evaluating GitHub repositories. "
    "You respond ONLY with valid JSON, no markdown, no extra text."
)

README_MAX_CHARS = 3000

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
{{"score": <integer 1-5>, "summary": "<one sentence in English>"}}

Score guide:
1 = Not interesting (abandoned, trivial, superseded)
2 = Low interest
3 = Moderate interest
4 = High interest
5 = Excellent (actively maintained, widely useful, innovative)
"""


def _build_prompt(row: sqlite3.Row) -> str:
    pushed = row["pushed_at"][:10] if row["pushed_at"] else "unknown"

    readme_section = ""
    readme_path = row["readme_path"] if "readme_path" in row.keys() else None  # noqa: SIM118
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


def _extract_json(text: str) -> dict:
    # Strip markdown code fences if present
    text = re.sub(r"```(?:json)?\s*", "", text).strip()
    return json.loads(text)


async def _analyze_one(row: sqlite3.Row) -> tuple[int, int, str]:
    """Returns (repo_id, score, summary)."""
    prompt = _build_prompt(row)
    text_parts: list[str] = []

    async for message in query(
        prompt=prompt,
        options=ClaudeCodeOptions(
            system_prompt=SYSTEM_PROMPT,
            permission_mode="bypassPermissions",
        ),
    ):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    text_parts.append(block.text)

    raw = "".join(text_parts).strip()
    data = _extract_json(raw)
    score = max(1, min(5, int(data["score"])))
    summary = str(data["summary"])
    return row["id"], score, summary


def _is_rate_limit(exc: BaseException) -> bool:
    return "rate_limit" in str(exc).lower()


@retry(
    retry=retry_if_exception(_is_rate_limit),
    wait=wait_exponential(multiplier=2, min=30, max=120),
    stop=stop_after_attempt(4),
    reraise=True,
)
def analyze_repo(row: sqlite3.Row) -> tuple[int, int, str]:
    return asyncio.run(_analyze_one(row))
