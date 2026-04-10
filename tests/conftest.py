import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from starred.db import open_db
from starred.models import StarredRepo


@pytest.fixture
def tmp_db(tmp_path: Path):
    """Create a SQLite database in a temp directory and yield an open connection."""
    db_path = tmp_path / "test.db"
    with open_db(db_path) as conn:
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
    Return a dict representing a sqlite3.Row-like mapping with all fields
    needed by _build_prompt.
    """
    return {
        "id": 1,
        "name_with_owner": "octocat/hello-world",
        "description": "A test repository",
        "primary_language": "Python",
        "is_archived": 0,
        "pushed_at": "2024-02-01T08:00:00+00:00",
        "stargazer_count": 42,
        "topics": "python, testing",
        "readme_path": None,
    }
