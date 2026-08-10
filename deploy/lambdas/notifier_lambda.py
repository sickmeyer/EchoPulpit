"""
Notifier Lambda -- triggered by a DynamoDB Streams subscription on the
EchoPulpitJobs table. Sends an email via SES when a job finishes: COMPLETE
(finished article, PDF attached) or FAILED (short alert).

Two separate DynamoDB updates land a completed job: sermon_pipeline.py's
mark_processed() sets status=COMPLETE first, and bootstrap.sh's later
set_s3_prefix() call adds s3_prefix afterward once the S3 upload finishes.
So the COMPLETE email is keyed off s3_prefix going from empty -> non-empty
(the point at which artifacts actually exist to email), not off the status
transition itself -- gating on "old status != new status" would mean the
record that actually has s3_prefix populated never looks like a transition,
and the email would never send. FAILED, by contrast, is a single atomic
update (mark_failed sets status+error together), so a status transition is
the right signal there.
"""
import os
import json
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from email.mime.text import MIMEText

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
SENDER = os.environ["SES_SENDER_ADDRESS"]
RECIPIENT = os.environ["NOTIFY_RECIPIENT_ADDRESS"]
ARTIFACTS_BUCKET = os.environ["SERMON_ARTIFACTS_BUCKET"]

_s3 = boto3.client("s3", region_name=REGION)
_ses = boto3.client("ses", region_name=REGION)


def _ddb_value(v):
    """Unwrap a DynamoDB Streams AttributeValue dict into a plain Python value."""
    if v is None:
        return None
    if "S" in v:
        return v["S"]
    if "N" in v:
        return v["N"]
    if "NULL" in v:
        return None
    return v


def _get_field(image: dict, key: str, default=""):
    if not image or key not in image:
        return default
    return _ddb_value(image[key])


def _send_complete_email(video_id: str, title: str, s3_prefix: str):
    # s3_prefix looks like "s3://bucket/sermons/<id>/"
    prefix = s3_prefix.split(f"s3://{ARTIFACTS_BUCKET}/", 1)[-1]
    article_key = f"{prefix}article.json"
    pdf_key = f"{prefix}sermon-article.pdf"
    md_key = f"{prefix}article.md"

    meta_description = ""
    needs_review = True
    reviewer_notes = {}
    try:
        obj = _s3.get_object(Bucket=ARTIFACTS_BUCKET, Key=article_key)
        article = json.loads(obj["Body"].read())
        meta_description = article.get("meta_description", "")
        needs_review = bool(article.get("needs_review", True))
        reviewer_notes = article.get("reviewer_notes") or {}
    except Exception as e:
        print(f"Could not read article.json for {video_id}: {e}")

    flags = reviewer_notes.get("flags") or []
    corrections = reviewer_notes.get("corrections") or []
    additions = reviewer_notes.get("additions") or []

    msg = MIMEMultipart()
    subject = title or f"EchoPulpit article ready: {video_id}"
    if needs_review:
        subject = f"[Review needed] {subject}"
    msg["Subject"] = subject
    msg["From"] = SENDER
    msg["To"] = RECIPIENT

    body_lines = [meta_description, ""]
    if flags:
        body_lines.append("Flagged for your review before publishing:")
        body_lines.extend(f"- {f}" for f in flags)
        body_lines.append("")
    if corrections or additions:
        body_lines.append(
            f"Automatically made {len(corrections)} correction(s) and "
            f"{len(additions)} scripture addition(s) -- see the PDF's "
            "Reviewer Notes section for details."
        )
    msg.attach(MIMEText("\n".join(body_lines), "plain"))

    try:
        pdf_obj = _s3.get_object(Bucket=ARTIFACTS_BUCKET, Key=pdf_key)
        attachment = MIMEApplication(pdf_obj["Body"].read(), _subtype="pdf")
        attachment.add_header(
            "Content-Disposition", "attachment", filename="sermon-article.pdf"
        )
        msg.attach(attachment)
    except Exception as e:
        print(f"Could not attach PDF for {video_id}: {e}")

    try:
        md_obj = _s3.get_object(Bucket=ARTIFACTS_BUCKET, Key=md_key)
        md_attachment = MIMEApplication(md_obj["Body"].read(), _subtype="markdown")
        md_attachment.add_header(
            "Content-Disposition", "attachment", filename="article.md"
        )
        msg.attach(md_attachment)
    except Exception as e:
        print(f"Could not attach article.md for {video_id}: {e}")

    _ses.send_raw_email(
        Source=SENDER, Destinations=[RECIPIENT], RawMessage={"Data": msg.as_string()}
    )
    print(f"Sent COMPLETE email for {video_id}")


def _send_failed_email(video_id: str, title: str, error: str, failure_count: str):
    subject = f"[FAILED] EchoPulpit: {title or video_id}"
    body = (
        f"Video {video_id} failed to process (attempt {failure_count}).\n\n"
        f"Error: {error}\n"
    )
    _ses.send_email(
        Source=SENDER,
        Destination={"ToAddresses": [RECIPIENT]},
        Message={
            "Subject": {"Data": subject},
            "Body": {"Text": {"Data": body}},
        },
    )
    print(f"Sent FAILED alert email for {video_id}")


def lambda_handler(event, context):
    for record in event.get("Records", []):
        if record.get("eventName") not in ("INSERT", "MODIFY"):
            continue

        new_image = record.get("dynamodb", {}).get("NewImage", {})
        old_image = record.get("dynamodb", {}).get("OldImage", {})

        new_status = _get_field(new_image, "status")
        old_status = _get_field(old_image, "status")
        video_id = _get_field(new_image, "video_id")
        title = _get_field(new_image, "title")

        old_s3_prefix = _get_field(old_image, "s3_prefix")
        new_s3_prefix = _get_field(new_image, "s3_prefix")
        s3_prefix_just_appeared = bool(new_s3_prefix) and not old_s3_prefix

        if new_status == "COMPLETE" and s3_prefix_just_appeared:
            _send_complete_email(video_id, title, new_s3_prefix)
        elif new_status == "FAILED" and old_status != "FAILED":
            error = _get_field(new_image, "error", "(no error message recorded)")
            failure_count = _get_field(new_image, "failure_count", "?")
            _send_failed_email(video_id, title, error, failure_count)

    return {"processed": len(event.get("Records", []))}
