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
WORKER_INSTANCE_TYPE="${WORKER_INSTANCE_TYPE:-c6i.xlarge}"
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
TABLE_STREAM_ARN="$(aws dynamodb describe-table --table-name "$TABLE_NAME" --region "$REGION" \
  --query 'Table.LatestStreamArn' --output text)"

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

ANTHROPIC_SECRET_NAME="echopulpit/anthropic-api-key"
if ! aws secretsmanager describe-secret --secret-id "$ANTHROPIC_SECRET_NAME" --region "$REGION" >/dev/null 2>&1; then
  echo "Creating Secrets Manager secret $ANTHROPIC_SECRET_NAME"
  aws secretsmanager create-secret --name "$ANTHROPIC_SECRET_NAME" --region "$REGION" \
    --secret-string "$ANTHROPIC_API_KEY" >/dev/null
else
  echo "Secret $ANTHROPIC_SECRET_NAME already exists, skipping create (not overwriting its value)"
fi

# ==========================================================================
# 3b) Resolve the latest stock Amazon Linux 2023 AMI ID via the public SSM
#     parameter (no custom AMI to build/maintain for v1).
# ==========================================================================
AMI_ID="$(aws ssm get-parameters --region "$REGION" \
  --names /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --query 'Parameters[0].Value' --output text)"
echo "Using stock AMI: $AMI_ID"

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

# ==========================================================================
# 10) Wire the DynamoDB Stream -> notifier Lambda
# ==========================================================================
NOTIFIER_FN_ARN="$(aws lambda get-function --function-name "$NOTIFIER_FN_NAME" --region "$REGION" --query 'Configuration.FunctionArn' --output text)"
if ! aws lambda list-event-source-mappings --function-name "$NOTIFIER_FN_NAME" --region "$REGION" \
    --query "EventSourceMappings[?EventSourceArn=='${TABLE_STREAM_ARN}']" --output text | grep -q .; then
  aws lambda create-event-source-mapping --function-name "$NOTIFIER_FN_ARN" --region "$REGION" \
    --event-source-arn "$TABLE_STREAM_ARN" --starting-position LATEST --batch-size 5 >/dev/null
fi

echo ""
echo "== Done =="
echo "Worker instance profile: $WORKER_INSTANCE_PROFILE_ARN"
echo "Poller Lambda:           $POLLER_FN_NAME (rate: 15 min)"
echo "Notifier Lambda:         $NOTIFIER_FN_NAME (DynamoDB Streams trigger)"
echo ""
echo "Remaining manual steps:"
echo "  1. Verify the SES sender identity: aws ses verify-email-identity --email-address $SES_SENDER_ADDRESS --region $REGION"
echo "  2. If your SES account is still in the sandbox, also verify the recipient: $NOTIFY_RECIPIENT_ADDRESS"
echo "  3. Sync app code to s3://$ARTIFACTS_BUCKET/app/ (sermon_pipeline.py, prompts.py, sermon_heuristics.py,"
echo "     render_pdf.py, storage.py, scripture_lookup.py, requirements-worker.txt, config.yaml, prompts/style_guide.md)"
echo "  4. Seed a test job to confirm the end-to-end path (see README.md 'Verification')"
