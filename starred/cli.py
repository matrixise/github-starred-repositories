import asyncio
import re as _re
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import click
import frontmatter
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from tqdm import tqdm

from .analyze import analyze_all_async
from .client import fetch_repo_metadata, fetch_starred
from .db import (
    get_all_repo_names,
    get_last_starred_at,
    get_repos_for_export,
    get_repos_for_readme,
    get_repos_starred_since,
    get_repos_to_analyze,
    open_db,
    set_meta,
    set_readme_path,
    update_repo_metadata,
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
    """Refresh stars, archived flag and dates for all repositories (lightweight sync)."""
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
            f"[dim]Refreshing metadata for {len(rows)} repositories "
            f"(batch size={batch_size})...[/dim]\n"
        )

        repos = [(r["id"], r["name_with_owner"]) for r in rows]
        updated = missing = 0
        try:
            with tqdm(total=len(repos), unit="repo", dynamic_ncols=True) as bar:
                for repo_id, meta in fetch_repo_metadata(repos, batch_size=batch_size):
                    if meta is None:
                        missing += 1
                    else:
                        update_repo_metadata(
                            conn,
                            repo_id,
                            meta.stargazer_count,
                            meta.is_archived,
                            meta.created_at,
                            meta.pushed_at,
                        )
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
    if errors:
        raise SystemExit(1)


@main.command()
@click.option("--limit", default=20, show_default=True, help="Number of repos to analyze per run")
@click.option(
    "--concurrency", default=3, show_default=True, help="Number of parallel Claude requests"
)
@click.option(
    "--delay",
    default=1.0,
    show_default=True,
    help="Seconds to stagger consecutive requests (0 to disable)",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Also re-analyze repos whose analysis has no tags",
)
@click.option("--db", "db_path", default=DB_PATH, type=Path, show_default=True)
def analyze(limit: int, concurrency: int, delay: float, force: bool, db_path: Path):
    """Analyze starred repositories with Claude and assign an interest score (1-5) and tags."""
    if not db_path.exists():
        console.print(
            f"[red]Database not found:[/red] {db_path}. Run [bold]starred sync[/bold] first."
        )
        raise SystemExit(1)

    ok = failed = 0

    with open_db(db_path) as conn:
        rows = get_repos_to_analyze(conn, limit, force=force)

        if not rows:
            console.print("[green]All repositories have already been analyzed.[/green]")
            return

        has_readme = sum(1 for r in rows if r["readme_path"])
        console.print(
            f"[dim]Analyzing {len(rows)} repositories ({has_readme} with README, "
            f"concurrency={concurrency})...[/dim]\n"
        )

        async def run():
            nonlocal ok, failed
            with tqdm(total=len(rows), unit="repo", dynamic_ncols=True) as bar:
                async for row, result, error in analyze_all_async(rows, concurrency, delay):
                    name = row["name_with_owner"]
                    if error is not None:
                        bar.write(f"  ✗ {name} — {error}")
                        failed += 1
                    else:
                        assert result is not None
                        upsert_analysis(
                            conn, result.repo_id, result.score, result.summary, result.tags
                        )
                        bar.write(
                            f"  ✓ {name}  {result.score}/5  {result.summary}  "
                            f"[{', '.join(result.tags)}]"
                        )
                        ok += 1
                    bar.set_postfix(analyzed=ok, failed=failed)
                    bar.update(1)

        asyncio.run(run())

    console.print(f"\n[bold]Done.[/bold] {ok} analyzed, {failed} failed.")
    if failed:
        raise SystemExit(1)


SINCE_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
OTHER_THEME = "autres"


def _parse_since(value: str) -> datetime:
    """Turn `24h`, `7d`, `90m`, `2w` or an ISO date into an absolute UTC bound."""
    match = _re.fullmatch(r"(\d+)\s*([mhdw])", value.strip().lower())
    if match:
        amount, unit = match.groups()
        return datetime.now(UTC) - timedelta(**{SINCE_UNITS[unit]: int(amount)})
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise click.BadParameter(
            f"{value!r} is not a duration like 24h/7d/2w nor an ISO date"
        ) from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _group_by_theme(rows: list[sqlite3.Row]) -> dict[str, list[sqlite3.Row]]:
    """
    Group repos under the tag they share with the most siblings.

    A tag needs at least two repos to form a theme; everything else lands
    in "Autres".
    """
    counts = Counter(tag for row in rows for tag in _split_csv(row["tags"]))
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        shared = [t for t in _split_csv(row["tags"]) if counts[t] > 1]
        theme = max(shared, key=lambda t: (counts[t], t)) if shared else OTHER_THEME
        groups.setdefault(theme, []).append(row)
    for group in groups.values():
        group.sort(key=lambda r: (-(r["score"] or 0), -r["stargazer_count"]))
    # "Autres" is a fallback, not a theme: it always comes last.
    return dict(sorted(groups.items(), key=lambda kv: (kv[0] == OTHER_THEME, -len(kv[1]), kv[0])))


def _repo_age(row: sqlite3.Row, now: datetime) -> str:
    if not row["created_at"]:
        return "âge inconnu"
    created = datetime.fromisoformat(row["created_at"])
    days = (now - created).days
    if days <= 1:
        return "créé aujourd'hui"
    if days < 60:
        return f"créé il y a {days} jours"
    if days < 730:
        return f"créé il y a {days // 30} mois"
    return f"créé il y a {days // 365} ans"


def _build_digest(rows: list[sqlite3.Row], since: datetime) -> str:
    now = datetime.now(UTC)
    scores = Counter(row["score"] for row in rows if row["score"])
    lines = [
        f"# {len(rows)} nouveaux dépôts depuis le {since.strftime('%d/%m/%Y %H:%M')} UTC",
        "",
        "Scores : "
        + (", ".join(f"{n} × {s}/5" for s, n in sorted(scores.items(), reverse=True)))
        + (
            f" ({len(rows) - sum(scores.values())} sans analyse)"
            if sum(scores.values()) < len(rows)
            else ""
        ),
        "",
    ]
    for theme, group in _group_by_theme(rows).items():
        lines.append(f"## {theme} ({len(group)})")
        lines.append("")
        for row in group:
            score = f"{row['score']}/5" if row["score"] else "non analysé"
            facts = ", ".join(
                [
                    score,
                    row["primary_language"] or "langage inconnu",
                    f"{row['stargazer_count']:,} ★".replace(",", " "),
                    _repo_age(row, now),
                ]
            )
            lines.append(f"- **{row['name_with_owner']}** ({facts})")
            lines.append(f"  {row['summary'] or row['description'] or '_(pas de description)_'}")
            if row["tags"]:
                lines.append(f"  Tags : {row['tags']}")
            lines.append(f"  {row['url']}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


@main.command()
@click.option(
    "--since", default="24h", show_default=True, help="Duration (24h, 7d, 2w) or ISO date"
)
@click.option("--min-score", default=None, type=int, help="Only include repos scored at least this")
@click.option("--db", "db_path", default=DB_PATH, type=Path, show_default=True)
def digest(since: str, min_score: int | None, db_path: Path):
    """Summarize recently starred repositories, ready to paste into an email."""
    if not db_path.exists():
        console.print(
            f"[red]Database not found:[/red] {db_path}. Run [bold]starred sync[/bold] first."
        )
        raise SystemExit(1)

    bound = _parse_since(since)
    with open_db(db_path) as conn:
        rows = get_repos_starred_since(conn, bound, min_score)

    if not rows:
        click.echo(f"No repository starred since {bound.isoformat()}.")
        return

    click.echo(_build_digest(rows, bound))


def _note_metadata(row: sqlite3.Row, tag: str) -> dict:
    lang = row["primary_language"] or "unknown"
    lang_tag = lang.lower().replace(" ", "-").replace("+", "plus").replace("#", "sharp")
    return {
        "title": row["name_with_owner"],
        "url": row["url"],
        "language": lang,
        "topics": _split_csv(row["topics"]),
        "score": row["score"],
        "summary": row["summary"],
        "starred_at": row["starred_at"][:10],
        "created_at": row["created_at"][:10] if row["created_at"] else None,
        "pushed_at": row["pushed_at"][:10] if row["pushed_at"] else None,
        "stars": row["stargazer_count"],
        "archived": bool(row["is_archived"]),
        "tags": [tag, lang_tag, *_split_csv(row["tags"])],
    }


def _split_csv(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _build_note(row: sqlite3.Row, tag: str) -> str:
    owner, repo = row["name_with_owner"].split("/", 1)
    meta = _note_metadata(row, tag)
    topics = meta["topics"]
    score_badge = "⭐" * row["score"]
    archived_line = "\n> ⚠️ Archived" if row["is_archived"] else ""
    pushed = meta["pushed_at"] or "unknown"
    body = f"""# {owner}/{repo}

> {row["summary"]}{archived_line}

{score_badge} **Score {row["score"]}/5** | **Language:** {meta["language"]} | **Stars:** {row["stargazer_count"]:,} | **Last push:** {pushed}

**Topics:** {", ".join(f"`{t}`" for t in topics) if topics else "_(none)_"}

[View on GitHub]({row["url"]})
"""
    return frontmatter.dumps(frontmatter.Post(body, **meta)) + "\n"


def _is_managed_note(path: Path, tag: str) -> bool:
    """True when the note carries our export tag, i.e. it was written by this command."""
    try:
        post = frontmatter.load(path)
    except Exception:
        return False
    tags = post.metadata.get("tags")
    if isinstance(tags, str):
        tags = _split_csv(tags)
    return isinstance(tags, list) and tag in tags


@main.command("export-obsidian")
@click.option("--vault", required=True, type=Path, help="Path to your Obsidian vault")
@click.option("--min-score", default=4, show_default=True, help="Minimum interest score to export")
@click.option("--tag", default="github/starred", show_default=True, help="Obsidian tag to add")
@click.option(
    "--prune", is_flag=True, default=False, help="Delete notes for repos no longer meeting criteria"
)
@click.option(
    "--dry-run", is_flag=True, default=False, help="Report what would change without writing"
)
@click.option("--db", "db_path", default=DB_PATH, type=Path, show_default=True)
def export_obsidian(
    vault: Path, min_score: int, tag: str, prune: bool, dry_run: bool, db_path: Path
):
    """Export high-scoring repositories as lightweight Obsidian notes."""
    if not db_path.exists():
        console.print(
            f"[red]Database not found:[/red] {db_path}. Run [bold]starred sync[/bold] first."
        )
        raise SystemExit(1)

    dest_dir = vault / "Sources" / "GitHub Stars"
    if not dry_run:
        dest_dir.mkdir(parents=True, exist_ok=True)

    with open_db(db_path) as conn:
        rows = get_repos_for_export(conn, min_score)

    if not rows:
        console.print(
            f"[yellow]No repositories with score >= {min_score} found. Run [bold]starred analyze[/bold] first.[/yellow]"
        )
        return

    console.print(f"[dim]Exporting {len(rows)} repositories to [cyan]{dest_dir}[/cyan]...[/dim]\n")
    created = updated = unchanged = pruned = skipped_prune = 0

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
            if not dry_run:
                note_path.write_text(note, encoding="utf-8")
            console.print(f"  [yellow]↺[/yellow] {filename} [dim](updated)[/dim]")
            updated += 1
        else:
            if not dry_run:
                note_path.write_text(note, encoding="utf-8")
            console.print(f"  [green]✓[/green] {filename} [dim](created)[/dim]")
            created += 1

    if prune and dest_dir.exists():
        for existing_file in sorted(dest_dir.glob("*.md")):
            if existing_file.name in valid_filenames:
                continue
            if not _is_managed_note(existing_file, tag):
                console.print(f"  [dim]skipped {existing_file.name} (not tagged {tag})[/dim]")
                skipped_prune += 1
                continue
            if not dry_run:
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
    if skipped_prune:
        parts.append(f"[dim]{skipped_prune} foreign notes kept[/dim]")
    prefix = "[bold]Dry run.[/bold]" if dry_run else "[bold]Done.[/bold]"
    console.print(f"\n{prefix} " + ", ".join(parts) + ".")


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
