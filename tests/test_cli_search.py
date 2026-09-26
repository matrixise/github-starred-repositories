from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from starred.cli import main
from starred.db import open_db, search_repos, upsert_analysis, upsert_repo
from starred.models import StarredRepo


def _repo(name: str, **overrides) -> StarredRepo:
    base = {
        "starred_at": datetime(2026, 3, 15, tzinfo=UTC),
        "name_with_owner": name,
        "description": None,
        "topics": [],
        "is_archived": False,
        "created_at": datetime(2024, 1, 10, tzinfo=UTC),
        "pushed_at": datetime(2026, 2, 1, tzinfo=UTC),
        "url": f"https://github.com/{name}",
        "primary_language": "Go",
        "stargazer_count": 100,
    }
    base.update(overrides)
    return StarredRepo(**base)


@pytest.fixture
def catalogue(tmp_path: Path) -> Path:
    """A small catalogue covering every field the search looks at."""
    db_path = tmp_path / "test.db"
    with open_db(db_path) as conn:
        # matches on summary + tags -> 2 keywords
        rid = upsert_repo(conn, _repo("acme/open-notebook", description="A notebook clone"))
        upsert_analysis(
            conn, rid, 4, "Self-hosted alternative with podcast generation", ["podcast"]
        )

        # matches on description only -> 1 keyword, but far more stars
        rid = upsert_repo(
            conn, _repo("big/popular", description="A podcast player", stargazer_count=99999)
        )
        upsert_analysis(conn, rid, 5, "Very popular thing", ["audio"])

        # matches on topics
        rid = upsert_repo(conn, _repo("some/tunnel", topics=["tunnel", "networking"]))
        upsert_analysis(conn, rid, 3, "Networking helper", ["networking"])

        # archived, matches on name
        rid = upsert_repo(conn, _repo("dead/podcast-tool", is_archived=True))
        upsert_analysis(conn, rid, 2, "Abandoned", ["legacy"])

        # never analyzed, matches on description
        upsert_repo(conn, _repo("raw/notebook-thing", description="notebook utilities"))

        upsert_repo(conn, _repo("unrelated/thing", description="nothing to see"))
    return db_path


class TestSearchRepos:
    def _names(self, rows) -> list[str]:
        return [r["name_with_owner"] for r in rows]

    def test_ranks_by_keywords_matched_before_stars(self, catalogue):
        with open_db(catalogue) as conn:
            rows = search_repos(conn, ["podcast", "self-hosted"])
        # acme matches 2 keywords with 100 stars, big/popular matches 1 with 99999
        assert self._names(rows)[0] == "acme/open-notebook"
        assert rows[0]["matched"] == 2
        assert "big/popular" in self._names(rows)

    def test_matches_on_topics(self, catalogue):
        with open_db(catalogue) as conn:
            rows = search_repos(conn, ["tunnel"])
        assert self._names(rows) == ["some/tunnel"]

    def test_matches_on_tags(self, catalogue):
        with open_db(catalogue) as conn:
            rows = search_repos(conn, ["audio"])
        assert self._names(rows) == ["big/popular"]

    def test_matches_on_name(self, catalogue):
        with open_db(catalogue) as conn:
            rows = search_repos(conn, ["dead/"])
        assert self._names(rows) == ["dead/podcast-tool"]

    def test_unanalyzed_repos_are_returned(self, catalogue):
        with open_db(catalogue) as conn:
            rows = search_repos(conn, ["utilities"])
        assert self._names(rows) == ["raw/notebook-thing"]
        assert rows[0]["score"] is None

    def test_no_match_returns_empty(self, catalogue):
        with open_db(catalogue) as conn:
            assert search_repos(conn, ["kubernetes"]) == []

    def test_min_score_filters(self, catalogue):
        with open_db(catalogue) as conn:
            rows = search_repos(conn, ["podcast"], min_score=4)
        names = self._names(rows)
        assert "dead/podcast-tool" not in names  # scored 2
        assert {"acme/open-notebook", "big/popular"} == set(names)

    def test_min_score_excludes_unanalyzed(self, catalogue):
        with open_db(catalogue) as conn:
            rows = search_repos(conn, ["notebook"], min_score=1)
        assert "raw/notebook-thing" not in self._names(rows)

    def test_no_archived_excludes(self, catalogue):
        with open_db(catalogue) as conn:
            rows = search_repos(conn, ["podcast"], include_archived=False)
        assert "dead/podcast-tool" not in self._names(rows)

    def test_archived_included_by_default(self, catalogue):
        with open_db(catalogue) as conn:
            rows = search_repos(conn, ["podcast"])
        assert "dead/podcast-tool" in self._names(rows)

    def test_limit_is_honoured(self, catalogue):
        with open_db(catalogue) as conn:
            assert len(search_repos(conn, ["podcast"], limit=1)) == 1

    def test_case_insensitive(self, catalogue):
        with open_db(catalogue) as conn:
            assert search_repos(conn, ["PODCAST"]) == search_repos(conn, ["podcast"])

    def test_multi_word_keyword(self, catalogue):
        with open_db(catalogue) as conn:
            rows = search_repos(conn, ["podcast generation"])
        assert self._names(rows) == ["acme/open-notebook"]

    def test_empty_keywords_rejected(self, catalogue):
        with open_db(catalogue) as conn, pytest.raises(ValueError):
            search_repos(conn, [])

    def test_each_keyword_counts_once(self, catalogue):
        """A keyword appearing in several fields still counts as one match."""
        with open_db(catalogue) as conn:
            rows = search_repos(conn, ["podcast"])
        assert all(r["matched"] == 1 for r in rows)


class TestSearchCommand:
    def _run(self, db_path: Path, *args):
        return CliRunner().invoke(main, ["search", "--db", str(db_path), *args])

    def test_lists_matches_with_rank(self, catalogue):
        result = self._run(catalogue, "podcast", "self-hosted")
        assert result.exit_code == 0
        assert "[2] **acme/open-notebook**" in result.output
        assert "https://github.com/acme/open-notebook" in result.output

    def test_reports_no_match(self, catalogue):
        result = self._run(catalogue, "kubernetes")
        assert result.exit_code == 0
        assert "No repository matching: kubernetes" in result.output

    def test_flags_archived_repos(self, catalogue):
        result = self._run(catalogue, "podcast")
        assert "**ARCHIVÉ**" in result.output

    def test_no_archived_option(self, catalogue):
        result = self._run(catalogue, "podcast", "--no-archived")
        assert "dead/podcast-tool" not in result.output

    def test_unanalyzed_repo_is_labelled(self, catalogue):
        result = self._run(catalogue, "utilities")
        assert "non analysé" in result.output

    def test_keywords_are_required(self, catalogue):
        result = self._run(catalogue)
        assert result.exit_code != 0

    def test_missing_db_exits_with_error(self, tmp_path):
        result = self._run(tmp_path / "missing.db", "podcast")
        assert result.exit_code == 1
        assert "Database not found" in result.output
