from datetime import UTC, datetime
from pathlib import Path

import pytest

from starred.db import (
    get_last_starred_at,
    get_repos_for_readme,
    get_repos_to_analyze,
    get_tags,
    open_db,
    update_repo_metadata,
    upsert_analysis,
    upsert_repo,
)
from starred.models import StarredRepo


@pytest.fixture
def db(tmp_path: Path):
    """Provide a live SQLite connection via open_db for the duration of the test."""
    db_path = tmp_path / "test.db"
    with open_db(db_path) as conn:
        yield conn


class TestUpsertRepo:
    def test_insert_and_retrieve(self, db, sample_repo):
        repo_id = upsert_repo(db, sample_repo)
        assert isinstance(repo_id, int)
        assert repo_id > 0

        row = db.execute("SELECT * FROM repositories WHERE id = ?", (repo_id,)).fetchone()
        assert row is not None
        assert row["name_with_owner"] == "octocat/hello-world"
        assert row["description"] == "A test repository"
        assert row["primary_language"] == "Python"
        assert row["stargazer_count"] == 42
        assert row["is_archived"] == 0
        assert row["created_at"] == "2023-01-10T12:00:00+00:00"

    def test_upsert_deduplication(self, db, sample_repo):
        """Inserting the same repo twice should not create a duplicate row."""
        id1 = upsert_repo(db, sample_repo)
        id2 = upsert_repo(db, sample_repo)
        assert id1 == id2

        count = db.execute("SELECT COUNT(*) FROM repositories").fetchone()[0]
        assert count == 1

    def test_upsert_updates_fields(self, db, sample_repo):
        upsert_repo(db, sample_repo)
        updated = StarredRepo(
            starred_at=sample_repo.starred_at,
            name_with_owner=sample_repo.name_with_owner,
            description="Updated description",
            topics=["updated"],
            is_archived=True,
            created_at=sample_repo.created_at,
            pushed_at=sample_repo.pushed_at,
            url=sample_repo.url,
            primary_language="Rust",
            stargazer_count=100,
        )
        upsert_repo(db, updated)

        row = db.execute(
            "SELECT * FROM repositories WHERE name_with_owner = ?",
            (sample_repo.name_with_owner,),
        ).fetchone()
        assert row["description"] == "Updated description"
        assert row["primary_language"] == "Rust"
        assert row["stargazer_count"] == 100
        assert row["is_archived"] == 1

    def test_topics_stored(self, db, sample_repo):
        repo_id = upsert_repo(db, sample_repo)
        topics = {
            row["topic_name"]
            for row in db.execute(
                "SELECT topic_name FROM topics WHERE repo_id = ?", (repo_id,)
            ).fetchall()
        }
        assert topics == {"python", "testing"}

    def test_topics_replaced_on_upsert(self, db, sample_repo):
        upsert_repo(db, sample_repo)
        updated = StarredRepo(
            starred_at=sample_repo.starred_at,
            name_with_owner=sample_repo.name_with_owner,
            description=sample_repo.description,
            topics=["new-topic"],
            is_archived=sample_repo.is_archived,
            created_at=sample_repo.created_at,
            pushed_at=sample_repo.pushed_at,
            url=sample_repo.url,
            primary_language=sample_repo.primary_language,
            stargazer_count=sample_repo.stargazer_count,
        )
        repo_id = upsert_repo(db, updated)
        topics = [
            row["topic_name"]
            for row in db.execute(
                "SELECT topic_name FROM topics WHERE repo_id = ?", (repo_id,)
            ).fetchall()
        ]
        assert topics == ["new-topic"]


class TestGetLastStarredAt:
    def test_empty_db_returns_none(self, db):
        assert get_last_starred_at(db) is None

    def test_returns_max_starred_at(self, db, sample_repo):
        upsert_repo(db, sample_repo)
        result = get_last_starred_at(db)
        assert result is not None
        assert isinstance(result, datetime)
        # The stored value is ISO-formatted from sample_repo.starred_at
        assert result.year == 2024
        assert result.month == 3
        assert result.day == 15

    def test_returns_most_recent_when_multiple(self, db, sample_repo):
        upsert_repo(db, sample_repo)
        older_repo = StarredRepo(
            starred_at=datetime(2023, 1, 1, tzinfo=UTC),
            name_with_owner="octocat/older-repo",
            description=None,
            topics=[],
            is_archived=False,
            created_at=None,
            pushed_at=None,
            url="https://github.com/octocat/older-repo",
            primary_language=None,
            stargazer_count=0,
        )
        upsert_repo(db, older_repo)

        result = get_last_starred_at(db)
        assert result is not None
        assert result.year == 2024


class TestGetReposForReadme:
    def test_empty_db_returns_empty_list(self, db):
        result = get_repos_for_readme(db, limit=None)
        assert result == []

    def test_returns_repos_without_readme_path(self, db, sample_repo):
        upsert_repo(db, sample_repo)
        result = get_repos_for_readme(db, limit=None)
        assert len(result) == 1
        assert result[0]["name_with_owner"] == "octocat/hello-world"

    def test_excludes_repos_with_readme_path(self, db, sample_repo):
        repo_id = upsert_repo(db, sample_repo)
        db.execute(
            "UPDATE repositories SET readme_path = ? WHERE id = ?",
            ("/some/path/README.md", repo_id),
        )
        result = get_repos_for_readme(db, limit=None)
        assert result == []

    def test_force_returns_all_repos(self, db, sample_repo):
        repo_id = upsert_repo(db, sample_repo)
        db.execute(
            "UPDATE repositories SET readme_path = ? WHERE id = ?",
            ("/some/path/README.md", repo_id),
        )
        result = get_repos_for_readme(db, limit=None, force=True)
        assert len(result) == 1

    def test_limit_is_respected(self, db):
        for i in range(5):
            repo = StarredRepo(
                starred_at=datetime(2024, 1, i + 1, tzinfo=UTC),
                name_with_owner=f"owner/repo-{i}",
                description=None,
                topics=[],
                is_archived=False,
                created_at=None,
                pushed_at=None,
                url=f"https://github.com/owner/repo-{i}",
                primary_language=None,
                stargazer_count=i,
            )
            upsert_repo(db, repo)

        result = get_repos_for_readme(db, limit=3)
        assert len(result) == 3


class TestUpsertAnalysis:
    def test_insert_analysis(self, db, sample_repo):
        repo_id = upsert_repo(db, sample_repo)
        upsert_analysis(db, repo_id, score=4, summary="Great tool for developers")

        row = db.execute("SELECT * FROM analysis WHERE repo_id = ?", (repo_id,)).fetchone()
        assert row is not None
        assert row["score"] == 4
        assert row["summary"] == "Great tool for developers"

    def test_upsert_analysis_overwrites(self, db, sample_repo):
        repo_id = upsert_repo(db, sample_repo)
        upsert_analysis(db, repo_id, score=2, summary="First analysis")
        upsert_analysis(db, repo_id, score=5, summary="Updated analysis")

        rows = db.execute("SELECT * FROM analysis WHERE repo_id = ?", (repo_id,)).fetchall()
        assert len(rows) == 1
        assert rows[0]["score"] == 5
        assert rows[0]["summary"] == "Updated analysis"

    def test_analysis_has_analyzed_at(self, db, sample_repo):
        repo_id = upsert_repo(db, sample_repo)
        upsert_analysis(db, repo_id, score=3, summary="Moderate interest")

        row = db.execute(
            "SELECT analyzed_at FROM analysis WHERE repo_id = ?", (repo_id,)
        ).fetchone()
        assert row["analyzed_at"] is not None
        # Should be a valid ISO datetime string
        parsed = datetime.fromisoformat(row["analyzed_at"])
        assert parsed is not None

    def test_tags_stored_in_their_own_table(self, db, sample_repo):
        repo_id = upsert_repo(db, sample_repo)
        upsert_analysis(db, repo_id, score=4, summary="Great tool", tags=["cli-tool", "automation"])

        assert get_tags(db, repo_id) == ["automation", "cli-tool"]

    def test_tags_default_to_empty(self, db, sample_repo):
        repo_id = upsert_repo(db, sample_repo)
        upsert_analysis(db, repo_id, score=4, summary="Great tool")

        assert get_tags(db, repo_id) == []

    def test_tags_overwritten_on_upsert(self, db, sample_repo):
        repo_id = upsert_repo(db, sample_repo)
        upsert_analysis(db, repo_id, score=2, summary="First analysis", tags=["old-tag"])
        upsert_analysis(db, repo_id, score=5, summary="Updated analysis", tags=["new-tag"])

        assert get_tags(db, repo_id) == ["new-tag"]

    def test_duplicate_tags_are_collapsed(self, db, sample_repo):
        repo_id = upsert_repo(db, sample_repo)
        upsert_analysis(db, repo_id, score=4, summary="x", tags=["cli", "cli", ""])

        assert get_tags(db, repo_id) == ["cli"]

    def test_tags_are_removed_with_the_repo(self, db, sample_repo):
        repo_id = upsert_repo(db, sample_repo)
        upsert_analysis(db, repo_id, score=4, summary="x", tags=["cli"])
        db.execute("DELETE FROM repositories WHERE id = ?", (repo_id,))

        assert get_tags(db, repo_id) == []


class TestGetReposToAnalyze:
    def test_excludes_analyzed_repos_by_default(self, db, sample_repo):
        repo_id = upsert_repo(db, sample_repo)
        upsert_analysis(db, repo_id, score=4, summary="Great tool", tags=["cli-tool"])

        result = get_repos_to_analyze(db, limit=20)
        assert result == []

    def test_includes_unanalyzed_repos(self, db, sample_repo):
        upsert_repo(db, sample_repo)

        result = get_repos_to_analyze(db, limit=20)
        assert len(result) == 1

    def test_force_includes_analyzed_repos_without_tags(self, db, sample_repo):
        repo_id = upsert_repo(db, sample_repo)
        upsert_analysis(db, repo_id, score=4, summary="Great tool")

        result = get_repos_to_analyze(db, limit=20, force=True)
        assert len(result) == 1

    def test_force_excludes_repos_already_tagged(self, db, sample_repo):
        repo_id = upsert_repo(db, sample_repo)
        upsert_analysis(db, repo_id, score=4, summary="Great tool", tags=["cli-tool"])

        result = get_repos_to_analyze(db, limit=20, force=True)
        assert result == []


class TestUpdateRepoMetadata:
    def test_updates_stars_archived_and_dates(self, db, sample_repo):
        repo_id = upsert_repo(db, sample_repo)
        update_repo_metadata(
            db,
            repo_id,
            stargazer_count=999,
            is_archived=True,
            created_at=datetime(2020, 5, 1, tzinfo=UTC),
            pushed_at=datetime(2025, 6, 2, tzinfo=UTC),
        )

        row = db.execute("SELECT * FROM repositories WHERE id = ?", (repo_id,)).fetchone()
        assert row["stargazer_count"] == 999
        assert row["is_archived"] == 1
        assert row["created_at"] == "2020-05-01T00:00:00+00:00"
        assert row["pushed_at"] == "2025-06-02T00:00:00+00:00"

    def test_null_dates_are_stored_as_null(self, db, sample_repo):
        repo_id = upsert_repo(db, sample_repo)
        update_repo_metadata(
            db,
            repo_id,
            stargazer_count=1,
            is_archived=False,
            created_at=None,
            pushed_at=None,
        )

        row = db.execute("SELECT * FROM repositories WHERE id = ?", (repo_id,)).fetchone()
        assert row["created_at"] is None
        assert row["pushed_at"] is None

    def test_refreshes_synced_at(self, db, sample_repo):
        repo_id = upsert_repo(db, sample_repo)
        db.execute("UPDATE repositories SET synced_at = ? WHERE id = ?", ("2000-01-01", repo_id))

        update_repo_metadata(
            db,
            repo_id,
            stargazer_count=5,
            is_archived=False,
            created_at=None,
            pushed_at=None,
        )
        after = db.execute(
            "SELECT synced_at FROM repositories WHERE id = ?", (repo_id,)
        ).fetchone()["synced_at"]
        assert after != "2000-01-01"
