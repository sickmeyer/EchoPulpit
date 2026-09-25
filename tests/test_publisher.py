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


def test_resolve_slug_keeps_pipeline_slug_when_title_unchanged():
    assert pl.resolve_slug(ARTICLE, ARTICLE["title"]) == ARTICLE["slug"]


def test_resolve_slug_regenerates_from_edited_title():
    assert pl.resolve_slug(ARTICLE, "A Whole New Title!") == "a-whole-new-title"


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
        expr = kw["UpdateExpression"].strip()
        values = kw.get("ExpressionAttributeValues", {})
        if expr.startswith("SET "):
            for clause in expr[4:].split(","):
                field, _, val_ref = clause.strip().partition("=")
                self.job[field.strip()] = values[val_ref.strip()]
        elif expr.startswith("REMOVE "):
            for field in expr[7:].split(","):
                self.job.pop(field.strip(), None)


@pytest.fixture
def env(monkeypatch):
    table = FakeTable(JOB)
    commits = []
    manage_calls = []
    posts = {}  # path -> (text, sha), populated by the fake commit_post/update_post
    monkeypatch.setattr(pl, "_table", table)
    monkeypatch.setattr(pl, "_secret", lambda name: KEY.decode())
    monkeypatch.setattr(pl, "_load", lambda token: (table.get_item(Key={})["Item"], state["article"]))
    monkeypatch.setattr(pl, "feed_episode", lambda job: None)

    def fake_commit_post(path, text, title):
        commits.append(path)
        posts[path] = (text, "sha-1")
        return "https://github/commit"

    def fake_get_post(path):
        return posts[path]

    def fake_update_post(path, text, message, sha):
        manage_calls.append(("update", path, message))
        posts[path] = (text, "sha-2")
        return "https://github/commit-update"

    def fake_delete_post(path, message, sha):
        manage_calls.append(("delete", path, message))
        posts.pop(path, None)
        return "https://github/commit-delete"

    monkeypatch.setattr(pl, "commit_post", fake_commit_post)
    monkeypatch.setattr(pl, "get_post", fake_get_post)
    monkeypatch.setattr(pl, "update_post", fake_update_post)
    monkeypatch.setattr(pl, "delete_post", fake_delete_post)
    state = {"article": dict(ARTICLE), "table": table, "commits": commits, "manage_calls": manage_calls,
             "posts": posts}
    return state


def _post(fields):
    fields = {"title": ARTICLE["title"], **fields}
    return {"requestContext": {"http": {"method": "POST"}}, "body": urllib.parse.urlencode(fields)}


def _get(t="tok"):
    return {"requestContext": {"http": {"method": "GET"}}, "queryStringParameters": {"t": t}}


def _get_republish(t="tok"):
    return {"requestContext": {"http": {"method": "GET"}}, "queryStringParameters": {"t": t, "republish": "1"}}


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
    assert "Manage:" in again["body"] and "Unpublish" in again["body"]
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


def test_post_rejects_empty_title(env):
    resp = pl.lambda_handler(_post({"t": "tok", "date": "2026-09-13", "title": ""}), None)
    assert "enter a title" in resp["body"] and env["commits"] == []


def test_editing_title_changes_slug_and_url(env):
    resp = pl.lambda_handler(
        _post({"t": "tok", "date": "2026-09-13", "title": "Faith That Confesses With the Mouth"}), None)
    assert "Published" in resp["body"]
    assert env["commits"] == ["src/content/posts/2026-09-13-faith-that-confesses-with-the-mouth.md"]
    published_url_update = next(u for u in env["table"].updates if ":u" in u.get("ExpressionAttributeValues", {}))
    assert published_url_update["ExpressionAttributeValues"][":u"] == \
        "https://blog.example.org/posts/faith-that-confesses-with-the-mouth/"
    assert published_url_update["ExpressionAttributeValues"][":ti"] == "Faith That Confesses With the Mouth"


def _publish(env):
    resp = pl.lambda_handler(_post({"t": "tok", "preacher": "Joseph", "date": "2026-09-13"}), None)
    assert "Published" in resp["body"]
    return env["table"].job["published_path"]


def test_manage_page_shows_after_publish(env):
    _publish(env)
    resp = pl.lambda_handler(_get(), None)
    assert resp["statusCode"] == 200
    assert "Manage:" in resp["body"] and "Unpublish" in resp["body"] and "Delete" in resp["body"]
    assert "Republish" not in resp["body"]


def test_unpublish_sets_draft_and_offers_republish(env):
    path = _publish(env)
    resp = pl.lambda_handler(_post({"t": "tok", "action": "unpublish"}), None)
    assert "Unpublished" in resp["body"] and "Republish" in resp["body"]
    assert env["manage_calls"] == [("update", path, "Unpublish: Faith Changes Things")]
    text, _ = env["posts"][path]
    assert "draft: true" in text
    # DynamoDB reflects it, and a fresh GET still shows the unpublished state.
    assert env["table"].job["is_draft"] is True
    again = pl.lambda_handler(_get(), None)
    assert "Unpublished" in again["body"] and "Republish" in again["body"]


def test_republish_click_shows_review_step_prefilled(env):
    path = _publish(env)
    pl.lambda_handler(_post({"t": "tok", "action": "unpublish"}), None)
    resp = pl.lambda_handler(_get_republish(), None)
    assert resp["statusCode"] == 200
    assert "Republish" in resp["body"]
    assert 'value="Faith Changes Things"' in resp["body"]  # pre-filled from published_title
    assert 'value="Joseph"' in resp["body"]  # pre-filled from published_preacher
    assert 'value="2026-09-13"' in resp["body"]  # pre-filled from published_date
    # No reviewer decisions/ack to re-accept -- the content isn't being regenerated.
    assert "Review needed" not in resp["body"] and "accept them" not in resp["body"]
    # editing the title here must not offer to change the URL
    assert "URL stays" in resp["body"]
    assert env["manage_calls"] == [("update", path, "Unpublish: Faith Changes Things")]  # no extra calls yet


def test_republish_with_edits_updates_content_but_not_url(env):
    path = _publish(env)
    pl.lambda_handler(_post({"t": "tok", "action": "unpublish"}), None)
    resp = pl.lambda_handler(_post({"t": "tok", "republish": "1", "title": "Faith Changes Things (Revised)",
                                    "preacher": "Nelson Bonilla", "date": "2026-09-14"}), None)
    assert "Republished" in resp["body"] and "Unpublish" in resp["body"]
    assert env["manage_calls"][-1] == ("update", path, "Republish: Faith Changes Things (Revised)")
    text, _ = env["posts"][path]
    assert "draft" not in text
    assert "title: Faith Changes Things (Revised)" in text
    assert "preacher: Nelson Bonilla" in text
    assert "pubDate: '2026-09-14'" in text
    assert "is_draft" not in env["table"].job
    assert env["table"].job["published_title"] == "Faith Changes Things (Revised)"
    assert env["table"].job["published_preacher"] == "Nelson Bonilla"
    assert env["table"].job["published_date"] == "2026-09-14"
    # the file path/URL never changed, even though the title did
    assert env["table"].job["published_path"] == path


def test_republish_without_edits_just_clears_draft(env):
    path = _publish(env)
    pl.lambda_handler(_post({"t": "tok", "action": "unpublish"}), None)
    resp = pl.lambda_handler(_post({"t": "tok", "republish": "1", "date": "2026-09-13"}), None)
    assert "Republished" in resp["body"]
    text, _ = env["posts"][path]
    assert "draft" not in text
    assert "title: Faith Changes Things" in text


def test_republish_rejects_empty_title(env):
    _publish(env)
    pl.lambda_handler(_post({"t": "tok", "action": "unpublish"}), None)
    resp = pl.lambda_handler(_post({"t": "tok", "republish": "1", "title": "", "date": "2026-09-13"}), None)
    assert "enter a title" in resp["body"]
    assert "is_draft" in env["table"].job  # still unpublished, nothing changed


def test_delete_removes_post_and_blocks_further_management(env):
    path = _publish(env)
    resp = pl.lambda_handler(_post({"t": "tok", "action": "delete"}), None)
    assert "Deleted" in resp["body"]
    assert env["manage_calls"] == [("delete", path, "Delete: Faith Changes Things")]
    assert path not in env["posts"]
    assert "deleted_at" in env["table"].job
    again = pl.lambda_handler(_get(), None)
    assert "Deleted" in again["body"]
    # No further action is possible once deleted -- neither a toggle action...
    resp = pl.lambda_handler(_post({"t": "tok", "action": "unpublish"}), None)
    assert "Deleted" in resp["body"]
    # ...nor the republish-edit flow, from either the GET or the POST side.
    resp = pl.lambda_handler(_get_republish(), None)
    assert "Deleted" in resp["body"]
    resp = pl.lambda_handler(_post({"t": "tok", "republish": "1", "title": "New Title", "date": "2026-09-13"}), None)
    assert "Deleted" in resp["body"]
    assert len(env["manage_calls"]) == 1


def test_manage_rejects_unknown_action(env):
    _publish(env)
    resp = pl.lambda_handler(_post({"t": "tok", "action": "explode"}), None)
    assert "Unknown action" in resp["body"]
    assert env["manage_calls"] == []


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
