"""
Set up (or update) the "Review & publish" flow. Safe to re-run.

  1. Signing key secret (echopulpit/publish-signing-key), created once.
  2. Publisher Lambda (deploy/lambdas/publisher_lambda.py) with its own IAM
     role, packaged with its two pure-Python dependencies (markdown, PyYAML),
     behind a public Lambda Function URL. The URL is safe to be public: every
     action needs a link signed with the key above.
  3. Notifier Lambda updated to include publish_token.py, read the signing
     key, and put a signed PUBLISH_URL link in every completion email.

The GitHub token the Publisher commits with is NOT created here -- create a
fine-grained token (repository access: only the blog repo; permissions:
Contents read and write) and store it yourself:
    aws secretsmanager create-secret --name echopulpit/github-token --secret-string "github_pat_..."

Uses boto3 (not the AWS CLI, which needs to be recent for Function URLs).
Usage (repo root):
    export ARTIFACTS_BUCKET=...  GITHUB_REPO=owner/blog  BLOG_URL=https://blog.example.org
    python deploy/setup_publisher.py
Optional env: AWS_REGION (us-east-1), TABLE_NAME (EchoPulpitJobs), GITHUB_BRANCH (main),
POSTS_DIR (src/content/posts), NOTIFY_TIMEZONE (America/Chicago). The Subsplash feed
URL is read from deploy/config.worker.yaml.
"""
import io
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import zipfile

import boto3
import yaml

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
LAMBDA_DIR = os.path.join(ROOT, "deploy", "lambdas")
IAM_DIR = os.path.join(ROOT, "deploy", "iam")

REGION = os.environ.get("AWS_REGION", "us-east-1")
BUCKET = os.environ["ARTIFACTS_BUCKET"]
TABLE = os.environ.get("TABLE_NAME", "EchoPulpitJobs")
GITHUB_REPO = os.environ["GITHUB_REPO"]
BLOG_URL = os.environ["BLOG_URL"]
FN = "echopulpit-publisher"
ROLE = "echopulpit-publisher-lambda-role"
NOTIFIER_FN = "echopulpit-notifier"
NOTIFIER_ROLE = "echopulpit-notifier-lambda-role"
KEY_SECRET = "echopulpit/publish-signing-key"
GITHUB_SECRET = "echopulpit/github-token"
TAGS = {"Project": "echopulpit"}

sts = boto3.client("sts", region_name=REGION)
iam = boto3.client("iam", region_name=REGION)
lam = boto3.client("lambda", region_name=REGION)
sm = boto3.client("secretsmanager", region_name=REGION)
ACCOUNT = sts.get_caller_identity()["Account"]


def render(template: str, **values) -> str:
    text = open(os.path.join(IAM_DIR, template), encoding="utf-8").read()
    for k, v in values.items():
        text = text.replace("${" + k + "}", v)
    return text


def ensure_signing_key():
    try:
        sm.describe_secret(SecretId=KEY_SECRET)
        print(f"secret {KEY_SECRET}: exists")
    except sm.exceptions.ResourceNotFoundException:
        sm.create_secret(Name=KEY_SECRET, SecretString=secrets.token_urlsafe(48),
                         Tags=[{"Key": k, "Value": v} for k, v in TAGS.items()])
        print(f"secret {KEY_SECRET}: created")


def ensure_role() -> str:
    try:
        arn = iam.get_role(RoleName=ROLE)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=render("publisher-lambda-trust.json"),
                              Tags=[{"Key": k, "Value": v} for k, v in TAGS.items()])["Role"]["Arn"]
        print(f"role {ROLE}: created (waiting for IAM to propagate)")
        time.sleep(12)
    iam.put_role_policy(RoleName=ROLE, PolicyName="publisher-inline", PolicyDocument=render(
        "publisher-lambda-policy.json", REGION=REGION, ACCOUNT_ID=ACCOUNT, TABLE_NAME=TABLE, ARTIFACTS_BUCKET=BUCKET))
    return arn


def publisher_zip() -> bytes:
    with tempfile.TemporaryDirectory() as deps:
        # Pure-Python wheels for Lambda's Linux runtime, whatever OS this runs on.
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "--target", deps,
                               "--platform", "manylinux2014_x86_64", "--python-version", "3.11",
                               "--only-binary=:all:", "markdown", "pyyaml"])
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name in ("publisher_lambda.py", "publish_token.py"):
                z.writestr(name, open(os.path.join(LAMBDA_DIR, name), encoding="utf-8").read().replace("\r", ""))
            for base, _dirs, files in os.walk(deps):
                for f in files:
                    full = os.path.join(base, f)
                    rel = os.path.relpath(full, deps)
                    if "__pycache__" not in rel:
                        z.write(full, rel)
        return buf.getvalue()


def notifier_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in ("notifier_lambda.py", "publish_token.py"):
            z.writestr(name, open(os.path.join(LAMBDA_DIR, name), encoding="utf-8").read().replace("\r", ""))
    return buf.getvalue()


def wait(fn: str):
    lam.get_waiter("function_updated").wait(FunctionName=fn)


def deploy_publisher(role_arn: str) -> str:
    cfg = yaml.safe_load(open(os.path.join(ROOT, "deploy", "config.worker.yaml"), encoding="utf-8"))
    env = {"Variables": {
        "SERMON_ARTIFACTS_BUCKET": BUCKET,
        "SERMON_JOBS_TABLE": TABLE,
        "PUBLISH_KEY_SECRET": KEY_SECRET,
        "GITHUB_TOKEN_SECRET": GITHUB_SECRET,
        "GITHUB_REPO": GITHUB_REPO,
        "GITHUB_BRANCH": os.environ.get("GITHUB_BRANCH", "main"),
        "POSTS_DIR": os.environ.get("POSTS_DIR", "src/content/posts"),
        "BLOG_URL": BLOG_URL,
        "SUBSPLASH_FEED_URL": (cfg.get("transcription") or {}).get("subsplash_feed_url", ""),
        "CHURCH_TIMEZONE": (cfg.get("church") or {}).get("timezone", "America/Chicago"),
    }}
    code = publisher_zip()
    try:
        lam.get_function(FunctionName=FN)
        lam.update_function_code(FunctionName=FN, ZipFile=code)
        wait(FN)
        lam.update_function_configuration(FunctionName=FN, Environment=env, Timeout=30, MemorySize=256)
        print(f"lambda {FN}: updated")
    except lam.exceptions.ResourceNotFoundException:
        for attempt in range(5):  # a brand-new role can take a few seconds to be assumable
            try:
                lam.create_function(FunctionName=FN, Runtime="python3.11", Role=role_arn,
                                    Handler="publisher_lambda.lambda_handler", Code={"ZipFile": code},
                                    Timeout=30, MemorySize=256, Environment=env, Tags=TAGS)
                break
            except lam.exceptions.InvalidParameterValueException:
                if attempt == 4:
                    raise
                time.sleep(8)
        print(f"lambda {FN}: created")
    wait(FN)

    try:
        url = lam.get_function_url_config(FunctionName=FN)["FunctionUrl"]
    except lam.exceptions.ResourceNotFoundException:
        url = lam.create_function_url_config(FunctionName=FN, AuthType="NONE")["FunctionUrl"]
        print(f"function URL: created")
    for sid, kwargs in (
        ("public-function-url", {"Action": "lambda:InvokeFunctionUrl", "FunctionUrlAuthType": "NONE"}),
        ("public-function-url-invoke", {"Action": "lambda:InvokeFunction", "InvokedViaFunctionUrl": True}),
    ):
        try:
            lam.add_permission(FunctionName=FN, StatementId=sid, Principal="*", **kwargs)
        except lam.exceptions.ResourceConflictException:
            pass  # already granted
        except Exception as e:  # older boto3 without InvokedViaFunctionUrl
            print(f"note: couldn't add permission {sid}: {e}")
    return url


def update_notifier(publish_url: str):
    env = lam.get_function_configuration(FunctionName=NOTIFIER_FN)["Environment"]["Variables"]
    sender_arn = f"arn:aws:ses:{REGION}:{ACCOUNT}:identity/{env['SES_SENDER_ADDRESS']}"
    iam.put_role_policy(RoleName=NOTIFIER_ROLE, PolicyName="notifier-inline", PolicyDocument=render(
        "notifier-lambda-policy.json", REGION=REGION, ACCOUNT_ID=ACCOUNT, TABLE_NAME=TABLE,
        ARTIFACTS_BUCKET=BUCKET, SES_SENDER_IDENTITY_ARN=sender_arn))
    lam.update_function_code(FunctionName=NOTIFIER_FN, ZipFile=notifier_zip())
    wait(NOTIFIER_FN)
    env.update(PUBLISH_URL=publish_url, PUBLISH_KEY_SECRET=KEY_SECRET)
    # Per-language extra reviewers, e.g. NOTIFY_EXTRA_RECIPIENTS_ES=a@x.com,b@y.com
    env.update({k: v for k, v in os.environ.items() if k.startswith("NOTIFY_EXTRA_RECIPIENTS_")})
    lam.update_function_configuration(FunctionName=NOTIFIER_FN, Environment={"Variables": env})
    wait(NOTIFIER_FN)
    print(f"lambda {NOTIFIER_FN}: updated (publish links -> {publish_url})")


def main():
    ensure_signing_key()
    url = deploy_publisher(ensure_role())
    update_notifier(url)
    try:
        sm.describe_secret(SecretId=GITHUB_SECRET)
        print(f"secret {GITHUB_SECRET}: exists")
    except sm.exceptions.ResourceNotFoundException:
        print(f"\nACTION NEEDED: store a GitHub token as {GITHUB_SECRET} before publishing works "
              "(see this script's docstring). Review pages already work without it.")
    print(f"\nPublisher URL: {url}")


if __name__ == "__main__":
    main()
