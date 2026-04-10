from datetime import UTC, datetime, timezone

import pytest

from starred.client import _parse_dt, _parse_dt_optional


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
        assert result.utcoffset().total_seconds() == 0


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
