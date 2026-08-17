#!/usr/bin/env bash
#
# EC2 user-data on a STOCK Amazon Linux 2023 AMI. One instance = one job:
# fetch the app code + a job's video metadata, install what's needed,
# generate the article, upload artifacts, record status, terminate.
#
# Deliberately no pre-baked custom AMI: the app is small and Claude-primary
# (no local GGUF model to bake in), so paying Packer's build/maintenance
# cost isn't worth it for v1 -- a few seconds of boot-time install is cheap
# and keeps the AMI itself just "stock AL2023."
#
# Deliberately does NOT use `set -e`: we want to reach the S3 upload and
# DynamoDB status update even if the pipeline itself fails, and we want the
# terminate-self step (registered via `trap ... EXIT`) to run unconditionally.
set -uo pipefail

LOG_FILE="/var/log/echopulpit-worker.log"
exec > >(tee -a "$LOG_FILE") 2>&1

now() { date -u +%FT%TZ; }
echo "[$(now)] bootstrap.sh starting"

APP_DIR="/opt/echopulpit"
OUTPUT_ROOT="/data/output"
ARTIFACTS_BUCKET="${SERMON_ARTIFACTS_BUCKET:-echopulpit-artifacts}"
APP_PREFIX="${SERMON_APP_PREFIX:-app}"
ANTHROPIC_SECRET_ID="${ANTHROPIC_SECRET_ID:-echopulpit/anthropic-api-key}"
WATCHDOG_HOURS="${SERMON_WATCHDOG_HOURS:-3}"

mkdir -p "$APP_DIR" "$OUTPUT_ROOT"

# ---- IMDSv2 token + metadata helpers ----
imds_token() {
  curl -sf -X PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 21600"
}
TOKEN="$(imds_token)"

imds() {
  curl -sf -H "X-aws-ec2-metadata-token: $TOKEN" "http://169.254.169.254/latest/meta-data/$1"
}

INSTANCE_ID="$(imds instance-id)"
REGION="$(imds placement/region)"
export AWS_DEFAULT_REGION="$REGION"

echo "[$(now)] instance_id=$INSTANCE_ID region=$REGION"

# ---- Boot-time watchdog: force-terminate no matter what, in case the main
# process hangs. This is a hard cost cap independent of the trap below
# (which itself might not fire if something goes really wrong). ----
if command -v at >/dev/null 2>&1; then
  echo "shutdown -h now" | at now + "${WATCHDOG_HOURS}" hours 2>/dev/null || true
else
  nohup bash -c "sleep $((WATCHDOG_HOURS * 3600)); shutdown -h now" >/dev/null 2>&1 &
fi

terminate_self() {
  # Best-effort log upload before termination -- console output for a
  # short-lived instance is often empty (the serial console buffer hasn't
  # flushed yet by the time it terminates), so this is the only reliable
  # way to debug a failure after the fact. Uses VIDEO_ID if we got far
  # enough to read it, otherwise falls back to the instance ID so an
  # early failure (e.g. missing tags) is still visible.
  local log_key="sermons/${VIDEO_ID:-unknown-$INSTANCE_ID}/bootstrap.log"
  for attempt in 1 2 3; do
    aws s3 cp "$LOG_FILE" "s3://${ARTIFACTS_BUCKET}/${log_key}" >/dev/null 2>&1 && break
    sleep 2
  done
  echo "[$(now)] Terminating instance $INSTANCE_ID"
  aws ec2 terminate-instances --instance-ids "$INSTANCE_ID" --region "$REGION" >/dev/null 2>&1 || true
  # Backstop in case the API call itself failed (network blip, IAM issue,
  # etc). InstanceInitiatedShutdownBehavior=terminate on the launch config
  # means this still results in TERMINATED, not just STOPPED.
  shutdown -h now || true
}
trap terminate_self EXIT

# ---- Read job metadata from our own instance tags. The Poller Lambda sets
# all of these when it launches the instance, so this worker never needs a
# YouTube API key of its own. ----
tag() {
  aws ec2 describe-tags --region "$REGION" \
    --filters "Name=resource-id,Values=$INSTANCE_ID" "Name=key,Values=$1" \
    --query 'Tags[0].Value' --output text 2>/dev/null
}

# This is the first IAM-authenticated call in the script. Instance-profile
# credentials via IMDS can take a few seconds to become available after
# boot -- calling before they're ready fails closed (empty VIDEO_ID), which
# used to abort the whole job. Retry instead of failing on the first miss.
VIDEO_ID=""
for attempt in $(seq 1 10); do
  VIDEO_ID="$(tag SermonVideoId)"
  if [[ -n "$VIDEO_ID" && "$VIDEO_ID" != "None" ]]; then
    break
  fi
  echo "[$(now)] SermonVideoId tag not readable yet (attempt $attempt/10), retrying..."
  sleep 3
done

VIDEO_TITLE="$(tag SermonVideoTitle)"
VIDEO_DURATION_SECONDS="$(tag SermonVideoDurationSeconds)"
VIDEO_END_TIME="$(tag SermonVideoEndTime)"

if [[ -z "$VIDEO_ID" || "$VIDEO_ID" == "None" ]]; then
  echo "[$(now)] ERROR: no SermonVideoId tag found on this instance -- aborting"
  exit 1
fi

echo "[$(now)] Processing video_id=$VIDEO_ID title=${VIDEO_TITLE:-<unknown>}"

# ---- Install OS-level deps ----
# No dnf here on purpose: an earlier throwaway test instance died mid-boot
# right after a `dnf install` step, consistent with AL2023's automatic
# update/reboot behavior kicking in during a dnf transaction. ffmpeg is
# fetched directly as a static build instead, so this never touches the
# package manager.

echo "[$(now)] Installing ffmpeg (static build)"
FFMPEG_OK=0
FFMPEG_TARBALL=/tmp/ffmpeg.tar.xz

# Worker instance type varies by architecture (Graviton/arm64 vs Intel/x86_64
# -- see WORKER_INSTANCE_TYPE on the Poller Lambda), so bootstrap.sh has to
# fetch the matching ffmpeg build rather than assuming amd64.
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64)
    FFMPEG_MIRROR_KEY="deps/ffmpeg-static-linux-amd64.tar.xz"
    FFMPEG_UPSTREAM_URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
    ;;
  aarch64)
    FFMPEG_MIRROR_KEY="deps/ffmpeg-static-linux-arm64.tar.xz"
    FFMPEG_UPSTREAM_URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz"
    ;;
  *)
    echo "[$(now)] ERROR: unrecognized architecture '$ARCH' -- no ffmpeg build mapping"
    FFMPEG_MIRROR_KEY=""
    FFMPEG_UPSTREAM_URL=""
    ;;
esac

fetch_ffmpeg() {
  # Our own S3-hosted mirror first -- fast, IAM-authenticated, and doesn't
  # depend on a third party's uptime for something this basic. Falls back
  # to the upstream static-build site only if our mirror is somehow
  # missing. Observed 2026-08-17: johnvansickle.com went fully unreachable
  # (TLS handshake failures, then connection timeouts) for an extended
  # period, independent of anything on our end, and took out every job in
  # flight -- exactly the single-point-of-failure this mirror removes.
  # Explicit timeouts on the fallback curl so a dead host fails in seconds,
  # not the ~5 minutes of OS-default timeout that let 3 retries burn ~15
  # minutes per instance during that outage.
  [[ -z "$FFMPEG_MIRROR_KEY" ]] && return 1
  if aws s3 cp "s3://${ARTIFACTS_BUCKET}/${FFMPEG_MIRROR_KEY}" "$FFMPEG_TARBALL" --region "$REGION" >/dev/null 2>&1; then
    return 0
  fi
  curl -sSL --connect-timeout 10 --max-time 120 -o "$FFMPEG_TARBALL" "$FFMPEG_UPSTREAM_URL"
}

for attempt in 1 2 3; do
  if fetch_ffmpeg \
      && mkdir -p /tmp/ffmpeg-extract \
      && tar -xJf "$FFMPEG_TARBALL" -C /tmp/ffmpeg-extract --strip-components=1 \
      && cp /tmp/ffmpeg-extract/ffmpeg /tmp/ffmpeg-extract/ffprobe /usr/local/bin/ \
      && chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe; then
    FFMPEG_OK=1
    rm -rf "$FFMPEG_TARBALL" /tmp/ffmpeg-extract
    break
  fi
  echo "[$(now)] ffmpeg install attempt $attempt/3 failed, retrying in 5s..."
  rm -rf "$FFMPEG_TARBALL" /tmp/ffmpeg-extract
  sleep 5
done

if [[ "$FFMPEG_OK" != "1" ]]; then
  # Fail fast here instead of limping through venv creation, S3 app sync,
  # pip install, and a Secrets Manager call on a job that's already doomed
  # -- ensure_tools() in sermon_pipeline.py would just crash on this same
  # missing binary a bit later anyway, with a less specific error and a
  # wasted ~40s of billed instance time. Uses the AWS CLI directly (venv +
  # storage.py aren't synced yet at this point) to record the failure the
  # same way StateStore.mark_failed() would, so the poller's retry/reclaim
  # logic and the Notifier Lambda's failure email both still see it.
  echo "[$(now)] ERROR: ffmpeg install failed after 3 attempts -- aborting"
  aws dynamodb update-item \
    --region "$REGION" \
    --table-name "${SERMON_JOBS_TABLE:-EchoPulpitJobs}" \
    --key "{\"video_id\": {\"S\": \"$VIDEO_ID\"}}" \
    --update-expression "SET #status = :failed, #error = :err, completed_at = :ts ADD failure_count :one" \
    --expression-attribute-names '{"#status":"status","#error":"error"}' \
    --expression-attribute-values "{\":failed\":{\"S\":\"FAILED\"},\":err\":{\"S\":\"bootstrap.sh: ffmpeg install failed after 3 attempts\"},\":ts\":{\"S\":\"$(now)\"},\":one\":{\"N\":\"1\"}}" \
    >/dev/null 2>&1 || echo "[$(now)] WARNING: failed to record ffmpeg failure in DynamoDB"
  exit 1
fi

echo "[$(now)] Creating Python virtualenv"
# AL2023's system `aws` CLI is itself a Python 3.9 tool that depends on
# system site-packages (pinned to distro<1.9.0). Installing our deps
# globally overwrites that pin (something in requirements-worker.txt pulls
# in distro>=1.9.0), which silently breaks every subsequent `aws` call in
# this script (Secrets Manager, S3 log upload, tag re-reads, terminate) --
# with no error surfaced until something downstream inexplicably fails. A
# venv keeps our deps fully isolated from the system aws CLI's own
# environment. Also sidesteps the get-pip.py 3.10+ version requirement
# entirely -- venv bundles its own pip via ensurepip.
#
# Lives OUTSIDE $APP_DIR deliberately: the app-code sync below does
# `s3 sync --delete` into $APP_DIR, which would wipe out a venv created
# inside it (learned the hard way -- it silently deleted the venv it had
# just created, since venv/ isn't part of the S3 app bundle).
VENV_DIR="/opt/echopulpit-venv"
python3 -m venv "$VENV_DIR"
PYTHON="${VENV_DIR}/bin/python3"

# ---- Fetch app code + config + style guide from S3 ----
echo "[$(now)] Syncing app code from s3://${ARTIFACTS_BUCKET}/${APP_PREFIX}/"
aws s3 sync "s3://${ARTIFACTS_BUCKET}/${APP_PREFIX}/" "$APP_DIR/" --delete

echo "[$(now)] Installing Python deps into venv"
"$PYTHON" -m pip install --quiet -r "${APP_DIR}/requirements-worker.txt"

# ---- Fetch the Anthropic API key from Secrets Manager. Never logged. ----
ANTHROPIC_API_KEY="$(aws secretsmanager get-secret-value \
  --secret-id "$ANTHROPIC_SECRET_ID" --region "$REGION" \
  --query SecretString --output text 2>/dev/null)"
if [[ -z "$ANTHROPIC_API_KEY" || "$ANTHROPIC_API_KEY" == "None" ]]; then
  echo "[$(now)] ERROR: could not read secret $ANTHROPIC_SECRET_ID -- aborting"
  exit 1
fi
export ANTHROPIC_API_KEY

# ---- Fetch yt-dlp cookies (Netscape format) from Secrets Manager, if the
# secret exists. Optional, not required: YouTube increasingly blocks
# requests from cloud/datacenter IPs without them, but a missing secret
# shouldn't hard-fail every job -- it just degrades back to that failure
# mode until the secret is populated. Never logged.
YTDLP_COOKIES_SECRET_ID="${YTDLP_COOKIES_SECRET_ID:-echopulpit/ytdlp-cookies}"
YTDLP_COOKIES_FILE="${APP_DIR}/cookies.txt"
if aws secretsmanager get-secret-value --secret-id "$YTDLP_COOKIES_SECRET_ID" --region "$REGION" \
    --query SecretString --output text >"$YTDLP_COOKIES_FILE" 2>/dev/null \
    && [[ -s "$YTDLP_COOKIES_FILE" ]]; then
  echo "[$(now)] Loaded yt-dlp cookies from Secrets Manager"
  export YTDLP_COOKIES_FILE
else
  echo "[$(now)] No yt-dlp cookies secret found ($YTDLP_COOKIES_SECRET_ID) -- proceeding without cookies"
  rm -f "$YTDLP_COOKIES_FILE"
fi

JOB_DIR="${OUTPUT_ROOT}/${VIDEO_ID}"
S3_PREFIX="s3://${ARTIFACTS_BUCKET}/sermons/${VIDEO_ID}/"

cd "$APP_DIR"
export CONFIG_PATH="${APP_DIR}/config.yaml"
export VIDEO_ID VIDEO_TITLE VIDEO_DURATION_SECONDS VIDEO_END_TIME
export SERMON_JOBS_TABLE="${SERMON_JOBS_TABLE:-EchoPulpitJobs}"
# yt-dlp (installed into the venv via requirements-worker.txt) is invoked
# as a subprocess by sermon_pipeline.py, which looks for it via PATH --
# calling "$PYTHON" directly doesn't put the venv's bin/ on PATH the way
# activating it would.
export PATH="${VENV_DIR}/bin:$PATH"

"$PYTHON" sermon_pipeline.py
PIPELINE_EXIT_CODE=$?

if [[ -d "$JOB_DIR" ]]; then
  echo "[$(now)] Uploading artifacts to $S3_PREFIX (excluding raw media)"
  if aws s3 sync "$JOB_DIR" "$S3_PREFIX" --exclude "media/*"; then
    UPLOAD_OK=1
  else
    echo "[$(now)] WARNING: S3 upload failed"
    UPLOAD_OK=0
  fi
else
  echo "[$(now)] WARNING: job dir $JOB_DIR does not exist, nothing to upload"
  UPLOAD_OK=0
fi

# sermon_pipeline.py already calls StateStore.mark_processed() itself on
# success (status=COMPLETE) -- we only need to record the S3 location here,
# and to record failure here since the Python process can't do that for its
# own crash.
"$PYTHON" - "$VIDEO_ID" "$PIPELINE_EXIT_CODE" "$UPLOAD_OK" "$S3_PREFIX" <<'PYEOF'
import sys
sys.path.insert(0, "/opt/echopulpit")
from storage import StateStore

video_id, exit_code, upload_ok, s3_prefix = sys.argv[1:5]
store = StateStore()

if exit_code != "0":
    store.mark_failed(video_id, f"bootstrap.sh: sermon_pipeline.py exited with code {exit_code}")
elif upload_ok == "1":
    store.set_s3_prefix(video_id, s3_prefix)
PYEOF

echo "[$(now)] bootstrap.sh finished (pipeline_exit_code=$PIPELINE_EXIT_CODE)"
# terminate_self runs automatically via the EXIT trap.
exit "$PIPELINE_EXIT_CODE"
