#!/usr/bin/env bash
#
# Runs on boot via systemd (echopulpit-worker.service, see deploy/systemd/) on
# the custom AMI. One instance = one job: read which video to process from
# our own instance tag, run the pipeline, upload artifacts to S3, record the
# result in DynamoDB, then terminate the instance no matter what happens.
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
# Must match config.yaml's output.dir baked into the AMI.
OUTPUT_ROOT="/data/output"
S3_BUCKET="${SERMON_ARTIFACTS_BUCKET:-echopulpit-artifacts}"
WATCHDOG_HOURS="${SERMON_WATCHDOG_HOURS:-3}"

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

VIDEO_ID="$(tag SermonVideoId)"
VIDEO_TITLE="$(tag SermonVideoTitle)"
VIDEO_DURATION_SECONDS="$(tag SermonVideoDurationSeconds)"
VIDEO_END_TIME="$(tag SermonVideoEndTime)"

if [[ -z "$VIDEO_ID" || "$VIDEO_ID" == "None" ]]; then
  echo "[$(now)] ERROR: no SermonVideoId tag found on this instance -- aborting"
  exit 1
fi

echo "[$(now)] Processing video_id=$VIDEO_ID title=${VIDEO_TITLE:-<unknown>}"

JOB_DIR="${OUTPUT_ROOT}/${VIDEO_ID}"
S3_PREFIX="s3://${S3_BUCKET}/sermons/${VIDEO_ID}/"

cd "$APP_DIR"
export CONFIG_PATH="${APP_DIR}/config.yaml"
export VIDEO_ID VIDEO_TITLE VIDEO_DURATION_SECONDS VIDEO_END_TIME
export SERMON_JOBS_TABLE="${SERMON_JOBS_TABLE:-EchoPulpitJobs}"

# This AMI is built on a GPU instance type (g4dn.xlarge / T4) specifically so
# these get used -- without them, both Whisper and the LLM silently fall
# back to CPU and the GPU spend buys nothing.
export WHISPER_DEVICE="${WHISPER_DEVICE:-cuda}"
export WHISPER_COMPUTE_TYPE="${WHISPER_COMPUTE_TYPE:-float16}"
export LLM_N_GPU_LAYERS="${LLM_N_GPU_LAYERS:-35}"  # full offload for a 7B Q5 model on a 16GB T4

python3.11 sermon_pipeline.py
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
python3.11 - "$VIDEO_ID" "$PIPELINE_EXIT_CODE" "$UPLOAD_OK" "$S3_PREFIX" <<'PYEOF'
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
