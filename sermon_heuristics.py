from __future__ import annotations
from dataclasses import dataclass
from typing import List, Dict, Tuple
import math

SERMON_CUES = {
    "sermon", "message", "scripture", "open your bible", "turn to", "book of", "chapter", "verse",
    "today we", "this morning", "this evening", "point", "first", "second", "third", "application",
    "pray", "amen", "in jesus", "gospel", "repent", "grace", "faith", "lord", "christ"
}

NON_SERMON_CUES = {
    "welcome", "announcements", "offering", "tithes", "giving", "worship", "song", "sing",
    "choir", "lyrics", "chorus", "bridge", "verse one", "verse two", "please stand", "you may be seated"
}

@dataclass
class Segment:
    start: float
    end: float
    text: str

def _norm(s: str) -> str:
    return " ".join(s.lower().strip().split())

def score_segment(text: str) -> float:
    t = _norm(text)
    score = 0.0
    for cue in SERMON_CUES:
        if cue in t:
            score += 1.2
    for cue in NON_SERMON_CUES:
        if cue in t:
            score -= 1.0
    # small boost for longer speech-like segments
    score += min(len(t) / 300.0, 0.8)
    return score

def pick_sermon_window(segments: List[Segment], window_minutes: int) -> Tuple[int, int]:
    """
    Slide a window by time (approx) and pick best contiguous region.
    Returns (start_index, end_index_exclusive).
    """
    if not segments:
        return (0, 0)

    window_seconds = window_minutes * 60
    best = (-1e9, 0, 0)

    # precompute scores
    seg_scores = [score_segment(s.text) for s in segments]

    # two-pointer time window
    j = 0
    running = 0.0
    for i in range(len(segments)):
        while j < len(segments) and (segments[j].end - segments[i].start) <= window_seconds:
            running += seg_scores[j]
            j += 1
        # scoring: total score, with mild preference for longer windows
        duration = segments[j-1].end - segments[i].start if j > i else 0.0
        value = running + 0.001 * duration
        if value > best[0]:
            best = (value, i, j)

        # move i forward
        running -= seg_scores[i]

    return best[1], best[2]

def refine_boundaries(
    segments: List[Segment],
    seed_i: int,
    seed_j: int,
    drop_threshold_segments: int,
    min_minutes: int,
    max_fraction_of_total: float
) -> Tuple[int, int]:
    if not segments:
        return (0, 0)

    seg_scores = [score_segment(s.text) for s in segments]
    total_duration = segments[-1].end - segments[0].start
    max_duration = total_duration * max_fraction_of_total
    min_duration = min_minutes * 60

    i, j = seed_i, seed_j
    # expand left while beneficial
    drops = 0
    k = i - 1
    while k >= 0:
        if seg_scores[k] < 0:
            drops += 1
        else:
            drops = max(drops - 1, 0)
        candidate_duration = segments[j-1].end - segments[k].start
        if drops >= drop_threshold_segments or candidate_duration > max_duration:
            break
        i = k
        k -= 1

    # expand right while beneficial
    drops = 0
    k = j
    while k < len(segments):
        if seg_scores[k] < 0:
            drops += 1
        else:
            drops = max(drops - 1, 0)
        candidate_duration = segments[k].end - segments[i].start
        if drops >= drop_threshold_segments or candidate_duration > max_duration:
            break
        j = k + 1
        k += 1

    # enforce minimum duration by stretching around seed if needed
    duration = segments[j-1].end - segments[i].start
    if duration < min_duration:
        # try to extend right first then left
        while j < len(segments) and (segments[j-1].end - segments[i].start) < min_duration:
            j += 1
        while i > 0 and (segments[j-1].end - segments[i].start) < min_duration:
            i -= 1

    # clamp
    i = max(i, 0)
    j = min(j, len(segments))
    return i, j

def extract_sermon(segments: List[Segment], cfg: Dict) -> Tuple[List[Segment], float, float]:
    seed_i, seed_j = pick_sermon_window(segments, cfg["window_minutes"])
    i, j = refine_boundaries(
        segments, seed_i, seed_j,
        drop_threshold_segments=cfg["drop_threshold_segments"],
        min_minutes=cfg["min_minutes"],
        max_fraction_of_total=cfg["max_fraction_of_total"],
    )
    chosen = segments[i:j]
    start = chosen[0].start if chosen else 0.0
    end = chosen[-1].end if chosen else 0.0
    return chosen, start, end