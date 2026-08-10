import json

from sermon_pipeline import parse_json3_captions, caption_coverage_ratio
from sermon_heuristics import Segment


def _write_json3(tmp_path, events):
    path = tmp_path / "captions.en.json3"
    path.write_text(json.dumps({"events": events}), encoding="utf-8")
    return str(path)


def test_parse_json3_captions_basic(tmp_path):
    events = [
        {"tStartMs": 1000, "dDurationMs": 2000, "segs": [{"utf8": "Hello "}, {"utf8": "world"}]},
        {"tStartMs": 3500, "dDurationMs": 1500, "segs": [{"utf8": "Second segment"}]},
    ]
    path = _write_json3(tmp_path, events)
    segments = parse_json3_captions(path)

    assert len(segments) == 2
    assert segments[0].text == "Hello world"
    assert segments[0].start == 1.0
    assert segments[0].end == 3.0
    assert segments[1].text == "Second segment"


def test_parse_json3_captions_skips_empty_and_newline_only_events(tmp_path):
    events = [
        {"tStartMs": 1000, "dDurationMs": 500, "segs": [{"utf8": "\n"}]},
        {"tStartMs": 2000, "segs": []},
        {"tStartMs": 3000, "dDurationMs": 1000, "segs": [{"utf8": "Real text"}]},
    ]
    path = _write_json3(tmp_path, events)
    segments = parse_json3_captions(path)

    assert len(segments) == 1
    assert segments[0].text == "Real text"


def test_parse_json3_captions_empty_events(tmp_path):
    path = _write_json3(tmp_path, [])
    assert parse_json3_captions(path) == []


def test_parse_json3_captions_missing_start_is_skipped(tmp_path):
    events = [{"segs": [{"utf8": "no start time"}]}]
    path = _write_json3(tmp_path, events)
    assert parse_json3_captions(path) == []


class TestCaptionCoverageRatio:
    def test_full_coverage(self):
        segments = [Segment(start=0, end=100, text="x")]
        assert caption_coverage_ratio(segments, 100) == 1.0

    def test_partial_coverage(self):
        segments = [Segment(start=10, end=60, text="x")]
        assert caption_coverage_ratio(segments, 100) == 0.5

    def test_no_segments_returns_zero(self):
        assert caption_coverage_ratio([], 100) == 0.0

    def test_zero_duration_returns_zero(self):
        segments = [Segment(start=0, end=10, text="x")]
        assert caption_coverage_ratio(segments, 0) == 0.0

    def test_negative_span_clamped_to_zero(self):
        # Pathological/out-of-order input shouldn't produce a negative ratio.
        segments = [Segment(start=50, end=10, text="x")]
        assert caption_coverage_ratio(segments, 100) == 0.0
