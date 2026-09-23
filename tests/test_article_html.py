import io
import json
import os
import sys

from sermon_pipeline import article_meta_keywords, build_article_html

ARTICLE = {
    "title": 'The Man Who Would Not Say "I Blew It"',
    "slug": "the-man-who-would-not-say-i-blew-it",
    "meta_description": "Self-serving bias from Adam to Saul & David -- and the healing power of <simply> saying 'I blew it.'",
    "focus_keyword": "self-serving bias",
    "keywords": ["Self-Serving Bias", "blame shifting", "repentance", ""],
    "preacher": "Pastor James Sickmeyer",
    "article_markdown": "Opening line.\n\n## A heading\n\nBody.",
}


def test_meta_keywords_focus_first_and_deduplicated():
    assert article_meta_keywords(ARTICLE) == ["self-serving bias", "blame shifting", "repentance"]


def test_meta_keywords_empty_when_missing():
    assert article_meta_keywords({"title": "x"}) == []


def test_html_has_seo_meta_tags_escaped():
    html = build_article_html(ARTICLE)
    assert "<title>The Man Who Would Not Say &quot;I Blew It&quot;</title>" in html
    assert '<meta name="keywords" content="self-serving bias, blame shifting, repentance"/>' in html
    assert '<meta name="author" content="Pastor James Sickmeyer"/>' in html
    assert 'content="Self-serving bias from Adam to Saul &amp; David -- and the healing power of &lt;simply&gt; saying &#x27;I blew it.&#x27;"' in html
    assert '<meta property="og:title"' in html and '<meta name="twitter:card" content="summary"/>' in html
    assert "<h2>A heading</h2>" in html


def test_html_omits_empty_keywords_and_author():
    html = build_article_html({"title": "T", "meta_description": "D", "article_markdown": "x"})
    assert 'name="keywords"' not in html
    assert 'name="author"' not in html


def _notifier():
    os.environ.setdefault("SES_SENDER_ADDRESS", "a@example.com")
    os.environ.setdefault("NOTIFY_RECIPIENT_ADDRESS", "a@example.com")
    os.environ.setdefault("SERMON_ARTIFACTS_BUCKET", "bkt")
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deploy", "lambdas"))
    import notifier_lambda
    return notifier_lambda


def test_email_body_has_labeled_seo_block():
    n = _notifier()
    lines = n._seo_lines(ARTICLE)
    text = "\n".join(lines)
    assert "URL slug: the-man-who-would-not-say-i-blew-it" in text
    assert "Focus keyword: self-serving bias" in text
    assert "Meta keywords: self-serving bias, blame shifting, repentance" in text
    assert f"Meta description ({len(ARTICLE['meta_description'])} characters" in text


def test_email_seo_block_flags_description_length():
    n = _notifier()
    short = n._seo_lines({**ARTICLE, "meta_description": "Too short."})
    assert any("aim for 140-160" in line for line in short)
    ok = n._seo_lines({**ARTICLE, "meta_description": "x" * 150})
    assert not any("aim for 140-160" in line for line in ok)
    assert n._seo_lines({}) == []


def test_completion_email_includes_seo_block():
    n = _notifier()
    files = {
        "sermons/v/article.json": json.dumps({**ARTICLE, "needs_review": False}).encode(),
        "sermons/v/sermon-article.pdf": b"%PDF",
        "sermons/v/article.md": b"# a",
        "sermons/v/sermon.txt": b"text",
    }

    class S3:
        def get_object(self, Bucket, Key):
            return {"Body": io.BytesIO(files[Key])}

    sent = {}

    class SES:
        def send_raw_email(self, **kw):
            sent.update(kw)

    n._s3, n._ses = S3(), SES()
    n._send_complete_email("v", "Weekly Bible Hour", "s3://bkt/sermons/v/", "2023-06-04T12:00:00Z")
    import email
    msg = email.message_from_string(sent["RawMessage"]["Data"])
    body = next(p for p in msg.walk() if p.get_content_type() == "text/plain" and not p.get_filename())
    text = body.get_payload(decode=True).decode("utf-8")
    assert "SEO -- for your blog or website" in text
    assert "Meta keywords: self-serving bias, blame shifting, repentance" in text
