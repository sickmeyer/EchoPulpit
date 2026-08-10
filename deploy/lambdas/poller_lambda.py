"""
Poller Lambda -- runs on an EventBridge schedule (e.g. rate(15 minutes)).

Cheaply checks whether the channel has a newly-ended livestream that isn't
already tracked, claims it in DynamoDB, and launches a spot worker instance
to process it. Deliberately avoids YouTube's search.list endpoint (100 quota
units/call) in favor of channels.list + playlistItems.list + videos.list
(1 unit each) -- at a 15-minute poll interval, search.list would blow the
default 10,000 unit/day quota.

Deployment note: this Lambda's zip must also include storage.py and
bootstrap.sh from the project root/deploy dir alongside this file --
storage.py because it reuses the same DynamoDB-backed state store the
worker uses, and bootstrap.sh because it's shipped as EC2 user-data (this
is a stock AMI, not a custom-baked one, so nothing runs at boot unless we
hand it the script directly).
"""
import os
import json

import boto3
from googleapiclient.discovery import build

from storage import StateStore, FAILED

REGION = os.environ.get("AWS_REGION", "us-east-1")
CHANNEL_ID = os.environ["CHANNEL_ID"]
YOUTUBE_API_KEY_SECRET_ARN = os.environ["YOUTUBE_API_KEY_SECRET_ARN"]
AMI_ID = os.environ["WORKER_AMI_ID"]
INSTANCE_TYPE = os.environ.get("WORKER_INSTANCE_TYPE", "c6i.xlarge")
SUBNET_ID = os.environ["WORKER_SUBNET_ID"]
SECURITY_GROUP_ID = os.environ["WORKER_SECURITY_GROUP_ID"]
WORKER_INSTANCE_PROFILE_ARN = os.environ["WORKER_INSTANCE_PROFILE_ARN"]
MAX_SPOT_PRICE = os.environ.get("WORKER_MAX_SPOT_PRICE")  # optional, empty = on-demand cap price
USE_SPOT = os.environ.get("WORKER_USE_SPOT", "true").lower() == "true"
STALE_AFTER_SECONDS = int(os.environ.get("STALE_AFTER_SECONDS", str(3 * 3600)))
RECENT_VIDEOS_TO_CHECK = int(os.environ.get("RECENT_VIDEOS_TO_CHECK", "10"))

_BOOTSTRAP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bootstrap.sh")
with open(_BOOTSTRAP_PATH, "r", encoding="utf-8") as _f:
    # Raw text, NOT pre-base64-encoded -- boto3's run_instances encodes
    # UserData itself. Pre-encoding here double-encodes it, so cloud-init
    # receives base64 text instead of a real script and silently never
    # runs it (no error, no log -- just a normal boot with no bootstrap).
    _BOOTSTRAP_USER_DATA = _f.read()

_secrets = boto3.client("secretsmanager", region_name=REGION)
_ec2 = boto3.client("ec2", region_name=REGION)


def _get_youtube_api_key() -> str:
    resp = _secrets.get_secret_value(SecretId=YOUTUBE_API_KEY_SECRET_ARN)
    secret = resp["SecretString"]
    # Support either a plain string secret or a {"YOUTUBE_API_KEY": "..."} JSON secret.
    try:
        return json.loads(secret)["YOUTUBE_API_KEY"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return secret


def _get_uploads_playlist_id(youtube, channel_id: str) -> str:
    resp = youtube.channels().list(part="contentDetails", id=channel_id).execute()
    items = resp.get("items", [])
    if not items:
        raise RuntimeError(f"Channel not found: {channel_id}")
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


def _get_recent_video_ids(youtube, playlist_id: str, max_results: int) -> list:
    resp = youtube.playlistItems().list(
        part="contentDetails", playlistId=playlist_id, maxResults=max_results
    ).execute()
    return [
        item["contentDetails"]["videoId"]
        for item in resp.get("items", [])
        if "videoId" in item.get("contentDetails", {})
    ]


_SPANISH_TITLE_MARKERS = ("español", "espanol", "spanish")


def _is_spanish(video: dict) -> bool:
    """
    The pipeline is English-only (Whisper language="en", Claude prompt
    assumes English pastoral prose) -- skip Spanish-language services
    rather than run them through it and produce a garbled/wrong-language
    article. Prefers YouTube's own language metadata when uploaders set it;
    falls back to a title keyword check since that's not always populated.
    """
    snippet = video.get("snippet", {})
    lang = (snippet.get("defaultAudioLanguage") or snippet.get("defaultLanguage") or "").lower()
    if lang.startswith("es"):
        return True
    title = (snippet.get("title") or "").lower()
    return any(marker in title for marker in _SPANISH_TITLE_MARKERS)


def _get_ended_livestreams(youtube, video_ids: list) -> list:
    """Returns video dicts (snippet/liveStreamingDetails/contentDetails) for
    videos in video_ids that were livestreams which have already ended."""
    if not video_ids:
        return []
    resp = youtube.videos().list(
        part="snippet,liveStreamingDetails,contentDetails", id=",".join(video_ids)
    ).execute()
    ended = []
    for item in resp.get("items", []):
        lsd = item.get("liveStreamingDetails", {})
        if lsd.get("actualEndTime"):
            ended.append(item)
    return ended


def _launch_worker(video_id: str, title: str, duration_seconds: float, end_time: str) -> str:
    kwargs = {
        "ImageId": AMI_ID,
        "InstanceType": INSTANCE_TYPE,
        "MinCount": 1,
        "MaxCount": 1,
        "SubnetId": SUBNET_ID,
        "SecurityGroupIds": [SECURITY_GROUP_ID],
        "IamInstanceProfile": {"Arn": WORKER_INSTANCE_PROFILE_ARN},
        "InstanceInitiatedShutdownBehavior": "terminate",
        "UserData": _BOOTSTRAP_USER_DATA,
        "TagSpecifications": [{
            "ResourceType": "instance",
            "Tags": [
                {"Key": "Name", "Value": f"echopulpit-worker-{video_id}"},
                # Required by the worker instance role's self-terminate IAM
                # condition (deploy/iam/worker-instance-policy.json) -- do
                # not remove.
                {"Key": "Project", "Value": "echopulpit"},
                {"Key": "SermonVideoId", "Value": video_id},
                {"Key": "SermonVideoTitle", "Value": (title or "")[:255]},
                {"Key": "SermonVideoDurationSeconds", "Value": str(duration_seconds)},
                {"Key": "SermonVideoEndTime", "Value": end_time or ""},
            ],
        }],
    }
    if USE_SPOT:
        kwargs["InstanceMarketOptions"] = {
            "MarketType": "spot",
            "SpotOptions": {"SpotInstanceType": "one-time"},
        }
    if MAX_SPOT_PRICE:
        kwargs["InstanceMarketOptions"]["SpotOptions"]["MaxPrice"] = MAX_SPOT_PRICE

    resp = _ec2.run_instances(**kwargs)
    return resp["Instances"][0]["InstanceId"]


def lambda_handler(event, context):
    store = StateStore()
    api_key = _get_youtube_api_key()
    youtube = build("youtube", "v3", developerKey=api_key)

    # ---- Reclaim anything stuck (spot reclaimed, worker crashed) or retry
    # failed jobs still under the attempt limit, before looking for new work.
    # Launch failures (e.g. transient spot InsufficientInstanceCapacity) are
    # caught per-video so one bad launch doesn't abort the whole poll and
    # block every other video in the batch -- it just stays QUEUED and gets
    # picked up again on a later poll.
    for item in store.list_active():
        vid = item["video_id"]
        if store.reclaim_if_stale(vid, STALE_AFTER_SECONDS):
            print(f"Reclaimed stale job {vid} (was {item.get('status')})")
            try:
                instance_id = _launch_worker(
                    vid, item.get("title", ""),
                    float(item.get("video_duration_seconds", 0) or 0),
                    item.get("actual_end_time", ""),
                )
            except Exception as e:
                print(f"Launch failed for reclaimed job {vid}: {e}")
                continue
            store.mark_launching(vid, instance_id)

    # ---- Low-quota check for new work: channels.list (1) + playlistItems.list
    # (1) + videos.list (1) = ~3 units/poll, vs. 100 units for search.list. ----
    uploads_playlist_id = _get_uploads_playlist_id(youtube, CHANNEL_ID)
    video_ids = _get_recent_video_ids(youtube, uploads_playlist_id, RECENT_VIDEOS_TO_CHECK)
    ended = _get_ended_livestreams(youtube, video_ids)

    launched = []
    for video in ended:
        video_id = video["id"]
        title = video["snippet"]["title"]
        if _is_spanish(video):
            print(f"Skipping Spanish-language video {video_id} ({title!r}) -- pipeline is English-only")
            continue
        end_time = video["liveStreamingDetails"]["actualEndTime"]
        try:
            duration_seconds = _iso8601_duration_to_seconds(
                video.get("contentDetails", {}).get("duration", "")
            )
        except ValueError:
            duration_seconds = 0.0

        status = store.get_status(video_id)
        should_launch = False

        if status is None:
            should_launch = store.claim_new(video_id, title, end_time, duration_seconds)
        elif status == FAILED:
            should_launch = store.retry_failed(video_id)
        # QUEUED/LAUNCHING/IN_PROGRESS/COMPLETE: leave alone (already handled
        # above, in progress, or done).

        if should_launch:
            try:
                instance_id = _launch_worker(video_id, title, duration_seconds, end_time)
            except Exception as e:
                print(f"Launch failed for new job {video_id}: {e}")
                continue
            store.mark_launching(video_id, instance_id)
            launched.append(video_id)
            print(f"Launched worker {instance_id} for video {video_id} ({title!r})")

    return {"launched": launched, "checked": len(video_ids)}


def _iso8601_duration_to_seconds(value: str) -> float:
    import re
    m = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?$", (value or "").strip())
    if not m:
        raise ValueError(f"Unrecognized ISO-8601 duration: {value!r}")
    hours, minutes, seconds = m.groups()
    return int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds or 0)
