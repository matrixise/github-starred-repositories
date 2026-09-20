import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from starred.cli import _build_digest, _group_by_theme, _parse_since, _repo_age, main
from starred.db import get_repos_starred_since, open_db, upsert_analysis, upsert_repo
from starred.models import StarredRepo


def _row(**overrides) -> sqlite3.Row:
    base = {
        "name_with_owner": "octocat/hello-world",
        "description": "A test repository",
        "url": "https://github.com/octocat/hello-world",
        "primary_language": "Python",
        "starred_at": "2026-09-19T10:00:00+00:00",
        "created_at": "2024-01-10T12:00:00+00:00",
        "pushed_at": "2026-09-01T08:00:00+00:00",
        "stargazer_count": 1234,
        "is_archived": 0,
        "score": 4,
        "summary": "Does something useful",
        "topics": "python",
        "tags": "cli-tool, automation",
    }
    base.update(overrides)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cols = ", ".join(f"? AS {k}" for k in base)
    return conn.execute(f"SELECT {cols}", tuple(base.values())).fetchone()


class TestParseSince:
    @pytest.mark.parametrize(
        ("value", "delta"),
        [
            ("24h", timedelta(hours=24)),
            ("7d", timedelta(days=7)),
            ("2w", timedelta(weeks=2)),
            ("90m", timedelta(minutes=90)),
            ("  12H  ", timedelta(hours=12)),
        ],
    )
    def test_durations(self, value, delta):
        result = _parse_since(value)
        assert abs((datetime.now(UTC) - delta) - result) < timedelta(seconds=5)

    def test_iso_date_is_assumed_utc(self):
        assert _parse_since("2026-09-19") == datetime(2026, 9, 19, tzinfo=UTC)

    def test_iso_datetime_keeps_its_offset(self):
        assert _parse_since("2026-09-19T10:00:00+00:00") == datetime(2026, 9, 19, 10, 0, tzinfo=UTC)

    @pytest.mark.parametrize("value", ["yesterday", "24", "h24", "", "3y"])
    def test_invalid_values_are_rejected(self, value):
        with pytest.raises(click.BadParameter):
            _parse_since(value)


class TestRepoAge:
    NOW = datetime(2026, 9, 20, tzinfo=UTC)

    @pytest.mark.parametrize(
        ("created", "expected"),
        [
            ("2026-09-20T09:00:00+00:00", "créé aujourd'hui"),
            ("2026-09-01T00:00:00+00:00", "créé il y a 19 jours"),
            ("2026-01-09T00:00:00+00:00", "créé il y a 8 mois"),
            ("2020-07-16T00:00:00+00:00", "créé il y a 6 ans"),
        ],
    )
    def test_formats(self, created, expected):
        assert _repo_age(_row(created_at=created), self.NOW) == expected

    def test_unknown_when_missing(self):
        assert _repo_age(_row(created_at=None), self.NOW) == "âge inconnu"


class TestGroupByTheme:
    def test_shared_tag_forms_a_theme(self):
        rows = [
            _row(name_with_owner="a/a", tags="mcp, cli-tool"),
            _row(name_with_owner="b/b", tags="mcp, ai-agents"),
        ]
        groups = _group_by_theme(rows)
        assert list(groups) == ["mcp"]

    def test_unique_tags_fall_back_to_other(self):
        rows = [_row(name_with_owner="a/a", tags="one"), _row(name_with_owner="b/b", tags="two")]
        assert list(_group_by_theme(rows)) == ["autres"]

    def test_other_is_listed_last(self):
        rows = [
            _row(name_with_owner="a/a", tags="mcp"),
            _row(name_with_owner="b/b", tags="mcp"),
            _row(name_with_owner="c/c", tags="alone"),
            _row(name_with_owner="d/d", tags="solo"),
            _row(name_with_owner="e/e", tags="unique"),
        ]
        assert list(_group_by_theme(rows)) == ["mcp", "autres"]

    def test_repos_sorted_by_score_then_stars(self):
        rows = [
            _row(name_with_owner="low/score", tags="mcp", score=3, stargazer_count=9000),
            _row(name_with_owner="few/stars", tags="mcp", score=5, stargazer_count=10),
            _row(name_with_owner="many/stars", tags="mcp", score=5, stargazer_count=500),
        ]
        names = [r["name_with_owner"] for r in _group_by_theme(rows)["mcp"]]
        assert names == ["many/stars", "few/stars", "low/score"]

    def test_repos_without_tags_are_grouped(self):
        assert list(_group_by_theme([_row(tags=None)])) == ["autres"]


class TestBuildDigest:
    SINCE = datetime(2026, 9, 19, 14, 0, tzinfo=UTC)

    def test_header_counts_repos_and_scores(self):
        out = _build_digest([_row(score=5), _row(name_with_owner="b/b", score=4)], self.SINCE)
        assert "# 2 nouveaux dépôts depuis le 19/09/2026 14:00 UTC" in out
        assert "1 × 5/5, 1 × 4/5" in out

    def test_unanalyzed_repos_are_flagged(self):
        out = _build_digest([_row(score=None, summary=None)], self.SINCE)
        assert "1 sans analyse" in out
        assert "non analysé" in out
        assert "A test repository" in out

    def test_entry_carries_facts_and_url(self):
        out = _build_digest([_row()], self.SINCE)
        assert "**octocat/hello-world** (4/5, Python, 1 234 ★" in out
        assert "https://github.com/octocat/hello-world" in out
        assert "Tags : cli-tool, automation" in out


@pytest.fixture
def digest_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    recent = datetime.now(UTC) - timedelta(hours=2)
    old = datetime.now(UTC) - timedelta(days=10)
    with open_db(db_path) as conn:
        for name, starred in (("new/repo", recent), ("old/repo", old)):
            repo_id = upsert_repo(
                conn,
                StarredRepo(
                    starred_at=starred,
                    name_with_owner=name,
                    description="desc",
                    topics=["python"],
                    is_archived=False,
                    created_at=datetime(2024, 1, 1, tzinfo=UTC),
                    pushed_at=None,
                    url=f"https://github.com/{name}",
                    primary_language="Python",
                    stargazer_count=10,
                ),
            )
            upsert_analysis(conn, repo_id, 4, f"summary of {name}", ["cli-tool"])
    return db_path


class TestGetReposStarredSince:
    def test_only_returns_recent_repos(self, digest_db):
        with open_db(digest_db) as conn:
            rows = get_repos_starred_since(conn, datetime.now(UTC) - timedelta(hours=24))
        assert [r["name_with_owner"] for r in rows] == ["new/repo"]

    def test_min_score_filters(self, digest_db):
        with open_db(digest_db) as conn:
            rows = get_repos_starred_since(
                conn, datetime.now(UTC) - timedelta(days=30), min_score=5
            )
        assert rows == []

    def test_tags_are_joined(self, digest_db):
        with open_db(digest_db) as conn:
            rows = get_repos_starred_since(conn, datetime.now(UTC) - timedelta(days=30))
        assert rows[0]["tags"] == "cli-tool"


class TestDigestCommand:
    def _run(self, db_path: Path, *args):
        return CliRunner().invoke(main, ["digest", "--db", str(db_path), *args])

    def test_default_window_is_24h(self, digest_db):
        result = self._run(digest_db)
        assert result.exit_code == 0
        assert "new/repo" in result.output
        assert "old/repo" not in result.output

    def test_since_widens_the_window(self, digest_db):
        result = self._run(digest_db, "--since", "30d")
        assert "old/repo" in result.output

    def test_empty_window_reports_nothing(self, digest_db):
        result = self._run(digest_db, "--since", "1m")
        assert result.exit_code == 0
        assert "No repository starred since" in result.output

    def test_invalid_since_is_rejected(self, digest_db):
        result = self._run(digest_db, "--since", "yesterday")
        assert result.exit_code != 0
        assert "not a duration" in result.output

    def test_missing_db_exits_with_error(self, tmp_path):
        result = self._run(tmp_path / "missing.db")
        assert result.exit_code == 1
        assert "Database not found" in result.output
