## EchoPulpit (YouTube -> Transcribe -> Sermon -> Article -> PDF -> Email)

Turns a church's YouTube livestream into an SEO-ready blog article (Markdown
frontmatter + PDF) using Claude, with no ongoing infrastructure cost between
sermons: a scheduled check detects a newly-ended livestream, a stock EC2
instance (a temporary cloud server -- "EC2" is Amazon's virtual-machine
service) does the work, emails you the result, and terminates itself.

This README is written so a developer or IT technician who isn't already an
AWS expert can get this running from scratch. AWS-specific terms are
explained in plain language the first time they come up.

---

## What you'll need before you start

Gather these first so setup is one straight pass with no waiting around
mid-way:

1. **An AWS account** with admin access (or someone who can grant it to you).
2. **A YouTube Data API key** and your church's **YouTube channel ID** --
   see the table below for exactly where to get each one.
3. **An Anthropic (Claude) API key** -- from [console.anthropic.com](https://console.anthropic.com/).
4. **An email address you control**, to send *from* and receive reports *at*
   (can be the same address).
5. On your own computer: **AWS CLI v2** installed and logged in, plus
   **Python 3.11+**, `pip`, and `zip`. (`aws --version` and `python3
   --version` to check what you already have.)

You do **not** need to know Lambda, IAM, or any other AWS service by name --
the setup script below handles all of that; the explanations here are just
so you understand what it's doing as it runs.

---

## Setup (one-time)

Five steps, run from a terminal with the AWS CLI configured. Each one is
copy-paste-able; fill in the placeholders (`your-...`, `UCxxxx...`, etc.)
with your own values first.

**Tip:** once you've exported the variables in Step 1/2 below, run
`./deploy/verify.sh --pre` to catch typos, missing tools, or bad credentials
before you provision anything -- much faster to fix a bad `SUBNET_ID` now
than to debug it after `setup.sh` has already created half your
infrastructure.

### Step 1 -- Create a dedicated deploy user

This creates a separate AWS identity ("IAM user" -- AWS's term for a
named account with its own permissions) just for setting this project up,
rather than using your personal admin login for everything.

```bash
cd deploy

export AWS_REGION=us-east-1
export ARTIFACTS_BUCKET=your-unique-bucket-name   # pick a globally-unique name; reuse it in every step below
export TABLE_NAME=EchoPulpitJobs
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

render() {
  content="$(cat "$1")"
  content="${content//\$\{REGION\}/$AWS_REGION}"
  content="${content//\$\{ACCOUNT_ID\}/$ACCOUNT_ID}"
  content="${content//\$\{ARTIFACTS_BUCKET\}/$ARTIFACTS_BUCKET}"
  content="${content//\$\{TABLE_NAME\}/$TABLE_NAME}"
  printf '%s' "$content" > "$2"
}
mkdir -p .build
render iam/deployer-packer-policy.json .build/deployer-packer-policy.json
render iam/deployer-setup-policy.json  .build/deployer-setup-policy.json

if ! aws iam get-user --user-name echopulpit-deployer >/dev/null 2>&1; then
  aws iam create-user --user-name echopulpit-deployer
fi

# Managed policies, not inline (`put-user-policy`) -- inline policies on an
# IAM *user* are capped at 2048 bytes, which deployer-setup-policy.json
# exceeds once its ${...} placeholders are filled in with real ARNs. Managed
# policies get a 6144-character limit and are versioned, so this is also
# safe to re-run as the policy content evolves.
create_or_update_policy() {
  local name="$1" doc="$2"
  local arn="arn:aws:iam::${ACCOUNT_ID}:policy/${name}"
  if aws iam get-policy --policy-arn "$arn" >/dev/null 2>&1; then
    local old_version
    old_version="$(aws iam list-policy-versions --policy-arn "$arn" \
      --query 'Versions[?IsDefaultVersion==`false`]|[0].VersionId' --output text)"
    if [[ "$old_version" != "None" && -n "$old_version" ]]; then
      aws iam delete-policy-version --policy-arn "$arn" --version-id "$old_version" || true
    fi
    aws iam create-policy-version --policy-arn "$arn" \
      --policy-document "file://${doc}" --set-as-default >/dev/null
  else
    aws iam create-policy --policy-name "$name" --policy-document "file://${doc}" >/dev/null
  fi
  echo "$arn"
}

PACKER_POLICY_ARN="$(create_or_update_policy echopulpit-packer-build .build/deployer-packer-policy.json)"
SETUP_POLICY_ARN="$(create_or_update_policy echopulpit-setup-provisioning .build/deployer-setup-policy.json)"

aws iam attach-user-policy --user-name echopulpit-deployer --policy-arn "$PACKER_POLICY_ARN"
aws iam attach-user-policy --user-name echopulpit-deployer --policy-arn "$SETUP_POLICY_ARN"

aws iam create-access-key --user-name echopulpit-deployer
```

The last command prints an `AccessKeyId`/`SecretAccessKey` **once** -- save
it immediately (e.g. `aws configure --profile echopulpit-deployer`); it
can't be retrieved again. (Re-running this later and already have a key?
Skip that last line -- `create-access-key` isn't idempotent and AWS caps
users at 2 active keys.)

<details>
<summary>What each policy actually grants (click to expand)</summary>

- `deploy/iam/deployer-packer-policy.json` -- only needed for the legacy
  custom-AMI path (`deploy/packer/`); not used by the default setup below.
- `deploy/iam/deployer-setup-policy.json` -- tightly scoped to the exact
  table/bucket/role-name-prefix/function names `deploy/setup.sh` creates.
  Notably, `iam:PassRole` is restricted to `role/echopulpit-*`, so this user
  can only hand off the roles this project creates, not arbitrary roles in
  your account.

For a personal/single-user project where that overhead isn't worth it, the
pragmatic alternative is attaching AWS-managed `PowerUserAccess` +
`IAMFullAccess` to the deployer user instead. Either way, consider deleting
the access key (or the whole user) once initial setup is done, and only
recreating it for occasional redeploys.
</details>

### Step 2 -- Provision everything in AWS

One script creates everything this project needs: the database that tracks
jobs, the file storage bucket, and four small serverless functions ("Lambda"
-- AWS's name for a function that runs on its own without a server you
manage) on their own schedules. Nothing here costs money while idle.

```bash
export AWS_REGION=us-east-1
export CHANNEL_ID=UCxxxxxxxxxxxxxxxxxxxxxx      # your channel ID, not its @handle -- see table below
export YOUTUBE_API_KEY=...                       # stored securely once; never saved in this repo
export ANTHROPIC_API_KEY=...                     # stored securely once; never saved in this repo
export ARTIFACTS_BUCKET=your-unique-bucket-name   # same value as Step 1
export SUBNET_ID=subnet-xxxxxxxx
export SECURITY_GROUP_ID=sg-xxxxxxxx
export SES_SENDER_ADDRESS=you@yourdomain.com
export NOTIFY_RECIPIENT_ADDRESS=you@yourdomain.com

./deploy/setup.sh
```

(`SUBNET_ID` / `SECURITY_GROUP_ID`: if you don't already have a VPC subnet +
security group picked out, any AWS account has a "default" VPC with these
pre-created -- `aws ec2 describe-subnets` and `aws ec2
describe-security-groups` will list what you have.)

Review `deploy/setup.sh` and the IAM policy templates in `deploy/iam/`
before running it against a real account -- worth knowing what it does
before it does it. It creates IAM roles, a database table, a storage
bucket, four Lambda functions, and their schedules, and prints a short list
of remaining manual steps when it finishes (SES verification, syncing the
app code, and the ffmpeg mirror below).

### Step 3 -- Verify your sending email

AWS won't let you send email until it's confirmed you own the address.

```bash
aws ses verify-email-identity --email-address "$SES_SENDER_ADDRESS" --region "$AWS_REGION"
# New AWS accounts default to SES "sandbox" mode, which only allows sending
# to *verified* addresses -- verify the recipient too, just in case:
aws ses verify-email-identity --email-address "$NOTIFY_RECIPIENT_ADDRESS" --region "$AWS_REGION"
```

Each address gets a confirmation email from AWS with a verification link --
click it.

### Step 4 -- Seed the ffmpeg mirror (recommended, not required)

Worker instances need `ffmpeg` (audio/video processing) at boot. `setup.sh`
already deployed a weekly job that keeps a copy of it in your own S3
bucket, but that job hasn't run for the first time yet. Skipping this step
is safe -- workers just fall back to downloading it directly until the
weekly job catches up (within 7 days) -- but running it now means your very
first job doesn't depend on a third-party website being up:

```bash
export ARTIFACTS_BUCKET=your-unique-bucket-name   # same value as before
export WORKER_ARCH=arm64                          # match setup.sh's default; use x86_64 if you changed it
./deploy/seed-ffmpeg-mirror.sh
```

### Step 5 -- Confirm it actually works end to end

Run `./deploy/verify.sh --post` first -- it checks that everything `setup.sh`
should have created (IAM roles, the database, all four Lambdas and their
schedules, secrets, SES verification, and more) actually exists and is
healthy, and tells you exactly what's missing if not. Re-run it any time
something seems broken; it never changes anything, only reports.

Then confirm the pipeline itself works:

- Manually invoke the poller Lambda (AWS Console -> Lambda ->
  `echopulpit-poller` -> Test) or just wait up to 15 minutes for its
  schedule, against a channel with a recently-ended livestream.
- Confirm: an EC2 instance launches (visible in the AWS Console under EC2),
  a file shows up at `s3://<bucket>/sermons/<video_id>/bootstrap.log` (the
  full boot-to-finish log -- useful even when things go right), the
  database entry reaches `COMPLETE`, an email arrives with the PDF
  attached, and the instance terminates itself shortly after.
- To test a specific video without waiting on the schedule, launch a worker
  instance by hand with the AMI `setup.sh` resolved and tag it
  `SermonVideoId=<id>`, `SermonVideoTitle=<title>`,
  `SermonVideoDurationSeconds=<seconds>`, `SermonVideoEndTime=<ISO8601>` --
  `bootstrap.sh` reads those tags directly.

**That's it -- the system runs itself from here.** Nothing further to do
until your church posts its next livestream.

---

## Required inputs & secrets reference

Nothing in this repo ships with real credentials -- `config.yaml` (which
would hold the YouTube channel ID) is gitignored, and every credential below
lives only in Secrets Manager (AWS's encrypted credential store) at runtime,
never on disk in this repo.

| Name | What it is | Used by | Where to get it |
|---|---|---|---|
| `YOUTUBE_API_KEY` | YouTube Data API v3 key | Poller Lambda only (via Secrets Manager; never touches the worker) | [Google Cloud Console](https://console.cloud.google.com/) → APIs & Services → Credentials → Create API Key, with the "YouTube Data API v3" enabled on the project |
| `CHANNEL_ID` | The channel's YouTube **channel ID**, not its `@handle` | Poller Lambda | On the channel's YouTube page: Share → Copy channel ID (starts with `UC...`) |
| `ANTHROPIC_API_KEY` | Claude API key for article generation | Worker (fetched from Secrets Manager at boot) | [console.anthropic.com](https://console.anthropic.com/). v1 is Claude-primary with no local-model fallback baked into the worker -- see "Local-model fallback" below if you want one |
| yt-dlp cookies (optional but recommended) | Netscape-format cookies from a real logged-in YouTube session | Worker, if the secret exists | YouTube increasingly blocks requests from cloud/datacenter IPs as bot traffic. Export via a browser extension ("Get cookies.txt LOCALLY") rather than `yt-dlp --cookies-from-browser`, which can fail against Chrome's newer cookie encryption on Windows. Store with `aws secretsmanager create-secret --name echopulpit/ytdlp-cookies --secret-string file://cookies.txt --tags Key=Project,Value=echopulpit`. Missing is fine -- the worker just degrades back to whatever success rate captions/no-cookie downloads get |
| AWS account + admin/root access (one-time) | To create the deployer IAM user | You, once | Your own AWS account |
| `SES_SENDER_ADDRESS` | Email address the article gets sent **from** | Notifier Lambda | Any address you control -- must be verified in SES (`aws ses verify-email-identity`) |
| `NOTIFY_RECIPIENT_ADDRESS` | Email address the article gets sent **to** | Notifier Lambda | Your inbox. Must *also* be verified if your SES account is still in the sandbox (new AWS accounts default to sandbox mode, which only allows sending to verified addresses) |
| `ARTIFACTS_BUCKET` | S3 bucket name for output artifacts **and** app code (`app/` prefix, synced at worker boot) | `setup.sh` | Any globally-unique name you choose |
| `SUBNET_ID` | A VPC subnet with internet egress (NAT or public + auto-assign IP) | `setup.sh`, Poller Lambda | An existing subnet in your AWS account |
| `SECURITY_GROUP_ID` | Security group for the worker instance | `setup.sh`, Poller Lambda | An existing (or new) SG allowing outbound internet access |

Optional tuning (not secrets, live in `config.yaml`): `transcription.whisper_model`,
`transcription.prefer_captions`/`min_caption_coverage`/`caption_langs`,
`sermon_extraction.*` (how much of the stream is "the sermon" vs.
announcements/worship), `llm.*` (`max_tokens`, `thinking_effort`, style
guide path, local-model path). See `config.yaml.example` for the full set
with defaults.

**What's deliberately never a secret on disk:** `config.yaml` has no
`api_key` field at all -- only environment variables at runtime, fetched
from Secrets Manager. Worker instances never see `YOUTUBE_API_KEY` or talk
to the YouTube API; the Poller Lambda resolves video metadata once and
passes it to the worker via EC2 instance tags (`SermonVideoId`,
`SermonVideoTitle`, `SermonVideoDurationSeconds`, `SermonVideoEndTime`),
which `bootstrap.sh` reads directly. The worker's own Secrets Manager access
is scoped to exactly two secret-name patterns (`echopulpit/anthropic-api-key-*`,
`echopulpit/ytdlp-cookies-*`) -- see `deploy/iam/worker-instance-policy.json`.

**Local-model fallback:** `sermon_pipeline.py` can still write articles with
a local GGUF model via `llama-cpp-python` when `ANTHROPIC_API_KEY` isn't
set (`build_backend()` in `sermon_pipeline.py`), but the production worker
doesn't install `llama-cpp-python` at all (see `requirements-worker.txt` --
it requires compiling from source, which is slow at every boot for a path
that's never exercised since `ANTHROPIC_API_KEY` is always present via
Secrets Manager). The import is lazy (only inside `init_llm()`), so this is
purely a local-dev/offline convenience, not a production fallback path.

---

## How it works

```
EventBridge (rate: 15 min)
        │
        ▼
Poller Lambda ──reads──> Secrets Manager (YOUTUBE_API_KEY)
        │  channels.list -> uploads playlist -> playlistItems.list ->
        │  videos.list (liveStreamingDetails/contentDetails)
        │  (never search.list -- 100x the quota cost for the same check)
        ▼
DynamoDB (EchoPulpitJobs): claim newly-ended video, or retry/reclaim stale jobs
        ▼
EC2 instance (stock Amazon Linux 2023 AMI, tagged SermonVideoId=<id>)
        │  bootstrap.sh (EC2 user-data, on boot):
        │   1. read video_id/title/duration from own instance tags
        │   2. install ffmpeg (static build) + create a Python venv
        │   3. sync app code from S3, install deps into the venv
        │   4. fetch ANTHROPIC_API_KEY (required) and yt-dlp cookies
        │      (optional, see below) from Secrets Manager
        │   5. run sermon_pipeline.py for that one video (captions-first,
        │      falls back to Whisper; Claude writes the article)
        │   6. upload artifacts + this boot log to S3
        │   7. record COMPLETE/FAILED in DynamoDB
        │   8. terminate self (+ boot-time watchdog force-terminates at
        │      +3h regardless, as a cost backstop)
        ▼
DynamoDB Streams ──triggers──> Notifier Lambda ──SES──> your inbox
                                (PDF attached; failure alerts too)
```

Nothing runs, and nothing costs money, between sermons. No custom AMI (a
pre-built server image) to build or maintain -- the AMI is stock AL2023,
resolved dynamically at deploy time via a public AWS parameter; the app
itself is small enough that installing it at boot costs a few seconds, and
there's no local model to bake in since article generation is
Claude-primary. Rough cost at weekly-sermon cadence: Lambda + DynamoDB + S3
+ SES are all effectively free at this volume; the real line items are
Claude API usage per article and a CPU instance for ~5-40 minutes per
sermon (`m7g.xlarge` on-demand by default, AWS Graviton/arm64 -- see "Spot
vs on-demand" below) -- well under $5/month total, versus $100+/month for a
24/7 container.

### Supporting infrastructure

Two more Lambdas run on their own schedules, independent of the per-sermon
flow above:

- **`echopulpit-ffmpeg-mirror-refresh`** (weekly) -- keeps
  `s3://<bucket>/deps/ffmpeg-static-linux-<arch>.tar.xz` current, the mirror
  `bootstrap.sh` tries before falling back to downloading ffmpeg directly
  from a third party at boot. Re-fetches upstream, verifies the result is
  actually a static binary (no dynamic linker required) before trusting it,
  and leaves the existing mirror untouched on any failure.
- **`echopulpit-monthly-report`** (8am UTC, 4th of each month) -- emails a
  summary of the previous calendar month: AWS spend and job outcomes. See
  "Monthly cost & job report" below.

### Monthly cost & job report

On the 4th of every month, `echopulpit-monthly-report` emails a plain-text
summary of the *previous* calendar month: how many jobs ran, how many
completed vs. failed (with the specific error for each failure), total AWS
spend, and a per-service cost breakdown. It runs on the 4th rather than the
1st to give AWS's cost data a couple of days to fully settle before
reporting on it.

Cost figures depend on the `Project=echopulpit` cost-allocation tag being
active on your account -- `setup.sh` tries to activate it automatically as
its last step. If that fails (older AWS CLI versions don't support the
command it needs), either run the fallback command `setup.sh` prints, or
activate it by hand at
[console.aws.amazon.com/costmanagement/home#/cost-allocation-tags](https://console.aws.amazon.com/costmanagement/home#/cost-allocation-tags).
One thing to know either way: AWS doesn't backfill cost data from before a
tag was activated, so the very first report will show incomplete spend for
however much of that month happened before you ran setup -- every month
after that is accurate.

### Spot vs on-demand

The Poller Lambda's `WORKER_USE_SPOT` env var (default `true`) controls
whether `_launch_worker()` requests a spot instance (spare AWS capacity at
a steep discount, which can occasionally be reclaimed with a couple minutes'
notice) or an on-demand one (guaranteed, full price). Set it to `false` for
guaranteed capacity if spot availability is unreliable in your region/AZ/
instance-type combination -- this pipeline's built-in retry logic handles
an interrupted spot instance gracefully either way, so spot is the
recommended default for a workload like this (short jobs, nobody waiting
on it live).

`WORKER_INSTANCE_TYPE` defaults to `m7g.xlarge` (AWS Graviton/arm64 --
~15% cheaper on-demand than the equivalent Intel instance for the same
CPU/memory); a smaller/cheaper type is workable too since the common path
(captions available) does no local transcription or model inference at
all -- size for the occasional Whisper-fallback case, not the common case.
`bootstrap.sh` fetches the matching-architecture ffmpeg build automatically
(see its `ARCH` detection), so switching instance families between arm64
and x86_64 needs no other changes; set `WORKER_ARCH=x86_64` in `setup.sh`
(and pick a matching x86_64 `WORKER_INSTANCE_TYPE`) to go back to Intel.

---

## Local development / testing

The pipeline code itself has no AWS dependency beyond `storage.py` (state) --
but that one dependency is real: `EchoPulpitJobs` must already exist as an
actual DynamoDB table, even for a local run. If you've already run Step 2
above, it exists already. Otherwise, grab just the `aws dynamodb
create-table` command from the "DynamoDB table" section of
`deploy/setup.sh` and run it by hand against whatever account/region you
want the local run to use. Everything else runs locally for iteration:

```bash
pip install -r requirements.txt
cp config.yaml.example config.yaml   # fill in channel_id, model_path
export VIDEO_ID=<a specific ended-livestream video ID>   # skips discovery
python sermon_pipeline.py
```

Keep secrets out of your shell history/this file by putting them in a
gitignored `.env.local` at the repo root instead of `export`ing them
directly:

```bash
# .env.local (gitignored -- never commit this)
YOUTUBE_API_KEY=...
CHANNEL_ID=...
AWS_REGION=us-east-1          # wherever EchoPulpitJobs actually lives
ANTHROPIC_API_KEY=...          # optional -- see below
```

then `set -a && source .env.local && set +a` before running
`python sermon_pipeline.py`.

If `ANTHROPIC_API_KEY` is set, article generation uses Claude instead of the
local GGUF model -- no `LLM_MODEL_PATH`/model file needed, no CPU inference
wait, and the chunk-then-summarize map-reduce pass is skipped entirely since
Claude's context window fits a full transcript in one call. Without it,
`LLM_MODEL_PATH` must point at a real GGUF file and generation runs locally
via `llama-cpp-python` (much slower, especially on CPU, but no per-call
cost, and `pip install llama-cpp-python` needed locally since it's not in
`requirements-worker.txt`). This is purely a local-dev/offline convenience
-- production workers are Claude-only (see "Local-model fallback" above).

Run the test suite (pure-function unit tests; no AWS or model files needed):

```bash
pip install pytest
pytest
```

### Transcription: captions-first, Whisper fallback

Before downloading audio at all, the pipeline tries YouTube's own caption
track (creator-uploaded or auto-generated, fetched via `yt-dlp`'s `json3`
format) and only falls back to downloading audio + running Whisper if no
usable captions exist. Controlled by `transcription.prefer_captions` /
`min_caption_coverage` / `caption_langs` in `config.yaml`, or
`FORCE_WHISPER=true` to bypass captions for a specific run.

Caveat: YouTube's archived-livestream auto-captions typically take 2-24
hours to become available after the stream ends, so same-day processing
will usually still fall back to Whisper -- captions mainly pay off if a
video is reprocessed later (`FORCE_REPROCESS=true` with `VIDEO_ID` set).

### Article generation: pastoral voice + verified scripture

The model returns a single document: `---`-delimited YAML frontmatter (SEO
fields, `scripture_references`, `needs_review`, `reviewer_notes`) followed
by the article itself in Markdown. `prompts.py`'s `SYSTEM_PROMPT` specifies
this exactly, along with the actual editorial voice -- register bans
("never 'the preacher argued'"), what to keep vs. cut from the raw
transcript, and how to handle politically/culturally sensitive material
(flagged for human review, never silently softened or sharpened).

**Scripture accuracy** is treated as the highest-stakes part of the job.
`scripture_lookup.py` is built to check every quoted verse after generation
against a bundled public-domain KJV text (`data/kjv.json`): references that
resolve get their quoted text replaced with the authoritative wording
(catching anything the model misremembered), and references that don't
resolve are removed entirely and logged in `reviewer_notes.flags` --
nothing invented gets a free pass. **`data/kjv.json` isn't currently bundled**
(the build script kept failing against its source repeatedly and was
dropped rather than shipping a partial/unreliable dataset) -- `kjv_available()`
gates this cleanly, so verification is skipped entirely rather than run
against an empty dataset, and every article gets a `reviewer_notes.flags`
entry saying scripture wasn't independently verified. Claude's own accuracy
is relied on for now; dropping in a real `data/kjv.json` re-enables full
verification with no other code changes.

**Pastoral style guide** (`prompts/style_guide.md`, set via
`llm.style_guide_path` / `STYLE_GUIDE_PATH`) describes how a specific
preacher's voice actually sounds in print -- sentence mechanics, diction,
calibration examples, and (most importantly) 2-3 real writing samples,
since the guide's own instruction is "where this guide and the samples
disagree, the samples win." It's plain markdown, meant to be readable and
editable by the pastor himself, not prompt-engineering jargon. Missing or
empty is fine -- generation still works from the system prompt's own voice
guidance, just less calibrated to one specific preacher.

**`claude-sonnet-5` reasons by default, and thinking tokens count against
`max_tokens`.** Confirmed the hard way in production: an `max_tokens: 8000`
budget got consumed almost entirely by thinking (7999 tokens), leaving
nothing for the actual article and producing a silently empty response.
`ClaudeBackend` sends `thinking={"type": "adaptive"}` +
`output_config={"effort": llm.thinking_effort}` (this model rejects the
older `thinking.type=enabled`/`budget_tokens` scheme with a 400) and keeps
`max_tokens` generous regardless, as a cheap safety ceiling.

### Output

Per-video artifacts land in `<output.dir>/<video_id>/`:
`transcript.json/.txt`, `transcript_meta.json` (records whether captions or
Whisper were used), `sermon.json/.txt` (extracted sermon portion),
`article.json` (frontmatter fields + the markdown body + `reviewer_notes`,
after scripture verification), `article_raw.md` (the model's original
output, pre-verification, kept for debugging), `article.md` (the finished,
publishable frontmatter + body reassembled into one clean document --
distinct from `article_raw.md`), `article.html`, `sermon-article.pdf`.
`bootstrap.sh` uploads all of this except `media/` (raw audio) to S3. The
Notifier Lambda attaches both `sermon-article.pdf` and `article.md` to the
completion email.

---

## Troubleshooting

**Start here:** `./deploy/verify.sh --post` -- it checks every piece of AWS
infrastructure this project depends on and tells you specifically what's
missing or misconfigured, before you go digging through the console.

- **A job failed.** Every job, successful or not, leaves a full boot-to-
  finish log at `s3://<bucket>/sermons/<video_id>/bootstrap.log` -- start
  there. The DynamoDB item for that `video_id` also has an `error` field
  with the last failure reason and a `failure_count` (jobs auto-retry up to
  3 times before being left `FAILED` for manual review).
- **No email arrived for a completed job.** Check SES is out of sandbox
  mode, or that both sender and recipient addresses are verified (Step 3
  above) -- sandboxed SES silently refuses to send to unverified addresses.
- **Every job fails at the ffmpeg step.** Run `deploy/seed-ffmpeg-mirror.sh`
  (Step 4 above); if that also fails, johnvansickle.com (the upstream
  source) may be down -- `bootstrap.sh` retries 3 times with short timeouts
  and fails the job cleanly rather than hanging, so this recovers on its
  own once either the mirror or upstream is reachable again.
- **Monthly report shows $0 cost.** Expected for the first report if
  `setup.sh` ran partway through that month -- see "Monthly cost & job
  report" above.

## Security notes

- `config.yaml`, `models/`, `output/`, `.env.local`, and `cookies.txt` are
  all gitignored -- never commit a filled-in `config.yaml`, model file,
  local secrets, or yt-dlp cookies.
- `deploy/.build/` (rendered IAM policies with your real account ID,
  zipped Lambda packages) is also gitignored -- it's local build output,
  regenerated by the scripts above.
- If you ever find a real API key or cookie file committed to a fork/branch
  history, rotate/re-export it immediately -- treat it as already leaked,
  regardless of whether the repo was public. yt-dlp cookies are equivalent
  to session-hijacking credentials for whatever account exported them --
  use a dedicated/church account rather than a personal one, and store them
  only via `aws secretsmanager create-secret ... --secret-string file://...`
  (never paste the cookie contents anywhere they'd be logged or committed).
- The worker instance role, Poller Lambda role, and Notifier Lambda role
  are each scoped to only what that component needs (see `deploy/iam/*.json`)
  -- notably, the worker never has YouTube API access, and its Secrets
  Manager access is scoped to exactly two secret-name patterns
  (`echopulpit/anthropic-api-key-*`, `echopulpit/ytdlp-cookies-*`), not
  arbitrary secrets in the account.
