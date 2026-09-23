from datetime import date

from prompts import USER_PROMPT_TEMPLATE
from sermon_pipeline import (
    FeedEpisode,
    build_service_details,
    build_service_info,
    compute_needs_review,
    flag_category,
    flag_text,
    normalize_article_frontmatter,
)
import sermon_pipeline as sp


def test_flag_category_from_tag():
    assert flag_category("[editorial] Softened the political aside.") == "editorial"
    assert flag_category("[Transcript] Ends mid-sentence.") == "transcript"
    assert flag_category("[attribution] Preacher unknown.") == "attribution"
    assert flag_category("[scripture] Job 25:6 is Bildad's words.") == "scripture"


def test_untagged_and_unknown_flags():
    assert flag_category("Some old untagged flag") == "other"
    assert flag_category("[misc] not a real category") == "other"
    # Verifier-written flags in older articles are scripture flags
    assert flag_category('Removed unverifiable scripture citation "Luke 19:8-9, Luke 19:10" -- ...') == "scripture"
    assert flag_category("Scripture citations were NOT independently verified against a KJV text") == "scripture"


def test_flag_text_strips_only_known_tags():
    assert flag_text("[editorial]  Softened it.") == "Softened it."
    assert flag_text("[misc] kept") == "[misc] kept"


def test_needs_review_only_for_decision_categories():
    assert compute_needs_review([]) is False
    assert compute_needs_review(["[transcript] cut off", "[attribution] no name"]) is False
    assert compute_needs_review(["[transcript] cut off", "[editorial] politics"]) is True
    assert compute_needs_review(["[scripture] ambiguous"]) is True
    assert compute_needs_review(["untagged legacy flag"]) is True


def test_normalize_computes_needs_review_ignoring_model_value():
    fm = normalize_article_frontmatter({"needs_review": True, "reviewer_notes": {"flags": ["[transcript] x"]}})
    assert fm["needs_review"] is False
    fm = normalize_article_frontmatter({"needs_review": False, "reviewer_notes": {"flags": ["[editorial] x"]}})
    assert fm["needs_review"] is True


def test_service_details_block():
    text = build_service_details({"title": "Sunday Main Worship", "date": "2026-09-13", "preacher": "Pastor James Sickmeyer"})
    assert "- Service: Sunday Main Worship" in text
    assert "- Preached on: 2026-09-13" in text
    assert "- Preacher: Pastor James Sickmeyer" in text
    assert "Preacher: not known" in build_service_details({"title": "X"})


def test_user_prompt_has_service_details_slot():
    assert "{service_details}" in USER_PROMPT_TEMPLATE


def test_service_info_local_date_and_feed_preacher(monkeypatch):
    ep = FeedEpisode("Midweek Worship Service", date(2026, 9, 17), 4841, "https://x/a.mp3",
                     guid="g1", author="Pastor James Sickmeyer")
    monkeypatch.setattr(sp, "find_feed_episode", lambda *a, **k: ep)
    cfg = {"church": {"timezone": "America/Chicago"}, "transcription": {"subsplash_feed_url": "https://feed"}}
    # Ends 01:23 UTC on the 18th = the evening of the 17th in Minnesota
    info = build_service_info(cfg, "nORTlDNd39M", "Midweek Worship Service", "2026-09-18T01:23:04Z", 4833)
    assert info == {"title": "Midweek Worship Service", "date": "2026-09-17", "preacher": "Pastor James Sickmeyer"}


def test_service_info_without_feed():
    info = build_service_info({}, "vid", "Weekly Bible Hour", "2026-09-13T15:35:45Z", 1986)
    assert info == {"title": "Weekly Bible Hour", "date": "2026-09-13"}
