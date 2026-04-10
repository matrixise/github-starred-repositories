import asyncio
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import httpx

from .client import _get_token

GITHUB_REST_URL = "https://api.github.com/repos/{name_with_owner}/readme"


def save_readme(content: str, name_with_owner: str, output_dir: Path) -> Path:
    """Save README content to {output_dir}/{owner}/{repo}/README.md."""
    owner, repo = name_with_owner.split("/", 1)
    dest = output_dir / owner / repo / "README.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    return dest


async def _fetch_one(
    row: sqlite3.Row,
    client: httpx.AsyncClient,
    headers: dict,
    semaphore: asyncio.Semaphore,
) -> tuple[sqlite3.Row, str | None, Exception | None]:
    async with semaphore:
        url = GITHUB_REST_URL.format(name_with_owner=row["name_with_owner"])
        try:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 404:
                return row, None, None
            resp.raise_for_status()
            return row, resp.text, None
        except Exception as e:
            return row, None, e


async def fetch_all_async(
    rows: list[sqlite3.Row],
    output_dir: Path,
    concurrency: int = 10,
) -> AsyncIterator[tuple[sqlite3.Row, Path | None, Exception | None]]:
    """
    Fetch READMEs for all rows concurrently.
    Yields (row, saved_path_or_None, error_or_None) as each completes.
    saved_path is None when the repo has no README (404).
    """
    token = _get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.raw+json",
    }
    semaphore = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)

    async with httpx.AsyncClient(timeout=30, limits=limits) as client:
        tasks = [asyncio.create_task(_fetch_one(row, client, headers, semaphore)) for row in rows]
        for coro in asyncio.as_completed(tasks):
            row, content, error = await coro
            if error is not None:
                yield row, None, error
            elif content is None:
                yield row, None, None
            else:
                path = save_readme(content, row["name_with_owner"], output_dir)
                yield row, path, None
