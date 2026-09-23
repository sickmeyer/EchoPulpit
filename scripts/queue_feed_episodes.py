"""
Queue sermons straight from the Subsplash podcast feed -- e.g. older
services that predate the YouTube livestreams -- for the normal worker
pipeline.

Each episode becomes an EchoPulpitJobs item keyed "subsplash-<feed guid>"
(so re-running never double-queues), marked QUEUED with an ancient
claimed_at: the poller's stale-job reclaim pass (every 15 min) launches a
worker for it, same as scripts/../deploy/requeue-failed.sh. The worker sees
the subsplash- prefix and takes audio from the feed only -- no YouTube
captions or yt-dlp (see get_transcript()). The job's end time is the
episode's feed date at 12:00 UTC, which the worker's title+date matching
resolves back to this exact episode.

Dry run by default. Usage:
    python scripts/queue_feed_episodes.py --top 10            # first 10 in feed order
    python scripts/queue_feed_episodes.py --top 10 --apply
    python scripts/queue_feed_episodes.py --date 2026-09-20 --title "Servicio en" --apply
                                                          # one service, by date and title

Each job's language is taken from its title ("Servicio en Español" -> es,
see sermon_pipeline.detect_language) unless --language is given.
Env: FEED_URL (default: the worker config's subsplash_feed_url),
TABLE_NAME (default EchoPulpitJobs), AWS_REGION (default us-east-1).
"""
import argparse
import hashlib
import os
import sys
import urllib.request
from decimal import Decimal

import boto3
import yaml
from botocore.exceptions import ClientError

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
from sermon_pipeline import SUBSPLASH_JOB_ID_PREFIX, _http_get, detect_language, parse_podcast_feed  # noqa: E402


def _default_feed_url() -> str:
    with open(os.path.join(ROOT, "deploy", "config.worker.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)["transcription"]["subsplash_feed_url"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", type=int, help="queue the first N (matching) episodes in feed order")
    ap.add_argument("--date", help="only episodes dated YYYY-MM-DD")
    ap.add_argument("--title", help="only episodes whose title contains this text (case-insensitive)")
    ap.add_argument("--language", choices=["en", "es"], help="override the language detected from the title")
    ap.add_argument("--apply", action="store_true", help="actually write the jobs (default: dry run)")
    args = ap.parse_args()

    feed_url = os.environ.get("FEED_URL") or _default_feed_url()
    with _http_get(feed_url, 60) as resp:
        episodes = parse_podcast_feed(resp.read())
    if args.date:
        episodes = [e for e in episodes if e.pub_date.isoformat() == args.date]
    if args.title:
        episodes = [e for e in episodes if args.title.casefold() in e.title.casefold()]
    if args.top:
        episodes = episodes[: args.top]
    if not (args.top or args.date or args.title):
        ap.error("give --top, --date and/or --title")

    table = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1")).Table(
        os.environ.get("TABLE_NAME", "EchoPulpitJobs"))

    for e in episodes:
        key = e.guid or hashlib.sha1(e.audio_url.encode()).hexdigest()
        video_id = f"{SUBSPLASH_JOB_ID_PREFIX}{key}"
        language = args.language or detect_language(e.title)
        label = f"{e.pub_date}  {e.duration_seconds / 60:4.0f} min  [{language}]  {e.title}"
        if not args.apply:
            print(f"would queue {video_id}  {label}")
            continue
        try:
            table.put_item(
                Item={
                    "video_id": video_id,
                    "status": "QUEUED",
                    "title": e.title,
                    "actual_end_time": f"{e.pub_date.isoformat()}T12:00:00Z",
                    "video_duration_seconds": Decimal(str(e.duration_seconds)),
                    "claimed_at": "1970-01-01T00:00:00+00:00",
                    "failure_count": 0,
                    "language": language,
                },
                ConditionExpression="attribute_not_exists(video_id)",
            )
            print(f"queued {video_id}  {label}")
        except ClientError as err:
            if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            print(f"already exists, skipped {video_id}  {label}")

    if not args.apply:
        print("\nDry run -- nothing written. Re-run with --apply to queue these.")
    else:
        print("\nThe poller (every 15 min) will launch a worker for each.")


if __name__ == "__main__":
    main()
