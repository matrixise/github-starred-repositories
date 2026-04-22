import asyncio
import re as _re
import sqlite3
import time
from pathlib import Path

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from tqdm import tqdm

from .analyze import analyze_repo
from .client import fetch_stargazer_counts, fetch_starred
from .db import (
    get_all_repo_names,
    get_last_starred_at,
    get_repos_for_export,
    get_repos_for_readme,
    get_repos_without_analysis_with_readme,
    open_db,
    set_meta,
    set_readme_path,
    update_stargazer_count,
    upsert_analysis,
    upsert_repo,
)
from .readme import fetch_all_async

load_dotenv()


def _safe_filename(name: str) -> str:
    return _re.sub(r'[<>:"/\\|?*\x00-\x1f.]', "_", name).strip()


console = Console()

DB_PATH = Path("starred.db")
README_DIR = Path("readmes")

SCORE_COLORS = {1: "red", 2: "yellow", 3: "white", 4: "green", 5: "bold green"}


def _score_cell(score: int | None) -> str:
    if score is None:
        return "[dim]—[/dim]"
    color = SCORE_COLORS.get(score, "white")
    return f"[{color}]{score}/5[/{color}]"


@click.group()
def main():
    """GitHub starred repositories manager."""


@main.command()
@click.option("--full", is_flag=True, default=False, help="Full refresh (ignore cache)")
@click.option("--db", "db_path", default=DB_PATH, type=Path, show_default=True)
def sync(full: bool, db_path: Path):
    """Fetch starred repositories and store them in SQLite."""
    with open_db(db_path) as conn:
        stop_at = None if full else get_last_starred_at(conn)

        if stop_at:
            console.print(f"[dim]Incremental sync — stopping at {stop_at.date()}[/dim]")
        else:
            console.print("[dim]Full sync — fetching all starred repositories[/dim]")

        count = 0
        last_cursor = None
        try:
            for repo, cursor in fetch_starred(stop_at=stop_at):
                upsert_repo(conn, repo)
                last_cursor = cursor
                count += 1
                console.print(
                    f"  [green]✓[/green] {repo.name_with_owner}  [dim]{repo.starred_at.date()}[/dim]"
                )
        except RuntimeError as e:
            console.print(f"[red]Error:[/red] {e}")
            raise SystemExit(1) from None

        if last_cursor:
            set_meta(conn, "last_cursor", last_cursor)

    console.print(f"\n[bold]Done.[/bold] {count} repositories synced to [cyan]{db_path}[/cyan]")


@main.command("refresh-stars")
@click.option("--batch-size", default=100, show_default=True, help="Repos per GraphQL request")
@click.option("--db", "db_path", default=DB_PATH, type=Path, show_default=True)
def refresh_stars(batch_size: int, db_path: Path):
    """Refresh stargazer counts for all repositories (lightweight sync)."""
    if not db_path.exists():
        console.print(
            f"[red]Database not found:[/red] {db_path}. Run [bold]starred sync[/bold] first."
        )
        raise SystemExit(1)

    with open_db(db_path) as conn:
        rows = get_all_repo_names(conn)
        if not rows:
            console.print("[yellow]No repositories in database.[/yellow]")
            return

        console.print(
            f"[dim]Refreshing star counts for {len(rows)} repositories "
            f"(batch size={batch_size})...[/dim]\n"
        )

        repos = [(r["id"], r["name_with_owner"]) for r in rows]
        updated = missing = 0
        try:
            with tqdm(total=len(repos), unit="repo", dynamic_ncols=True) as bar:
                for repo_id, count in fetch_stargazer_counts(repos, batch_size=batch_size):
                    if count is None:
                        missing += 1
                    else:
                        update_stargazer_count(conn, repo_id, count)
                        updated += 1
                    bar.set_postfix(updated=updated, missing=missing)
                    bar.update(1)
        except RuntimeError as e:
            console.print(f"[red]Error:[/red] {e}")
            raise SystemExit(1) from None

    console.print(f"\n[bold]Done.[/bold] {updated} updated, {missing} missing.")


@main.command("fetch-readme")
@click.option("--limit", default=None, type=int, help="Max repos to process (default: all)")
@click.option("--force", is_flag=True, default=False, help="Re-fetch already downloaded READMEs")
@click.option("--concurrency", default=10, show_default=True, help="Number of parallel requests")
@click.option("--output-dir", default=README_DIR, type=Path, show_default=True)
@click.option("--db", "db_path", default=DB_PATH, type=Path, show_default=True)
def fetch_readme_cmd(
    limit: int | None, force: bool, concurrency: int, output_dir: Path, db_path: Path
):
    """Fetch README files from GitHub and store them locally (async)."""
    if not db_path.exists():
        console.print(
            f"[red]Database not found:[/red] {db_path}. Run [bold]starred sync[/bold] first."
        )
        raise SystemExit(1)

    with open_db(db_path) as conn:
        rows = get_repos_for_readme(conn, limit=limit, force=force)

        if not rows:
            console.print("[green]All repositories already have a README.[/green]")
            return

        console.print(
            f"[dim]Fetching READMEs for {len(rows)} repositories (concurrency={concurrency})...[/dim]\n"
        )
        ok = skipped = errors = 0

        async def run():
            nonlocal ok, skipped, errors
            with tqdm(total=len(rows), unit="repo", dynamic_ncols=True) as bar:
                async for row, path, error in fetch_all_async(rows, output_dir, concurrency):
                    name = row["name_with_owner"]
                    if error is not None:
                        bar.write(f"  ✗ {name} — {error}")
                        errors += 1
                    elif path is None:
                        set_readme_path(conn, row["id"], None)
                        bar.write(f"  — {name} — no README")
                        skipped += 1
                    else:
                        set_readme_path(conn, row["id"], str(path))
                        bar.write(f"  ✓ {name}")
                        ok += 1
                    bar.set_postfix(fetched=ok, skipped=skipped, errors=errors)
                    bar.update(1)

        asyncio.run(run())

    console.print(f"\n[bold]Done.[/bold] {ok} fetched, {skipped} without README, {errors} errors.")


@main.command()
@click.option("--limit", default=20, show_default=True, help="Number of repos to analyze per run")
@click.option("--db", "db_path", default=DB_PATH, type=Path, show_default=True)
def analyze(limit: int, db_path: Path):
    """Analyze starred repositories with Claude and assign an interest score (1-5)."""
    if not db_path.exists():
        console.print(
            f"[red]Database not found:[/red] {db_path}. Run [bold]starred sync[/bold] first."
        )
        raise SystemExit(1)

    with open_db(db_path) as conn:
        rows = get_repos_without_analysis_with_readme(conn, limit)

        if not rows:
            console.print("[green]All repositories have already been analyzed.[/green]")
            return

        has_readme = sum(1 for r in rows if r["readme_path"])
        console.print(
            f"[dim]Analyzing {len(rows)} repositories ({has_readme} with README)...[/dim]\n"
        )

        for i, row in enumerate(rows):
            name = row["name_with_owner"]
            readme_indicator = " [dim]+README[/dim]" if row["readme_path"] else ""
            console.print(f"  Analyzing [cyan]{name}[/cyan]{readme_indicator}...", end=" ")
            try:
                repo_id, score, summary = analyze_repo(row)
                upsert_analysis(conn, repo_id, score, summary)
                color = SCORE_COLORS.get(score, "white")
                console.print(f"[{color}]{score}/5[/{color}]  {summary}")
            except Exception as e:
                console.print(f"[red]error:[/red] {e}")

            if i < len(rows) - 1:
                time.sleep(5)

    console.print("\n[bold]Done.[/bold]")


def _build_note(row: sqlite3.Row, tag: str) -> str:
    owner, repo = row["name_with_owner"].split("/", 1)
    topics_raw = row["topics"] or ""
    topics_list = [t.strip() for t in topics_raw.split(",") if t.strip()]
    topics_yaml = "[" + ", ".join(topics_list) + "]" if topics_list else "[]"
    lang = row["primary_language"] or "unknown"
    lang_tag = lang.lower().replace(" ", "-").replace("+", "plus").replace("#", "sharp")
    tags_yaml = f'["{tag}", "{lang_tag}"]'
    pushed = row["pushed_at"][:10] if row["pushed_at"] else "unknown"
    score_badge = "⭐" * row["score"]
    archived_line = "\n> ⚠️ Archived" if row["is_archived"] else ""
    return f"""---
title: "{row["name_with_owner"]}"
url: {row["url"]}
language: {lang}
topics: {topics_yaml}
score: {row["score"]}
summary: "{row["summary"]}"
starred_at: {row["starred_at"][:10]}
tags: {tags_yaml}
---

# {owner}/{repo}

> {row["summary"]}{archived_line}

{score_badge} **Score {row["score"]}/5** | **Language:** {lang} | **Stars:** {row["stargazer_count"]:,} | **Last push:** {pushed}

**Topics:** {", ".join(f"`{t}`" for t in topics_list) if topics_list else "_(none)_"}

[View on GitHub]({row["url"]})
"""


@main.command("export-obsidian")
@click.option("--vault", required=True, type=Path, help="Path to your Obsidian vault")
@click.option("--min-score", default=4, show_default=True, help="Minimum interest score to export")
@click.option("--tag", default="github/starred", show_default=True, help="Obsidian tag to add")
@click.option(
    "--prune", is_flag=True, default=False, help="Delete notes for repos no longer meeting criteria"
)
@click.option("--db", "db_path", default=DB_PATH, type=Path, show_default=True)
def export_obsidian(vault: Path, min_score: int, tag: str, prune: bool, db_path: Path):
    """Export high-scoring repositories as lightweight Obsidian notes."""
    if not db_path.exists():
        console.print(
            f"[red]Database not found:[/red] {db_path}. Run [bold]starred sync[/bold] first."
        )
        raise SystemExit(1)

    dest_dir = vault / "Sources" / "GitHub Stars"
    dest_dir.mkdir(parents=True, exist_ok=True)

    with open_db(db_path) as conn:
        rows = get_repos_for_export(conn, min_score)

    if not rows:
        console.print(
            f"[yellow]No repositories with score >= {min_score} found. Run [bold]starred analyze[/bold] first.[/yellow]"
        )
        return

    console.print(f"[dim]Exporting {len(rows)} repositories to [cyan]{dest_dir}[/cyan]...[/dim]\n")
    created = updated = unchanged = pruned = 0

    valid_filenames: set[str] = set()

    for row in rows:
        owner, repo_name = row["name_with_owner"].split("/", 1)
        filename = f"{_safe_filename(owner)} - {_safe_filename(repo_name)}.md"
        valid_filenames.add(filename)
        note_path = dest_dir / filename
        note = _build_note(row, tag)

        if note_path.exists():
            existing = note_path.read_text(encoding="utf-8")
            if existing == note:
                unchanged += 1
                continue
            note_path.write_text(note, encoding="utf-8")
            console.print(f"  [yellow]↺[/yellow] {filename} [dim](updated)[/dim]")
            updated += 1
        else:
            note_path.write_text(note, encoding="utf-8")
            console.print(f"  [green]✓[/green] {filename} [dim](created)[/dim]")
            created += 1

    if prune:
        for existing_file in dest_dir.glob("*.md"):
            if existing_file.name not in valid_filenames:
                existing_file.unlink()
                console.print(f"  [red]✗[/red] {existing_file.name} [dim](pruned)[/dim]")
                pruned += 1

    parts = []
    if created:
        parts.append(f"[green]{created} created[/green]")
    if updated:
        parts.append(f"[yellow]{updated} updated[/yellow]")
    if unchanged:
        parts.append(f"[dim]{unchanged} unchanged[/dim]")
    if pruned:
        parts.append(f"[red]{pruned} pruned[/red]")
    console.print("\n[bold]Done.[/bold] " + ", ".join(parts) + ".")


@main.command("list")
@click.option("--lang", default=None, help="Filter by primary language")
@click.option("--archived", is_flag=True, default=False, help="Show only archived repos")
@click.option("--topic", default=None, help="Filter by topic name")
@click.option("--min-score", default=None, type=int, help="Filter by minimum interest score (1-5)")
@click.option("--db", "db_path", default=DB_PATH, type=Path, show_default=True)
@click.option("--limit", default=50, show_default=True, help="Max rows to display")
@click.option(
    "--description",
    "show_description",
    is_flag=True,
    default=False,
    help="Show repository description",
)
def list_repos(
    lang: str | None,
    archived: bool,
    topic: str | None,
    min_score: int | None,
    db_path: Path,
    limit: int,
    show_description: bool,
):
    """List starred repositories."""
    if not db_path.exists():
        console.print(
            f"[red]Database not found:[/red] {db_path}. Run [bold]starred sync[/bold] first."
        )
        raise SystemExit(1)

    with open_db(db_path) as conn:
        sql = """
            SELECT r.name_with_owner, r.description, r.primary_language,
                   r.is_archived, r.starred_at, r.pushed_at, r.stargazer_count,
                   GROUP_CONCAT(DISTINCT t.topic_name) AS topics,
                   a.score, a.summary
            FROM repositories r
            LEFT JOIN topics t ON t.repo_id = r.id
            LEFT JOIN analysis a ON a.repo_id = r.id
        """
        conditions = []
        params: list = []

        if lang:
            conditions.append("r.primary_language = ?")
            params.append(lang)
        if archived:
            conditions.append("r.is_archived = 1")
        if topic:
            conditions.append(
                "EXISTS (SELECT 1 FROM topics WHERE repo_id = r.id AND topic_name = ?)"
            )
            params.append(topic)
        if min_score is not None:
            conditions.append("a.score >= ?")
            params.append(min_score)

        if conditions:
            sql += " WHERE " + " AND ".join(conditions)

        sql += " GROUP BY r.id ORDER BY r.starred_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Repository", style="cyan", no_wrap=True)
    if show_description:
        table.add_column("Description")
    table.add_column("Language", width=12)
    table.add_column("Starred", width=11)
    table.add_column("Pushed", width=11)
    table.add_column("Stars", justify="right", width=7)
    table.add_column("Archived", width=8)
    table.add_column("Score", width=6, justify="center")
    table.add_column("Topics")

    for row in rows:
        pushed = row["pushed_at"][:10] if row["pushed_at"] else "—"
        cells = [row["name_with_owner"]]
        if show_description:
            cells.append(row["description"] or "")
        cells += [
            row["primary_language"] or "—",
            row["starred_at"][:10],
            pushed,
            str(row["stargazer_count"]),
            "[red]yes[/red]" if row["is_archived"] else "no",
            _score_cell(row["score"]),
            row["topics"] or "",
        ]
        table.add_row(*cells)

    console.print(table)
    console.print(f"[dim]{len(rows)} repositories[/dim]")
