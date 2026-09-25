"""
Publisher Lambda -- behind a Lambda Function URL. Serves the page a
reviewer reaches from the "Review & publish" button in each completion
email, and publishes the article to the blog repo when they confirm.

  GET  /?t=<token>   review page: the article, its reviewer notes grouped
                     by kind, editable preacher/date, and a Publish button.
                     Never changes anything -- mail scanners open links in
                     emails automatically, so a GET must be harmless.
  POST /             (form: t, preacher, date, ack) -- verify the signed
                     token (publish_token.py), require the acknowledgement
                     when the article has decisions to review, claim the
                     job in DynamoDB (published_at, conditional so a link
                     publishes at most once), then commit the post to
                     GITHUB_REPO/POSTS_DIR via the GitHub contents API.
                     The blog's GitHub Action deploys it in ~2 minutes.

The blog post is built the same way the blog's own importer builds drafts:
public fields only (never reviewer notes), the preacher and sermon audio
from the Subsplash feed, a YouTube link for livestreamed services.
"""
import base64
import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import boto3
import markdown
import yaml

from publish_token import InvalidToken, read_token

REGION = os.environ.get("AWS_REGION", "us-east-1")
TABLE_NAME = os.environ.get("SERMON_JOBS_TABLE", "EchoPulpitJobs")
ARTIFACTS_BUCKET = os.environ["SERMON_ARTIFACTS_BUCKET"]
PUBLISH_KEY_SECRET = os.environ.get("PUBLISH_KEY_SECRET", "echopulpit/publish-signing-key")
GITHUB_TOKEN_SECRET = os.environ.get("GITHUB_TOKEN_SECRET", "echopulpit/github-token")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")            # owner/name
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
POSTS_DIR = os.environ.get("POSTS_DIR", "src/content/posts")
BLOG_URL = os.environ.get("BLOG_URL", "").rstrip("/")
FEED_URL = os.environ.get("SUBSPLASH_FEED_URL", "")
CHURCH_TZ = ZoneInfo(os.environ.get("CHURCH_TIMEZONE", "America/Chicago"))

SERVICES = ["Sunday Main Worship", "Sunday Afternoon Worship", "Weekly Bible Hour", "Midweek Worship Service",
            "Servicio en Español"]
SUBSPLASH_PREFIX = "subsplash-"
ITUNES = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}

_s3 = boto3.client("s3", region_name=REGION)
_secrets = boto3.client("secretsmanager", region_name=REGION)
_table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)
_secret_cache: dict = {}


def _secret(name: str) -> str:
    if name not in _secret_cache:
        _secret_cache[name] = _secrets.get_secret_value(SecretId=name)["SecretString"]
    return _secret_cache[name]


# ---- reviewer flags (same rules as sermon_pipeline.flag_category) ----

FLAG_CATEGORIES = ("editorial", "scripture", "transcript", "attribution")
REVIEW_CATEGORIES = {"editorial", "scripture", "other"}
_TAG = re.compile(r"^\s*\[(\w+)\]\s*")


def flag_category(flag: str) -> str:
    m = _TAG.match(str(flag))
    if m and m.group(1).lower() in FLAG_CATEGORIES:
        return m.group(1).lower()
    if re.match(r"\s*(Removed unverifiable scripture citation|Scripture citations were NOT)", str(flag)):
        return "scripture"
    return "other"


def flag_text(flag: str) -> str:
    return _TAG.sub("", str(flag), count=1) if flag_category(flag) in FLAG_CATEGORIES else str(flag)


def flag_label(flag: str) -> str:
    """Display label; untagged (older) flags show as a plain note."""
    c = flag_category(flag)
    return "note" if c == "other" else c


def needs_review(article: dict) -> bool:
    flags = (article.get("reviewer_notes") or {}).get("flags") or []
    return any(flag_category(f) in REVIEW_CATEGORIES for f in flags)


# ---- job facts ----

def service_date(job: dict) -> str:
    end = job.get("actual_end_time") or ""
    try:
        return datetime.fromisoformat(end.replace("Z", "+00:00")).astimezone(CHURCH_TZ).date().isoformat()
    except ValueError:
        return ""


def _norm(s: str) -> str:
    return " ".join((s or "").split()).casefold()


def feed_episode(job: dict):
    """(title, date, audio_url, author) of this job's Subsplash episode, or None."""
    if not FEED_URL:
        return None
    try:
        with urllib.request.urlopen(FEED_URL, timeout=20) as resp:
            channel = ET.fromstring(resp.read()).find("channel")
    except Exception as e:
        print(f"feed fetch failed: {e}")
        return None
    vid = job["video_id"]
    end = job.get("actual_end_time") or ""
    end_date = None
    if end:
        end_date = datetime.fromisoformat(end.replace("Z", "+00:00")).astimezone(timezone.utc).date()
    best, best_gap = None, None
    for item in channel.findall("item"):
        enc = item.find("enclosure")
        pub = item.findtext("pubDate")
        if enc is None or not pub:
            continue
        ep = {
            "title": (item.findtext("title") or "").strip(),
            "date": parsedate_to_datetime(pub).date(),
            "audio": enc.get("url"),
            "author": (item.findtext("itunes:author", default="", namespaces=ITUNES) or "").strip(),
            "guid": (item.findtext("guid") or "").strip(),
            "duration": float(item.findtext("itunes:duration", default="0", namespaces=ITUNES) or 0),
        }
        if vid.startswith(SUBSPLASH_PREFIX):
            if ep["guid"] == vid[len(SUBSPLASH_PREFIX):]:
                return ep
            continue
        if end_date and _norm(ep["title"]) == _norm(job.get("title", "")) \
                and end_date - timedelta(days=1) <= ep["date"] <= end_date:
            gap = abs(ep["duration"] - float(job.get("video_duration_seconds") or 0))
            if best is None or gap < best_gap:
                best, best_gap = ep, gap
    return best


def service_of(*titles: str):
    for t in titles:
        for s in SERVICES:
            if _norm(t) == _norm(s) or _norm(t).endswith(_norm(s)):
                return s
    return None


def separate_blockquotes(md: str) -> str:
    """EchoPulpit writes the paragraph after a scripture quote on the next
    line, which Markdown folds into the quote; end each quote cleanly."""
    return re.sub(r"^(>[^\n]*\n)(?=[^>\s])", r"\1\n", md, flags=re.M)


def slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def resolve_slug(article: dict, title: str) -> str:
    """Slug for `title` -- keep the pipeline's slug unless the reviewer edited the title."""
    if title == (article.get("title") or ""):
        return article.get("slug") or slugify(title)
    return slugify(title)


def build_post(article: dict, job: dict, preacher: str, pub_date: str, episode) -> tuple[str, str]:
    """(repo path, file contents) for the blog post."""
    vid = job["video_id"]
    slug = article.get("slug") or slugify(article["title"])
    front = {
        "title": article["title"],
        "slug": slug,
        "description": article.get("meta_description") or "",
        "pubDate": pub_date,
        "preacher": preacher or None,
        "service": service_of(job.get("title", ""), episode["title"] if episode else ""),
        "primaryPassage": article.get("primary_passage") or None,
        "scripture": article.get("scripture_references") or [],
        "keywords": article.get("keywords") or [],
        "tags": [],
        "audioUrl": episode["audio"] if episode else None,
        "videoUrl": None if vid.startswith(SUBSPLASH_PREFIX) else f"https://www.youtube.com/watch?v={vid}",
        "sourceId": vid,
        # Blog shows "es" posts under /es/ (English is the default, left out).
        "lang": None if (article.get("language") or "en") == "en" else article["language"],
    }
    front = {k: v for k, v in front.items() if v not in (None, "")}
    body = separate_blockquotes((article.get("article_markdown") or "").strip() + "\n")
    text = "---\n" + yaml.safe_dump(front, sort_keys=False, allow_unicode=True, width=1000) + "---\n\n" + body
    return f"{POSTS_DIR}/{pub_date}-{slug}.md", text


# ---- GitHub ----

def _github(method: str, path: str, payload: dict | None = None):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/{urllib.parse.quote(path)}"
        + (f"?ref={GITHUB_BRANCH}" if method == "GET" else ""),
        method=method,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={
            "Authorization": f"Bearer {_secret(GITHUB_TOKEN_SECRET).strip()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "EchoPulpit-publisher",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def commit_post(path: str, text: str, title: str) -> str:
    status, _ = _github("GET", path)
    if status == 200:
        raise RuntimeError(f"{path} already exists in the blog repo")
    status, body = _github("PUT", path, {
        "message": f"Publish: {title}",
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        "branch": GITHUB_BRANCH,
    })
    if status not in (200, 201):
        raise RuntimeError(f"GitHub returned {status}: {body.get('message', body)}")
    return body.get("commit", {}).get("html_url", "")


def get_post(path: str) -> tuple[str, str]:
    """(file contents, blob sha) of an already-published post."""
    status, body = _github("GET", path)
    if status != 200:
        raise RuntimeError(f"{path} not found in the blog repo (GitHub returned {status})")
    return base64.b64decode(body["content"]).decode("utf-8"), body["sha"]


def update_post(path: str, text: str, message: str, sha: str) -> str:
    status, body = _github("PUT", path, {
        "message": message,
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        "branch": GITHUB_BRANCH,
        "sha": sha,
    })
    if status not in (200, 201):
        raise RuntimeError(f"GitHub returned {status}: {body.get('message', body)}")
    return body.get("commit", {}).get("html_url", "")


def delete_post(path: str, message: str, sha: str) -> str:
    status, body = _github("DELETE", path, {"message": message, "branch": GITHUB_BRANCH, "sha": sha})
    if status not in (200, 201):
        raise RuntimeError(f"GitHub returned {status}: {body.get('message', body)}")
    return body.get("commit", {}).get("html_url", "")


_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n\n?(.*)$", re.S)


def patch_frontmatter(text: str, **fields) -> str:
    """Update specific frontmatter fields on an already-built post file,
    leaving everything else (including any hand edits, and the file's own
    path/URL) untouched. A value of None or "" removes that field."""
    m = _FRONTMATTER.match(text)
    if not m:
        raise RuntimeError("post file has no frontmatter")
    front = yaml.safe_load(m.group(1)) or {}
    for k, v in fields.items():
        if v in (None, ""):
            front.pop(k, None)
        else:
            front[k] = v
    return "---\n" + yaml.safe_dump(front, sort_keys=False, allow_unicode=True, width=1000) + "---\n\n" + m.group(2)


# ---- pages ----

CSS = """
:root{--navy:#064060;--red:#8a1515;--bg:#fbfaf7;--fg:#1d2329;--muted:#6d7782;--line:#e3e0d8;--card:#fff;--quote:#f7f1e4;--warn:#fff4e5;--ok:#e8f5ee}
@media (prefers-color-scheme:dark){:root{--bg:#10161c;--fg:#e6e8eb;--muted:#8a939d;--line:#27313b;--card:#161e26;--quote:#1c2530;--warn:#2a2114;--ok:#14261c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,-apple-system,'Segoe UI',sans-serif}
header{background:var(--navy);color:#fff;border-bottom:4px solid var(--red);padding:14px 16px}
header b{font-size:1.05rem}main{max-width:46rem;margin:0 auto;padding:20px 16px 48px}
h1{font-family:Georgia,serif;font-size:1.7rem;line-height:1.2;margin:.2em 0 .3em}.muted{color:var(--muted);font-size:.92rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:16px 0}
.card h2{font-size:.8rem;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);margin:0 0 8px}
.banner{border-radius:10px;padding:12px 14px;margin:14px 0;font-weight:600}.banner.warn{background:var(--warn)}.banner.ok{background:var(--ok)}
ul{margin:0;padding-left:1.2em}li{margin:.35em 0}.tag{display:inline-block;font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;padding:1px 7px;border-radius:99px;background:var(--quote);margin-right:6px}
label{display:block;font-weight:600;margin:10px 0 4px}input[type=text],input[type=date]{width:100%;padding:9px 10px;border:1px solid var(--line);border-radius:8px;font:inherit;background:var(--bg);color:var(--fg)}
.title-wrap{margin:.2em 0 .3em}.title-input{font-family:Georgia,serif;font-size:1.7rem;line-height:1.2;font-weight:700;width:100%;border:1px solid transparent;background:transparent;color:var(--fg);padding:.15em 0}.title-input:hover,.title-input:focus{outline:none;border-color:var(--line);background:var(--card);border-radius:8px;padding:.15em .5em}
#slug-preview{word-break:break-all}
.ack{display:flex;gap:10px;align-items:flex-start;font-weight:400;margin-top:14px}.ack input{margin-top:4px;width:18px;height:18px}
button{margin-top:16px;width:100%;padding:13px;border:0;border-radius:10px;background:var(--navy);color:#fff;font:inherit;font-weight:700;font-size:1.05rem;cursor:pointer}
.article{font-family:Georgia,serif;font-size:1.08rem;line-height:1.7}.article blockquote{margin:1em 0;padding:.7em 1em;background:var(--quote);border-left:4px solid var(--red);font-style:italic}
.err{color:#c0392b;font-weight:600}a{color:inherit}
"""


def page(title: str, content: str, status: int = 200) -> dict:
    doc = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<meta name="robots" content="noindex,nofollow"><title>{html.escape(title)}</title>'
           f"<style>{CSS}</style></head><body><header><b>Sermon articles</b> · publish</header>"
           f"<main>{content}</main></body></html>")
    return {"statusCode": status, "headers": {"Content-Type": "text/html; charset=utf-8",
                                              "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            "body": doc}


def message_page(title: str, text: str, status: int = 200) -> dict:
    return page(title, f"<h1>{html.escape(title)}</h1><p>{text}</p>", status)


def _list(items) -> str:
    return "<ul>" + "".join(f"<li>{html.escape(str(i))}</li>" for i in items) + "</ul>"


def review_page(token: str, job: dict, article: dict, error: str = "", form: dict | None = None,
                republish: bool = False) -> dict:
    """The publish-review form, and (republish=True) the same form reused to
    make minor title/preacher/date edits when bringing an unpublished post
    back -- no reviewer decisions to re-accept there (the article content
    isn't being regenerated), and the URL stays whatever it already is."""
    esc = html.escape
    notes = {} if republish else (article.get("reviewer_notes") or {})
    flags = notes.get("flags") or []
    decisions = [f for f in flags if flag_category(f) in REVIEW_CATEGORIES]
    info = [f for f in flags if flag_category(f) not in REVIEW_CATEGORIES]
    review = False if republish else needs_review(article)
    episode = feed_episode(job)
    form = form or {}
    title = form.get("title", article.get("title") or "")
    preacher = form.get("preacher", article.get("preacher") or (episode or {}).get("author") or "")
    pub_date = form.get("date", service_date(job) or article.get("preached_on") or "")

    if republish:
        title_block = f'''<div class="title-wrap">
<input type="text" id="title" name="title" form="pub" class="title-input" value="{esc(title)}" required>
<p class="muted">URL stays {esc(job.get("published_url") or "")}</p>
</div>'''
    else:
        slug = resolve_slug(article, title)
        lang_prefix = "" if (article.get("language") or "en") == "en" else f"/{article['language']}"
        title_block = f'''<div class="title-wrap">
<input type="text" id="title" name="title" form="pub" class="title-input" value="{esc(title)}" required
       oninput="document.getElementById('slug-preview').textContent=this.value.toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'')">
<p class="muted">URL: {esc(BLOG_URL or "")}{esc(lang_prefix)}/posts/<span id="slug-preview">{esc(slug)}</span>/</p>
</div>'''

    def flag_items(items):
        return "<ul>" + "".join(
            f'<li><span class="tag">{esc(flag_label(f))}</span>{esc(flag_text(f))}</li>' for f in items) + "</ul>"

    parts = [
        f'<p class="muted">{esc(job.get("title", ""))} · {esc(pub_date)}</p>',
        title_block,
        f'<p class="muted">{esc(article.get("meta_description", ""))}</p>',
    ]
    if not republish:
        parts.append(f'<div class="banner warn">Review needed: {len(decisions)} decision(s) below. Publishing means you accept them.</div>'
                     if review else '<div class="banner ok">No decisions to review. Ready to publish.</div>')
    if error:
        parts.append(f'<p class="err">{esc(error)}</p>')
    if decisions:
        parts.append(f'<div class="card"><h2>Decisions to review</h2>{flag_items(decisions)}</div>')
    if info:
        parts.append(f'<div class="card"><h2>For your information</h2>{flag_items(info)}</div>')
    if notes.get("corrections"):
        parts.append(f'<div class="card"><h2>Corrections made</h2>{_list(notes["corrections"])}</div>')
    if notes.get("additions"):
        parts.append(f'<div class="card"><h2>Verses added</h2>{_list(notes["additions"])}</div>')
    ack = ""
    if review:
        ack = ('<label class="ack"><input type="checkbox" name="ack" value="1" required> '
               "I've read the decisions above and accept them.</label>")
    republish_field = '<input type="hidden" name="republish" value="1">' if republish else ""
    button_label = "Republish" if republish else "Publish to the blog"
    caption = (f"Live again at {esc(job.get('published_url') or BLOG_URL or 'the blog')} about two minutes after "
              "republishing." if republish else
              f"Goes live at {esc(BLOG_URL or 'the blog')} about two minutes after publishing.")
    parts.append(f"""<form class="card" method="post" action="./" id="pub">
<h2>{button_label}</h2>
<input type="hidden" name="t" value="{esc(token)}">
{republish_field}
<label for="preacher">Preacher</label>
<input type="text" id="preacher" name="preacher" value="{esc(preacher)}" placeholder="e.g. Pastor James Sickmeyer">
<label for="date">Service date</label>
<input type="date" id="date" name="date" value="{esc(pub_date)}" required>
{ack}
<button type="submit">{button_label}</button>
<p class="muted">{caption}</p>
</form>""")
    body_md = separate_blockquotes(article.get("article_markdown") or "")
    parts.append(f'<div class="card"><h2>The article</h2><div class="article">{markdown.markdown(body_md)}</div></div>')
    return page(f"{'Republish' if republish else 'Publish'}: {title}", "\n".join(parts))


def manage_page(token: str, job: dict, article: dict, error: str = "", notice: str = "") -> dict:
    esc = html.escape
    title = job.get("published_title") or article.get("title", "")
    url = job.get("published_url") or ""
    is_draft = bool(job.get("is_draft"))
    when = (job.get("published_date") or (job.get("published_at") or "")[:10])
    parts = [
        f"<h1>{esc(title)}</h1>",
        f'<p class="muted">Published {esc(when)}'
        + (f' &middot; <a href="{esc(url)}">{esc(url)}</a>' if url else "") + "</p>",
        ('<div class="banner warn">Unpublished &mdash; hidden from the live site.</div>' if is_draft
         else '<div class="banner ok">Live on the blog.</div>'),
    ]
    if notice:
        parts.append(f'<div class="banner ok">{esc(notice)}</div>')
    if error:
        parts.append(f'<p class="err">{esc(error)}</p>')

    if is_draft:
        parts.append(f"""<form class="card" method="get" action="./">
<h2>Republish</h2>
<p class="muted">Make this post live on the blog again -- brings up the same review step as the
original publish, in case you want to make minor edits first.</p>
<input type="hidden" name="t" value="{esc(token)}">
<input type="hidden" name="republish" value="1">
<button type="submit">Republish&hellip;</button>
</form>""")
    else:
        parts.append(f"""<form class="card" method="post" action="./" \
onsubmit="return confirm('Hide this post from the live site? You can republish it later.')">
<h2>Unpublish</h2>
<p class="muted">Hides this post from the live site. You can republish it later from this same page.</p>
<input type="hidden" name="t" value="{esc(token)}">
<input type="hidden" name="action" value="unpublish">
<button type="submit">Unpublish</button>
</form>""")

    parts.append(f"""<form class="card" method="post" action="./" \
onsubmit="return confirm('Delete this post? It comes down from the live site, and this page cannot bring it back afterward.')">
<h2>Delete</h2>
<p class="muted">Removes this post from the live site. It stays recoverable in the blog's GitHub history, but this
page cannot bring it back afterward.</p>
<input type="hidden" name="t" value="{esc(token)}">
<input type="hidden" name="action" value="delete">
<button type="submit" style="background:var(--red)">Delete</button>
</form>""")
    return page(f"Manage: {title}", "\n".join(parts))


def deleted_page(job: dict) -> dict:
    title = job.get("published_title") or ""
    when = (job.get("deleted_at") or "")[:10]
    return message_page("Deleted", f"“{html.escape(title)}” was deleted on {html.escape(when)}.")


# ---- handler ----

def _form(event) -> dict:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}


def _load(token: str):
    video_id = read_token(_secret(PUBLISH_KEY_SECRET).encode("utf-8"), token)
    job = _table.get_item(Key={"video_id": video_id}).get("Item")
    if not job or job.get("status") != "COMPLETE":
        raise InvalidToken("this article isn't available to publish")
    obj = _s3.get_object(Bucket=ARTIFACTS_BUCKET, Key=f"sermons/{video_id}/article.json")
    return job, json.loads(obj["Body"].read())


def _handle_manage(token: str, job: dict, article: dict, form: dict) -> dict:
    action = form.get("action", "")
    if action not in ("unpublish", "delete"):
        return manage_page(token, job, article, "Unknown action.")
    vid = job["video_id"]
    path = job.get("published_path", "")
    title = job.get("published_title") or article.get("title", "")
    if not path:
        return manage_page(token, job, article, "No published file on record; can't modify.")
    try:
        content, sha = get_post(path)
    except Exception as e:
        return manage_page(token, job, article, f"Could not load the post from GitHub: {e}")

    if action == "delete":
        try:
            delete_post(path, f"Delete: {title}", sha)
        except Exception as e:
            return manage_page(token, job, article, f"Couldn't delete: {e}")
        now = datetime.now(timezone.utc).isoformat()
        _table.update_item(Key={"video_id": vid}, UpdateExpression="SET deleted_at = :t",
                           ExpressionAttributeValues={":t": now})
        return deleted_page({**job, "deleted_at": now})

    try:
        update_post(path, patch_frontmatter(content, draft=True), f"Unpublish: {title}", sha)
    except Exception as e:
        return manage_page(token, job, article, f"Couldn't unpublish: {e}")
    _table.update_item(Key={"video_id": vid}, UpdateExpression="SET is_draft = :t",
                       ExpressionAttributeValues={":t": True})
    return manage_page(token, {**job, "is_draft": True}, article,
                       notice="Unpublished. Hidden from the live site in about two minutes.")


def _handle_republish_edit(token: str, job: dict, article: dict, form: dict) -> dict:
    """POST from review_page(republish=True): apply the (possibly edited)
    title/preacher/date to the existing file in place and clear `draft` --
    the URL/path never change here, only content."""
    title = (form.get("title") or "").strip()[:200]
    preacher = (form.get("preacher") or "").strip()[:120]
    pub_date = (form.get("date") or "").strip()
    if not title:
        return review_page(token, job, article, "Please enter a title.", form, republish=True)
    try:
        date.fromisoformat(pub_date)
    except ValueError:
        return review_page(token, job, article, "Please enter the service date.", form, republish=True)

    vid = job["video_id"]
    path = job.get("published_path", "")
    if not path:
        return manage_page(token, job, article, "No published file on record; can't modify.")
    try:
        content, sha = get_post(path)
    except Exception as e:
        return review_page(token, job, article, f"Could not load the post from GitHub: {e}", form, republish=True)

    try:
        new_text = patch_frontmatter(content, title=title, preacher=preacher, pubDate=pub_date, draft=None)
        update_post(path, new_text, f"Republish: {title}", sha)
    except Exception as e:
        return review_page(token, job, article, f"Couldn't republish: {e}", form, republish=True)

    _table.update_item(
        Key={"video_id": vid},
        UpdateExpression="SET published_title = :ti, published_preacher = :pr, published_date = :d",
        ExpressionAttributeValues={":ti": title, ":pr": preacher, ":d": pub_date},
    )
    _table.update_item(Key={"video_id": vid}, UpdateExpression="REMOVE is_draft")
    return manage_page(token, {**job, "published_title": title, "published_preacher": preacher,
                               "published_date": pub_date, "is_draft": False}, article,
                       notice="Republished. Live again in about two minutes.")


def lambda_handler(event, context):
    method = (event.get("requestContext", {}).get("http", {}).get("method") or "GET").upper()
    if method == "POST":
        form = _form(event)
        token = form.get("t", "")
    else:
        form = {}
        token = (event.get("queryStringParameters") or {}).get("t", "")
    if not token:
        return message_page("Nothing to publish", "Open this page from the link in an article email.", 400)
    try:
        job, article = _load(token)
    except InvalidToken as e:
        return message_page("Link not valid", html.escape(str(e).capitalize()) + ".", 403)

    if job.get("deleted_at"):
        return deleted_page(job)
    if job.get("published_at"):
        if method == "POST":
            if form.get("republish") == "1":
                return _handle_republish_edit(token, job, article, form)
            return _handle_manage(token, job, article, form)
        if (event.get("queryStringParameters") or {}).get("republish") == "1":
            seed = {"title": job.get("published_title") or article.get("title") or "",
                    "preacher": job.get("published_preacher") or "",
                    "date": job.get("published_date") or ""}
            return review_page(token, job, article, form=seed, republish=True)
        return manage_page(token, job, article)
    if method != "POST":
        return review_page(token, job, article)

    title = (form.get("title") or "").strip()[:200]
    preacher = (form.get("preacher") or "").strip()[:120]
    pub_date = (form.get("date") or "").strip()
    if not title:
        return review_page(token, job, article, "Please enter a title.", form)
    try:
        date.fromisoformat(pub_date)
    except ValueError:
        return review_page(token, job, article, "Please enter the service date.", form)
    review = needs_review(article)
    if review and form.get("ack") != "1":
        return review_page(token, job, article, "Please confirm you've read and accept the decisions.", form)

    vid = job["video_id"]
    now = datetime.now(timezone.utc).isoformat()
    flags = (article.get("reviewer_notes") or {}).get("flags") or []
    try:
        _table.update_item(
            Key={"video_id": vid},
            UpdateExpression="SET published_at = :t, review_acknowledged = :ack, review_flags_accepted = :f",
            ConditionExpression="attribute_not_exists(published_at)",
            ExpressionAttributeValues={":t": now, ":ack": review, ":f": [str(f) for f in flags] if review else []},
        )
    except _table.meta.client.exceptions.ConditionalCheckFailedException:
        return manage_page(token, _table.get_item(Key={"video_id": vid})["Item"], article)

    effective_article = {**article, "title": title, "slug": resolve_slug(article, title)}
    try:
        path, text = build_post(effective_article, job, preacher, pub_date, feed_episode(job))
        commit_url = commit_post(path, text, title)
    except Exception as e:
        # Release the claim so the reviewer can try again.
        _table.update_item(Key={"video_id": vid},
                           UpdateExpression="REMOVE published_at, review_acknowledged, review_flags_accepted")
        print(f"publish failed for {vid}: {e!r}")
        return review_page(token, job, article, f"Publishing failed, nothing was published: {e}", form)

    slug = path.rsplit("/", 1)[-1][len(pub_date) + 1:-3]
    lang_prefix = "" if (effective_article.get("language") or "en") == "en" else f"/{effective_article['language']}"
    post_url = f"{BLOG_URL}{lang_prefix}/posts/{effective_article.get('slug') or slug}/" if BLOG_URL else ""
    _table.update_item(
        Key={"video_id": vid},
        UpdateExpression="SET published_url = :u, published_path = :p, published_commit = :c, "
                         "published_preacher = :pr, published_date = :d, published_title = :ti",
        ExpressionAttributeValues={":u": post_url, ":p": path, ":c": commit_url, ":pr": preacher, ":d": pub_date,
                                   ":ti": title},
    )
    print(f"published {vid} -> {path}")
    link = f'<a href="{html.escape(post_url)}">{html.escape(post_url)}</a>' if post_url else "the blog"
    return message_page("Published", f"“{html.escape(title)}” will be live at {link} in about two "
                        "minutes, once the site finishes rebuilding.")
