from __future__ import annotations

import os
import re
import json
import shutil
import subprocess
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Tuple

import yaml
from googleapiclient.discovery import build

from faster_whisper import WhisperModel
from llama_cpp import Llama

from storage import StateStore
from sermon_heuristics import Segment, extract_sermon
from prompts import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE
from render_pdf import render_pdf


# ---- Prompt helpers ----
CHUNK_SUMMARY_SYSTEM = """You are a careful sermon note-taker for a pastor preparing a church blog post.
Return concise bullet notes only (no JSON, no markdown fences) capturing:
- The main theme and big idea
- Key teaching points (numbered if possible)
- Any scripture references mentioned or strongly implied (references only, add 'KJV')
- Illustrations/examples used
- Practical applications / calls to action
Be faithful to the content; do not add new doctrine or random verses."""

FIRST_PERSON_ENFORCER_SYSTEM = """You are the pastor who preached this message.
Write in first person (I/we) as the speaker, addressing the reader directly (you/your).
NEVER refer to "the pastor" or "the speaker" in third person. Do not write "he said/she said".
If you mention the sermon, say "when I preached this message..." not third person.
"""

FIRST_PERSON_ENFORCER_USER = """IMPORTANT:
Write the article as if you (the preacher) are the author.
Use first person ("I", "we", "my prayer is...") and speak directly to the reader ("you").
Never describe yourself as "the speaker" or "the pastor".
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg


def env_or(cfg_val: str, env_key: str) -> str:
    v = os.environ.get(env_key)
    return v if v else cfg_val


def ensure_tools():
    for tool in ["ffmpeg", "yt-dlp"]:
        if shutil.which(tool) is None:
            raise RuntimeError(f"Missing required tool in PATH: {tool}")


def file_exists_nonempty(path: str) -> bool:
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def yt_api(api_key: str):
    return build("youtube", "v3", developerKey=api_key)


def find_latest_live_video(youtube, channel_id: str) -> Optional[str]:
    """Find currently-live broadcast video ID (if any)."""
    req = youtube.search().list(
        part="id",
        channelId=channel_id,
        eventType="live",
        type="video",
        order="date",
        maxResults=1,
    )
    resp = req.execute()
    items = resp.get("items", [])
    if not items:
        return None
    return items[0]["id"]["videoId"]


def find_latest_recent_stream_video(youtube, channel_id: str, lookback_hours: int) -> Optional[str]:
    """
    If no current live stream, try to find a very recent video that was a live stream.
    We'll fetch latest videos and inspect liveStreamingDetails.
    """
    published_after = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat()

    req = youtube.search().list(
        part="id",
        channelId=channel_id,
        type="video",
        order="date",
        publishedAfter=published_after,
        maxResults=10,
    )
    resp = req.execute()
    ids = [it["id"]["videoId"] for it in resp.get("items", []) if "videoId" in it.get("id", {})]
    if not ids:
        return None

    vreq = youtube.videos().list(part="liveStreamingDetails,snippet", id=",".join(ids))
    vresp = vreq.execute()
    candidates = []
    for v in vresp.get("items", []):
        lsd = v.get("liveStreamingDetails", {})
        if lsd.get("actualStartTime") or lsd.get("scheduledStartTime"):
            candidates.append(v["id"])
    return candidates[0] if candidates else None


def get_video_details(youtube, video_id: str) -> Dict[str, Any]:
    req = youtube.videos().list(part="snippet,liveStreamingDetails,status,contentDetails", id=video_id)
    resp = req.execute()
    items = resp.get("items", [])
    if not items:
        raise RuntimeError(f"Video not found: {video_id}")
    return items[0]


def is_stream_ended(video_details: Dict[str, Any]) -> bool:
    lsd = video_details.get("liveStreamingDetails", {})
    return bool(lsd.get("actualEndTime"))


YT_DLP_TIMEOUT_SECONDS = int(os.environ.get("YT_DLP_TIMEOUT_SECONDS", "1800"))
FFMPEG_TIMEOUT_SECONDS = int(os.environ.get("FFMPEG_TIMEOUT_SECONDS", "1800"))


def _clean_audio_leftovers(out_dir: str):
    """Remove any prior 'audio.*' files (including .part/.ytdl) so a stale
    leftover from an interrupted download can't be mistaken for this run's
    output."""
    if not os.path.isdir(out_dir):
        return
    for fn in os.listdir(out_dir):
        if fn.startswith("audio."):
            try:
                os.remove(os.path.join(out_dir, fn))
            except OSError:
                pass


def download_audio(video_id: str, out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    _clean_audio_leftovers(out_dir)
    out_tpl = os.path.join(out_dir, "audio.%(ext)s")
    url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = [
        "yt-dlp",
        "-f", "bestaudio[protocol!*=m3u8]/bestaudio/best",
        "--no-playlist",
        "--extractor-args", "youtube:player_client=android",
        "--js-runtimes", "node",
        "--add-header", "Referer:https://www.youtube.com/",
        "-o", out_tpl,
        url
    ]
    try:
        subprocess.check_call(cmd, timeout=YT_DLP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"yt-dlp audio download timed out after {YT_DLP_TIMEOUT_SECONDS}s") from e
    for fn in os.listdir(out_dir):
        if fn.startswith("audio."):
            return os.path.join(out_dir, fn)
    raise RuntimeError("Audio download succeeded but file not found.")


def ffmpeg_to_wav(in_path: str, wav_path: str):
    cmd = [
        "ffmpeg", "-y",
        "-i", in_path,
        "-ac", "1",
        "-ar", "16000",
        wav_path
    ]
    try:
        subprocess.check_call(cmd, timeout=FFMPEG_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"ffmpeg conversion timed out after {FFMPEG_TIMEOUT_SECONDS}s") from e


def transcribe_whisper(wav_path: str, cfg: Dict[str, Any]) -> List[Segment]:
    model_name = cfg["whisper_model"]
    language = cfg.get("language", "en")
    vad_filter = bool(cfg.get("vad_filter", True))

    print(f"Loading Whisper model: {model_name}")
    device = os.environ.get("WHISPER_DEVICE", "cpu")   # cuda|cpu
    compute_type = os.environ.get("WHISPER_COMPUTE_TYPE", "float16" if device == "cuda" else "int8")

    model = WhisperModel(model_name, device=device, compute_type=compute_type)

    print("Starting transcription...")
    segments_iter, _info = model.transcribe(
        wav_path,
        language=language,
        vad_filter=vad_filter,
        word_timestamps=False,
    )

    out: List[Segment] = []
    for idx, s in enumerate(segments_iter):
        if idx % 50 == 0:
            print(f"Transcribed {idx} segments...")
        out.append(Segment(start=float(s.start), end=float(s.end), text=s.text.strip()))

    print("Transcription complete.")
    return out


def segments_to_text(segments: List[Segment]) -> str:
    return "\n".join(s.text for s in segments if s.text.strip())


# ---- Caption-first transcription (avoids downloading audio + running
# Whisper when YouTube already has a usable transcript) ----

def fetch_captions_json3(video_id: str, out_dir: str, langs: List[str]) -> Optional[str]:
    """
    Try to fetch a YouTube caption track (manual first, then auto-generated)
    in YouTube's json3 format via yt-dlp, with no video/audio download.

    json3 is used instead of vtt because YouTube's vtt auto-caption output
    uses a "rolling" cue display (each cue re-renders part of the previous
    line), which causes massive text duplication if concatenated naively.
    json3 is the underlying clean caption data with no such duplication.
    """
    os.makedirs(out_dir, exist_ok=True)
    out_tpl = os.path.join(out_dir, "captions")
    url = f"https://www.youtube.com/watch?v={video_id}"
    lang_arg = ",".join(langs)

    def _run(subs_flag: str) -> Optional[str]:
        cmd = [
            "yt-dlp",
            subs_flag,
            "--sub-lang", lang_arg,
            "--sub-format", "json3",
            "--skip-download",
            "--extractor-args", "youtube:player_client=android",
            "--js-runtimes", "node",
            "-o", out_tpl,
            url,
        ]
        try:
            subprocess.check_call(cmd, timeout=YT_DLP_TIMEOUT_SECONDS)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return None
        for lang in langs:
            candidate = f"{out_tpl}.{lang}.json3"
            if file_exists_nonempty(candidate):
                return candidate
        return None

    return _run("--write-subs") or _run("--write-auto-sub")


def parse_json3_captions(path: str) -> List[Segment]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    segments: List[Segment] = []
    for ev in data.get("events") or []:
        start_ms = ev.get("tStartMs")
        segs = ev.get("segs")
        if start_ms is None or not segs:
            continue
        text = "".join(s.get("utf8", "") for s in segs).strip()
        if not text:
            continue
        start = start_ms / 1000.0
        end = (start_ms + (ev.get("dDurationMs") or 0)) / 1000.0
        segments.append(Segment(start=start, end=max(end, start), text=text))
    return segments


def caption_coverage_ratio(segments: List[Segment], video_duration_seconds: float) -> float:
    if not segments or video_duration_seconds <= 0:
        return 0.0
    span = segments[-1].end - segments[0].start
    return max(0.0, span) / video_duration_seconds


def get_transcript(
    video_id: str,
    media_dir: str,
    cfg: Dict[str, Any],
    video_duration_seconds: float,
) -> Tuple[List[Segment], str]:
    """
    Try YouTube captions first (no audio download needed); fall back to
    downloading audio + running Whisper if captions are unavailable,
    unparseable, or don't cover enough of the video's actual duration.
    Returns (segments, source) where source is "captions" or "whisper".
    """
    force_whisper = os.environ.get("FORCE_WHISPER", "").lower() in ("1", "true", "yes")
    prefer_captions = bool(cfg.get("prefer_captions", True)) and not force_whisper

    if prefer_captions:
        langs = cfg.get("caption_langs", ["en", "en-US", "en-GB"])
        min_coverage = float(cfg.get("min_caption_coverage", 0.85))
        try:
            caption_path = fetch_captions_json3(video_id, media_dir, langs)
            if caption_path:
                segs = parse_json3_captions(caption_path)
                coverage = caption_coverage_ratio(segs, video_duration_seconds)
                print(f"[{utc_now_iso()}] Captions found: {len(segs)} segments, coverage={coverage:.2f}")
                if segs and coverage >= min_coverage:
                    return segs, "captions"
                print(f"[{utc_now_iso()}] Caption coverage below threshold ({min_coverage}); falling back to Whisper.")
            else:
                print(f"[{utc_now_iso()}] No captions available; falling back to Whisper.")
        except Exception as e:
            print(f"[{utc_now_iso()}] Caption fetch/parse failed ({e}); falling back to Whisper.")

    wav_path = os.path.join(media_dir, "audio_16k.wav")
    if file_exists_nonempty(wav_path):
        print(f"[{utc_now_iso()}] Found existing WAV, skipping download/ffmpeg: {wav_path}")
    else:
        audio_path = download_audio(video_id, media_dir)
        ffmpeg_to_wav(audio_path, wav_path)

    segs = transcribe_whisper(wav_path, cfg)
    return segs, "whisper"



def parse_timestamp_to_seconds(value: str) -> float:
    """
    Parse a timestamp into seconds.
    Accepts:
      - seconds as int/float string: "1234" or "1234.5"
      - HH:MM:SS or H:MM:SS
      - MM:SS
    """
    v = (value or "").strip()
    if not v:
        raise ValueError("Empty timestamp")
    # numeric seconds
    try:
        return float(v)
    except ValueError:
        pass

    parts = v.split(":")
    if len(parts) == 2:  # MM:SS
        mm, ss = parts
        return int(mm) * 60 + float(ss)
    if len(parts) == 3:  # HH:MM:SS
        hh, mm, ss = parts
        return int(hh) * 3600 + int(mm) * 60 + float(ss)

    raise ValueError(f"Unrecognized timestamp format: {value!r}")


_ISO8601_DURATION_RE = re.compile(
    r"^PT(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?$"
)


def parse_iso8601_duration(value: str) -> float:
    """Parse a YouTube contentDetails.duration string (e.g. 'PT1H12M4S') into seconds."""
    m = _ISO8601_DURATION_RE.match((value or "").strip())
    if not m:
        raise ValueError(f"Unrecognized ISO-8601 duration: {value!r}")
    hours = int(m.group("hours") or 0)
    minutes = int(m.group("minutes") or 0)
    seconds = float(m.group("seconds") or 0)
    return hours * 3600 + minutes * 60 + seconds


def chunk_text_by_chars(text: str, max_chars: int = 5000) -> List[str]:
    """
    Simple, reliable chunking without a tokenizer.
    Keeps chunks under max_chars, splitting on paragraph boundaries when possible.
    """
    text = text.strip()
    if len(text) <= max_chars:
        return [text]

    paras = [p.strip() for p in text.split("\n") if p.strip()]
    chunks: List[str] = []
    cur: List[str] = []
    cur_len = 0

    for p in paras:
        p_len = len(p) + 1
        if cur_len + p_len > max_chars and cur:
            chunks.append("\n".join(cur).strip())
            cur = [p]
            cur_len = p_len
        else:
            cur.append(p)
            cur_len += p_len

    if cur:
        chunks.append("\n".join(cur).strip())

    return [c for c in chunks if c]


def llm_chat(llm: Llama, messages: List[Dict[str, str]], cfg: Dict[str, Any]) -> str:
    temperature = float(cfg.get("temperature", 0.6))
    top_p = float(cfg.get("top_p", 0.9))
    max_tokens = int(cfg.get("max_tokens", 1400))
    resp = llm.create_chat_completion(
        messages=messages,
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
    )
    return resp["choices"][0]["message"]["content"].strip()


def init_llm(cfg: Dict[str, Any]) -> Llama:
    model_path = cfg["model_path"]
    if not model_path or not os.path.exists(model_path):
        raise RuntimeError(f"LLM model_path does not exist: {model_path}")
    return Llama(
        model_path=model_path,
        n_ctx=int(cfg.get("n_ctx", 4096)),
        n_threads=int(os.environ.get("LLM_THREADS", "8")),
        n_gpu_layers=int(os.environ.get("LLM_N_GPU_LAYERS", "0")),
        verbose=False,
    )


# ---- YAML article parsing/repair ----
def _extract_yaml_block(text: str) -> str:
    t = text.strip().replace("\t", "  ")

    # Strip markdown fences if present
    if t.startswith("```"):
        # drop first fence line
        t = t.split("\n", 1)[-1]
        # drop trailing fence
        if "```" in t:
            t = t.rsplit("```", 1)[0].strip()

    # Strip common leading chatter
    lower = t.lower()
    for prefix in ["here is the yaml:", "yaml:", "output:", "result:"]:
        if lower.startswith(prefix):
            t = t[len(prefix):].lstrip()
            break

    return t


def parse_article_yaml(yaml_text: str) -> Dict[str, Any]:
    data = yaml.safe_load(_extract_yaml_block(yaml_text))
    if not isinstance(data, dict):
        raise ValueError("YAML did not parse into a dict/object.")
    return data


def repair_yaml(llm: Llama, bad: str, cfg: Dict[str, Any]) -> str:
    repair_system = (
        "You are a strict YAML formatter. Output ONLY valid YAML. "
        "No markdown. No commentary. No extra keys."
    )

    repair_user = f"""Convert the following into VALID YAML that matches this EXACT schema and key names.

REQUIRED YAML (use these keys EXACTLY, lowercase with underscores):
title_options:
  - "..."
  - "..."
  - "..."
  - "..."
  - "..."
chosen_title: "..."
meta_title: "..."
meta_description: "..."
slug: "kebab-case-slug"
primary_keyword: "..."
secondary_keywords:
  - "..."
  - "..."
  - "..."
  - "..."
  - "..."
featured_image_prompt: "..."
outline:
  - "H2 ..."
  - "H3 ..."
article_html: |
  <h2>...</h2>
  <p>...</p>
scripture_references:
  - "John 3:16 KJV"
takeaway_points:
  - "..."
  - "..."
  - "..."
  - "..."
  - "..."

RULES (non-negotiable):
- Output ONLY YAML.
- Do NOT output any free-floating title lines. Every line must belong to a key/value.
- Use exactly 2 spaces indentation. No tabs.
- article_html MUST use the block scalar (|) exactly as shown.
- Keep content complete; do not truncate.

INPUT TO FIX:
{bad}
"""
    return llm_chat(
        llm,
        [
            {"role": "system", "content": repair_system},
            {"role": "user", "content": repair_user},
        ],
        {**cfg, "temperature": 0.0, "top_p": 1.0, "max_tokens": 2600},
    )


def normalize_article_schema(article: Dict[str, Any]) -> Dict[str, Any]:
    """
    Make the model output safe + predictable for downstream usage.
    No FAQ. YAML-first output is converted to dict then normalized.
    """

    def ensure_str(x) -> str:
        return x if isinstance(x, str) else ("" if x is None else str(x))

    def ensure_list(x):
        return x if isinstance(x, list) else []

    defaults = {
        "title_options": [],
        "chosen_title": "",
        "meta_title": "",
        "meta_description": "",
        "slug": "",
        "primary_keyword": "",
        "secondary_keywords": [],
        "featured_image_prompt": "",
        "outline": [],
        "article_html": "",
        "scripture_references": [],
        "takeaway_points": [],
    }

    for k, v in defaults.items():
        if k not in article or article[k] is None:
            article[k] = v

    for k in ["title_options", "secondary_keywords", "outline", "scripture_references", "takeaway_points"]:
        article[k] = ensure_list(article.get(k))

    for k in [
        "chosen_title", "meta_title", "meta_description", "slug",
        "primary_keyword", "featured_image_prompt", "article_html"
    ]:
        article[k] = ensure_str(article.get(k))

    # light enforcement
    if len(article["title_options"]) > 5:
        article["title_options"] = article["title_options"][:5]
    if len(article["takeaway_points"]) > 5:
        article["takeaway_points"] = article["takeaway_points"][:5]

    return article


def llm_generate_article(llm: Llama, sermon_text: str, cfg: Dict[str, Any], job_dir: str) -> Dict[str, Any]:
    """
    YAML-first, context-safe generation:
    - If sermon is long, chunk it and summarize chunks first (map step).
    - Cache the combined notes to notes.txt so retries don't redo chunking.
    - Then generate the final YAML article from the combined notes (reduce step).
    - If YAML is invalid, do one YAML repair pass and parse again.
    - Enforces first-person pastor voice regardless of prompt drift.
    """
    os.makedirs(job_dir, exist_ok=True)

    notes_path = os.path.join(job_dir, "notes.txt")
    raw_yaml_path = os.path.join(job_dir, "article_raw.yaml")
    repaired_yaml_path = os.path.join(job_dir, "article_repaired.yaml")

    def _fill_template(template: str, text: str) -> str:
        # Avoid Python .format() entirely to prevent brace/key errors.
        return template.replace("{sermon_text}", text)

    # --- Step 1: get or build combined notes ---
    combined_notes: str = ""

    if file_exists_nonempty(notes_path):
        print(f"[{utc_now_iso()}] Found existing notes.txt, skipping chunk summarization.")
        with open(notes_path, "r", encoding="utf-8") as f:
            combined_notes = f.read().strip()
    else:
        chunks = chunk_text_by_chars(
            sermon_text,
            max_chars=int(os.environ.get("SERMON_CHUNK_CHARS", "5000"))
        )

        if len(chunks) == 1:
            combined_notes = ""
        else:
            print(f"[{utc_now_iso()}] Sermon is long; summarizing in {len(chunks)} chunks to fit context.")
            notes_all: List[str] = []
            for i, ch in enumerate(chunks, start=1):
                print(f"[{utc_now_iso()}] Summarizing chunk {i}/{len(chunks)}...")
                chunk_notes = llm_chat(
                    llm,
                    [
                        {"role": "system", "content": CHUNK_SUMMARY_SYSTEM},
                        {"role": "user", "content": ch},
                    ],
                    {**cfg, "max_tokens": 450, "temperature": 0.2}
                )
                notes_all.append(f"CHUNK {i} NOTES:\n{chunk_notes}".strip())

            combined_notes = "\n\n".join(notes_all).strip()
            with open(notes_path, "w", encoding="utf-8") as f:
                f.write(combined_notes)
            print(f"[{utc_now_iso()}] Wrote notes cache: {notes_path}")


    def _regen_missing_html(notes_text: str) -> Dict[str, Any]:
        fix_system = "You output ONLY valid YAML matching the required schema."
        fix_user = f"""Your prior output was missing article_html (or it was empty/too short).
Return VALID YAML with the EXACT required keys, and ensure article_html is a long 5–10 minute read (target 1,200–1,800 words).
Use only allowed HTML tags (<h2>, <h3>, <p>, <ul>, <li>, <blockquote>).
Return ONLY YAML.

Use these sermon notes:
{notes_text}
"""
        y = llm_chat(
            llm,
            [{"role": "system", "content": fix_system}, {"role": "user", "content": fix_user}],
            {**cfg, "temperature": 0.2, "top_p": 0.9, "max_tokens": 2600},
        )
        obj = normalize_article_schema(parse_article_yaml(y))
        return obj

    # --- Step 2: Generate final YAML article ---
    chunks = chunk_text_by_chars(
        sermon_text,
        max_chars=int(os.environ.get("SERMON_CHUNK_CHARS", "5000"))
    )

    if len(chunks) == 1:
        user_prompt = _fill_template(USER_PROMPT_TEMPLATE, chunks[0][:120000])
        user_prompt = FIRST_PERSON_ENFORCER_USER + "\n" + user_prompt

        print(f"[{utc_now_iso()}] Generating article YAML from sermon text (single chunk)...")
        content = llm_chat(
            llm,
            [
                {"role": "system", "content": FIRST_PERSON_ENFORCER_SYSTEM + SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            cfg
        )
    else:
        reduced_input = (
            "Use the following sermon notes (summarized from the full transcript) to write the article.\n\n"
            + combined_notes
        )
        final_prompt = _fill_template(USER_PROMPT_TEMPLATE, reduced_input[:120000])
        final_prompt = FIRST_PERSON_ENFORCER_USER + "\n" + final_prompt

        print(f"[{utc_now_iso()}] Generating final article YAML from summarized notes...")
        content = llm_chat(
            llm,
            [
                {"role": "system", "content": FIRST_PERSON_ENFORCER_SYSTEM + SYSTEM_PROMPT},
                {"role": "user", "content": final_prompt},
            ],
            # YAML is more reliable with lower temperature
            {**cfg, "temperature": min(float(cfg.get("temperature", 0.6)), 0.4)}
        )

    # Save raw YAML output
    try:
        with open(raw_yaml_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"[{utc_now_iso()}] Wrote raw LLM YAML output: {raw_yaml_path}")
    except Exception:
        pass

    # --- Step 3: Parse YAML with one repair attempt ---
    try:
        article_obj = normalize_article_schema(parse_article_yaml(content))
        if len(article_obj.get("article_html", "").strip()) < 200:
            print(f"[{utc_now_iso()}] article_html was empty/too short; regenerating once...")
            article_obj = _regen_missing_html(combined_notes[:120000])
        return article_obj
    except Exception as e:
        print(f"[{utc_now_iso()}] LLM output was not valid YAML; attempting one repair pass... ({e})")
        fixed = repair_yaml(llm, content, cfg)

        try:
            with open(repaired_yaml_path, "w", encoding="utf-8") as f:
                f.write(fixed)
            print(f"[{utc_now_iso()}] Wrote repaired LLM YAML output: {repaired_yaml_path}")
        except Exception:
            pass

        article_obj = normalize_article_schema(parse_article_yaml(fixed))
        if len(article_obj.get("article_html", "").strip()) < 200:
            print(f"[{utc_now_iso()}] article_html was empty/too short after repair; regenerating once...")
            article_obj = _regen_missing_html(combined_notes[:120000])
        return article_obj


def write_json(path: str, obj: Any):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def write_text(path: str, txt: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(txt)


def run_pipeline(cfg: Dict[str, Any], store: StateStore):
    ensure_tools()

    out_root = cfg["output"]["dir"]
    os.makedirs(out_root, exist_ok=True)

    override_video_id = os.environ.get("VIDEO_ID")
    override_title = os.environ.get("VIDEO_TITLE")
    override_duration = os.environ.get("VIDEO_DURATION_SECONDS")
    override_end_time = os.environ.get("VIDEO_END_TIME", "")

    if override_video_id and override_title and override_duration:
        # Worker path: the Poller Lambda already resolved this video and
        # passed its metadata through (see bootstrap.sh), so skip the
        # YouTube Data API entirely -- the worker instance needs no
        # YOUTUBE_API_KEY / Secrets Manager access at all.
        candidate_id = override_video_id
        title = override_title
        end_time = override_end_time
        video_duration_seconds = float(override_duration)
        print(f"[{utc_now_iso()}] Worker mode: video_id={candidate_id} title={title!r} (no YouTube API call)")
    else:
        api_key = env_or(cfg["youtube"].get("api_key", ""), "YOUTUBE_API_KEY")
        channel_id = env_or(cfg["youtube"].get("channel_id", ""), "CHANNEL_ID")
        if not api_key or not channel_id:
            raise RuntimeError("Missing YOUTUBE_API_KEY and/or CHANNEL_ID")
        youtube = yt_api(api_key)

        if override_video_id:
            print(f"[{utc_now_iso()}] VIDEO_ID override detected: {override_video_id}")
            candidate_id = override_video_id
        else:
            live_id = find_latest_live_video(youtube, channel_id)
            if live_id:
                live_details = get_video_details(youtube, live_id)
                if not is_stream_ended(live_details):
                    print(f"[{utc_now_iso()}] Live now: {live_id} — {live_details['snippet']['title']}")
                    return

            candidate_id = live_id or find_latest_recent_stream_video(
                youtube, channel_id, int(cfg["youtube"].get("lookback_hours", 48))
            )
            if not candidate_id:
                print(f"[{utc_now_iso()}] No recent livestream candidate found.")
                return

        details = get_video_details(youtube, candidate_id)
        title = details["snippet"]["title"]
        end_time = details.get("liveStreamingDetails", {}).get("actualEndTime", "")

        if not override_video_id and not end_time:
            print(f"[{utc_now_iso()}] Candidate not ended yet: {candidate_id} — {title}")
            return
        if override_video_id:
            if end_time:
                print(f"[{utc_now_iso()}] Override mode — processing ended livestream: {candidate_id} — {title} (ended {end_time})")
            else:
                print(f"[{utc_now_iso()}] Override mode — processing video regardless of livestream status: {candidate_id} — {title}")

        try:
            video_duration_seconds = parse_iso8601_duration(
                details.get("contentDetails", {}).get("duration", "")
            )
        except ValueError:
            video_duration_seconds = 0.0

    # If it's fully processed already, skip (unless FORCE_REPROCESS=true)
    force = os.environ.get("FORCE_REPROCESS", "").lower() in ("1", "true", "yes")
    if (not force) and store.is_processed(candidate_id):
        print(f"[{utc_now_iso()}] Already processed: {candidate_id}")
        return

    if end_time:
        print(f"[{utc_now_iso()}] Processing ended livestream: {candidate_id} — {title} (ended {end_time})")
    else:
        print(f"[{utc_now_iso()}] Processing video: {candidate_id} — {title}")

    job_dir = os.path.join(out_root, candidate_id)
    os.makedirs(job_dir, exist_ok=True)

    media_dir = os.path.join(job_dir, "media")
    os.makedirs(media_dir, exist_ok=True)

    # 1) get transcript: try YouTube captions first (no audio download),
    #    fall back to download + Whisper (skip entirely if transcript
    #    artifacts already exist from a prior run)
    transcript_json_path = os.path.join(job_dir, "transcript.json")
    transcript_txt_path = os.path.join(job_dir, "transcript.txt")
    transcript_meta_path = os.path.join(job_dir, "transcript_meta.json")

    if file_exists_nonempty(transcript_json_path):
        print(f"[{utc_now_iso()}] Found existing transcript.json, skipping transcription.")
        with open(transcript_json_path, "r", encoding="utf-8") as f:
            transcript_json = json.load(f)
        segs = [Segment(start=s["start"], end=s["end"], text=s["text"]) for s in transcript_json]
        if not file_exists_nonempty(transcript_txt_path):
            write_text(transcript_txt_path, segments_to_text(segs))
    else:
        segs, transcript_source = get_transcript(
            candidate_id, media_dir, cfg["transcription"], video_duration_seconds
        )
        transcript_json = [{"start": s.start, "end": s.end, "text": s.text} for s in segs]
        write_json(transcript_json_path, transcript_json)
        write_text(transcript_txt_path, segments_to_text(segs))
        write_json(transcript_meta_path, {
            "source": transcript_source,
            "video_duration_seconds": video_duration_seconds,
        })
        print(f"[{utc_now_iso()}] Transcript source: {transcript_source}")

        # 3) sermon extraction (skip if sermon artifacts exist)
    sermon_json_path = os.path.join(job_dir, "sermon.json")
    sermon_txt_path = os.path.join(job_dir, "sermon.txt")

    # Optional override: user-provided sermon start (and optional end) timestamp
    # Examples:
    #   SERMON_START=1250           (seconds)
    #   SERMON_START=20:50          (MM:SS)
    #   SERMON_START=1:02:30        (H:MM:SS)
    # Optional:
    #   SERMON_END=...              (same formats)
    sermon_start_override = os.environ.get("SERMON_START") or os.environ.get("SERMON_START_SECONDS")
    sermon_end_override = os.environ.get("SERMON_END") or os.environ.get("SERMON_END_SECONDS")
    force_sermon_override = os.environ.get("FORCE_SERMON", "").lower() in ("1", "true", "yes")

    if file_exists_nonempty(sermon_json_path) and file_exists_nonempty(sermon_txt_path) and not force_sermon_override and not sermon_start_override:
        print(f"[{utc_now_iso()}] Found existing sermon.json/sermon.txt, skipping sermon extraction.")
        with open(sermon_txt_path, "r", encoding="utf-8") as f:
            sermon_text = f.read()
    else:
        if sermon_start_override:
            try:
                start_sec = float(parse_timestamp_to_seconds(sermon_start_override))
            except Exception as e:
                raise RuntimeError(f"Invalid SERMON_START value {sermon_start_override!r}: {e}")

            end_sec: float | None = None
            if sermon_end_override:
                try:
                    end_sec = float(parse_timestamp_to_seconds(sermon_end_override))
                except Exception as e:
                    raise RuntimeError(f"Invalid SERMON_END value {sermon_end_override!r}: {e}")

            print(f"[{utc_now_iso()}] Using SERMON_START override at {start_sec:.2f}s" + (f" and SERMON_END {end_sec:.2f}s" if end_sec is not None else ""))

            sermon_segs = []
            for s in segs:
                if float(s.end) <= start_sec:
                    continue
                if end_sec is not None and float(s.start) >= end_sec:
                    break
                sermon_segs.append(s)

            if not sermon_segs:
                raise RuntimeError("SERMON_START override produced zero segments; check timestamp.")

            sermon_start = start_sec
            sermon_end = float(sermon_segs[-1].end) if end_sec is None else end_sec

            sermon_json = {
                "start_seconds": sermon_start,
                "end_seconds": sermon_end,
                "segments": [{"start": s.start, "end": s.end, "text": s.text} for s in sermon_segs],
                "override": {
                    "sermon_start": sermon_start_override,
                    "sermon_end": sermon_end_override or "",
                },
            }
            sermon_text = segments_to_text(sermon_segs)
            write_json(sermon_json_path, sermon_json)
            write_text(sermon_txt_path, sermon_text)
        else:
            sermon_cfg = cfg["sermon_extraction"]
            sermon_segs, sermon_start, sermon_end = extract_sermon(segs, sermon_cfg)
            sermon_json = {
                "start_seconds": sermon_start,
                "end_seconds": sermon_end,
                "segments": [{"start": s.start, "end": s.end, "text": s.text} for s in sermon_segs],
            }
            sermon_text = segments_to_text(sermon_segs)
            write_json(sermon_json_path, sermon_json)
            write_text(sermon_txt_path, sermon_text)


    # 4) local LLM article generation (skip if article artifacts exist)
    # Controls:
    #   FORCE_ARTICLE=true  -> regenerate article from notes/sermon_text even if article.json exists
    #   FORCE_FROM_NOTES=true -> alias for FORCE_ARTICLE (use cached notes.txt if present)
    # Notes:
    # - If article.json exists but article.html is missing, we regenerate article.html from article.json
    article_json_path = os.path.join(job_dir, "article.json")
    html_path = os.path.join(job_dir, "article.html")

    force_article = os.environ.get("FORCE_ARTICLE", "").lower() in ("1", "true", "yes")
    if os.environ.get("FORCE_FROM_NOTES", "").lower() in ("1", "true", "yes"):
        force_article = True

    article: Dict[str, Any]

    if (not force_article) and file_exists_nonempty(article_json_path):
        print(f"[{utc_now_iso()}] Found existing article.json, skipping LLM generation.")
        with open(article_json_path, "r", encoding="utf-8") as f:
            article = json.load(f)
    else:
        llm_cfg = cfg["llm"].copy()
        llm_cfg["model_path"] = env_or(llm_cfg.get("model_path", ""), "LLM_MODEL_PATH")
        llm = init_llm(llm_cfg)
        article = llm_generate_article(llm, sermon_text, llm_cfg, job_dir)
        write_json(article_json_path, article)

    # Ensure we have a standalone HTML file (even if we didn't run LLM this time)
    if not file_exists_nonempty(html_path):
        html_doc = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<title>{article.get("chosen_title","Sermon Article")}</title>
<meta name="description" content="{article.get("meta_description","")}"/>
</head>
<body>
<h1>{article.get("chosen_title","")}</h1>
{article.get("article_html","")}
</body>
</html>
"""
        write_text(html_path, html_doc)
        print(f"[{utc_now_iso()}] Wrote article HTML: {html_path}")

    # 5) render PDF (skip if already exists)
    pdf_path = os.path.join(job_dir, "sermon-article.pdf")
    force_pdf = os.environ.get("FORCE_PDF", "").lower() in ("1", "true", "yes")
    if file_exists_nonempty(pdf_path) and not force_pdf:
        print(f"[{utc_now_iso()}] Found existing PDF, skipping render: {pdf_path}")
    else:
        if force_pdf and file_exists_nonempty(pdf_path):
            print(f"[{utc_now_iso()}] FORCE_PDF enabled; re-rendering PDF: {pdf_path}")
        render_pdf(
            pdf_path=pdf_path,
            title=article.get("chosen_title", "Sermon Article"),
            meta_description=article.get("meta_description", ""),
            html_body=article.get("article_html", ""),
            scripture_refs=article.get("scripture_references", []),
            takeaways=article.get("takeaway_points", []),
        )

    # Mark processed only once we have the PDF output
    store.mark_processed(candidate_id, utc_now_iso(), end_time or "", title)
    print(f"[{utc_now_iso()}] Done. PDF: {pdf_path}")


def main():
    """
    Single-shot entrypoint: process one job and exit. There is no watch/poll
    loop anymore -- discovering new videos to process is the Poller Lambda's
    job (see poller_lambda.py); this process is launched by bootstrap.sh on
    an ephemeral worker instance for exactly one VIDEO_ID (or, for local/
    manual runs, VIDEO_ID may be omitted and run_pipeline() will fall back
    to discovering the channel's most recently ended livestream itself).
    An unhandled exception here is intentionally left to propagate --
    bootstrap.sh is responsible for catching it, recording the failure in
    DynamoDB, and still terminating the instance.
    """
    cfg_path = os.environ.get("CONFIG_PATH", "/app/config.yaml")
    if not os.path.exists(cfg_path):
        raise RuntimeError(f"Config not found at {cfg_path}. Copy config.yaml.example to config.yaml and mount it.")

    cfg = load_config(cfg_path)
    out_dir = cfg["output"]["dir"]
    os.makedirs(out_dir, exist_ok=True)

    store = StateStore()
    run_pipeline(cfg, store)


if __name__ == "__main__":
    main()
