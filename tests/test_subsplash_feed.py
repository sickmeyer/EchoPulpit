from datetime import date

from sermon_pipeline import FeedEpisode, match_feed_episode, parse_podcast_feed


FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
<channel>
  <title>WBT Sermons</title>
  <item>
    <title>Self-serving Bias</title>
    <pubDate>Sun, 04 Jun 2023 10:00:00 +0000</pubDate>
    <itunes:duration>3268</itunes:duration>
    <enclosure url="https://example.com/old.mp3" type="audio/mpeg" length="1"/>
  </item>
  <item>
    <title>Weekly Bible Hour</title>
    <pubDate>Sun, 13 Sep 2026 10:00:00 +0000</pubDate>
    <itunes:duration>33:05</itunes:duration>
    <enclosure url="https://example.com/wbh.mp3" type="audio/mpeg" length="1"/>
  </item>
  <item>
    <title>No enclosure</title>
    <pubDate>Sun, 13 Sep 2026 10:00:00 +0000</pubDate>
  </item>
</channel>
</rss>"""


def test_parse_podcast_feed_reads_items_with_enclosures():
    episodes = parse_podcast_feed(FEED)

    assert [e.title for e in episodes] == ["Self-serving Bias", "Weekly Bible Hour"]
    assert episodes[1].pub_date == date(2026, 9, 13)
    assert episodes[1].duration_seconds == 1985
    assert episodes[1].audio_url == "https://example.com/wbh.mp3"


def _ep(title, d, duration):
    return FeedEpisode(title, d, duration, f"https://example.com/{title}-{d}.mp3")


def test_match_same_day_by_title():
    episodes = [
        _ep("Weekly Bible Hour", date(2026, 9, 13), 1985),
        _ep("Sunday Main Worship", date(2026, 9, 13), 5213),
        _ep("Weekly Bible Hour", date(2026, 9, 6), 4100),
    ]
    match = match_feed_episode(episodes, "Sunday Main Worship", "2026-09-13T17:28:37Z", 5208)
    assert match is episodes[1]


def test_match_evening_service_dated_day_before_utc_end():
    # Midweek service ends after midnight UTC; the feed dates it the local day.
    episodes = [_ep("Midweek Worship Service", date(2026, 9, 17), 4841)]
    match = match_feed_episode(episodes, "Midweek Worship Service", "2026-09-18T01:23:04Z", 4833)
    assert match is episodes[0]


def test_match_title_is_whitespace_and_case_insensitive():
    episodes = [_ep("Weekly  Bible hour ", date(2026, 9, 13), 1985)]
    assert match_feed_episode(episodes, "Weekly Bible Hour", "2026-09-13T15:35:45Z", 1986) is episodes[0]


def test_no_match_for_other_weeks_or_titles():
    episodes = [
        _ep("Sunday Main Worship", date(2026, 9, 6), 2804),
        _ep("Servicio en Español", date(2026, 9, 20), 3214),
    ]
    assert match_feed_episode(episodes, "Sunday Main Worship", "2026-09-20T17:39:04Z", 5869) is None


def test_closest_duration_breaks_ties():
    episodes = [
        _ep("Weekly Bible Hour", date(2026, 9, 19), 900),
        _ep("Weekly Bible Hour", date(2026, 9, 20), 6328),
    ]
    match = match_feed_episode(episodes, "Weekly Bible Hour", "2026-09-20T17:39:08Z", 6000)
    assert match is episodes[1]


def test_no_end_time_means_no_match():
    episodes = [_ep("Weekly Bible Hour", date(2026, 9, 13), 1985)]
    assert match_feed_episode(episodes, "Weekly Bible Hour", "", 1985) is None


def test_parse_podcast_feed_reads_guid():
    feed = FEED.replace(
        b"<title>Weekly Bible Hour</title>",
        b"<title>Weekly Bible Hour</title><guid isPermaLink=\"false\">3efe714928c087830c59ae6c87dacefb</guid>",
    )
    episodes = parse_podcast_feed(feed)
    assert episodes[1].guid == "3efe714928c087830c59ae6c87dacefb"
    assert episodes[0].guid == ""


def test_feed_job_end_time_matches_its_own_episode():
    # queue_feed_episodes.py sets end_time to "<pub_date>T12:00:00Z"
    episodes = [
        _ep("Self-serving Bias", date(2023, 6, 4), 3268),
        _ep("Heaven's Reward Fallacy", date(2023, 5, 28), 2863),
    ]
    assert match_feed_episode(episodes, "Self-serving Bias", "2023-06-04T12:00:00Z", 3268) is episodes[0]


def test_subsplash_job_ids():
    from sermon_pipeline import is_subsplash_job
    assert is_subsplash_job("subsplash-f256cbe023cf21abb5a3884621cb2b53")
    assert not is_subsplash_job("J8uEgK3bFA0")
