#!/usr/bin/env bash
#
# One-shot provisioning script for EchoPulpit's ephemeral-spot pipeline.
# Requires: aws-cli v2, configured credentials, python3 + pip (for packaging
# the poller Lambda's dependencies), and a bash-like shell.
#
# This is meant to be read and adjusted, not blindly run -- review the
# variables below (and the IAM policy JSON files it substitutes into)
# before executing anything against a real AWS account.
#
# v1 uses a STOCK Amazon Linux 2023 AMI (resolved dynamically below via the
# public SSM parameter), not a Packer-baked custom AMI -- see bootstrap.sh
# for why (Claude-primary means no local model to bake in, so boot-time
# install is cheap enough that maintaining a custom AMI isn't worth it).
# bootstrap.sh is shipped as this instance's EC2 user-data by poller_lambda.py.
set -euo pipefail

# ---- Required configuration (edit these or export as env vars first) ----
: "${AWS_REGION:?set AWS_REGION, e.g. us-east-1}"
: "${CHANNEL_ID:?set CHANNEL_ID (the YouTube channel ID, not @handle)}"
: "${YOUTUBE_API_KEY:?set YOUTUBE_API_KEY (used once, to seed Secrets Manager)}"
: "${ANTHROPIC_API_KEY:?set ANTHROPIC_API_KEY (used once, to seed Secrets Manager)}"
: "${ARTIFACTS_BUCKET:?set ARTIFACTS_BUCKET (S3 bucket name, must be globally unique)}"
: "${SUBNET_ID:?set SUBNET_ID (worker instances launch here; needs internet access)}"
: "${SECURITY_GROUP_ID:?set SECURITY_GROUP_ID for worker instances}"
: "${SES_SENDER_ADDRESS:?set SES_SENDER_ADDRESS (must be a verified SES identity)}"
: "${NOTIFY_RECIPIENT_ADDRESS:?set NOTIFY_RECIPIENT_ADDRESS, e.g. josh@missionlaunch.us}"

TABLE_NAME="${TABLE_NAME:-EchoPulpitJobs}"
WORKER_INSTANCE_TYPE="${WORKER_INSTANCE_TYPE:-m7g.xlarge}"
WORKER_ROLE_NAME="${WORKER_ROLE_NAME:-echopulpit-worker-instance-role}"
POLLER_ROLE_NAME="${POLLER_ROLE_NAME:-echopulpit-poller-lambda-role}"
NOTIFIER_ROLE_NAME="${NOTIFIER_ROLE_NAME:-echopulpit-notifier-lambda-role}"
POLLER_FN_NAME="${POLLER_FN_NAME:-echopulpit-poller}"
NOTIFIER_FN_NAME="${NOTIFIER_FN_NAME:-echopulpit-notifier}"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REGION="$AWS_REGION"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IAM_DIR="${SCRIPT_DIR}/iam"
LAMBDA_DIR="${SCRIPT_DIR}/lambdas"
BUILD_DIR="${SCRIPT_DIR}/.build"
mkdir -p "$BUILD_DIR"

echo "== Account: $ACCOUNT_ID  Region: $REGION =="

# ---- Small helper: substitute ${VAR} placeholders in a template file with
# the values of the matching shell variables (portable, no envsubst dep). ----
render_template() {
  local template="$1"
  local out="$2"
  shift 2
  local content
  content="$(cat "$template")"
  for pair in "$@"; do
    local key="${pair%%=*}"
    local val="${pair#*=}"
    content="${content//\$\{${key}\}/${val}}"
  done
  printf '%s' "$content" > "$out"
}

# ==========================================================================
# 1) S3 bucket for artifacts
# ==========================================================================
if ! aws s3api head-bucket --bucket "$ARTIFACTS_BUCKET" 2>/dev/null; then
  echo "Creating S3 bucket $ARTIFACTS_BUCKET"
  if [[ "$REGION" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "$ARTIFACTS_BUCKET" --region "$REGION"
  else
    aws s3api create-bucket --bucket "$ARTIFACTS_BUCKET" --region "$REGION" \
      --create-bucket-configuration LocationConstraint="$REGION"
  fi
  aws s3api put-bucket-encryption --bucket "$ARTIFACTS_BUCKET" \
    --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
else
  echo "S3 bucket $ARTIFACTS_BUCKET already exists, skipping"
fi
# Idempotent regardless of create-vs-existing, so cost allocation can find
# this resource once the Project tag is activated (see README).
aws s3api put-bucket-tagging --bucket "$ARTIFACTS_BUCKET" --region "$REGION" \
  --tagging 'TagSet=[{Key=Project,Value=echopulpit}]'

# ==========================================================================
# 2) DynamoDB table (Streams enabled -- the notifier Lambda subscribes to it)
# ==========================================================================
if ! aws dynamodb describe-table --table-name "$TABLE_NAME" --region "$REGION" >/dev/null 2>&1; then
  echo "Creating DynamoDB table $TABLE_NAME"
  aws dynamodb create-table \
    --table-name "$TABLE_NAME" \
    --region "$REGION" \
    --attribute-definitions AttributeName=video_id,AttributeType=S \
    --key-schema AttributeName=video_id,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --stream-specification StreamEnabled=true,StreamViewType=NEW_AND_OLD_IMAGES
  aws dynamodb wait table-exists --table-name "$TABLE_NAME" --region "$REGION"
else
  echo "DynamoDB table $TABLE_NAME already exists, skipping"
fi
TABLE_ARN="$(aws dynamodb describe-table --table-name "$TABLE_NAME" --region "$REGION" \
  --query 'Table.TableArn' --output text)"
TABLE_STREAM_ARN="$(aws dynamodb describe-table --table-name "$TABLE_NAME" --region "$REGION" \
  --query 'Table.LatestStreamArn' --output text)"
aws dynamodb tag-resource --resource-arn "$TABLE_ARN" --region "$REGION" \
  --tags Key=Project,Value=echopulpit

# ==========================================================================
# 3) Secrets Manager: YouTube API key (worker instances never see this --
#    only the poller Lambda reads it) + Anthropic API key (only the worker
#    reads this, at boot, in bootstrap.sh)
# ==========================================================================
SECRET_NAME="echopulpit/youtube-api-key"
if ! aws secretsmanager describe-secret --secret-id "$SECRET_NAME" --region "$REGION" >/dev/null 2>&1; then
  echo "Creating Secrets Manager secret $SECRET_NAME"
  YOUTUBE_API_KEY_SECRET_ARN="$(aws secretsmanager create-secret \
    --name "$SECRET_NAME" --region "$REGION" \
    --secret-string "{\"YOUTUBE_API_KEY\":\"${YOUTUBE_API_KEY}\"}" \
    --query ARN --output text)"
else
  echo "Secret $SECRET_NAME already exists, skipping create (not overwriting its value)"
  YOUTUBE_API_KEY_SECRET_ARN="$(aws secretsmanager describe-secret --secret-id "$SECRET_NAME" --region "$REGION" --query ARN --output text)"
fi
aws secretsmanager tag-resource --secret-id "$SECRET_NAME" --region "$REGION" \
  --tags Key=Project,Value=echopulpit

ANTHROPIC_SECRET_NAME="echopulpit/anthropic-api-key"
if ! aws secretsmanager describe-secret --secret-id "$ANTHROPIC_SECRET_NAME" --region "$REGION" >/dev/null 2>&1; then
  echo "Creating Secrets Manager secret $ANTHROPIC_SECRET_NAME"
  aws secretsmanager create-secret --name "$ANTHROPIC_SECRET_NAME" --region "$REGION" \
    --secret-string "$ANTHROPIC_API_KEY" >/dev/null
else
  echo "Secret $ANTHROPIC_SECRET_NAME already exists, skipping create (not overwriting its value)"
fi
aws secretsmanager tag-resource --secret-id "$ANTHROPIC_SECRET_NAME" --region "$REGION" \
  --tags Key=Project,Value=echopulpit

# ==========================================================================
# 3b) Resolve the latest stock Amazon Linux 2023 AMI ID via the public SSM
#     parameter (no custom AMI to build/maintain for v1). arm64/Graviton by
#     default (m7g.xlarge below) -- ~15% cheaper on-demand than the
#     equivalent Intel instance for the same vCPU/memory, and the only
#     architecture-sensitive dependency (ffmpeg) has its own per-arch S3
#     mirror (see bootstrap.sh's ARCH detection). Set WORKER_ARCH=x86_64 to
#     go back to Intel if Graviton spot/on-demand capacity is ever tight in
#     your region/AZ.
# ==========================================================================
WORKER_ARCH="${WORKER_ARCH:-arm64}"
AMI_ID="$(aws ssm get-parameters --region "$REGION" \
  --names "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-${WORKER_ARCH}" \
  --query 'Parameters[0].Value' --output text)"
echo "Using stock AMI ($WORKER_ARCH): $AMI_ID"

# ==========================================================================
# 4) IAM: worker instance role + instance profile
# ==========================================================================
if ! aws iam get-role --role-name "$WORKER_ROLE_NAME" >/dev/null 2>&1; then
  aws iam create-role --role-name "$WORKER_ROLE_NAME" \
    --assume-role-policy-document "file://${IAM_DIR}/worker-instance-trust.json"
fi
render_template "${IAM_DIR}/worker-instance-policy.json" "${BUILD_DIR}/worker-instance-policy.json" \
  "REGION=${REGION}" "ACCOUNT_ID=${ACCOUNT_ID}" "TABLE_NAME=${TABLE_NAME}" "ARTIFACTS_BUCKET=${ARTIFACTS_BUCKET}"
aws iam put-role-policy --role-name "$WORKER_ROLE_NAME" --policy-name worker-inline \
  --policy-document "file://${BUILD_DIR}/worker-instance-policy.json"

if ! aws iam get-instance-profile --instance-profile-name "$WORKER_ROLE_NAME" >/dev/null 2>&1; then
  aws iam create-instance-profile --instance-profile-name "$WORKER_ROLE_NAME"
  aws iam add-role-to-instance-profile --instance-profile-name "$WORKER_ROLE_NAME" --role-name "$WORKER_ROLE_NAME"
  echo "Waiting for instance profile propagation..."
  sleep 10
fi
WORKER_INSTANCE_PROFILE_ARN="$(aws iam get-instance-profile --instance-profile-name "$WORKER_ROLE_NAME" \
  --query 'InstanceProfile.Arn' --output text)"

# ==========================================================================
# 5) IAM: poller Lambda role
# ==========================================================================
if ! aws iam get-role --role-name "$POLLER_ROLE_NAME" >/dev/null 2>&1; then
  aws iam create-role --role-name "$POLLER_ROLE_NAME" \
    --assume-role-policy-document "file://${IAM_DIR}/poller-lambda-trust.json"
fi
render_template "${IAM_DIR}/poller-lambda-policy.json" "${BUILD_DIR}/poller-lambda-policy.json" \
  "REGION=${REGION}" "ACCOUNT_ID=${ACCOUNT_ID}" "TABLE_NAME=${TABLE_NAME}" \
  "SUBNET_ID=${SUBNET_ID}" "SECURITY_GROUP_ID=${SECURITY_GROUP_ID}" "AMI_ID=${AMI_ID}" \
  "WORKER_ROLE_NAME=${WORKER_ROLE_NAME}" "YOUTUBE_API_KEY_SECRET_ARN=${YOUTUBE_API_KEY_SECRET_ARN}"
aws iam put-role-policy --role-name "$POLLER_ROLE_NAME" --policy-name poller-inline \
  --policy-document "file://${BUILD_DIR}/poller-lambda-policy.json"
POLLER_ROLE_ARN="$(aws iam get-role --role-name "$POLLER_ROLE_NAME" --query 'Role.Arn' --output text)"

# ==========================================================================
# 6) IAM: notifier Lambda role
# ==========================================================================
if ! aws iam get-role --role-name "$NOTIFIER_ROLE_NAME" >/dev/null 2>&1; then
  aws iam create-role --role-name "$NOTIFIER_ROLE_NAME" \
    --assume-role-policy-document "file://${IAM_DIR}/notifier-lambda-trust.json"
fi
SES_SENDER_IDENTITY_ARN="arn:aws:ses:${REGION}:${ACCOUNT_ID}:identity/${SES_SENDER_ADDRESS}"
render_template "${IAM_DIR}/notifier-lambda-policy.json" "${BUILD_DIR}/notifier-lambda-policy.json" \
  "REGION=${REGION}" "ACCOUNT_ID=${ACCOUNT_ID}" "TABLE_NAME=${TABLE_NAME}" \
  "ARTIFACTS_BUCKET=${ARTIFACTS_BUCKET}" "SES_SENDER_IDENTITY_ARN=${SES_SENDER_IDENTITY_ARN}"
aws iam put-role-policy --role-name "$NOTIFIER_ROLE_NAME" --policy-name notifier-inline \
  --policy-document "file://${BUILD_DIR}/notifier-lambda-policy.json"
NOTIFIER_ROLE_ARN="$(aws iam get-role --role-name "$NOTIFIER_ROLE_NAME" --query 'Role.Arn' --output text)"

echo "Waiting for IAM role propagation..."
sleep 10

# ==========================================================================
# 7) Package + deploy the poller Lambda (needs storage.py + google-api-
#    python-client, which isn't in the default Lambda runtime)
# ==========================================================================
POLLER_BUILD="${BUILD_DIR}/poller"
rm -rf "$POLLER_BUILD" && mkdir -p "$POLLER_BUILD"
cp "${LAMBDA_DIR}/poller_lambda.py" "$POLLER_BUILD/"
cp "${SCRIPT_DIR}/../storage.py" "$POLLER_BUILD/"
cp "${SCRIPT_DIR}/bootstrap.sh" "$POLLER_BUILD/"
python3 -m pip install --target "$POLLER_BUILD" \
  google-api-python-client google-auth google-auth-httplib2 httplib2 boto3 --quiet
(cd "$POLLER_BUILD" && zip -qr "${BUILD_DIR}/poller-lambda.zip" .)

POLLER_ENV="Variables={CHANNEL_ID=${CHANNEL_ID},YOUTUBE_API_KEY_SECRET_ARN=${YOUTUBE_API_KEY_SECRET_ARN},WORKER_AMI_ID=${AMI_ID},WORKER_INSTANCE_TYPE=${WORKER_INSTANCE_TYPE},WORKER_SUBNET_ID=${SUBNET_ID},WORKER_SECURITY_GROUP_ID=${SECURITY_GROUP_ID},WORKER_INSTANCE_PROFILE_ARN=${WORKER_INSTANCE_PROFILE_ARN},SERMON_JOBS_TABLE=${TABLE_NAME}}"

if aws lambda get-function --function-name "$POLLER_FN_NAME" --region "$REGION" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$POLLER_FN_NAME" --region "$REGION" \
    --zip-file "fileb://${BUILD_DIR}/poller-lambda.zip" >/dev/null
  aws lambda update-function-configuration --function-name "$POLLER_FN_NAME" --region "$REGION" \
    --environment "$POLLER_ENV" --timeout 60 --memory-size 256 >/dev/null
else
  aws lambda create-function --function-name "$POLLER_FN_NAME" --region "$REGION" \
    --runtime python3.11 --handler poller_lambda.lambda_handler \
    --role "$POLLER_ROLE_ARN" --timeout 60 --memory-size 256 \
    --zip-file "fileb://${BUILD_DIR}/poller-lambda.zip" \
    --environment "$POLLER_ENV" >/dev/null
fi
aws lambda tag-resource --resource "arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${POLLER_FN_NAME}" \
  --tags Project=echopulpit --region "$REGION"

# ==========================================================================
# 8) EventBridge schedule -> poller Lambda
# ==========================================================================
aws events put-rule --name echopulpit-poller-schedule --region "$REGION" \
  --schedule-expression "rate(15 minutes)" --state ENABLED >/dev/null
POLLER_FN_ARN="$(aws lambda get-function --function-name "$POLLER_FN_NAME" --region "$REGION" --query 'Configuration.FunctionArn' --output text)"
aws events put-targets --rule echopulpit-poller-schedule --region "$REGION" \
  --targets "Id=poller,Arn=${POLLER_FN_ARN}" >/dev/null
aws lambda add-permission --function-name "$POLLER_FN_NAME" --region "$REGION" \
  --statement-id echopulpit-poller-eventbridge --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn "arn:aws:events:${REGION}:${ACCOUNT_ID}:rule/echopulpit-poller-schedule" \
  >/dev/null 2>&1 || echo "(permission already granted, skipping)"

# ==========================================================================
# 9) Package + deploy the notifier Lambda (stdlib + boto3 only, no extra deps)
# ==========================================================================
(cd "$LAMBDA_DIR" && zip -qj "${BUILD_DIR}/notifier-lambda.zip" notifier_lambda.py)

NOTIFIER_ENV="Variables={SES_SENDER_ADDRESS=${SES_SENDER_ADDRESS},NOTIFY_RECIPIENT_ADDRESS=${NOTIFY_RECIPIENT_ADDRESS},SERMON_ARTIFACTS_BUCKET=${ARTIFACTS_BUCKET}}"

if aws lambda get-function --function-name "$NOTIFIER_FN_NAME" --region "$REGION" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$NOTIFIER_FN_NAME" --region "$REGION" \
    --zip-file "fileb://${BUILD_DIR}/notifier-lambda.zip" >/dev/null
  aws lambda update-function-configuration --function-name "$NOTIFIER_FN_NAME" --region "$REGION" \
    --environment "$NOTIFIER_ENV" --timeout 30 --memory-size 256 >/dev/null
else
  aws lambda create-function --function-name "$NOTIFIER_FN_NAME" --region "$REGION" \
    --runtime python3.11 --handler notifier_lambda.lambda_handler \
    --role "$NOTIFIER_ROLE_ARN" --timeout 30 --memory-size 256 \
    --zip-file "fileb://${BUILD_DIR}/notifier-lambda.zip" \
    --environment "$NOTIFIER_ENV" >/dev/null
fi
aws lambda tag-resource --resource "arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${NOTIFIER_FN_NAME}" \
  --tags Project=echopulpit --region "$REGION"

# ==========================================================================
# 10) Wire the DynamoDB Stream -> notifier Lambda
# ==========================================================================
NOTIFIER_FN_ARN="$(aws lambda get-function --function-name "$NOTIFIER_FN_NAME" --region "$REGION" --query 'Configuration.FunctionArn' --output text)"
if ! aws lambda list-event-source-mappings --function-name "$NOTIFIER_FN_NAME" --region "$REGION" \
    --query "EventSourceMappings[?EventSourceArn=='${TABLE_STREAM_ARN}']" --output text | grep -q .; then
  aws lambda create-event-source-mapping --function-name "$NOTIFIER_FN_ARN" --region "$REGION" \
    --event-source-arn "$TABLE_STREAM_ARN" --starting-position LATEST --batch-size 5 >/dev/null
fi

# ==========================================================================
# 11) IAM + deploy: ffmpeg mirror refresh Lambda (periodic). Keeps
#    s3://$ARTIFACTS_BUCKET/deps/ffmpeg-static-linux-amd64.tar.xz -- what
#    bootstrap.sh's fetch_ffmpeg() tries before falling back to
#    johnvansickle.com -- from silently going stale forever. Seed the
#    mirror once by hand before first use (see README); this Lambda just
#    keeps it current on a schedule.
# ==========================================================================
FFMPEG_MIRROR_ROLE_NAME="${FFMPEG_MIRROR_ROLE_NAME:-echopulpit-ffmpeg-mirror-lambda-role}"
FFMPEG_MIRROR_FN_NAME="${FFMPEG_MIRROR_FN_NAME:-echopulpit-ffmpeg-mirror-refresh}"

if ! aws iam get-role --role-name "$FFMPEG_MIRROR_ROLE_NAME" >/dev/null 2>&1; then
  aws iam create-role --role-name "$FFMPEG_MIRROR_ROLE_NAME" \
    --assume-role-policy-document "file://${IAM_DIR}/ffmpeg-mirror-lambda-trust.json"
  sleep 10  # IAM role propagation
fi
render_template "${IAM_DIR}/ffmpeg-mirror-lambda-policy.json" "${BUILD_DIR}/ffmpeg-mirror-lambda-policy.json" \
  "REGION=${REGION}" "ACCOUNT_ID=${ACCOUNT_ID}" "ARTIFACTS_BUCKET=${ARTIFACTS_BUCKET}"
aws iam put-role-policy --role-name "$FFMPEG_MIRROR_ROLE_NAME" --policy-name ffmpeg-mirror-inline \
  --policy-document "file://${BUILD_DIR}/ffmpeg-mirror-lambda-policy.json"
FFMPEG_MIRROR_ROLE_ARN="$(aws iam get-role --role-name "$FFMPEG_MIRROR_ROLE_NAME" --query 'Role.Arn' --output text)"

(cd "$LAMBDA_DIR" && zip -qj "${BUILD_DIR}/ffmpeg-mirror-lambda.zip" ffmpeg_mirror_refresh_lambda.py)
FFMPEG_MIRROR_ENV="Variables={SERMON_ARTIFACTS_BUCKET=${ARTIFACTS_BUCKET}}"

if aws lambda get-function --function-name "$FFMPEG_MIRROR_FN_NAME" --region "$REGION" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$FFMPEG_MIRROR_FN_NAME" --region "$REGION" \
    --zip-file "fileb://${BUILD_DIR}/ffmpeg-mirror-lambda.zip" >/dev/null
  aws lambda update-function-configuration --function-name "$FFMPEG_MIRROR_FN_NAME" --region "$REGION" \
    --environment "$FFMPEG_MIRROR_ENV" --timeout 120 --memory-size 256 >/dev/null
else
  aws lambda create-function --function-name "$FFMPEG_MIRROR_FN_NAME" --region "$REGION" \
    --runtime python3.11 --handler ffmpeg_mirror_refresh_lambda.lambda_handler \
    --role "$FFMPEG_MIRROR_ROLE_ARN" --timeout 120 --memory-size 256 \
    --zip-file "fileb://${BUILD_DIR}/ffmpeg-mirror-lambda.zip" \
    --environment "$FFMPEG_MIRROR_ENV" >/dev/null
fi
aws lambda tag-resource --resource "arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${FFMPEG_MIRROR_FN_NAME}" \
  --tags Project=echopulpit --region "$REGION"

aws events put-rule --name echopulpit-ffmpeg-mirror-schedule --region "$REGION" \
  --schedule-expression "rate(7 days)" --state ENABLED >/dev/null
FFMPEG_MIRROR_FN_ARN="$(aws lambda get-function --function-name "$FFMPEG_MIRROR_FN_NAME" --region "$REGION" --query 'Configuration.FunctionArn' --output text)"
aws events put-targets --rule echopulpit-ffmpeg-mirror-schedule --region "$REGION" \
  --targets "Id=ffmpeg-mirror-refresh,Arn=${FFMPEG_MIRROR_FN_ARN}" >/dev/null
aws lambda add-permission --function-name "$FFMPEG_MIRROR_FN_NAME" --region "$REGION" \
  --statement-id echopulpit-ffmpeg-mirror-eventbridge --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn "arn:aws:events:${REGION}:${ACCOUNT_ID}:rule/echopulpit-ffmpeg-mirror-schedule" \
  >/dev/null 2>&1 || echo "(permission already granted, skipping)"

# ==========================================================================
# 12) IAM + deploy: monthly report Lambda. Emails a summary (AWS cost +
#    job outcomes) for the *previous* calendar month, on the 4th of each
#    month -- a few days after month-end to give Cost Explorer's usual
#    data-processing lag room to settle. Cost figures depend on the
#    Project=echopulpit cost-allocation tag being active (enable once,
#    manually -- see README) and every relevant resource carrying that tag.
# ==========================================================================
MONTHLY_REPORT_ROLE_NAME="${MONTHLY_REPORT_ROLE_NAME:-echopulpit-monthly-report-lambda-role}"
MONTHLY_REPORT_FN_NAME="${MONTHLY_REPORT_FN_NAME:-echopulpit-monthly-report}"

if ! aws iam get-role --role-name "$MONTHLY_REPORT_ROLE_NAME" >/dev/null 2>&1; then
  aws iam create-role --role-name "$MONTHLY_REPORT_ROLE_NAME" \
    --assume-role-policy-document "file://${IAM_DIR}/monthly-report-lambda-trust.json"
  sleep 10  # IAM role propagation
fi
render_template "${IAM_DIR}/monthly-report-lambda-policy.json" "${BUILD_DIR}/monthly-report-lambda-policy.json" \
  "REGION=${REGION}" "ACCOUNT_ID=${ACCOUNT_ID}" "TABLE_NAME=${TABLE_NAME}" \
  "SES_SENDER_IDENTITY_ARN=${SES_SENDER_IDENTITY_ARN}"
aws iam put-role-policy --role-name "$MONTHLY_REPORT_ROLE_NAME" --policy-name monthly-report-inline \
  --policy-document "file://${BUILD_DIR}/monthly-report-lambda-policy.json"
MONTHLY_REPORT_ROLE_ARN="$(aws iam get-role --role-name "$MONTHLY_REPORT_ROLE_NAME" --query 'Role.Arn' --output text)"

(cd "$LAMBDA_DIR" && zip -qj "${BUILD_DIR}/monthly-report-lambda.zip" monthly_report_lambda.py)
MONTHLY_REPORT_ENV="Variables={SES_SENDER_ADDRESS=${SES_SENDER_ADDRESS},NOTIFY_RECIPIENT_ADDRESS=${NOTIFY_RECIPIENT_ADDRESS},SERMON_JOBS_TABLE=${TABLE_NAME},COST_PROJECT_TAG=echopulpit}"

if aws lambda get-function --function-name "$MONTHLY_REPORT_FN_NAME" --region "$REGION" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$MONTHLY_REPORT_FN_NAME" --region "$REGION" \
    --zip-file "fileb://${BUILD_DIR}/monthly-report-lambda.zip" >/dev/null
  aws lambda update-function-configuration --function-name "$MONTHLY_REPORT_FN_NAME" --region "$REGION" \
    --environment "$MONTHLY_REPORT_ENV" --timeout 60 --memory-size 256 >/dev/null
else
  aws lambda create-function --function-name "$MONTHLY_REPORT_FN_NAME" --region "$REGION" \
    --runtime python3.11 --handler monthly_report_lambda.lambda_handler \
    --role "$MONTHLY_REPORT_ROLE_ARN" --timeout 60 --memory-size 256 \
    --zip-file "fileb://${BUILD_DIR}/monthly-report-lambda.zip" \
    --environment "$MONTHLY_REPORT_ENV" >/dev/null
fi
aws lambda tag-resource --resource "arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${MONTHLY_REPORT_FN_NAME}" \
  --tags Project=echopulpit --region "$REGION"

# cron(min hour day-of-month month day-of-week year) -- 08:00 UTC on the 4th
# of every month. day-of-week must be "?" when day-of-month is given.
aws events put-rule --name echopulpit-monthly-report-schedule --region "$REGION" \
  --schedule-expression "cron(0 8 4 * ? *)" --state ENABLED >/dev/null
MONTHLY_REPORT_FN_ARN="$(aws lambda get-function --function-name "$MONTHLY_REPORT_FN_NAME" --region "$REGION" --query 'Configuration.FunctionArn' --output text)"
aws events put-targets --rule echopulpit-monthly-report-schedule --region "$REGION" \
  --targets "Id=monthly-report,Arn=${MONTHLY_REPORT_FN_ARN}" >/dev/null
aws lambda add-permission --function-name "$MONTHLY_REPORT_FN_NAME" --region "$REGION" \
  --statement-id echopulpit-monthly-report-eventbridge --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn "arn:aws:events:${REGION}:${ACCOUNT_ID}:rule/echopulpit-monthly-report-schedule" \
  >/dev/null 2>&1 || echo "(permission already granted, skipping)"

# ==========================================================================
# 13) Activate the Project cost-allocation tag (one-time, account-level).
#    Without this, the monthly report Lambda's Cost Explorer query returns
#    $0 forever -- tags exist on every resource above, but AWS won't use a
#    tag for cost grouping until it's explicitly activated as a cost
#    allocation tag, and that activation only takes effect for spend going
#    forward (no retroactive backfill). `aws ce update-cost-allocation-tags-status`
#    needs a reasonably current AWS CLI v2 (older ones, e.g. 2.0.x, predate
#    this subcommand and will error below) -- if that happens, run the boto3
#    fallback printed in the error message, or activate it manually at
#    https://console.aws.amazon.com/costmanagement/home#/cost-allocation-tags
# ==========================================================================
if aws ce update-cost-allocation-tags-status \
    --cost-allocation-tags-status '[{"TagKey":"Project","Status":"Active"}]' >/dev/null 2>&1; then
  echo "Activated the 'Project' cost allocation tag"
else
  echo "WARNING: could not activate the 'Project' cost allocation tag automatically"
  echo "  (likely an AWS CLI version too old for 'aws ce update-cost-allocation-tags-status')."
  echo "  Fix with either:"
  echo "    pip install boto3 && python3 -c \"import boto3; boto3.client('ce').update_cost_allocation_tags_status(CostAllocationTagsStatus=[{'TagKey':'Project','Status':'Active'}])\""
  echo "  or activate 'Project' manually at:"
  echo "    https://console.aws.amazon.com/costmanagement/home#/cost-allocation-tags"
fi

echo ""
echo "== Done =="
echo "Worker instance profile: $WORKER_INSTANCE_PROFILE_ARN"
echo "Poller Lambda:           $POLLER_FN_NAME (rate: 15 min)"
echo "Notifier Lambda:         $NOTIFIER_FN_NAME (DynamoDB Streams trigger)"
echo "FFmpeg mirror refresh:   $FFMPEG_MIRROR_FN_NAME (rate: 7 days)"
echo "Monthly report:          $MONTHLY_REPORT_FN_NAME (8am UTC, 4th of each month)"
echo ""
echo "Remaining manual steps (see README.md 'Provision AWS resources' for exact commands):"
echo "  1. Verify the SES sender identity: aws ses verify-email-identity --email-address $SES_SENDER_ADDRESS --region $REGION"
echo "  2. If your SES account is still in the sandbox, also verify the recipient: $NOTIFY_RECIPIENT_ADDRESS"
echo "  3. Sync app code to s3://$ARTIFACTS_BUCKET/app/ (sermon_pipeline.py, prompts.py, sermon_heuristics.py,"
echo "     render_pdf.py, storage.py, scripture_lookup.py, requirements-worker.txt, config.yaml, prompts/style_guide.md)"
echo "  4. Seed s3://$ARTIFACTS_BUCKET/deps/ffmpeg-static-linux-${WORKER_ARCH}.tar.xz once by hand (a single top-level"
echo "     dir containing ffmpeg + ffprobe) -- the refresh Lambda keeps it current from there but doesn't create it"
echo "  5. If you use yt-dlp cookies (optional, see README), tag that secret too:"
echo "     aws secretsmanager tag-resource --secret-id echopulpit/ytdlp-cookies --tags Key=Project,Value=echopulpit --region $REGION"
echo "  6. Seed a test job to confirm the end-to-end path (see README.md 'Verification')"
