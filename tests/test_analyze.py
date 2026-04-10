import json

import pytest

from starred.analyze import _build_prompt, _extract_json


class TestExtractJson:
    def test_plain_json(self):
        result = _extract_json('{"score": 4, "summary": "Great tool"}')
        assert result == {"score": 4, "summary": "Great tool"}

    def test_strips_json_fences(self):
        text = '```json\n{"score": 3, "summary": "OK"}\n```'
        result = _extract_json(text)
        assert result == {"score": 3, "summary": "OK"}

    def test_strips_plain_fences(self):
        text = '```\n{"score": 2, "summary": "Meh"}\n```'
        result = _extract_json(text)
        assert result == {"score": 2, "summary": "Meh"}

    def test_invalid_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            _extract_json("invalid json")

    @pytest.mark.xfail(
        reason="Validation of required keys not yet in _extract_json; pending fix",
        strict=True,
    )
    def test_missing_summary_raises_value_error(self):
        """_extract_json should raise ValueError when 'summary' key is missing."""
        with pytest.raises(ValueError):
            _extract_json('{"score": 4}')

    @pytest.mark.xfail(
        reason="Validation of required keys not yet in _extract_json; pending fix",
        strict=True,
    )
    def test_missing_score_raises_value_error(self):
        """_extract_json should raise ValueError when 'score' key is missing."""
        with pytest.raises(ValueError):
            _extract_json('{"summary": "OK"}')


class TestBuildPrompt:
    def test_contains_repo_name(self, sample_row_dict):
        prompt = _build_prompt(sample_row_dict)
        assert "octocat/hello-world" in prompt

    def test_contains_description(self, sample_row_dict):
        prompt = _build_prompt(sample_row_dict)
        assert "A test repository" in prompt

    def test_contains_language(self, sample_row_dict):
        prompt = _build_prompt(sample_row_dict)
        assert "Python" in prompt

    def test_contains_pushed_date(self, sample_row_dict):
        prompt = _build_prompt(sample_row_dict)
        # pushed_at[:10] → "2024-02-01"
        assert "2024-02-01" in prompt

    def test_no_readme_section_when_none(self, sample_row_dict):
        prompt = _build_prompt(sample_row_dict)
        assert "README" not in prompt

    def test_no_description_fallback(self, sample_row_dict):
        sample_row_dict["description"] = None
        prompt = _build_prompt(sample_row_dict)
        assert "(no description)" in prompt

    def test_no_language_fallback(self, sample_row_dict):
        sample_row_dict["primary_language"] = None
        prompt = _build_prompt(sample_row_dict)
        assert "unknown" in prompt

    def test_no_pushed_at_fallback(self, sample_row_dict):
        sample_row_dict["pushed_at"] = None
        prompt = _build_prompt(sample_row_dict)
        assert "unknown" in prompt

    def test_archived_yes(self, sample_row_dict):
        sample_row_dict["is_archived"] = 1
        prompt = _build_prompt(sample_row_dict)
        assert "Archived: yes" in prompt

    def test_archived_no(self, sample_row_dict):
        prompt = _build_prompt(sample_row_dict)
        assert "Archived: no" in prompt
