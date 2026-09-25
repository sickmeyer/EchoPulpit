"""
Notifier Lambda -- triggered by a DynamoDB Streams subscription on the
EchoPulpitJobs table. Sends an email via SES when a job finishes: COMPLETE
(finished article; PDF, Markdown and sermon transcript attached) or
FAILED (short alert).

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
import html
import os
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from email.header import Header
from email.mime.text import MIMEText

import boto3

from publish_token import make_token

REGION = os.environ.get("AWS_REGION", "us-east-1")
SENDER = os.environ["SES_SENDER_ADDRESS"]
RECIPIENT = os.environ["NOTIFY_RECIPIENT_ADDRESS"]
ARTIFACTS_BUCKET = os.environ["SERMON_ARTIFACTS_BUCKET"]
# Timezone the service date in subject lines is shown in -- the stream's end
# time is UTC, which would put an evening service on the next day's date.
NOTIFY_TIMEZONE = os.environ.get("NOTIFY_TIMEZONE", "America/Chicago")
# Publisher Lambda's Function URL; when set, completion emails carry a
# signed "Review & publish" link to it (see publisher_lambda.py).
PUBLISH_URL = os.environ.get("PUBLISH_URL", "")
# Extra recipients for article emails in a given language, e.g.
# NOTIFY_EXTRA_RECIPIENTS_ES="preacher@example.org" -- the Spanish service's
# preacher also reviews and can publish its articles. Failure alerts go only
# to NOTIFY_RECIPIENT_ADDRESS.


def _recipients(language: str) -> list:
    """Article-email recipients: you, plus that language's extra reviewers.
    Failure alerts go to NOTIFY_RECIPIENT_ADDRESS only (_send_failed_email)."""
    extra = os.environ.get(f"NOTIFY_EXTRA_RECIPIENTS_{(language or 'en').upper()}", "")
    out = [RECIPIENT]
    for addr in (a.strip() for a in extra.split(",")):
        if addr and addr.lower() not in {o.lower() for o in out}:
            out.append(addr)
    return out
PUBLISH_KEY_SECRET = os.environ.get("PUBLISH_KEY_SECRET", "echopulpit/publish-signing-key")

_s3 = boto3.client("s3", region_name=REGION)
_ses = boto3.client("ses", region_name=REGION)
_secrets = boto3.client("secretsmanager", region_name=REGION)
_signing_key = None


# A "manage this post" link (Unpublish/Delete) needs to keep working for as
# long as the post is up, not just the 30-day window a publish link gets --
# there's no way to revoke one early short of rotating the signing key, so a
# long TTL costs nothing (it can only ever unpublish/delete this one post).
MANAGE_LINK_TTL_DAYS = 365 * 5


def _publish_link(video_id: str, ttl_days: float | None = None) -> str:
    """Signed single-article publish/manage link, or "" when publishing isn't set up."""
    global _signing_key
    if not PUBLISH_URL:
        return ""
    try:
        if _signing_key is None:
            _signing_key = _secrets.get_secret_value(SecretId=PUBLISH_KEY_SECRET)["SecretString"].encode("utf-8")
        kwargs = {} if ttl_days is None else {"ttl_days": ttl_days}
        return f"{PUBLISH_URL.rstrip('/')}/?t={make_token(_signing_key, video_id, **kwargs)}"
    except Exception as e:  # never let the link block the email itself
        print(f"Could not create publish link for {video_id}: {e}")
        return ""


# Reviewer flags start with a category tag ("[editorial] ..."); these
# categories are decisions for the reviewer, the rest are informational.
# Same rules as sermon_pipeline.flag_category.
_FLAG_CATEGORIES = ("editorial", "scripture", "transcript", "attribution")
_DECISION_CATEGORIES = {"editorial", "scripture", "other"}
_TAG = re.compile(r"^\s*\[(\w+)\]\s*")


def _flag_category(flag: str) -> str:
    m = _TAG.match(str(flag))
    if m and m.group(1).lower() in _FLAG_CATEGORIES:
        return m.group(1).lower()
    if re.match(r"\s*(Removed unverifiable scripture citation|Scripture citations were NOT)", str(flag)):
        return "scripture"
    return "other"


def _flag_text(flag: str) -> str:
    return _TAG.sub("", str(flag), count=1) if _flag_category(flag) in _FLAG_CATEGORIES else str(flag)


def _flag_label(flag: str) -> str:
    c = _flag_category(flag)
    return "note" if c == "other" else c


def _split_flags(flags: list) -> tuple:
    decisions = [f for f in flags if _flag_category(f) in _DECISION_CATEGORIES]
    info = [f for f in flags if _flag_category(f) not in _DECISION_CATEGORIES]
    return decisions, info


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


def _subject(status: str, title: str, video_id: str, end_time: str) -> str:
    """
    Every EchoPulpit email subject has the same shape, so recipients can
    filter on "[EchoPulpit]" for everything or on the status word for one
    kind:  [EchoPulpit] <Status>: <service title> -- <service date>
    The date keeps weekly repeats of the same service title (e.g. "Sunday
    Main Worship") from collapsing into one thread in mail clients.
    """
    subject = f"[EchoPulpit] {status}: {title or video_id}"
    if end_time:
        try:
            ended = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
            try:
                ended = ended.astimezone(ZoneInfo(NOTIFY_TIMEZONE))
            except Exception:
                pass  # unknown zone / no tz database: fall back to the UTC date
            subject += f" — {ended:%b} {ended.day}, {ended.year}"
        except ValueError:
            pass
    return subject


def _seo_lines(article: dict) -> list:
    """SEO fields laid out for copying into a blog/CMS by hand. The same
    fields are in article.md's frontmatter and article.html's <meta> tags."""
    if not article:
        return []
    description = article.get("meta_description", "")
    keywords = []
    for k in [article.get("focus_keyword", "")] + list(article.get("keywords") or []):
        k = str(k).strip()
        if k and k.casefold() not in {o.casefold() for o in keywords}:
            keywords.append(k)
    lines = ["SEO -- for your blog or website", "-" * 31]
    lines.append(f"Title: {article.get('title', '')}")
    if article.get("slug"):
        lines.append(f"URL slug: {article['slug']}")
    length_note = "" if 140 <= len(description) <= 160 else " -- aim for 140-160"
    lines.append(f"Meta description ({len(description)} characters{length_note}):")
    lines.append(f"  {description}")
    if article.get("focus_keyword"):
        lines.append(f"Focus keyword: {article['focus_keyword']}")
    if keywords:
        lines.append(f"Meta keywords: {', '.join(keywords)}")
    return lines


def _html_body(article, description, link, button_label, decisions, info, corrections, additions) -> str:
    """HTML version of the completion email: the publish button plus the
    same notes as the plain-text part. Inline styles only (email clients)."""
    esc = html.escape

    def flag_list(items):
        return "<ul style='margin:6px 0 14px;padding-left:20px'>" + "".join(
            f"<li style='margin:4px 0'><b style='text-transform:uppercase;font-size:11px;letter-spacing:.05em;"
            f"color:#6d7782'>{esc(_flag_label(f))}</b> {esc(_flag_text(f))}</li>" for f in items) + "</ul>"

    parts = [f"<p style='font-size:16px;margin:0 0 16px'>{esc(description)}</p>"]
    if link:
        parts.append(
            f"<p style='margin:0 0 20px'><a href='{esc(link)}' style='display:inline-block;background:#064060;"
            f"color:#ffffff;text-decoration:none;font-weight:bold;padding:12px 22px;border-radius:8px'>"
            f"{esc(button_label)}</a></p>")
    if decisions:
        parts.append(f"<h3 style='margin:0;font-size:15px'>Decisions to review before publishing</h3>{flag_list(decisions)}")
    if info:
        parts.append(f"<h3 style='margin:0;font-size:15px'>For your information</h3>{flag_list(info)}")
    if corrections or additions:
        parts.append(f"<p style='color:#4a545e'>Automatically made {len(corrections)} correction(s) and "
                     f"{len(additions)} scripture addition(s); see the PDF's Reviewer Notes for details.</p>")
    seo = _seo_lines(article)
    if seo:
        parts.append("<pre style='white-space:pre-wrap;font-family:Consolas,Menlo,monospace;font-size:13px;"
                     f"background:#f5f3ee;padding:12px;border-radius:8px'>{esc(chr(10).join(seo))}</pre>")
    return ("<div style='font-family:-apple-system,Segoe UI,Arial,sans-serif;color:#1d2329;max-width:640px'>"
            + "".join(parts) + "</div>")


def _send_complete_email(video_id: str, title: str, s3_prefix: str, end_time: str):
    # s3_prefix looks like "s3://bucket/sermons/<id>/"
    prefix = s3_prefix.split(f"s3://{ARTIFACTS_BUCKET}/", 1)[-1]
    article_key = f"{prefix}article.json"
    pdf_key = f"{prefix}sermon-article.pdf"
    md_key = f"{prefix}article.md"
    sermon_txt_key = f"{prefix}sermon.txt"

    article = {}
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
    # Header(): the subject can be non-ASCII (the date's em dash, curly
    # quotes in a service title), which must be RFC 2047-encoded here.
    msg["Subject"] = Header(
        _subject("Review needed" if needs_review else "Article ready", title, video_id, end_time),
        "utf-8",
    )
    recipients = _recipients(article.get("language") or "en")
    msg["From"] = SENDER
    msg["To"] = ", ".join(recipients)

    decisions, info = _split_flags(flags)
    link = _publish_link(video_id)
    button_label = "Review & publish" if needs_review else "Publish to blog"

    body_lines = [meta_description, ""]
    if link:
        body_lines += [f"{button_label}: {link}", ""]
    if decisions:
        body_lines.append("Decisions to review before publishing:")
        body_lines.extend(f"- [{_flag_label(f)}] {_flag_text(f)}" for f in decisions)
        body_lines.append("")
    if info:
        body_lines.append("For your information:")
        body_lines.extend(f"- [{_flag_label(f)}] {_flag_text(f)}" for f in info)
        body_lines.append("")
    if corrections or additions:
        body_lines.append(
            f"Automatically made {len(corrections)} correction(s) and "
            f"{len(additions)} scripture addition(s) -- see the PDF's "
            "Reviewer Notes section for details."
        )
        body_lines.append("")
    body_lines.extend(_seo_lines(article))

    body = MIMEMultipart("alternative")
    body.attach(MIMEText("\n".join(body_lines), "plain", "utf-8"))
    body.attach(MIMEText(_html_body(article, meta_description, link, button_label, decisions, info,
                                    corrections, additions), "html", "utf-8"))
    msg.attach(body)

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

    # The sermon portion of the transcript the article was written from, so
    # the reviewer can check the article against what was actually preached.
    try:
        sermon_obj = _s3.get_object(Bucket=ARTIFACTS_BUCKET, Key=sermon_txt_key)
        sermon_attachment = MIMEText(sermon_obj["Body"].read().decode("utf-8"), "plain", "utf-8")
        sermon_attachment.add_header(
            "Content-Disposition", "attachment", filename="sermon-transcript.txt"
        )
        msg.attach(sermon_attachment)
    except Exception as e:
        print(f"Could not attach sermon transcript for {video_id}: {e}")

    _ses.send_raw_email(
        Source=SENDER, Destinations=recipients, RawMessage={"Data": msg.as_string()}
    )
    print(f"Sent COMPLETE email for {video_id}")


def _send_failed_email(video_id: str, title: str, error: str, failure_count: str, end_time: str):
    subject = _subject(f"Failed (attempt {failure_count})", title, video_id, end_time)
    body = (
        f"Video {video_id} failed to process (attempt {failure_count}).\n\n"
        f"Error: {error}\n"
    )
    _ses.send_email(
        Source=SENDER,
        Destination={"ToAddresses": [RECIPIENT]},
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
        },
    )
    print(f"Sent FAILED alert email for {video_id}")


def _send_published_email(video_id: str, title: str, published_title: str, published_url: str, end_time: str):
    """Sent once, when a "Review & publish" click actually lands the post on
    the blog. Carries a long-lived "Modify" link to unpublish or delete it --
    same manage page the publish link turns into once the article is live."""
    display_title = published_title or title
    article = {}
    try:
        obj = _s3.get_object(Bucket=ARTIFACTS_BUCKET, Key=f"sermons/{video_id}/article.json")
        article = json.loads(obj["Body"].read())
    except Exception as e:
        print(f"Could not read article.json for {video_id}: {e}")

    recipients = _recipients(article.get("language") or "en")
    link = _publish_link(video_id, ttl_days=MANAGE_LINK_TTL_DAYS)
    esc = html.escape

    msg = MIMEMultipart()
    msg["Subject"] = Header(_subject("Published", title, video_id, end_time), "utf-8")
    msg["From"] = SENDER
    msg["To"] = ", ".join(recipients)

    live_line = f'"{display_title}" is now live' + (f" at {published_url}." if published_url else ".")
    body_lines = [live_line, ""]
    if link:
        body_lines += [f"Modify (unpublish or delete): {link}", ""]

    html_parts = [f"<p style='font-size:16px;margin:0 0 16px'>{esc(display_title)} is now live"
                 + (f" at <a href='{esc(published_url)}'>{esc(published_url)}</a>." if published_url else ".")
                 + "</p>"]
    if link:
        html_parts.append(
            f"<p style='margin:0 0 20px'><a href='{esc(link)}' style='display:inline-block;background:#064060;"
            f"color:#ffffff;text-decoration:none;font-weight:bold;padding:12px 22px;border-radius:8px'>"
            f"Modify</a></p>")
    html_body = ("<div style='font-family:-apple-system,Segoe UI,Arial,sans-serif;color:#1d2329;max-width:640px'>"
                + "".join(html_parts) + "</div>")

    body = MIMEMultipart("alternative")
    body.attach(MIMEText("\n".join(body_lines), "plain", "utf-8"))
    body.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(body)

    _ses.send_raw_email(Source=SENDER, Destinations=recipients, RawMessage={"Data": msg.as_string()})
    print(f"Sent PUBLISHED email for {video_id}")


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
        end_time = _get_field(new_image, "actual_end_time")

        old_s3_prefix = _get_field(old_image, "s3_prefix")
        new_s3_prefix = _get_field(new_image, "s3_prefix")
        s3_prefix_just_appeared = bool(new_s3_prefix) and not old_s3_prefix

        old_published_url = _get_field(old_image, "published_url")
        new_published_url = _get_field(new_image, "published_url")
        published_just_happened = bool(new_published_url) and not old_published_url

        if new_status == "COMPLETE" and s3_prefix_just_appeared:
            _send_complete_email(video_id, title, new_s3_prefix, end_time)
        elif new_status == "FAILED" and old_status != "FAILED":
            error = _get_field(new_image, "error", "(no error message recorded)")
            failure_count = _get_field(new_image, "failure_count", "?")
            _send_failed_email(video_id, title, error, failure_count, end_time)
        elif published_just_happened:
            published_title = _get_field(new_image, "published_title")
            _send_published_email(video_id, title, published_title, new_published_url, end_time)

    return {"processed": len(event.get("Records", []))}
