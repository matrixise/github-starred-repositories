import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .models import StarredRepo

Row = dict[str, Any]
Conn = psycopg.Connection[Row]

DEFAULT_DSN = "postgresql://starred:starred@localhost:5432/starred"

SCHEMA = """
CREATE TABLE IF NOT EXISTS repositories (
    id                INTEGER     GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name_with_owner   TEXT        NOT NULL UNIQUE,
    description       TEXT,
    url               TEXT        NOT NULL,
    is_archived       BOOLEAN     NOT NULL DEFAULT FALSE,
    pushed_at         TIMESTAMPTZ,
    starred_at        TIMESTAMPTZ NOT NULL,
    primary_language  TEXT,
    stargazer_count   INTEGER     NOT NULL DEFAULT 0,
    readme_path       TEXT,
    synced_at         TIMESTAMPTZ NOT NULL
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
    repo_id     INTEGER     PRIMARY KEY REFERENCES repositories(id) ON DELETE CASCADE,
    score       INTEGER     NOT NULL CHECK (score BETWEEN 1 AND 5),
    summary     TEXT        NOT NULL,
    analyzed_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS repositories_starred_at_idx ON repositories (starred_at DESC);
"""


def default_dsn() -> str:
    return os.environ.get("DATABASE_URL", "").strip() or DEFAULT_DSN


@contextmanager
def open_db(dsn: str | None = None) -> Iterator[Conn]:
    conn = psycopg.Connection[Row].connect(dsn or default_dsn(), row_factory=dict_row)
    try:
        conn.execute(SCHEMA)
        conn.commit()
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_repo(conn: Conn, repo: StarredRepo) -> int:
    now = datetime.now(UTC)
    row = conn.execute(
        """
        INSERT INTO repositories
            (name_with_owner, description, url, is_archived, pushed_at,
             starred_at, primary_language, stargazer_count, synced_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(name_with_owner) DO UPDATE SET
            description      = excluded.description,
            url              = excluded.url,
            is_archived      = excluded.is_archived,
            pushed_at        = excluded.pushed_at,
            starred_at       = excluded.starred_at,
            primary_language = excluded.primary_language,
            stargazer_count  = excluded.stargazer_count,
            synced_at        = excluded.synced_at
        RETURNING id
        """,
        (
            repo.name_with_owner,
            repo.description,
            repo.url,
            repo.is_archived,
            repo.pushed_at,
            repo.starred_at,
            repo.primary_language,
            repo.stargazer_count,
            now,
        ),
    ).fetchone()
    assert row is not None  # the upsert always returns a row
    repo_id = row["id"]

    conn.execute("DELETE FROM topics WHERE repo_id = %s", (repo_id,))
    if repo.topics:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO topics (repo_id, topic_name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                [(repo_id, t) for t in repo.topics],
            )
    return repo_id


def get_meta(conn: Conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: Conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (%s, %s) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def upsert_analysis(conn: Conn, repo_id: int, score: int, summary: str) -> None:
    now = datetime.now(UTC)
    conn.execute(
        """
        INSERT INTO analysis (repo_id, score, summary, analyzed_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT(repo_id) DO UPDATE SET
            score       = excluded.score,
            summary     = excluded.summary,
            analyzed_at = excluded.analyzed_at
        """,
        (repo_id, score, summary, now),
    )


def get_repos_for_readme(conn: Conn, limit: int | None, force: bool = False) -> list[Row]:
    sql = "SELECT id, name_with_owner FROM repositories"
    if not force:
        sql += " WHERE readme_path IS NULL"
    sql += " ORDER BY starred_at DESC"
    params: tuple = ()
    if limit is not None:
        sql += " LIMIT %s"
        params = (limit,)
    return conn.execute(sql, params).fetchall()


def set_readme_path(conn: Conn, repo_id: int, path: str | None) -> None:
    conn.execute(
        "UPDATE repositories SET readme_path = %s WHERE id = %s",
        (path, repo_id),
    )


def get_all_repo_names(conn: Conn) -> list[Row]:
    return conn.execute("SELECT id, name_with_owner FROM repositories ORDER BY id").fetchall()


def update_stargazer_count(conn: Conn, repo_id: int, count: int) -> None:
    now = datetime.now(UTC)
    conn.execute(
        "UPDATE repositories SET stargazer_count = %s, synced_at = %s WHERE id = %s",
        (count, now, repo_id),
    )


def get_repos_for_export(conn: Conn, min_score: int) -> list[Row]:
    return conn.execute(
        """
        SELECT r.name_with_owner, r.description, r.url, r.primary_language,
               r.starred_at, r.pushed_at, r.stargazer_count, r.is_archived,
               a.score, a.summary,
               string_agg(DISTINCT t.topic_name, ',') AS topics
        FROM repositories r
        JOIN analysis a ON a.repo_id = r.id
        LEFT JOIN topics t ON t.repo_id = r.id
        WHERE a.score >= %s
        GROUP BY r.id, a.repo_id
        ORDER BY a.score DESC, r.stargazer_count DESC
        """,
        (min_score,),
    ).fetchall()


def get_repos_without_analysis_with_readme(conn: Conn, limit: int) -> list[Row]:
    return conn.execute(
        """
        SELECT r.id, r.name_with_owner, r.description, r.primary_language,
               r.is_archived, r.pushed_at, r.stargazer_count, r.readme_path,
               string_agg(t.topic_name, ', ') AS topics
        FROM repositories r
        LEFT JOIN topics t ON t.repo_id = r.id
        LEFT JOIN analysis a ON a.repo_id = r.id
        WHERE a.repo_id IS NULL
        GROUP BY r.id
        ORDER BY r.starred_at DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()


def get_last_starred_at(conn: Conn) -> datetime | None:
    row = conn.execute("SELECT MAX(starred_at) AS max_starred FROM repositories").fetchone()
    return row["max_starred"] if row else None
