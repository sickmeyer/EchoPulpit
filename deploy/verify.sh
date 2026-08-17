#!/usr/bin/env bash
#
# Preflight + health check for EchoPulpit's AWS setup.
#
#   ./deploy/verify.sh --pre    run BEFORE deploy/setup.sh -- catches missing
#                                local tooling, bad credentials, and typo'd
#                                env vars before a multi-step AWS provisioning
#                                run fails halfway through.
#   ./deploy/verify.sh --post   run AFTER deploy/setup.sh (default if no flag
#                                given) -- confirms every resource setup.sh
#                                should have created actually exists and is
#                                healthy. Safe to re-run any time as a
#                                "why isn't this working" diagnostic.
#
# Never modifies anything -- every check here is read-only. Doesn't require
# `set -e`: a single failed check should not stop the rest from running, so
# results are collected and summarized at the end instead.
set -uo pipefail

MODE="post"
if [[ "${1:-}" == "--pre" ]]; then
  MODE="pre"
elif [[ "${1:-}" == "--post" ]]; then
  MODE="post"
elif [[ -n "${1:-}" ]]; then
  echo "Usage: $0 [--pre|--post]" >&2
  exit 2
fi

REGION="${AWS_REGION:-us-east-1}"
ARTIFACTS_BUCKET="${ARTIFACTS_BUCKET:-}"
TABLE_NAME="${TABLE_NAME:-EchoPulpitJobs}"
WORKER_ARCH="${WORKER_ARCH:-arm64}"

PASS=0
WARN=0
FAIL=0

pass() { echo "  [OK]   $1"; PASS=$((PASS + 1)); }
warn() { echo "  [WARN] $1"; WARN=$((WARN + 1)); }
fail() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }
section() { echo ""; echo "== $1 =="; }

# ==========================================================================
# --pre: local tooling + credentials + the env vars Step 1/2 of the README
# ask you to export, checked before you run setup.sh against a real account.
# ==========================================================================
run_preflight() {
  section "Local tooling"
  if command -v aws >/dev/null 2>&1; then
    ver="$(aws --version 2>&1)"
    pass "AWS CLI found ($ver)"
    if [[ "$ver" == *"aws-cli/1."* ]]; then
      warn "AWS CLI v1 detected -- this project assumes v2 syntax; upgrade recommended"
    fi
  else
    fail "AWS CLI not found -- install it: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
  fi
  for tool in python3 pip zip curl tar; do
    if command -v "$tool" >/dev/null 2>&1; then
      pass "$tool found"
    else
      fail "$tool not found -- required for packaging/deploying the Lambdas"
    fi
  done

  section "AWS credentials"
  if identity="$(aws sts get-caller-identity --output json 2>&1)"; then
    account="$(echo "$identity" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Account"])' 2>/dev/null || echo "?")"
    arn="$(echo "$identity" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Arn"])' 2>/dev/null || echo "?")"
    pass "Credentials valid (account $account, $arn)"
  else
    fail "AWS credentials not working -- run 'aws configure' or check your profile/env vars"
    echo "         $identity" | head -3
  fi

  section "Required environment variables"
  local required=(AWS_REGION CHANNEL_ID YOUTUBE_API_KEY ANTHROPIC_API_KEY \
    ARTIFACTS_BUCKET SUBNET_ID SECURITY_GROUP_ID SES_SENDER_ADDRESS NOTIFY_RECIPIENT_ADDRESS)
  for var in "${required[@]}"; do
    if [[ -n "${!var:-}" ]]; then
      case "$var" in
        YOUTUBE_API_KEY|ANTHROPIC_API_KEY) pass "$var is set (hidden)" ;;
        *) pass "$var=${!var}" ;;
      esac
    else
      fail "$var is not set -- see README 'Step 2 -- Provision everything in AWS'"
    fi
  done

  if [[ -n "${CHANNEL_ID:-}" ]]; then
    if [[ "$CHANNEL_ID" != UC* ]]; then
      warn "CHANNEL_ID '$CHANNEL_ID' doesn't start with 'UC' -- make sure this is the channel ID, not the @handle"
    fi
  fi

  section "Network (SUBNET_ID / SECURITY_GROUP_ID)"
  if [[ -n "${SUBNET_ID:-}" ]]; then
    if subnet_json="$(aws ec2 describe-subnets --subnet-ids "$SUBNET_ID" --region "$REGION" --output json 2>&1)"; then
      vpc="$(echo "$subnet_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Subnets"][0]["VpcId"])' 2>/dev/null || echo "?")"
      map_public="$(echo "$subnet_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Subnets"][0].get("MapPublicIpOnLaunch", False))' 2>/dev/null || echo "?")"
      pass "Subnet $SUBNET_ID exists (VPC $vpc)"
      if [[ "$map_public" == "False" ]]; then
        warn "Subnet does not auto-assign public IPs -- worker instances need a NAT gateway for internet access instead; confirm one exists in this VPC"
      fi
    else
      fail "Subnet $SUBNET_ID not found in region $REGION"
    fi
  else
    warn "SUBNET_ID not set, skipping"
  fi
  if [[ -n "${SECURITY_GROUP_ID:-}" ]]; then
    if aws ec2 describe-security-groups --group-ids "$SECURITY_GROUP_ID" --region "$REGION" >/dev/null 2>&1; then
      pass "Security group $SECURITY_GROUP_ID exists"
    else
      fail "Security group $SECURITY_GROUP_ID not found in region $REGION"
    fi
  else
    warn "SECURITY_GROUP_ID not set, skipping"
  fi

  section "Artifacts bucket name"
  if [[ -n "${ARTIFACTS_BUCKET:-}" ]]; then
    if aws s3api head-bucket --bucket "$ARTIFACTS_BUCKET" --region "$REGION" 2>/dev/null; then
      pass "s3://$ARTIFACTS_BUCKET already exists and you own it (setup.sh will reuse it)"
    else
      # head-bucket fails both for "doesn't exist" and "exists but isn't
      # yours" -- s3api list-buckets (account-scoped) disambiguates.
      if aws s3api list-buckets --query "Buckets[?Name=='$ARTIFACTS_BUCKET']" --output text 2>/dev/null | grep -q .; then
        warn "s3://$ARTIFACTS_BUCKET check inconclusive -- verify manually"
      else
        pass "s3://$ARTIFACTS_BUCKET name is available (or reachable) for setup.sh to create"
      fi
    fi
  fi
}

# ==========================================================================
# --post: does every resource setup.sh should have created actually exist?
# ==========================================================================
run_healthcheck() {
  if [[ -z "$ARTIFACTS_BUCKET" ]]; then
    echo "ARTIFACTS_BUCKET is not set -- export it (same value you gave setup.sh) and re-run." >&2
    exit 2
  fi

  section "IAM roles"
  for role in echopulpit-worker-instance-role echopulpit-poller-lambda-role \
    echopulpit-notifier-lambda-role echopulpit-ffmpeg-mirror-lambda-role \
    echopulpit-monthly-report-lambda-role; do
    if aws iam get-role --role-name "$role" >/dev/null 2>&1; then
      pass "Role $role exists"
    else
      fail "Role $role missing"
    fi
  done
  if aws iam get-instance-profile --instance-profile-name echopulpit-worker-instance-role >/dev/null 2>&1; then
    pass "Worker instance profile exists"
  else
    fail "Worker instance profile missing"
  fi

  section "DynamoDB"
  if table_json="$(aws dynamodb describe-table --table-name "$TABLE_NAME" --region "$REGION" --output json 2>&1)"; then
    pass "Table $TABLE_NAME exists"
    stream="$(echo "$table_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Table"].get("StreamSpecification",{}).get("StreamEnabled", False))' 2>/dev/null || echo "?")"
    [[ "$stream" == "True" ]] && pass "DynamoDB Streams enabled" || fail "DynamoDB Streams not enabled -- the notifier Lambda won't fire"
  else
    fail "Table $TABLE_NAME not found"
  fi

  section "S3"
  if aws s3api head-bucket --bucket "$ARTIFACTS_BUCKET" --region "$REGION" 2>/dev/null; then
    pass "Bucket $ARTIFACTS_BUCKET exists"
  else
    fail "Bucket $ARTIFACTS_BUCKET not found or not accessible"
  fi
  if aws s3api head-object --bucket "$ARTIFACTS_BUCKET" --key "app/sermon_pipeline.py" --region "$REGION" >/dev/null 2>&1; then
    pass "App code synced to s3://$ARTIFACTS_BUCKET/app/"
  else
    warn "App code not found at s3://$ARTIFACTS_BUCKET/app/sermon_pipeline.py -- sync it (see README Step 2)"
  fi
  if aws s3api head-object --bucket "$ARTIFACTS_BUCKET" --key "deps/ffmpeg-static-linux-${WORKER_ARCH}.tar.xz" --region "$REGION" >/dev/null 2>&1; then
    pass "ffmpeg mirror seeded for $WORKER_ARCH"
  else
    warn "ffmpeg mirror not seeded for $WORKER_ARCH yet -- not fatal (workers fall back to a direct download), but run deploy/seed-ffmpeg-mirror.sh for day-one reliability"
  fi

  section "Lambda functions"
  for fn in echopulpit-poller echopulpit-notifier echopulpit-ffmpeg-mirror-refresh echopulpit-monthly-report; do
    if state="$(aws lambda get-function-configuration --function-name "$fn" --region "$REGION" --query 'State' --output text 2>&1)"; then
      if [[ "$state" == "Active" ]]; then
        pass "$fn is Active"
      else
        warn "$fn exists but state is '$state' (expected Active)"
      fi
    else
      fail "$fn not found"
    fi
  done

  section "Schedules"
  for rule in echopulpit-poller-schedule echopulpit-ffmpeg-mirror-schedule echopulpit-monthly-report-schedule; do
    if state="$(aws events describe-rule --name "$rule" --region "$REGION" --query 'State' --output text 2>&1)"; then
      [[ "$state" == "ENABLED" ]] && pass "$rule is ENABLED" || warn "$rule exists but is $state"
    else
      fail "$rule not found"
    fi
  done
  if aws lambda list-event-source-mappings --function-name echopulpit-notifier --region "$REGION" \
      --query 'EventSourceMappings[0].State' --output text 2>/dev/null | grep -qi enabled; then
    pass "DynamoDB Stream -> notifier trigger is Enabled"
  else
    fail "DynamoDB Stream -> notifier trigger missing or not enabled"
  fi

  section "Secrets"
  if aws secretsmanager describe-secret --secret-id echopulpit/youtube-api-key --region "$REGION" >/dev/null 2>&1; then
    pass "Secret echopulpit/youtube-api-key exists"
  else
    fail "Secret echopulpit/youtube-api-key missing"
  fi
  if aws secretsmanager describe-secret --secret-id echopulpit/anthropic-api-key --region "$REGION" >/dev/null 2>&1; then
    pass "Secret echopulpit/anthropic-api-key exists"
  else
    fail "Secret echopulpit/anthropic-api-key missing"
  fi
  if aws secretsmanager describe-secret --secret-id echopulpit/ytdlp-cookies --region "$REGION" >/dev/null 2>&1; then
    pass "Secret echopulpit/ytdlp-cookies exists (optional)"
  else
    warn "Secret echopulpit/ytdlp-cookies not set -- optional, but recommended (see README)"
  fi

  section "SES"
  for addr_var in SES_SENDER_ADDRESS NOTIFY_RECIPIENT_ADDRESS; do
    addr="${!addr_var:-}"
    if [[ -z "$addr" ]]; then
      warn "$addr_var not set, skipping SES check for it"
      continue
    fi
    status="$(aws ses get-identity-verification-attributes --identities "$addr" --region "$REGION" \
      --query "VerificationAttributes.\"$addr\".VerificationStatus" --output text 2>/dev/null || echo "NotFound")"
    if [[ "$status" == "Success" ]]; then
      pass "$addr_var ($addr) is verified in SES"
    else
      warn "$addr_var ($addr) is not verified in SES (status: $status) -- see README Step 3"
    fi
  done

  section "Cost tracking"
  tag_err="$(aws ce list-cost-allocation-tags --region us-east-1 --tag-keys Project --output json 2>&1 1>/dev/null)"
  if [[ -n "$tag_err" ]]; then
    if [[ "$tag_err" == *"Invalid choice"* ]]; then
      warn "Can't check the Project cost-allocation tag -- this AWS CLI is too old for 'aws ce list-cost-allocation-tags'. Check it in the console instead: https://console.aws.amazon.com/costmanagement/home#/cost-allocation-tags"
    else
      warn "Could not check cost-allocation tag status: $(echo "$tag_err" | head -1)"
    fi
  else
    tag_status="$(aws ce list-cost-allocation-tags --region us-east-1 --tag-keys Project \
      --query "CostAllocationTags[?TagKey=='Project'].Status" --output text 2>/dev/null)"
    if [[ "$tag_status" == "Active" ]]; then
      pass "Project cost-allocation tag is Active"
    else
      warn "Project cost-allocation tag is not active (status: '${tag_status:-not found}') -- the monthly report's cost figures will read \$0 until this is on; see README 'Monthly cost & job report'"
    fi
  fi
}

if [[ "$MODE" == "pre" ]]; then
  echo "EchoPulpit preflight check"
  run_preflight
else
  echo "EchoPulpit health check (region: $REGION, bucket: ${ARTIFACTS_BUCKET:-<not set>})"
  run_healthcheck
fi

echo ""
echo "== Summary =="
echo "  $PASS passed, $WARN warning(s), $FAIL failure(s)"
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
