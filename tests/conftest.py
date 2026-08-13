from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from testcontainers.community.postgres import PostgresContainer

from starred.db import open_db
from starred.models import StarredRepo

TABLES = "repositories, topics, analysis, meta"


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    """Start a throwaway PostgreSQL 18 container for the whole test session."""
    with PostgresContainer("postgres:18-alpine", driver=None) as container:
        yield container.get_connection_url()


@pytest.fixture
def db(postgres_dsn: str):
    """Yield a connection on an empty schema (tables truncated between tests)."""
    with open_db(postgres_dsn) as conn:
        conn.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
        yield conn


@pytest.fixture
def sample_repo() -> StarredRepo:
    """Return a StarredRepo with complete fake data."""
    return StarredRepo(
        starred_at=datetime(2024, 3, 15, 10, 30, 0, tzinfo=UTC),
        name_with_owner="octocat/hello-world",
        description="A test repository",
        topics=["python", "testing"],
        is_archived=False,
        pushed_at=datetime(2024, 2, 1, 8, 0, 0, tzinfo=UTC),
        url="https://github.com/octocat/hello-world",
        primary_language="Python",
        stargazer_count=42,
    )


@pytest.fixture
def sample_row_dict() -> dict:
    """
    Return a dict representing a database row with all fields
    needed by _build_prompt.
    """
    return {
        "id": 1,
        "name_with_owner": "octocat/hello-world",
        "description": "A test repository",
        "primary_language": "Python",
        "is_archived": False,
        "pushed_at": datetime(2024, 2, 1, 8, 0, 0, tzinfo=UTC),
        "stargazer_count": 42,
        "topics": "python, testing",
        "readme_path": None,
    }
