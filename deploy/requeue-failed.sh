#!/usr/bin/env bash
#
# Put FAILED jobs back in the queue after fixing whatever made them fail
# (most often: YouTube rotated the yt-dlp cookies and every audio download
# since has hit "Sign in to confirm you're not a bot").
#
# Why this is needed: the poller only retries a FAILED job while it's under
# SERMON_MAX_ATTEMPTS (3) AND still among the channel's most recent uploads.
# Anything that failed 3 times, or has scrolled out of the recent list, is
# dead-lettered forever. This resets failure_count to 0 and marks the job
# QUEUED with an ancient claimed_at, so the poller's stale-job reclaim pass
# (which scans the table, not YouTube) relaunches it on its next run
# (every 15 min) regardless of age.
#
# Dry-run by default -- lists what it would requeue and changes nothing.
#
# Usage:
#   export AWS_REGION=us-east-1                 # optional
#   export TABLE_NAME=EchoPulpitJobs            # optional
#   ./deploy/requeue-failed.sh                  # dry run: list FAILED jobs
#   ./deploy/requeue-failed.sh --cookies cookies.txt --apply
#                                               # store fresh cookies, then requeue all
#   ./deploy/requeue-failed.sh --apply VIDEO_ID [VIDEO_ID...]
#                                               # requeue just these
#
# --cookies FILE  replaces the echopulpit/ytdlp-cookies secret with FILE
#                 (Netscape format, see README) before requeueing. Only
#                 takes effect with --apply.
# --force         requeue even though the cookies secret hasn't changed
#                 since the latest failure (normally refused, since every
#                 requeued job would just launch an instance and fail again).
set -uo pipefail

REGION="${AWS_REGION:-us-east-1}"
TABLE_NAME="${TABLE_NAME:-EchoPulpitJobs}"
COOKIES_SECRET="echopulpit/ytdlp-cookies"
PY="$(command -v python3 || command -v python)"

APPLY=0
FORCE=0
COOKIES_FILE=""
ONLY_IDS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --force) FORCE=1 ;;
    --cookies) COOKIES_FILE="${2:?--cookies needs a file path}"; shift ;;
    -h|--help) sed -n '2,31p' "$0"; exit 0 ;;
    -*) echo "Unknown option: $1" >&2; exit 2 ;;
    *) ONLY_IDS+=("$1") ;;
  esac
  shift
done

if [[ -n "$COOKIES_FILE" && ! -s "$COOKIES_FILE" ]]; then
  echo "Cookies file '$COOKIES_FILE' is missing or empty" >&2
  exit 1
fi

# ---- Collect FAILED jobs (tab-separated: id, attempts, completed_at, title) ----
jobs="$(aws dynamodb scan --table-name "$TABLE_NAME" --region "$REGION" \
  --filter-expression "#s = :f" \
  --expression-attribute-names '{"#s":"status"}' \
  --expression-attribute-values '{":f":{"S":"FAILED"}}' \
  --output json | "$PY" -c '
import json, sys
only = set(sys.argv[1:])
items = json.load(sys.stdin)["Items"]
g = lambda i, k: list(i.get(k, {"S": ""}).values())[0]
for i in sorted(items, key=lambda i: g(i, "completed_at")):
    if only and g(i, "video_id") not in only:
        continue
    print("\t".join([g(i, "video_id"), str(g(i, "failure_count")), g(i, "completed_at"), g(i, "title")]))
' "${ONLY_IDS[@]}")" || { echo "Failed to scan $TABLE_NAME" >&2; exit 1; }

if [[ -z "$jobs" ]]; then
  echo "No FAILED jobs${ONLY_IDS:+ matching the given IDs} in $TABLE_NAME -- nothing to do."
  exit 0
fi

count="$(printf '%s\n' "$jobs" | wc -l | tr -d ' ')"
echo "FAILED jobs ($count):"
printf '%s\n' "$jobs" | awk -F'\t' '{printf "  %-12s attempts=%-2s failed=%s  %s\n", $1, $2, substr($3,1,19), $4}'

if [[ $APPLY -eq 0 ]]; then
  echo ""
  echo "Dry run -- nothing changed. Re-run with --apply (and --cookies FILE if the"
  echo "failures were YouTube bot checks) to requeue these."
  exit 0
fi

# ---- Refresh cookies first, so the relaunched workers pick them up ----
if [[ -n "$COOKIES_FILE" ]]; then
  echo ""
  echo "Updating secret $COOKIES_SECRET from $COOKIES_FILE"
  aws secretsmanager put-secret-value --secret-id "$COOKIES_SECRET" --region "$REGION" \
    --secret-string "file://${COOKIES_FILE}" >/dev/null || { echo "Failed to update cookies secret" >&2; exit 1; }
elif [[ $FORCE -eq 0 ]]; then
  # Guard against requeueing into the same failure: if the cookies haven't
  # changed since the most recent failure, every job will just fail again.
  cookies_changed="$(aws secretsmanager describe-secret --secret-id "$COOKIES_SECRET" --region "$REGION" \
    --query LastChangedDate --output text 2>/dev/null)"
  latest_failure="$(printf '%s\n' "$jobs" | cut -f3 | sort | tail -1)"
  if [[ -n "$cookies_changed" ]] && "$PY" -c '
import sys
from datetime import datetime
changed, failed = (datetime.fromisoformat(s) for s in sys.argv[1:3])
sys.exit(0 if changed < failed else 1)
' "$cookies_changed" "$latest_failure"; then
    echo ""
    echo "Refusing: $COOKIES_SECRET was last changed $cookies_changed, before the latest"
    echo "failure ($latest_failure), so these jobs would most likely fail the same way."
    echo "Pass --cookies FILE with a fresh export, or --force if you fixed something else"
    echo "(e.g. enabled the Subsplash feed in the worker config)."
    exit 1
  fi
fi

# ---- Requeue ----
echo ""
ok=0
while IFS=$'\t' read -r vid _attempts _completed _title; do
  if aws dynamodb update-item --table-name "$TABLE_NAME" --region "$REGION" \
      --key "{\"video_id\":{\"S\":\"$vid\"}}" \
      --update-expression "SET #s = :q, failure_count = :zero, claimed_at = :epoch REMOVE #e, completed_at, instance_id" \
      --condition-expression "#s = :f" \
      --expression-attribute-names '{"#s":"status","#e":"error"}' \
      --expression-attribute-values '{":q":{"S":"QUEUED"},":f":{"S":"FAILED"},":zero":{"N":"0"},":epoch":{"S":"1970-01-01T00:00:00+00:00"}}' \
      >/dev/null; then
    echo "  requeued $vid"
    ok=$((ok + 1))
  else
    echo "  FAILED to requeue $vid (status changed underneath us?)"
  fi
done <<< "$jobs"

echo ""
echo "Requeued $ok of $count. The poller (every 15 min) will launch a worker for each;"
echo "you'll get the usual COMPLETE/FAILED email per job. Watch progress with:"
echo "  aws dynamodb scan --table-name $TABLE_NAME --region $REGION --query 'Items[].[video_id.S,status.S]' --output text"
