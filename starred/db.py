import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .models import StarredRepo

DEFAULT_DB = Path("starred.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS repositories (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name_with_owner   TEXT    NOT NULL UNIQUE,
    description       TEXT,
    url               TEXT    NOT NULL,
    is_archived       INTEGER NOT NULL DEFAULT 0,
    pushed_at         TEXT,
    starred_at        TEXT    NOT NULL,
    primary_language  TEXT,
    stargazer_count   INTEGER NOT NULL DEFAULT 0,
    synced_at         TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS topics (
    repo_id    INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    topic_name TEXT    NOT NULL,
    PRIMARY KEY (repo_id, topic_name)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS analysis (
    repo_id     INTEGER PRIMARY KEY REFERENCES repositories(id) ON DELETE CASCADE,
    score       INTEGER NOT NULL CHECK (score BETWEEN 1 AND 5),
    summary     TEXT    NOT NULL,
    analyzed_at TEXT    NOT NULL
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply incremental schema migrations."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(repositories)")}
    if "readme_path" not in columns:
        conn.execute("ALTER TABLE repositories ADD COLUMN readme_path TEXT")


@contextmanager
def open_db(path: Path = DEFAULT_DB):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_repo(conn: sqlite3.Connection, repo: StarredRepo) -> int:
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO repositories
            (name_with_owner, description, url, is_archived, pushed_at,
             starred_at, primary_language, stargazer_count, synced_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name_with_owner) DO UPDATE SET
            description      = excluded.description,
            url              = excluded.url,
            is_archived      = excluded.is_archived,
            pushed_at        = excluded.pushed_at,
            starred_at       = excluded.starred_at,
            primary_language = excluded.primary_language,
            stargazer_count  = excluded.stargazer_count,
            synced_at        = excluded.synced_at
        """,
        (
            repo.name_with_owner,
            repo.description,
            repo.url,
            int(repo.is_archived),
            repo.pushed_at.isoformat() if repo.pushed_at else None,
            repo.starred_at.isoformat(),
            repo.primary_language,
            repo.stargazer_count,
            now,
        ),
    )
    row = conn.execute(
        "SELECT id FROM repositories WHERE name_with_owner = ?",
        (repo.name_with_owner,),
    ).fetchone()
    repo_id = row["id"]

    conn.execute("DELETE FROM topics WHERE repo_id = ?", (repo_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO topics (repo_id, topic_name) VALUES (?, ?)",
        [(repo_id, t) for t in repo.topics],
    )
    return repo_id


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def upsert_analysis(conn: sqlite3.Connection, repo_id: int, score: int, summary: str) -> None:
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO analysis (repo_id, score, summary, analyzed_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(repo_id) DO UPDATE SET
            score       = excluded.score,
            summary     = excluded.summary,
            analyzed_at = excluded.analyzed_at
        """,
        (repo_id, score, summary, now),
    )


def get_repos_for_readme(
    conn: sqlite3.Connection, limit: int | None, force: bool = False
) -> list[sqlite3.Row]:
    sql = "SELECT id, name_with_owner FROM repositories"
    if not force:
        sql += " WHERE readme_path IS NULL"
    sql += " ORDER BY starred_at DESC"
    params: tuple = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    return conn.execute(sql, params).fetchall()


def set_readme_path(conn: sqlite3.Connection, repo_id: int, path: str | None) -> None:
    conn.execute(
        "UPDATE repositories SET readme_path = ? WHERE id = ?",
        (path, repo_id),
    )


def get_all_repo_names(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT id, name_with_owner FROM repositories ORDER BY id").fetchall()


def update_stargazer_count(conn: sqlite3.Connection, repo_id: int, count: int) -> None:
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "UPDATE repositories SET stargazer_count = ?, synced_at = ? WHERE id = ?",
        (count, now, repo_id),
    )


def get_repos_for_export(conn: sqlite3.Connection, min_score: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT r.name_with_owner, r.description, r.url, r.primary_language,
               r.starred_at, r.pushed_at, r.stargazer_count, r.is_archived,
               a.score, a.summary,
               GROUP_CONCAT(DISTINCT t.topic_name) AS topics
        FROM repositories r
        JOIN analysis a ON a.repo_id = r.id
        LEFT JOIN topics t ON t.repo_id = r.id
        WHERE a.score >= ?
        GROUP BY r.id
        ORDER BY a.score DESC, r.stargazer_count DESC
        """,
        (min_score,),
    ).fetchall()


def get_repos_without_analysis_with_readme(
    conn: sqlite3.Connection, limit: int
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT r.id, r.name_with_owner, r.description, r.primary_language,
               r.is_archived, r.pushed_at, r.stargazer_count, r.readme_path,
               GROUP_CONCAT(t.topic_name, ', ') AS topics
        FROM repositories r
        LEFT JOIN topics t ON t.repo_id = r.id
        LEFT JOIN analysis a ON a.repo_id = r.id
        WHERE a.repo_id IS NULL
        GROUP BY r.id
        ORDER BY r.starred_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def get_last_starred_at(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute("SELECT MAX(starred_at) AS max_starred FROM repositories").fetchone()
    if row and row["max_starred"]:
        return datetime.fromisoformat(row["max_starred"])
    return None
