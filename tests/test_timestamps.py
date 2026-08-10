import pytest

from sermon_pipeline import parse_timestamp_to_seconds, parse_iso8601_duration


class TestParseTimestampToSeconds:
    def test_plain_seconds_int(self):
        assert parse_timestamp_to_seconds("1234") == 1234.0

    def test_plain_seconds_float(self):
        assert parse_timestamp_to_seconds("1234.5") == 1234.5

    def test_mm_ss(self):
        assert parse_timestamp_to_seconds("20:50") == 20 * 60 + 50

    def test_h_mm_ss(self):
        assert parse_timestamp_to_seconds("1:02:30") == 3600 + 2 * 60 + 30

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            parse_timestamp_to_seconds("")

    def test_garbage_raises(self):
        with pytest.raises(ValueError):
            parse_timestamp_to_seconds("not-a-time")


class TestParseIso8601Duration:
    def test_hours_minutes_seconds(self):
        assert parse_iso8601_duration("PT1H12M4S") == 3600 + 12 * 60 + 4

    def test_minutes_seconds_only(self):
        assert parse_iso8601_duration("PT45M30S") == 45 * 60 + 30

    def test_seconds_only(self):
        assert parse_iso8601_duration("PT90S") == 90

    def test_hours_only(self):
        assert parse_iso8601_duration("PT2H") == 7200

    def test_fractional_seconds(self):
        assert parse_iso8601_duration("PT1M2.5S") == 62.5

    def test_zero_duration(self):
        assert parse_iso8601_duration("PT0S") == 0

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_iso8601_duration("not-a-duration")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            parse_iso8601_duration("")
