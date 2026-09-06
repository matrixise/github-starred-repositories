from datetime import UTC, datetime

import pytest

from starred.client import _parse_dt, _parse_dt_optional, _parse_edge, _parse_metadata


class TestParseDt:
    def test_z_suffix(self):
        result = _parse_dt("2024-03-15T10:30:00Z")
        assert result == datetime(2024, 3, 15, 10, 30, 0, tzinfo=UTC)

    @pytest.mark.xfail(
        raises=ValueError,
        reason="_parse_dt appends +00:00 unconditionally; breaks when input already has offset",
        strict=True,
    )
    def test_utc_offset(self):
        # NOTE: current implementation strips "Z" and appends "+00:00".
        # When the input already carries "+00:00", the double-appended suffix
        # causes a ValueError. This test documents the known limitation.
        result = _parse_dt("2024-03-15T10:30:00+00:00")
        assert result == datetime(2024, 3, 15, 10, 30, 0, tzinfo=UTC)

    @pytest.mark.xfail(
        raises=ValueError,
        reason="Depends on test_utc_offset: _parse_dt cannot parse pre-offset strings",
        strict=True,
    )
    def test_z_and_offset_equal(self):
        dt_z = _parse_dt("2024-03-15T10:30:00Z")
        dt_offset = _parse_dt("2024-03-15T10:30:00+00:00")
        assert dt_z == dt_offset

    def test_returns_timezone_aware(self):
        result = _parse_dt("2024-03-15T10:30:00Z")
        assert result.tzinfo is not None
        offset = result.utcoffset()
        assert offset is not None and offset.total_seconds() == 0


class TestParseDtOptional:
    def test_none_returns_none(self):
        assert _parse_dt_optional(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_dt_optional("") is None

    def test_valid_string_returns_datetime(self):
        result = _parse_dt_optional("2024-03-15T10:30:00Z")
        assert result == datetime(2024, 3, 15, 10, 30, 0, tzinfo=UTC)

    def test_returns_timezone_aware(self):
        result = _parse_dt_optional("2024-03-15T10:30:00Z")
        assert result is not None
        assert result.tzinfo is not None


class TestParseEdge:
    @pytest.fixture
    def edge(self) -> dict:
        return {
            "starredAt": "2024-03-15T10:30:00Z",
            "node": {
                "nameWithOwner": "octocat/hello-world",
                "description": "A test repository",
                "url": "https://github.com/octocat/hello-world",
                "isArchived": False,
                "createdAt": "2023-01-10T12:00:00Z",
                "pushedAt": "2024-02-01T08:00:00Z",
                "stargazerCount": 42,
                "primaryLanguage": {"name": "Python"},
                "repositoryTopics": {"nodes": [{"topic": {"name": "python"}}]},
            },
        }

    def test_parses_created_at(self, edge):
        repo = _parse_edge(edge)
        assert repo.created_at == datetime(2023, 1, 10, 12, 0, 0, tzinfo=UTC)

    def test_missing_created_at_is_none(self, edge):
        del edge["node"]["createdAt"]
        assert _parse_edge(edge).created_at is None

    def test_parses_remaining_fields(self, edge):
        repo = _parse_edge(edge)
        assert repo.name_with_owner == "octocat/hello-world"
        assert repo.primary_language == "Python"
        assert repo.topics == ["python"]
        assert repo.stargazer_count == 42


class TestParseMetadata:
    @pytest.fixture
    def node(self) -> dict:
        return {
            "stargazerCount": 1234,
            "isArchived": True,
            "createdAt": "2023-01-10T12:00:00Z",
            "pushedAt": "2024-02-01T08:00:00Z",
        }

    def test_parses_all_fields(self, node):
        meta = _parse_metadata(node)
        assert meta.stargazer_count == 1234
        assert meta.is_archived is True
        assert meta.created_at == datetime(2023, 1, 10, 12, 0, 0, tzinfo=UTC)
        assert meta.pushed_at == datetime(2024, 2, 1, 8, 0, 0, tzinfo=UTC)

    def test_missing_dates_are_none(self, node):
        del node["createdAt"]
        node["pushedAt"] = None
        meta = _parse_metadata(node)
        assert meta.created_at is None
        assert meta.pushed_at is None
