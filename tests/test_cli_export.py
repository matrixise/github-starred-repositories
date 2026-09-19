import sqlite3

import frontmatter
import pytest

from starred.cli import _build_note

TAG = "github/starred"


def _row(**overrides) -> sqlite3.Row:
    base = {
        "name_with_owner": "octocat/hello-world",
        "description": "A test repository",
        "url": "https://github.com/octocat/hello-world",
        "primary_language": "Python",
        "starred_at": "2024-03-15T10:30:00+00:00",
        "created_at": "2023-01-10T12:00:00+00:00",
        "pushed_at": "2024-02-01T08:00:00+00:00",
        "stargazer_count": 42,
        "is_archived": 0,
        "score": 5,
        "summary": "A test repository",
        "topics": "python, testing",
        "tags": "cli-tool, automation",
    }
    base.update(overrides)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cols = ", ".join(f"? AS {k}" for k in base)
    return conn.execute(f"SELECT {cols}", tuple(base.values())).fetchone()


class TestBuildNote:
    def test_frontmatter_is_parseable(self):
        post = frontmatter.loads(_build_note(_row(), TAG))
        assert post["title"] == "octocat/hello-world"
        assert post["score"] == 5
        assert post["topics"] == ["python", "testing"]
        assert post["stars"] == 42
        assert post["archived"] is False
        assert post["created_at"] == "2023-01-10"

    def test_tags_combine_export_language_and_analysis(self):
        post = frontmatter.loads(_build_note(_row(), TAG))
        assert post["tags"] == [TAG, "python", "cli-tool", "automation"]

    @pytest.mark.parametrize(
        "summary",
        [
            'He said "hello" loudly',
            "Tool: does things",
            "Ends with a backslash \\",
            "Multi\nline summary",
            "- leading dash",
        ],
    )
    def test_special_characters_survive_a_roundtrip(self, summary):
        post = frontmatter.loads(_build_note(_row(summary=summary), TAG))
        assert post["summary"] == summary

    def test_missing_dates_are_null(self):
        post = frontmatter.loads(_build_note(_row(created_at=None, pushed_at=None), TAG))
        assert post["created_at"] is None
        assert "Last push:** unknown" in post.content

    def test_body_mentions_archived_repos(self):
        post = frontmatter.loads(_build_note(_row(is_archived=1), TAG))
        assert post["archived"] is True
        assert "Archived" in post.content
