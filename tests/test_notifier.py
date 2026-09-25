import email
import io
import json
import os
import sys

import pytest

# Same values test_spanish.py's _lambdas() uses -- notifier_lambda's module-level
# constants are computed once at first import, so every test module that imports
# it needs to agree on these (setdefault is a no-op after the first import).
os.environ.setdefault("SES_SENDER_ADDRESS", "a@example.com")
os.environ.setdefault("NOTIFY_RECIPIENT_ADDRESS", "a@example.com")
os.environ.setdefault("SERMON_ARTIFACTS_BUCKET", "bkt")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("PUBLISH_URL", "https://publish.example.org")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deploy", "lambdas"))

import notifier_lambda as nl  # noqa: E402


class FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}


class FakeSecrets:
    def get_secret_value(self, SecretId):
        return {"SecretString": "test-signing-key"}


class FakeSES:
    def __init__(self):
        self.sent = []

    def send_raw_email(self, **kw):
        self.sent.append(kw)

    def send_email(self, **kw):
        self.sent.append(kw)


def _image(d: dict) -> dict:
    return {k: {"S": v} for k, v in d.items()}


def _ddb_record(old: dict, new: dict) -> dict:
    return {"eventName": "MODIFY", "dynamodb": {"OldImage": _image(old), "NewImage": _image(new)}}


def _decoded_body(raw_message: str) -> str:
    """Every text/plain and text/html part, decoded (email bodies are base64/QP-encoded)."""
    msg = email.message_from_string(raw_message)
    parts = []
    for part in msg.walk():
        if part.get_content_type() in ("text/plain", "text/html"):
            parts.append(part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8"))
    return "\n".join(parts)


@pytest.fixture
def env(monkeypatch):
    ses = FakeSES()
    s3 = FakeS3({"sermons/vid1/article.json": json.dumps({"language": "en"}).encode("utf-8")})
    monkeypatch.setattr(nl, "_ses", ses)
    monkeypatch.setattr(nl, "_s3", s3)
    monkeypatch.setattr(nl, "_secrets", FakeSecrets())
    monkeypatch.setattr(nl, "_signing_key", None)
    return {"ses": ses, "s3": s3}


def test_published_email_sent_when_published_url_appears(env):
    record = _ddb_record(
        old={"video_id": "vid1", "status": "COMPLETE", "s3_prefix": "s3://bkt/sermons/vid1/"},
        new={"video_id": "vid1", "status": "COMPLETE", "s3_prefix": "s3://bkt/sermons/vid1/",
             "title": "Sunday Main Worship", "published_url": "https://blog.example.org/posts/x/",
             "published_title": "Faith Changes Things"})
    nl.lambda_handler({"Records": [record]}, None)
    assert len(env["ses"].sent) == 1
    call = env["ses"].sent[0]
    assert call["Destinations"] == ["a@example.com"]
    body = _decoded_body(call["RawMessage"]["Data"])
    assert "Faith Changes Things" in body
    assert "?t=" in body  # the manage link


def test_published_email_not_sent_when_url_unchanged(env):
    record = _ddb_record(
        old={"video_id": "vid1", "published_url": "https://blog.example.org/posts/x/"},
        new={"video_id": "vid1", "published_url": "https://blog.example.org/posts/x/", "is_draft": "true"})
    nl.lambda_handler({"Records": [record]}, None)
    assert env["ses"].sent == []


def test_published_email_recipients_match_reviewer_recipients(env, monkeypatch):
    monkeypatch.setenv("NOTIFY_EXTRA_RECIPIENTS_ES", "nelson@example.org")
    env["s3"].objects["sermons/vid2/article.json"] = json.dumps({"language": "es"}).encode("utf-8")
    record = _ddb_record(
        old={"video_id": "vid2"},
        new={"video_id": "vid2", "title": "Servicio en Espanol",
             "published_url": "https://blog.example.org/es/posts/x/", "published_title": "Titulo"})
    nl.lambda_handler({"Records": [record]}, None)
    assert env["ses"].sent[0]["Destinations"] == ["a@example.com", "nelson@example.org"]


def test_manage_link_uses_long_ttl(env, monkeypatch):
    captured = {}
    real_make_token = nl.make_token

    def spy(key, video_id, **kw):
        captured.update(kw)
        return real_make_token(key, video_id, **kw)

    monkeypatch.setattr(nl, "make_token", spy)
    record = _ddb_record(
        old={"video_id": "vid1"},
        new={"video_id": "vid1", "title": "T", "published_url": "https://blog.example.org/posts/x/",
             "published_title": "T"})
    nl.lambda_handler({"Records": [record]}, None)
    assert captured.get("ttl_days") == nl.MANAGE_LINK_TTL_DAYS
