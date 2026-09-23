"""
Regenerate finished articles from their saved sermon transcripts with the
current article model/settings (deploy/config.worker.yaml's llm section),
then re-send each completion email. Use after changing the model, prompt
or style guide.

No re-transcription: reads sermons/<id>/sermon.txt from S3 and runs the
production generation path (llm_generate_article, which includes KJV
scripture verification). For each job it rebuilds article.json,
article.md, article.html, article_raw.md and sermon-article.pdf exactly as
run_pipeline() does, uploads them over the old ones, then clears and
re-sets the job's s3_prefix so the Notifier Lambda sends the email.

Selects COMPLETE jobs whose service date (the stream's end time in
CHURCH_TIMEZONE) falls in --from..--to, or the video IDs given. Skips
transcripts under MIN_SERMON_WORDS (they would fail anyway).

Dry run by default. Usage (repo root, with AWS credentials):
    python scripts/regenerate_articles.py --from 2026-08-01 --to 2026-09-30
    python scripts/regenerate_articles.py --from 2026-08-01 --to 2026-09-30 --apply
    python scripts/regenerate_articles.py --apply VIDEO_ID [VIDEO_ID...]
"""
import argparse
import json
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from zoneinfo import ZoneInfo

import boto3
import yaml

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import anthropic  # noqa: E402

import sermon_pipeline as sp  # noqa: E402
from render_pdf import render_pdf  # noqa: E402

REGION = os.environ.get("AWS_REGION", "us-east-1")
BUCKET = os.environ.get("ARTIFACTS_BUCKET", "echopulpit-artifacts")
TABLE = os.environ.get("TABLE_NAME", "EchoPulpitJobs")
CHURCH_TZ = ZoneInfo(os.environ.get("CHURCH_TIMEZONE", "America/Chicago"))

s3 = boto3.client("s3", region_name=REGION)
table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


def get_text(key: str) -> str:
    return s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8")


def service_date(job) -> date | None:
    end = job.get("actual_end_time")
    if not end:
        return None
    return datetime.fromisoformat(end.replace("Z", "+00:00")).astimezone(CHURCH_TZ).date()


def select_jobs(args):
    jobs, kwargs = [], {}
    while True:
        resp = table.scan(**kwargs)
        jobs += resp["Items"]
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    done = [j for j in jobs if j.get("status") == "COMPLETE" and j.get("s3_prefix")]
    if args.ids:
        return [j for j in done if j["video_id"] in args.ids]
    lo, hi = date.fromisoformat(args.date_from), date.fromisoformat(args.date_to)
    return sorted((j for j in done if service_date(j) and lo <= service_date(j) <= hi),
                  key=lambda j: j.get("actual_end_time", ""))


def regenerate(job, llm_cfg, style_guide, apply):
    vid = job["video_id"]
    prefix = f"sermons/{vid}/"
    label = f"{vid} ({job.get('title', '')}, {service_date(job)})"
    sermon_text = get_text(prefix + "sermon.txt")
    try:
        sp.check_sermon_text_length(sermon_text)
    except RuntimeError as e:
        return f"skip   {label}: {e}"
    if not apply:
        return f"would  {label}: {len(sermon_text.split())} words of sermon text"

    start = time.time()
    backend = sp.ClaudeBackend(anthropic.Anthropic(), llm_cfg["claude_model"])
    with tempfile.TemporaryDirectory() as job_dir:
        article = sp.llm_generate_article(backend, sermon_text, dict(llm_cfg), job_dir, style_guide=style_guide)

        frontmatter_only = {k: v for k, v in article.items() if k != "article_markdown"}
        outputs = {
            "article.json": json.dumps(article, ensure_ascii=False, indent=2),
            "article.md": "---\n" + yaml.safe_dump(frontmatter_only, sort_keys=False, allow_unicode=True)
                          + "---\n\n" + article.get("article_markdown", ""),
            "article.html": sp.build_article_html(article),
        }
        for name, text in outputs.items():
            with open(os.path.join(job_dir, name), "w", encoding="utf-8") as f:
                f.write(text)
        render_pdf(
            pdf_path=os.path.join(job_dir, "sermon-article.pdf"),
            title=article.get("title", "Sermon Article"),
            meta_description=article.get("meta_description", ""),
            article_markdown=article.get("article_markdown", ""),
            scripture_refs=article.get("scripture_references", []),
            reviewer_notes=article.get("reviewer_notes", {}),
        )
        types = {"article.json": "application/json", "article.md": "text/markdown; charset=utf-8",
                 "article.html": "text/html; charset=utf-8", "article_raw.md": "text/markdown; charset=utf-8",
                 "sermon-article.pdf": "application/pdf"}
        for name, ctype in types.items():
            path = os.path.join(job_dir, name)
            if os.path.exists(path):
                s3.upload_file(path, BUCKET, prefix + name, ExtraArgs={"ContentType": ctype})

    # Two separate updates so the Notifier sees s3_prefix go empty -> set.
    table.update_item(Key={"video_id": vid}, UpdateExpression="REMOVE s3_prefix")
    table.update_item(Key={"video_id": vid}, UpdateExpression="SET s3_prefix = :p",
                      ExpressionAttributeValues={":p": job["s3_prefix"]})
    words = len(article.get("article_markdown", "").split())
    return (f"done   {label}: {words} words, '{article.get('title', '')}' "
            f"({time.time() - start:.0f}s) -- email re-sent")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="*", help="specific video IDs (instead of a date range)")
    ap.add_argument("--from", dest="date_from", help="first service date, YYYY-MM-DD")
    ap.add_argument("--to", dest="date_to", help="last service date, YYYY-MM-DD")
    ap.add_argument("--apply", action="store_true", help="regenerate, upload and re-send (default: dry run)")
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args()
    if not args.ids and not (args.date_from and args.date_to):
        ap.error("give video IDs or --from and --to")

    llm_cfg = yaml.safe_load(open("deploy/config.worker.yaml", encoding="utf-8"))["llm"]
    style_guide = get_text("app/prompts/style_guide.md")
    if args.apply and not os.environ.get("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = boto3.client("secretsmanager", region_name=REGION).get_secret_value(
            SecretId="echopulpit/anthropic-api-key")["SecretString"]

    jobs = select_jobs(args)
    print(f"{len(jobs)} job(s) selected; model {llm_cfg['claude_model']}, "
          f"claude_max_tokens {llm_cfg.get('claude_max_tokens')}, effort {llm_cfg.get('thinking_effort')}\n")
    failed = 0
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = {pool.submit(regenerate, j, llm_cfg, style_guide, args.apply): j for j in jobs}
        for f in as_completed(futures):
            try:
                print(f.result(), flush=True)
            except Exception as e:
                failed += 1
                print(f"FAILED {futures[f]['video_id']}: {e!r}", flush=True)
    if not args.apply:
        print("\nDry run -- nothing changed. Re-run with --apply.")
    elif failed:
        print(f"\n{failed} job(s) failed; their old article and email were left as they were.")


if __name__ == "__main__":
    sys.exit(main())
