from pathlib import Path

import pytest

from starred.readme import save_readme


class TestSaveReadme:
    def test_creates_file(self, tmp_path: Path):
        save_readme("# Hello World", "octocat/hello-world", tmp_path)
        dest = tmp_path / "octocat" / "hello-world" / "README.md"
        assert dest.exists()

    def test_returns_correct_path(self, tmp_path: Path):
        result = save_readme("# Hello World", "octocat/hello-world", tmp_path)
        expected = tmp_path / "octocat" / "hello-world" / "README.md"
        assert result == expected

    def test_file_content_is_correct(self, tmp_path: Path):
        content = "# Hello World\n\nThis is a test readme."
        save_readme(content, "octocat/hello-world", tmp_path)
        dest = tmp_path / "octocat" / "hello-world" / "README.md"
        assert dest.read_text(encoding="utf-8") == content

    def test_creates_parent_directories(self, tmp_path: Path):
        save_readme("content", "deep-owner/deep-repo", tmp_path)
        assert (tmp_path / "deep-owner" / "deep-repo").is_dir()

    def test_different_owner_and_repo(self, tmp_path: Path):
        result = save_readme("# Rust\n", "rust-lang/rust", tmp_path)
        expected = tmp_path / "rust-lang" / "rust" / "README.md"
        assert result == expected
        assert expected.exists()

    def test_overwrites_existing_file(self, tmp_path: Path):
        save_readme("original content", "octocat/hello-world", tmp_path)
        save_readme("updated content", "octocat/hello-world", tmp_path)
        dest = tmp_path / "octocat" / "hello-world" / "README.md"
        assert dest.read_text(encoding="utf-8") == "updated content"
