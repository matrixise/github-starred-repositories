import sqlite3
from pathlib import Path

import frontmatter
import pytest
from click.testing import CliRunner

from starred.cli import _build_note, _is_managed_note, main
from starred.db import open_db, upsert_analysis, upsert_repo

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


class TestIsManagedNote:
    def test_true_for_exported_note(self, tmp_path: Path):
        path = tmp_path / "note.md"
        path.write_text(_build_note(_row(), TAG), encoding="utf-8")
        assert _is_managed_note(path, TAG) is True

    def test_false_for_foreign_note(self, tmp_path: Path):
        path = tmp_path / "personal.md"
        path.write_text("---\ntags: [my-own]\n---\n\nHand written\n", encoding="utf-8")
        assert _is_managed_note(path, TAG) is False

    def test_false_without_frontmatter(self, tmp_path: Path):
        path = tmp_path / "plain.md"
        path.write_text("Just some notes\n", encoding="utf-8")
        assert _is_managed_note(path, TAG) is False

    def test_false_on_broken_yaml(self, tmp_path: Path):
        path = tmp_path / "broken.md"
        path.write_text("---\ntags: [unclosed\n---\n\nBody\n", encoding="utf-8")
        assert _is_managed_note(path, TAG) is False


@pytest.fixture
def populated_db(tmp_path: Path, sample_repo) -> Path:
    db_path = tmp_path / "test.db"
    with open_db(db_path) as conn:
        repo_id = upsert_repo(conn, sample_repo)
        upsert_analysis(conn, repo_id, 5, "A test repository", ["cli-tool"])
    return db_path


class TestExportObsidian:
    def _run(self, db_path: Path, vault: Path, *args):
        return CliRunner().invoke(
            main,
            ["export-obsidian", "--vault", str(vault), "--db", str(db_path), *args],
        )

    def test_creates_note(self, populated_db, tmp_path):
        vault = tmp_path / "vault"
        result = self._run(populated_db, vault)
        assert result.exit_code == 0
        note = vault / "Sources" / "GitHub Stars" / "octocat - hello-world.md"
        assert frontmatter.load(note)["score"] == 5

    def test_second_run_reports_unchanged(self, populated_db, tmp_path):
        vault = tmp_path / "vault"
        self._run(populated_db, vault)
        result = self._run(populated_db, vault)
        assert "1 unchanged" in result.output

    def test_prune_keeps_foreign_notes(self, populated_db, tmp_path):
        vault = tmp_path / "vault"
        self._run(populated_db, vault)
        dest = vault / "Sources" / "GitHub Stars"
        mine = dest / "My own note.md"
        mine.write_text("---\ntags: [personal]\n---\n\nKeep me\n", encoding="utf-8")
        stale = dest / "old - repo.md"
        stale.write_text(_build_note(_row(), "github/starred"), encoding="utf-8")

        result = self._run(populated_db, vault, "--prune")
        assert mine.exists()
        assert not stale.exists()
        assert "1 foreign notes kept" in result.output

    def test_dry_run_writes_nothing(self, populated_db, tmp_path):
        vault = tmp_path / "vault"
        result = self._run(populated_db, vault, "--dry-run")
        assert "Dry run." in result.output
        assert not (vault / "Sources" / "GitHub Stars").exists()

    def test_missing_db_exits_with_error(self, tmp_path):
        result = self._run(tmp_path / "missing.db", tmp_path / "vault")
        assert result.exit_code == 1
        assert "Database not found" in result.output
