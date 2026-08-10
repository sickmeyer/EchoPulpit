import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List

import boto3
from botocore.exceptions import ClientError

# Statuses an EchoPulpitJobs item can be in.
QUEUED = "QUEUED"
LAUNCHING = "LAUNCHING"
IN_PROGRESS = "IN_PROGRESS"
COMPLETE = "COMPLETE"
FAILED = "FAILED"

MAX_ATTEMPTS = int(os.environ.get("SERMON_MAX_ATTEMPTS", "3"))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_decimal(value: float) -> Decimal:
    """DynamoDB's boto3 resource API rejects native floats; it requires Decimal."""
    return Decimal(str(value))


class StateStore:
    """
    DynamoDB-backed job state, replacing the old local SQLite store.

    State has to live outside any single instance since worker instances are
    ephemeral (spot, terminated after one job) -- this table is the only
    thing that persists across jobs.

    Table schema (partition key: video_id):
      status          QUEUED | LAUNCHING | IN_PROGRESS | COMPLETE | FAILED
      instance_id     EC2 instance ID of the worker that claimed this job
      title           video title (passed through from the poller so the
                       worker doesn't need its own YouTube API access)
      actual_end_time livestream end time, if known
      claimed_at      ISO8601 timestamp of the most recent claim/launch
      completed_at    ISO8601 timestamp of COMPLETE or terminal FAILED
      failure_count   number of failed attempts so far
      error           last error message, if FAILED
      s3_prefix       s3://bucket/sermons/<video_id>/ once artifacts exist
    """

    def __init__(self, table_name: Optional[str] = None, region_name: Optional[str] = None):
        table_name = table_name or os.environ.get("SERMON_JOBS_TABLE", "EchoPulpitJobs")
        region_name = region_name or os.environ.get("AWS_REGION")
        self._ddb = boto3.resource("dynamodb", region_name=region_name)
        self.table = self._ddb.Table(table_name)

    # ---- Used by run_pipeline() / bootstrap.sh on the worker ----

    def is_processed(self, video_id: str) -> bool:
        item = self._get(video_id)
        return bool(item) and item.get("status") == COMPLETE

    def mark_processed(
        self,
        video_id: str,
        processed_at: str,
        actual_end_time: Optional[str],
        title: Optional[str],
    ) -> None:
        self.table.update_item(
            Key={"video_id": video_id},
            UpdateExpression=(
                "SET #status = :complete, completed_at = :ts, "
                "actual_end_time = :end, title = :title"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":complete": COMPLETE,
                ":ts": processed_at,
                ":end": actual_end_time or "",
                ":title": title or "",
            },
        )

    def mark_failed(self, video_id: str, error: str) -> int:
        """Increment failure_count and record the error. Returns the new count."""
        resp = self.table.update_item(
            Key={"video_id": video_id},
            UpdateExpression=(
                "SET #status = :failed, error = :err, completed_at = :ts "
                "ADD failure_count :one"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":failed": FAILED,
                ":err": str(error)[:2000],
                ":ts": utc_now_iso(),
                ":one": 1,
            },
            ReturnValues="ALL_NEW",
        )
        return int(resp["Attributes"].get("failure_count", 1))

    def set_s3_prefix(self, video_id: str, s3_prefix: str) -> None:
        self.table.update_item(
            Key={"video_id": video_id},
            UpdateExpression="SET s3_prefix = :p",
            ExpressionAttributeValues={":p": s3_prefix},
        )

    def mark_in_progress(self, video_id: str) -> None:
        self.table.update_item(
            Key={"video_id": video_id},
            UpdateExpression="SET #status = :s, heartbeat = :ts",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":s": IN_PROGRESS, ":ts": utc_now_iso()},
        )

    # ---- Used by the Poller Lambda ----

    def claim_new(
        self,
        video_id: str,
        title: str,
        actual_end_time: str = "",
        video_duration_seconds: float = 0.0,
    ) -> bool:
        """
        Atomically claim a video that hasn't been seen before.
        Returns True if this call won the claim, False if it already existed.
        """
        try:
            self.table.put_item(
                Item={
                    "video_id": video_id,
                    "status": QUEUED,
                    "title": title,
                    "actual_end_time": actual_end_time,
                    "video_duration_seconds": _to_decimal(video_duration_seconds),
                    "claimed_at": utc_now_iso(),
                    "failure_count": 0,
                },
                ConditionExpression="attribute_not_exists(video_id)",
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def mark_launching(self, video_id: str, instance_id: str) -> None:
        self.table.update_item(
            Key={"video_id": video_id},
            UpdateExpression="SET #status = :s, instance_id = :iid, claimed_at = :ts",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":s": LAUNCHING,
                ":iid": instance_id,
                ":ts": utc_now_iso(),
            },
        )

    def reclaim_if_stale(self, video_id: str, stale_after_seconds: int = 3 * 3600) -> bool:
        """
        If an existing item has been stuck in QUEUED/LAUNCHING/IN_PROGRESS
        past stale_after_seconds (spot reclaimed, worker crashed before
        updating state), reset it to QUEUED so the poller can relaunch it.
        Returns True if it reclaimed (and the poller should relaunch).
        """
        item = self._get(video_id)
        if not item:
            return False
        if item.get("status") not in (QUEUED, LAUNCHING, IN_PROGRESS):
            return False

        claimed_at = item.get("claimed_at")
        if not claimed_at:
            return False
        try:
            age = time.time() - datetime.fromisoformat(claimed_at).timestamp()
        except ValueError:
            return False
        if age < stale_after_seconds:
            return False

        try:
            self.table.update_item(
                Key={"video_id": video_id},
                UpdateExpression="SET #status = :s, claimed_at = :ts",
                ConditionExpression="claimed_at = :old_ts",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":s": QUEUED,
                    ":ts": utc_now_iso(),
                    ":old_ts": claimed_at,
                },
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def should_retry(self, video_id: str) -> bool:
        item = self._get(video_id)
        if not item:
            return True
        return int(item.get("failure_count", 0)) < MAX_ATTEMPTS

    def retry_failed(self, video_id: str) -> bool:
        """
        Reset a FAILED job back to QUEUED so the poller can relaunch it,
        but only while it's still under MAX_ATTEMPTS -- past that it stays
        FAILED forever (dead-lettered) for manual review.
        """
        item = self._get(video_id)
        if not item or item.get("status") != FAILED:
            return False
        if int(item.get("failure_count", 0)) >= MAX_ATTEMPTS:
            return False
        try:
            self.table.update_item(
                Key={"video_id": video_id},
                UpdateExpression="SET #status = :s, claimed_at = :ts",
                ConditionExpression="#status = :failed",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":s": QUEUED,
                    ":ts": utc_now_iso(),
                    ":failed": FAILED,
                },
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def get_status(self, video_id: str) -> Optional[str]:
        item = self._get(video_id)
        return item.get("status") if item else None

    def list_active(self) -> List[dict]:
        """Scan for jobs not yet in a terminal state. Fine at this table's tiny scale."""
        resp = self.table.scan(
            FilterExpression="#status IN (:q, :l, :p)",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":q": QUEUED, ":l": LAUNCHING, ":p": IN_PROGRESS},
        )
        return resp.get("Items", [])

    # ---- internal ----

    def _get(self, video_id: str) -> Optional[dict]:
        resp = self.table.get_item(Key={"video_id": video_id})
        return resp.get("Item")
