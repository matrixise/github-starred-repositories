import asyncio
import json
import sqlite3

import pytest

from starred import analyze as analyze_mod
from starred.analyze import AnalysisResult, _build_prompt, _extract_json, _normalize_tag


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


class TestNormalizeTag:
    def test_lowercases(self):
        assert _normalize_tag("CLI Tool") == "cli-tool"

    def test_collapses_non_alnum_to_dash(self):
        assert _normalize_tag("data_visualization!!") == "data-visualization"

    def test_strips_leading_trailing_dashes(self):
        assert _normalize_tag("  -web framework- ") == "web-framework"


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


class TestAnalyzeAllAsync:
    @staticmethod
    def _rows(n: int) -> list[sqlite3.Row]:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        return [
            conn.execute("SELECT ? AS id, ? AS name_with_owner", (i, f"owner/repo{i}")).fetchone()
            for i in range(n)
        ]

    @pytest.mark.asyncio
    async def test_yields_one_result_per_row(self, monkeypatch):
        async def fake(row):
            return AnalysisResult(row["id"], 4, "ok", ["tag"])

        monkeypatch.setattr(analyze_mod, "_analyze_one", fake)
        out = [r async for r in analyze_mod.analyze_all_async(self._rows(5), concurrency=2)]

        assert len(out) == 5
        assert {row["id"] for row, _, _ in out} == {0, 1, 2, 3, 4}
        assert all(error is None and result is not None for _, result, error in out)

    @pytest.mark.asyncio
    async def test_failures_are_reported_not_raised(self, monkeypatch):
        async def fake(row):
            if row["id"] == 1:
                raise ValueError("boom")
            return AnalysisResult(row["id"], 3, "ok", [])

        monkeypatch.setattr(analyze_mod, "_analyze_one", fake)
        out = [r async for r in analyze_mod.analyze_all_async(self._rows(3), concurrency=3)]

        failures = [(row, error) for row, _, error in out if error is not None]
        assert len(failures) == 1
        assert failures[0][0]["id"] == 1
        assert str(failures[0][1]) == "boom"
        assert sum(1 for _, result, _ in out if result is not None) == 2

    @pytest.mark.asyncio
    async def test_concurrency_limit_is_honoured(self, monkeypatch):
        running = 0
        peak = 0

        async def fake(row):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1
            return AnalysisResult(row["id"], 4, "ok", [])

        monkeypatch.setattr(analyze_mod, "_analyze_one", fake)
        [r async for r in analyze_mod.analyze_all_async(self._rows(8), concurrency=2)]

        assert peak <= 2

    @pytest.mark.asyncio
    async def test_empty_input(self, monkeypatch):
        out = [r async for r in analyze_mod.analyze_all_async([], concurrency=2)]
        assert out == []
