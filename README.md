## EchoPulpit (YouTube -> Transcribe -> Sermon -> Article -> PDF -> Email)

Turns a church's YouTube livestream into an SEO-ready blog article (YAML +
HTML + PDF) using a local LLM, with no ongoing infrastructure cost between
sermons: a scheduled check detects a newly-ended livestream, a GPU spot
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
EC2 spot instance (custom AMI, tagged SermonVideoId=<id>)
        │  bootstrap.sh (systemd, on boot):
        │   1. read video_id/title/duration from own instance tags
        │   2. run sermon_pipeline.py for that one video (captions-first,
        │      falls back to Whisper; local LLM writes the article)
        │   3. upload artifacts to S3
        │   4. record COMPLETE/FAILED in DynamoDB
        │   5. terminate self (+ boot-time watchdog force-terminates at
        │      +3h regardless, as a cost backstop)
        ▼
DynamoDB Streams ──triggers──> Notifier Lambda ──SES──> your inbox
                                (PDF attached; failure alerts too)
```

Nothing runs, and nothing costs money, between sermons. Rough cost at
weekly-sermon cadence: Lambda + DynamoDB + S3 + SES are all effectively free
at this volume; the only real line item is the GPU spot instance for
~20-40 minutes per sermon (roughly $0.10-0.20/job on a `g4dn.xlarge`) --
well under $2/month total, versus $100+/month for a 24/7 GPU container.

---

### Required inputs & secrets

Nothing in this repo ships with real credentials -- `config.yaml` (which
would hold the YouTube channel ID) and `models/` (the multi-GB LLM weights)
are both gitignored, and the API key is never stored on disk anywhere in
this design. You'll need to supply the following:

| Name | What it is | Used by | Where to get it |
|---|---|---|---|
| `YOUTUBE_API_KEY` | YouTube Data API v3 key | Poller Lambda only (via Secrets Manager; never touches the worker) | [Google Cloud Console](https://console.cloud.google.com/) → APIs & Services → Credentials → Create API Key, with the "YouTube Data API v3" enabled on the project |
| `CHANNEL_ID` | The channel's YouTube **channel ID**, not its `@handle` | Poller Lambda | On the channel's YouTube page: Share → Copy channel ID (starts with `UC...`) |
| GGUF model file | Local LLM weights used to write the article | Baked into the worker AMI | e.g. a Mistral-7B-Instruct GGUF quant from Hugging Face -- place at `./models/<file>.gguf` before building the AMI |
| AWS account + admin/root access (one-time) | To create the deployer IAM user | You, once | Your own AWS account |
| `SES_SENDER_ADDRESS` | Email address the article gets sent **from** | Notifier Lambda | Any address you control -- must be verified in SES (`aws ses verify-email-identity`) |
| `NOTIFY_RECIPIENT_ADDRESS` | Email address the article gets sent **to** | Notifier Lambda | Your inbox. Must *also* be verified if your SES account is still in the sandbox (new AWS accounts default to sandbox mode, which only allows sending to verified addresses) |
| `ARTIFACTS_BUCKET` | S3 bucket name for output artifacts | `setup.sh` | Any globally-unique name you choose |
| `SUBNET_ID` | A VPC subnet with internet egress (NAT or public + auto-assign IP) | `setup.sh`, Poller Lambda | An existing subnet in your AWS account |
| `SECURITY_GROUP_ID` | Security group for the worker instance | `setup.sh`, Poller Lambda | An existing (or new) SG allowing outbound internet access |
| `AMI_ID` | The custom worker AMI | `setup.sh` | Output of the Packer build (step 3 below) |

Optional tuning (not secrets, live in `config.yaml`): `transcription.whisper_model`,
`transcription.prefer_captions`/`min_caption_coverage`/`caption_langs`,
`sermon_extraction.*` (how much of the stream is "the sermon" vs.
announcements/worship), `llm.*` (temperature, context size, model path).
See `config.yaml.example` for the full set with defaults.

**What's deliberately never a secret on disk:** `config.yaml` has no
`api_key` field at all -- only environment variables at runtime. Worker
instances never see `YOUTUBE_API_KEY` or talk to the YouTube API; the
Poller Lambda resolves video metadata once and passes it to the worker via
EC2 instance tags (`SermonVideoId`, `SermonVideoTitle`,
`SermonVideoDurationSeconds`, `SermonVideoEndTime`), which `bootstrap.sh`
reads directly.

---

### Prerequisites (local tooling)

- [Packer](https://developer.hashicorp.com/packer) (AMI build)
- AWS CLI v2, configured with credentials (provisioning + deploys)
- Python 3.11 + `pip` + `zip` (packaging the Poller Lambda's dependencies)
- An AWS account with an available VPC subnet + security group

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

aws iam create-user --user-name echopulpit-deployer

aws iam put-user-policy --user-name echopulpit-deployer \
  --policy-name packer-build --policy-document file://.build/deployer-packer-policy.json

aws iam put-user-policy --user-name echopulpit-deployer \
  --policy-name setup-provisioning --policy-document file://.build/deployer-setup-policy.json

aws iam create-access-key --user-name echopulpit-deployer
```

The last command prints an `AccessKeyId`/`SecretAccessKey` **once** -- save
it immediately (e.g. `aws configure --profile echopulpit-deployer`); it can't be
retrieved again.

What each policy covers:
- `deploy/iam/deployer-packer-policy.json` -- HashiCorp's documented
  minimal permission set for Packer's `amazon-ebs` builder. Mostly
  `Resource: "*"` because EC2 doesn't support meaningful resource-level
  scoping for AMI building (Packer creates/discovers a temporary instance,
  key pair, and security group per build).
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

### 1) Build the worker AMI

The AMI bakes in the LLM model, the Whisper model cache, and the app code so
no per-job download is needed. Place your GGUF model at `./models/` first
(see "Required inputs & secrets" above).

```bash
cd deploy/packer
packer init echopulpit-worker.pkr.hcl
packer build -var subnet_id=<a subnet with internet access> echopulpit-worker.pkr.hcl
```

Note the resulting AMI ID -- it's needed by `deploy/setup.sh` below.

### 2) Provision AWS resources

```bash
export AWS_REGION=us-east-1
export CHANNEL_ID=UCxxxxxxxxxxxxxxxxxxxxxx      # not the @handle
export YOUTUBE_API_KEY=...                       # seeds Secrets Manager once; not stored in this repo
export ARTIFACTS_BUCKET=your-unique-bucket-name
export SUBNET_ID=subnet-xxxxxxxx
export SECURITY_GROUP_ID=sg-xxxxxxxx
export AMI_ID=ami-xxxxxxxx                        # from step 1
export SES_SENDER_ADDRESS=you@yourdomain.com
export NOTIFY_RECIPIENT_ADDRESS=you@yourdomain.com

./deploy/setup.sh
```

Review `deploy/setup.sh` and the IAM policy templates in `deploy/iam/`
before running it against a real account -- it creates IAM roles, a
DynamoDB table, an S3 bucket, two Lambda functions, and an EventBridge rule.

### 3) Verify SES

```bash
aws ses verify-email-identity --email-address "$SES_SENDER_ADDRESS" --region "$AWS_REGION"
# If your SES account is still in the sandbox (true for new AWS accounts by
# default), the recipient must be verified too:
aws ses verify-email-identity --email-address "$NOTIFY_RECIPIENT_ADDRESS" --region "$AWS_REGION"
```

Each address gets a confirmation email from AWS with a verification link.

### 4) Verify end-to-end

- Manually invoke the poller Lambda (or wait for its 15-minute schedule)
  against a channel with a recently-ended livestream.
- Confirm in the AWS Console: a spot instance launches tagged with
  `SermonVideoId`, artifacts land under
  `s3://<bucket>/sermons/<video_id>/`, the DynamoDB item reaches
  `COMPLETE`, an email arrives with the PDF attached, and the instance is
  terminated (not just stopped) shortly after.
- To force a specific video through the pipeline for testing without
  waiting on the poller, launch a worker instance by hand with the AMI and
  tag it `SermonVideoId=<id>`, `SermonVideoTitle=<title>`,
  `SermonVideoDurationSeconds=<seconds>`, `SermonVideoEndTime=<ISO8601>` --
  `bootstrap.sh` reads those tags directly.

---

### Local development / testing

The pipeline code itself has no AWS dependency beyond `storage.py` (state)
-- everything else runs locally for iteration:

```bash
pip install -r requirements.txt
cp config.yaml.example config.yaml   # fill in channel_id, model_path
export YOUTUBE_API_KEY=...
export CHANNEL_ID=...
export LLM_MODEL_PATH=/path/to/model.gguf
export VIDEO_ID=<a specific ended-livestream video ID>   # skips discovery
python sermon_pipeline.py
```

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

### Output

Per-video artifacts land in `<output.dir>/<video_id>/`:
`transcript.json/.txt`, `transcript_meta.json` (records whether captions or
Whisper were used), `sermon.json/.txt` (extracted sermon portion),
`article.json` (full SEO package), `article.html`, `sermon-article.pdf`.
`bootstrap.sh` uploads all of this except `media/` (raw audio) to S3.

### Security notes

- `config.yaml`, `models/`, and `output/` are gitignored -- never commit a
  filled-in `config.yaml` or the GGUF model file.
- `deploy/.build/` (rendered IAM policies with your real account ID,
  zipped Lambda packages) is also gitignored -- it's local build output,
  regenerated by the scripts above.
- If you ever find a real API key committed to a fork/branch history,
  rotate it in Google Cloud Console immediately -- treat it as already
  leaked, regardless of whether the repo was public.
- The worker instance role, Poller Lambda role, and Notifier Lambda role
  are each scoped to only what that component needs (see `deploy/iam/*.json`)
  -- notably, the worker never has YouTube or Secrets Manager access at all.
