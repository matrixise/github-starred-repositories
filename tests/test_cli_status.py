from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from starred.cli import main
from starred.db import get_repos_to_analyze, get_status, open_db, upsert_analysis, upsert_repo
from starred.models import StarredRepo


def _repo(name: str, **overrides) -> StarredRepo:
    base = {
        "starred_at": datetime(2026, 3, 15, tzinfo=UTC),
        "name_with_owner": name,
        "description": "desc",
        "topics": [],
        "is_archived": False,
        "created_at": datetime(2024, 1, 10, tzinfo=UTC),
        "pushed_at": datetime(2026, 2, 1, tzinfo=UTC),
        "url": f"https://github.com/{name}",
        "primary_language": "Go",
        "stargazer_count": 10,
    }
    base.update(overrides)
    return StarredRepo(**base)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


class TestGetStatus:
    def test_empty_catalogue(self, db_path):
        with open_db(db_path) as conn:
            assert get_status(conn) == {
                "repositories": 0,
                "analyses": 0,
                "missing_analysis": 0,
                "missing_readme": 0,
                "missing_tags": 0,
                "archived": 0,
                "unresolved": 0,
            }

    def test_counts_every_dimension(self, db_path):
        with open_db(db_path) as conn:
            analyzed = upsert_repo(conn, _repo("a/analyzed"))
            upsert_analysis(conn, analyzed, 4, "ok", ["tag"])
            no_tags = upsert_repo(conn, _repo("b/no-tags"))
            upsert_analysis(conn, no_tags, 3, "ok")
            upsert_repo(conn, _repo("c/pending"))
            upsert_repo(conn, _repo("d/archived", is_archived=True))
            upsert_repo(conn, _repo("e/unresolved", created_at=None))

            counters = get_status(conn)

        assert counters["repositories"] == 5
        assert counters["analyses"] == 2
        assert counters["missing_analysis"] == 3
        assert counters["missing_tags"] == 1
        assert counters["missing_readme"] == 5  # none fetched in this fixture
        assert counters["archived"] == 1
        assert counters["unresolved"] == 1


class TestGetReposToAnalyzeAll:
    def test_none_limit_returns_everything(self, db_path):
        with open_db(db_path) as conn:
            for i in range(25):
                upsert_repo(conn, _repo(f"owner/repo{i}"))
            assert len(get_repos_to_analyze(conn, None)) == 25
            assert len(get_repos_to_analyze(conn, 20)) == 20


class TestStatusCommand:
    def _run(self, db_path: Path, *args):
        return CliRunner().invoke(main, ["status", "--db", str(db_path), *args])

    def test_reports_counters(self, db_path):
        with open_db(db_path) as conn:
            rid = upsert_repo(conn, _repo("a/analyzed"))
            upsert_analysis(conn, rid, 4, "ok", ["tag"])
        result = self._run(db_path)
        assert result.exit_code == 0
        assert "Repositories" in result.output

    def test_complete_catalogue_is_announced(self, db_path):
        with open_db(db_path) as conn:
            rid = upsert_repo(conn, _repo("a/analyzed"))
            upsert_analysis(conn, rid, 4, "ok", ["tag"])
            conn.execute("UPDATE repositories SET readme_path = 'x'")
        result = self._run(db_path)
        assert "Catalogue complete." in result.output

    def test_strict_fails_on_missing_analysis(self, db_path):
        with open_db(db_path) as conn:
            upsert_repo(conn, _repo("a/pending"))
        result = self._run(db_path, "--strict")
        assert result.exit_code == 1
        assert "1 repositories without analysis" in result.output
        assert "analyze --all" in result.output

    def test_strict_fails_on_missing_tags(self, db_path):
        with open_db(db_path) as conn:
            rid = upsert_repo(conn, _repo("a/no-tags"))
            upsert_analysis(conn, rid, 4, "ok")
        result = self._run(db_path, "--strict")
        assert result.exit_code == 1
        assert "without tags" in result.output

    def test_strict_tolerates_missing_readme(self, db_path):
        """A repo with no README on GitHub must never fail the pipeline."""
        with open_db(db_path) as conn:
            rid = upsert_repo(conn, _repo("a/no-readme"))
            upsert_analysis(conn, rid, 4, "ok", ["tag"])
        result = self._run(db_path, "--strict")
        assert result.exit_code == 0
        assert "without README" in result.output

    def test_non_strict_never_fails(self, db_path):
        with open_db(db_path) as conn:
            upsert_repo(conn, _repo("a/pending"))
        assert self._run(db_path).exit_code == 0

    def test_missing_db_exits_with_error(self, tmp_path):
        result = self._run(tmp_path / "missing.db")
        assert result.exit_code == 1
        assert "Database not found" in result.output
