"""
Apply KJV scripture verification to articles that were generated while
data/kjv.json was missing (they carry a "Scripture citations were NOT
independently verified" reviewer flag), then re-render their outputs and
re-send the completion email.

No LLM call: verification is deterministic post-processing of the article
text already in S3 -- the same verify_and_correct_scripture() step the
pipeline runs right after generation. For each video ID:
  1. download article.json (+ article_raw.md for the model's own
     needs_review verdict)
  2. drop the "not verified" flag, verify/correct every scripture
     blockquote, recompute scripture_references / flags / needs_review
  3. rebuild article.json, article.md, article.html, sermon-article.pdf
     exactly as run_pipeline() does, and upload them over the old ones
  4. clear and re-set the job's s3_prefix in DynamoDB -- the Notifier
     Lambda sends the completion email when s3_prefix appears

Dry run by default (prints what would change). Usage:
    python scripts/reverify_scripture.py VIDEO_ID [VIDEO_ID...]
    python scripts/reverify_scripture.py --apply VIDEO_ID [VIDEO_ID...]
Env: ARTIFACTS_BUCKET (default echopulpit-artifacts), TABLE_NAME
(default EchoPulpitJobs), AWS_REGION (default us-east-1).
"""
import json
import os
import sys
import tempfile

import boto3

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import yaml  # noqa: E402

from render_pdf import render_pdf  # noqa: E402
from scripture_lookup import kjv_available, verify_and_correct_scripture  # noqa: E402
from sermon_pipeline import build_article_html, compute_needs_review, parse_article_markdown  # noqa: E402

REGION = os.environ.get("AWS_REGION", "us-east-1")
BUCKET = os.environ.get("ARTIFACTS_BUCKET", "echopulpit-artifacts")
TABLE = os.environ.get("TABLE_NAME", "EchoPulpitJobs")
UNVERIFIED_FLAG_PREFIX = "Scripture citations were NOT independently verified"

s3 = boto3.client("s3", region_name=REGION)
table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


def _get(key: str) -> str:
    return s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8")


def reverify(video_id: str, apply: bool) -> None:
    prefix = f"sermons/{video_id}/"
    item = table.get_item(Key={"video_id": video_id}).get("Item") or {}
    if item.get("status") != "COMPLETE" or not item.get("s3_prefix"):
        print(f"{video_id}: skipped (status={item.get('status')}, s3_prefix={item.get('s3_prefix')!r})")
        return

    article = json.loads(_get(prefix + "article.json"))
    notes = article.setdefault("reviewer_notes", {})
    old_flags = notes.get("flags") or []
    if not any(UNVERIFIED_FLAG_PREFIX in f for f in old_flags):
        print(f"{video_id}: skipped (already verified)")
        return

    model_needs_review = True
    try:
        raw_frontmatter, _ = parse_article_markdown(_get(prefix + "article_raw.md"))
        model_needs_review = bool(raw_frontmatter.get("needs_review", True))
    except Exception as e:
        print(f"{video_id}: couldn't read the model's needs_review ({e}); keeping review on")

    body, refs, new_flags = verify_and_correct_scripture(article.get("article_markdown", ""))
    kept_flags = [f for f in old_flags if UNVERIFIED_FLAG_PREFIX not in f]
    notes["flags"] = kept_flags + [f"[scripture] {f}" for f in new_flags]
    article["scripture_references"] = refs
    article["article_markdown"] = body
    article["needs_review"] = compute_needs_review(notes["flags"])

    changed = sum(
        a != b for a, b in zip(
            [l for l in json.loads(_get(prefix + "article.json"))["article_markdown"].splitlines() if l.startswith(">")],
            [l for l in body.splitlines() if l.startswith(">")],
        )
    )
    print(f"{video_id}: {len(refs)} citation(s) verified, {changed} quote line(s) corrected, "
          f"{len(new_flags)} removed as unverifiable; needs_review={article['needs_review']} "
          f"({item.get('title')})")
    for f in new_flags:
        print(f"    flag: {f}")
    if not apply:
        return

    with tempfile.TemporaryDirectory() as tmp:
        json_path = os.path.join(tmp, "article.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(article, f, ensure_ascii=False, indent=2)

        frontmatter_only = {k: v for k, v in article.items() if k != "article_markdown"}
        md_path = os.path.join(tmp, "article.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("---\n" + yaml.safe_dump(frontmatter_only, sort_keys=False, allow_unicode=True)
                    + "---\n\n" + body)

        html_path = os.path.join(tmp, "article.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(build_article_html(article))

        pdf_path = os.path.join(tmp, "sermon-article.pdf")
        render_pdf(
            pdf_path=pdf_path,
            title=article.get("title", "Sermon Article"),
            meta_description=article.get("meta_description", ""),
            article_markdown=body,
            scripture_refs=refs,
            reviewer_notes=notes,
        )

        for path, ctype in ((json_path, "application/json"), (md_path, "text/markdown; charset=utf-8"),
                            (html_path, "text/html; charset=utf-8"), (pdf_path, "application/pdf")):
            s3.upload_file(path, BUCKET, prefix + os.path.basename(path), ExtraArgs={"ContentType": ctype})

    # Two separate updates so the Notifier sees s3_prefix go empty -> set.
    s3_prefix = item["s3_prefix"]
    table.update_item(Key={"video_id": video_id}, UpdateExpression="REMOVE s3_prefix")
    table.update_item(Key={"video_id": video_id}, UpdateExpression="SET s3_prefix = :p",
                      ExpressionAttributeValues={":p": s3_prefix})
    print(f"{video_id}: uploaded and completion email re-triggered")


def main() -> None:
    args = sys.argv[1:]
    apply = "--apply" in args
    ids = [a for a in args if a != "--apply"]
    if not ids:
        sys.exit(__doc__)
    if not kjv_available():
        sys.exit("data/kjv.json is missing -- run scripts/build_kjv.py first")
    for vid in ids:
        reverify(vid, apply)
    if not apply:
        print("\nDry run -- nothing changed. Re-run with --apply to upload and re-send emails.")


if __name__ == "__main__":
    main()
