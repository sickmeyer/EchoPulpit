from sermon_heuristics import Segment, score_segment, pick_sermon_window, extract_sermon


def test_score_segment_rewards_sermon_cues():
    assert score_segment("Turn to the book of John chapter 3") > 0


def test_score_segment_penalizes_non_sermon_cues():
    worship = score_segment("Let's stand and sing the chorus together")
    sermon = score_segment("Today we look at faith and grace in the gospel")
    assert worship < sermon


def test_pick_sermon_window_empty_segments():
    assert pick_sermon_window([], window_minutes=12) == (0, 0)


def _make_segments():
    segments = []
    t = 0.0
    # Announcements / worship preamble (should score low/negative)
    for _ in range(10):
        segments.append(Segment(start=t, end=t + 30, text="Welcome, let's stand and sing the chorus"))
        t += 30
    # Sermon body (should score high)
    for _ in range(40):
        segments.append(Segment(
            start=t, end=t + 30,
            text="Today we turn to the book of Romans, point one, faith and grace in Christ",
        ))
        t += 30
    # Closing worship/announcements (should score low/negative again)
    for _ in range(10):
        segments.append(Segment(start=t, end=t + 30, text="Please stand as the choir sings the closing song"))
        t += 30
    return segments


def test_extract_sermon_finds_the_sermon_body():
    segments = _make_segments()
    cfg = {
        "window_minutes": 12,
        "drop_threshold_segments": 8,
        "min_minutes": 5,
        "max_fraction_of_total": 0.9,
    }
    chosen, start, end = extract_sermon(segments, cfg)

    assert chosen, "extract_sermon should return a non-empty window"
    # The chosen window should sit mostly within the sermon-body region
    # (segments 10..49 out of 60), not the worship preamble/closing.
    sermon_body = segments[10:50]
    overlap = [s for s in chosen if s in sermon_body]
    assert len(overlap) / len(chosen) > 0.7


def test_extract_sermon_empty_input():
    cfg = {
        "window_minutes": 12,
        "drop_threshold_segments": 8,
        "min_minutes": 5,
        "max_fraction_of_total": 0.9,
    }
    chosen, start, end = extract_sermon([], cfg)
    assert chosen == []
    assert start == 0.0
    assert end == 0.0
