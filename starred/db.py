import sqlite3
from collections.abc import Sequence
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
    created_at        TEXT,
    pushed_at         TEXT,
    starred_at        TEXT    NOT NULL,
    primary_language  TEXT,
    stargazer_count   INTEGER NOT NULL DEFAULT 0,
    readme_path       TEXT,
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

CREATE TABLE IF NOT EXISTS analysis_tags (
    repo_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    tag     TEXT    NOT NULL,
    PRIMARY KEY (repo_id, tag)
);
"""


INDEXES = """
CREATE INDEX IF NOT EXISTS idx_repositories_language ON repositories(primary_language);
CREATE INDEX IF NOT EXISTS idx_repositories_starred  ON repositories(starred_at DESC);
CREATE INDEX IF NOT EXISTS idx_repositories_readme   ON repositories(readme_path);
CREATE INDEX IF NOT EXISTS idx_analysis_score        ON analysis(score);
CREATE INDEX IF NOT EXISTS idx_analysis_tags_tag     ON analysis_tags(tag);
CREATE INDEX IF NOT EXISTS idx_topics_name           ON topics(topic_name);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply incremental schema migrations."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(repositories)")}
    if "readme_path" not in columns:
        conn.execute("ALTER TABLE repositories ADD COLUMN readme_path TEXT")
    if "created_at" not in columns:
        conn.execute("ALTER TABLE repositories ADD COLUMN created_at TEXT")

    analysis_columns = {row[1] for row in conn.execute("PRAGMA table_info(analysis)")}
    if "tags" in analysis_columns:
        # Tags used to live in a comma-separated column; move them to analysis_tags.
        conn.executemany(
            "INSERT OR IGNORE INTO analysis_tags (repo_id, tag) VALUES (?, ?)",
            [
                (row["repo_id"], tag)
                for row in conn.execute(
                    "SELECT repo_id, tags FROM analysis WHERE tags IS NOT NULL AND TRIM(tags) != ''"
                )
                for tag in (t.strip() for t in row["tags"].split(","))
                if tag
            ],
        )
        conn.execute("ALTER TABLE analysis DROP COLUMN tags")


@contextmanager
def open_db(path: Path = DEFAULT_DB):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.executescript(INDEXES)
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
            (name_with_owner, description, url, is_archived, created_at, pushed_at,
             starred_at, primary_language, stargazer_count, synced_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name_with_owner) DO UPDATE SET
            description      = excluded.description,
            url              = excluded.url,
            is_archived      = excluded.is_archived,
            created_at       = excluded.created_at,
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
            repo.created_at.isoformat() if repo.created_at else None,
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


def upsert_analysis(
    conn: sqlite3.Connection,
    repo_id: int,
    score: int,
    summary: str,
    tags: Sequence[str] = (),
) -> None:
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
    conn.execute("DELETE FROM analysis_tags WHERE repo_id = ?", (repo_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO analysis_tags (repo_id, tag) VALUES (?, ?)",
        [(repo_id, t) for t in tags if t],
    )


def get_tags(conn: sqlite3.Connection, repo_id: int) -> list[str]:
    return [
        row["tag"]
        for row in conn.execute(
            "SELECT tag FROM analysis_tags WHERE repo_id = ? ORDER BY tag", (repo_id,)
        )
    ]


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


def update_repo_metadata(
    conn: sqlite3.Connection,
    repo_id: int,
    stargazer_count: int,
    is_archived: bool,
    created_at: datetime | None,
    pushed_at: datetime | None,
) -> None:
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """
        UPDATE repositories
        SET stargazer_count = ?,
            is_archived     = ?,
            created_at      = ?,
            pushed_at       = ?,
            synced_at       = ?
        WHERE id = ?
        """,
        (
            stargazer_count,
            int(is_archived),
            created_at.isoformat() if created_at else None,
            pushed_at.isoformat() if pushed_at else None,
            now,
            repo_id,
        ),
    )


def get_repos_for_export(conn: sqlite3.Connection, min_score: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT r.name_with_owner, r.description, r.url, r.primary_language,
               r.starred_at, r.created_at, r.pushed_at, r.stargazer_count, r.is_archived,
               a.score, a.summary,
               (SELECT GROUP_CONCAT(topic_name, ', ')
                  FROM (SELECT topic_name FROM topics WHERE repo_id = r.id ORDER BY topic_name)
               ) AS topics,
               (SELECT GROUP_CONCAT(tag, ', ')
                  FROM (SELECT tag FROM analysis_tags WHERE repo_id = r.id ORDER BY tag)
               ) AS tags
        FROM repositories r
        JOIN analysis a ON a.repo_id = r.id
        WHERE a.score >= ?
        ORDER BY a.score DESC, r.stargazer_count DESC
        """,
        (min_score,),
    ).fetchall()


SEARCH_BLOB = """LOWER(
            r.name_with_owner || ' ' ||
            COALESCE(r.description, '') || ' ' ||
            COALESCE(a.summary, '') || ' ' ||
            COALESCE(tg.tags, '') || ' ' ||
            COALESCE(tp.topics, '')
        )"""


def search_repos(
    conn: sqlite3.Connection,
    keywords: Sequence[str],
    min_score: int | None = None,
    include_archived: bool = True,
    limit: int = 25,
) -> list[sqlite3.Row]:
    """
    Repos whose name, description, summary, tags or topics contain any keyword.

    Ranked by how many distinct keywords a repo matches, then score, then stars:
    a repo hitting two keywords outranks a popular one hitting a single generic
    term. `matched` carries that count so callers can show it.
    """
    if not keywords:
        raise ValueError("at least one keyword is required")

    matched = " + ".join([f"(CASE WHEN {SEARCH_BLOB} LIKE ? THEN 1 ELSE 0 END)"] * len(keywords))
    params: list = [f"%{k.strip().lower()}%" for k in keywords]

    conditions = ["matched > 0"]
    if min_score is not None:
        conditions.append("score >= ?")
        params.append(min_score)
    if not include_archived:
        conditions.append("is_archived = 0")
    params.append(limit)

    sql = f"""
        WITH tg AS (
            SELECT repo_id, GROUP_CONCAT(tag, ', ') AS tags
            FROM (SELECT repo_id, tag FROM analysis_tags ORDER BY tag)
            GROUP BY repo_id
        ),
        tp AS (
            SELECT repo_id, GROUP_CONCAT(topic_name, ', ') AS topics
            FROM (SELECT repo_id, topic_name FROM topics ORDER BY topic_name)
            GROUP BY repo_id
        )
        SELECT * FROM (
            SELECT r.name_with_owner, r.description, r.url, r.primary_language,
                   r.stargazer_count, r.is_archived, r.created_at, r.pushed_at,
                   a.score, a.summary, tg.tags, tp.topics,
                   ({matched}) AS matched
            FROM repositories r
            LEFT JOIN analysis a ON a.repo_id = r.id
            LEFT JOIN tg ON tg.repo_id = r.id
            LEFT JOIN tp ON tp.repo_id = r.id
        )
        WHERE {" AND ".join(conditions)}
        ORDER BY matched DESC, score DESC, stargazer_count DESC
        LIMIT ?
    """
    return conn.execute(sql, params).fetchall()


def get_repos_starred_since(
    conn: sqlite3.Connection, since: datetime, min_score: int | None = None
) -> list[sqlite3.Row]:
    """
    Repos starred at or after `since`, newest first.

    starred_at is always stored as an ISO string in UTC, so a lexicographic
    comparison against an equally formatted bound is safe here.
    """
    sql = """
        SELECT r.name_with_owner, r.description, r.url, r.primary_language,
               r.starred_at, r.created_at, r.pushed_at, r.stargazer_count, r.is_archived,
               a.score, a.summary,
               (SELECT GROUP_CONCAT(topic_name, ', ')
                  FROM (SELECT topic_name FROM topics WHERE repo_id = r.id ORDER BY topic_name)
               ) AS topics,
               (SELECT GROUP_CONCAT(tag, ', ')
                  FROM (SELECT tag FROM analysis_tags WHERE repo_id = r.id ORDER BY tag)
               ) AS tags
        FROM repositories r
        LEFT JOIN analysis a ON a.repo_id = r.id
        WHERE r.starred_at >= ?
    """
    params: list = [since.astimezone(UTC).isoformat()]
    if min_score is not None:
        sql += " AND a.score >= ?"
        params.append(min_score)
    sql += " ORDER BY r.starred_at DESC"
    return conn.execute(sql, params).fetchall()


def get_repos_to_analyze(
    conn: sqlite3.Connection, limit: int | None, force: bool = False
) -> list[sqlite3.Row]:
    """Repos still lacking an analysis, those with a README first. `limit=None` for all."""
    where = (
        "a.repo_id IS NULL OR NOT EXISTS (SELECT 1 FROM analysis_tags WHERE repo_id = r.id)"
        if force
        else "a.repo_id IS NULL"
    )
    sql = f"""
        SELECT r.id, r.name_with_owner, r.description, r.primary_language,
               r.is_archived, r.pushed_at, r.stargazer_count, r.readme_path,
               GROUP_CONCAT(t.topic_name, ', ') AS topics
        FROM repositories r
        LEFT JOIN topics t ON t.repo_id = r.id
        LEFT JOIN analysis a ON a.repo_id = r.id
        WHERE {where}
        GROUP BY r.id
        ORDER BY (r.readme_path IS NULL), r.starred_at DESC
    """
    params: tuple = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    return conn.execute(sql, params).fetchall()


def get_status(conn: sqlite3.Connection) -> dict[str, int]:
    """Counters describing how complete the catalogue is."""
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "repositories": q("SELECT COUNT(*) FROM repositories"),
        "analyses": q("SELECT COUNT(*) FROM analysis"),
        "missing_analysis": q(
            """SELECT COUNT(*) FROM repositories r
               LEFT JOIN analysis a ON a.repo_id = r.id
               WHERE a.repo_id IS NULL"""
        ),
        "missing_readme": q("SELECT COUNT(*) FROM repositories WHERE readme_path IS NULL"),
        "missing_tags": q(
            """SELECT COUNT(*) FROM analysis a
               WHERE NOT EXISTS (SELECT 1 FROM analysis_tags WHERE repo_id = a.repo_id)"""
        ),
        "archived": q("SELECT COUNT(*) FROM repositories WHERE is_archived = 1"),
        "unresolved": q("SELECT COUNT(*) FROM repositories WHERE created_at IS NULL"),
    }


def get_last_starred_at(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute("SELECT MAX(starred_at) AS max_starred FROM repositories").fetchone()
    if row and row["max_starred"]:
        return datetime.fromisoformat(row["max_starred"])
    return None
