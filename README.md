## EchoPulpit (YouTube -> Transcribe -> Sermon -> Article -> PDF -> Email)

Turns a church's YouTube livestream into an SEO-ready blog article (Markdown
frontmatter + PDF) using Claude, with no ongoing infrastructure cost between
sermons: a scheduled check detects a newly-ended livestream, a stock EC2
instance does the work, emails you the result, and terminates itself.

### Architecture

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

Nothing runs, and nothing costs money, between sermons. No custom AMI to
build or maintain -- the AMI is stock AL2023, resolved dynamically at deploy
time via the public SSM parameter; the app itself is small enough that
installing it at boot costs a few seconds, and there's no local model to
bake in since article generation is Claude-primary. Rough cost at
weekly-sermon cadence: Lambda + DynamoDB + S3 + SES are all effectively free
at this volume; the real line items are Claude API usage per article and a
CPU instance for ~5-40 minutes per sermon (`m6i.xlarge` on-demand by
default -- see "Spot vs on-demand" below) -- well under $5/month total,
versus $100+/month for a 24/7 container.

---

### Required inputs & secrets

Nothing in this repo ships with real credentials -- `config.yaml` (which
would hold the YouTube channel ID) is gitignored, and every credential below
lives only in Secrets Manager at runtime, never on disk in this repo. You'll
need to supply the following:

| Name | What it is | Used by | Where to get it |
|---|---|---|---|
| `YOUTUBE_API_KEY` | YouTube Data API v3 key | Poller Lambda only (via Secrets Manager; never touches the worker) | [Google Cloud Console](https://console.cloud.google.com/) → APIs & Services → Credentials → Create API Key, with the "YouTube Data API v3" enabled on the project |
| `CHANNEL_ID` | The channel's YouTube **channel ID**, not its `@handle` | Poller Lambda | On the channel's YouTube page: Share → Copy channel ID (starts with `UC...`) |
| `ANTHROPIC_API_KEY` | Claude API key for article generation | Worker (fetched from Secrets Manager at boot) | [console.anthropic.com](https://console.anthropic.com/). v1 is Claude-primary with no local-model fallback baked into the worker -- see "Local-model fallback" below if you want one |
| yt-dlp cookies (optional but recommended) | Netscape-format cookies from a real logged-in YouTube session | Worker, if the secret exists | YouTube increasingly blocks requests from cloud/datacenter IPs as bot traffic. Export via a browser extension ("Get cookies.txt LOCALLY") rather than `yt-dlp --cookies-from-browser`, which can fail against Chrome's newer cookie encryption on Windows. Store with `aws secretsmanager create-secret --name echopulpit/ytdlp-cookies --secret-string file://cookies.txt`. Missing is fine -- the worker just degrades back to whatever success rate captions/no-cookie downloads get |
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

### Prerequisites (local tooling)

- AWS CLI v2, configured with credentials (provisioning + deploys)
- Python 3.11 + `pip` + `zip` (packaging the Poller Lambda's dependencies)
- An AWS account with an available VPC subnet + security group
- [Packer](https://developer.hashicorp.com/packer) -- only if you want the
  legacy custom-AMI path (`deploy/packer/`); not needed for the default
  stock-AMI setup below

### 0) Create a deployer IAM user

This is a one-time bootstrapping step, done with your own admin/root AWS
session. It creates a dedicated IAM user scoped to only what building and
deploying this project requires -- distinct from (and broader than) the
tightly-scoped runtime roles the deploy scripts create for the Lambdas and
worker instance itself (see `deploy/iam/`).

```bash
cd deploy

export AWS_REGION=us-east-1
export ARTIFACTS_BUCKET=your-unique-bucket-name   # must match what you'll pass to setup.sh later
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
it immediately (e.g. `aws configure --profile echopulpit-deployer`); it can't be
retrieved again. (If you're re-running this and already have a key, skip that
last line -- `create-access-key` isn't idempotent and AWS caps users at 2
active keys.)

What each policy covers:
- `deploy/iam/deployer-packer-policy.json` -- only needed for the legacy
  custom-AMI path (`deploy/packer/`); skip attaching this one for the
  default stock-AMI setup below.
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

### 1) Provision AWS resources

No AMI to build -- `deploy/setup.sh` resolves the latest stock Amazon Linux
2023 AMI itself (via the public `/aws/service/ami-amazon-linux-latest/...`
SSM parameter) and syncs the app code to `s3://$ARTIFACTS_BUCKET/app/` for
the worker to fetch at boot.

```bash
export AWS_REGION=us-east-1
export CHANNEL_ID=UCxxxxxxxxxxxxxxxxxxxxxx      # not the @handle
export YOUTUBE_API_KEY=...                       # seeds Secrets Manager once; not stored in this repo
export ANTHROPIC_API_KEY=...                     # seeds Secrets Manager once; not stored in this repo
export ARTIFACTS_BUCKET=your-unique-bucket-name
export SUBNET_ID=subnet-xxxxxxxx
export SECURITY_GROUP_ID=sg-xxxxxxxx
export SES_SENDER_ADDRESS=you@yourdomain.com
export NOTIFY_RECIPIENT_ADDRESS=you@yourdomain.com

./deploy/setup.sh
```

Review `deploy/setup.sh` and the IAM policy templates in `deploy/iam/`
before running it against a real account -- it creates IAM roles, a
DynamoDB table, an S3 bucket, two Lambda functions, and an EventBridge rule.
After it finishes, sync the app code once (`setup.sh` prints the exact
command) and, if you have yt-dlp cookies, store them per the table above.

### 2) Verify SES

```bash
aws ses verify-email-identity --email-address "$SES_SENDER_ADDRESS" --region "$AWS_REGION"
# If your SES account is still in the sandbox (true for new AWS accounts by
# default), the recipient must be verified too:
aws ses verify-email-identity --email-address "$NOTIFY_RECIPIENT_ADDRESS" --region "$AWS_REGION"
```

Each address gets a confirmation email from AWS with a verification link.

### 3) Verify end-to-end

- Manually invoke the poller Lambda (or wait for its 15-minute schedule)
  against a channel with a recently-ended livestream.
- Confirm in the AWS Console: an instance launches tagged with
  `SermonVideoId`, artifacts land under
  `s3://<bucket>/sermons/<video_id>/` (including `bootstrap.log`, the full
  boot-to-finish log -- useful for debugging even successful runs), the
  DynamoDB item reaches `COMPLETE`, an email arrives with the PDF attached,
  and the instance is terminated (not just stopped) shortly after.
- To force a specific video through the pipeline for testing without
  waiting on the poller, launch a worker instance by hand with the resolved
  AMI and tag it `SermonVideoId=<id>`, `SermonVideoTitle=<title>`,
  `SermonVideoDurationSeconds=<seconds>`, `SermonVideoEndTime=<ISO8601>` --
  `bootstrap.sh` reads those tags directly.

### Spot vs on-demand

The Poller Lambda's `WORKER_USE_SPOT` env var (default `true`) controls
whether `_launch_worker()` requests a spot or on-demand instance; set it to
`false` for guaranteed capacity if spot availability is unreliable in your
region/AZ/instance-type combination. `WORKER_INSTANCE_TYPE` defaults to
`m6i.xlarge`; a smaller/cheaper type is workable too since the common path
(captions available) does no local transcription or model inference at
all -- size for the occasional Whisper-fallback case, not the common case.

---

### Local development / testing

The pipeline code itself has no AWS dependency beyond `storage.py` (state) --
but that one dependency is real: `EchoPulpitJobs` must already exist as an
actual DynamoDB table, even for a local run (see the `aws dynamodb
create-table` command under "Provision AWS resources" above -- run just that
one command against whatever account/region you want the local run to use).
Everything else runs locally for iteration:

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

### Security notes

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
