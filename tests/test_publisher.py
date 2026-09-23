import os
import sys
import urllib.parse

import pytest

os.environ.setdefault("SERMON_ARTIFACTS_BUCKET", "bkt")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("GITHUB_REPO", "owner/blog")
os.environ.setdefault("BLOG_URL", "https://blog.example.org")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deploy", "lambdas"))

import publish_token as pt  # noqa: E402
import publisher_lambda as pl  # noqa: E402

KEY = b"test-signing-key"


# ---- tokens ----

def test_token_round_trip():
    assert pt.read_token(KEY, pt.make_token(KEY, "J8uEgK3bFA0")) == "J8uEgK3bFA0"


def test_token_tampered_or_wrong_key():
    token = pt.make_token(KEY, "vid")
    payload, sig = token.split(".")
    other = pt.make_token(KEY, "other-vid").split(".")[0]
    with pytest.raises(pt.InvalidToken):
        pt.read_token(KEY, f"{other}.{sig}")  # someone else's article, same signature
    with pytest.raises(pt.InvalidToken):
        pt.read_token(b"wrong-key", token)
    with pytest.raises(pt.InvalidToken):
        pt.read_token(KEY, "garbage")


def test_token_expiry():
    token = pt.make_token(KEY, "vid", ttl_days=1, now=1_000_000)
    assert pt.read_token(KEY, token, now=1_000_000 + 3600) == "vid"
    with pytest.raises(pt.InvalidToken, match="expired"):
        pt.read_token(KEY, token, now=1_000_000 + 2 * 86400)


# ---- post building ----

ARTICLE = {
    "title": "Faith Changes Things",
    "slug": "faith-changes-things-romans-10",
    "meta_description": "Romans 10 says faith is confessed with the mouth.",
    "primary_passage": "Romans 10:6-9",
    "scripture_references": ["Romans 10:6-9", "Luke 17:15-16"],
    "keywords": ["faith", "Romans 10"],
    "preacher": "Joseph",
    "article_markdown": '> "And one of them..." (Luke 17:15-16)\nNine men walked on.\n\n## Heading\n\nBody.',
    "reviewer_notes": {"flags": ["[transcript] ends mid-sentence"], "corrections": [], "additions": []},
}
JOB = {"video_id": "VQ7TXygc1_o", "title": "Weekly Bible Hour", "actual_end_time": "2026-09-13T15:35:45Z",
       "video_duration_seconds": 1986, "status": "COMPLETE"}


def test_build_post_public_fields_only():
    ep = {"title": "Weekly Bible Hour", "audio": "https://cdn/audio.mp3", "author": ""}
    path, text = pl.build_post(ARTICLE, JOB, "Pastor James Sickmeyer", "2026-09-13", ep)
    assert path == "src/content/posts/2026-09-13-faith-changes-things-romans-10.md"
    assert "reviewer_notes" not in text and "needs_review" not in text
    assert "preacher: Pastor James Sickmeyer" in text
    assert "service: Weekly Bible Hour" in text
    assert "audioUrl: https://cdn/audio.mp3" in text
    assert "videoUrl: https://www.youtube.com/watch?v=VQ7TXygc1_o" in text
    assert "pubDate: '2026-09-13'" in text
    # the paragraph after a quote is separated from it
    assert "(Luke 17:15-16)\n\nNine men walked on." in text


def test_build_post_feed_job_has_no_video_and_omits_empty_preacher():
    job = {**JOB, "video_id": "subsplash-abc", "title": "Self-serving Bias"}
    _, text = pl.build_post(ARTICLE, job, "", "2023-06-04", None)
    assert "videoUrl" not in text and "preacher" not in text and "audioUrl" not in text


def test_needs_review_rules():
    assert pl.needs_review(ARTICLE) is False
    assert pl.needs_review({"reviewer_notes": {"flags": ["[editorial] politics"]}}) is True
    assert pl.needs_review({"reviewer_notes": {"flags": ["legacy untagged flag"]}}) is True


# ---- handler ----

class FakeTable:
    def __init__(self, job):
        self.job = dict(job)
        self.updates = []

        class Exc:
            class ConditionalCheckFailedException(Exception):
                pass

        class Client:
            exceptions = Exc

        class Meta:
            client = Client

        self.meta = Meta

    def get_item(self, Key):
        return {"Item": dict(self.job)}

    def update_item(self, **kw):
        self.updates.append(kw)
        if kw.get("ConditionExpression") == "attribute_not_exists(published_at)" and "published_at" in self.job:
            raise self.meta.client.exceptions.ConditionalCheckFailedException()
        if kw["UpdateExpression"].startswith("SET published_at"):
            self.job["published_at"] = kw["ExpressionAttributeValues"][":t"]
        elif kw["UpdateExpression"].startswith("REMOVE published_at"):
            self.job.pop("published_at", None)


@pytest.fixture
def env(monkeypatch):
    table = FakeTable(JOB)
    commits = []
    monkeypatch.setattr(pl, "_table", table)
    monkeypatch.setattr(pl, "_secret", lambda name: KEY.decode())
    monkeypatch.setattr(pl, "_load", lambda token: (table.get_item(Key={})["Item"], state["article"]))
    monkeypatch.setattr(pl, "feed_episode", lambda job: None)
    monkeypatch.setattr(pl, "commit_post", lambda path, text, title: commits.append(path) or "https://github/commit")
    state = {"article": dict(ARTICLE), "table": table, "commits": commits}
    return state


def _post(fields):
    return {"requestContext": {"http": {"method": "POST"}}, "body": urllib.parse.urlencode(fields)}


def test_get_shows_review_page_and_never_publishes(env):
    resp = pl.lambda_handler({"requestContext": {"http": {"method": "GET"}},
                              "queryStringParameters": {"t": "tok"}}, None)
    assert resp["statusCode"] == 200
    assert "Publish to the blog" in resp["body"]
    assert env["commits"] == [] and env["table"].updates == []


def test_post_publishes_once(env):
    resp = pl.lambda_handler(_post({"t": "tok", "preacher": "Pastor James Sickmeyer", "date": "2026-09-13"}), None)
    assert "Published" in resp["body"]
    assert env["commits"] == ["src/content/posts/2026-09-13-faith-changes-things-romans-10.md"]
    again = pl.lambda_handler(_post({"t": "tok", "date": "2026-09-13"}), None)
    assert "Already published" in again["body"]
    assert len(env["commits"]) == 1


def test_post_requires_ack_when_review_needed(env):
    env["article"]["reviewer_notes"] = {"flags": ["[editorial] softened a political aside"]}
    resp = pl.lambda_handler(_post({"t": "tok", "date": "2026-09-13"}), None)
    assert "confirm" in resp["body"] and env["commits"] == []
    resp = pl.lambda_handler(_post({"t": "tok", "date": "2026-09-13", "ack": "1"}), None)
    assert "Published" in resp["body"] and len(env["commits"]) == 1


def test_post_rejects_bad_date(env):
    resp = pl.lambda_handler(_post({"t": "tok", "date": "not-a-date"}), None)
    assert "service date" in resp["body"] and env["commits"] == []


def test_failed_commit_releases_claim(env, monkeypatch):
    def boom(*a):
        raise RuntimeError("GitHub returned 401")
    monkeypatch.setattr(pl, "commit_post", boom)
    resp = pl.lambda_handler(_post({"t": "tok", "date": "2026-09-13"}), None)
    assert "nothing was published" in resp["body"]
    assert "published_at" not in env["table"].job  # can be retried


def test_invalid_token_page(monkeypatch):
    monkeypatch.setattr(pl, "_secret", lambda name: KEY.decode())
    resp = pl.lambda_handler({"requestContext": {"http": {"method": "GET"}},
                              "queryStringParameters": {"t": "forged.token"}}, None)
    assert resp["statusCode"] == 403
