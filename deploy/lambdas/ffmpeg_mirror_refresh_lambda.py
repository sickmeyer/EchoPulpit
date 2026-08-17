"""
FFmpeg Mirror Refresh Lambda -- runs on a low-frequency EventBridge
schedule (weekly by default) to keep our S3-hosted ffmpeg mirror
(s3://<bucket>/deps/ffmpeg-static-linux-amd64.tar.xz) from becoming a
permanently-pinned stale snapshot.

The mirror exists so worker instances (bootstrap.sh's fetch_ffmpeg())
never depend on johnvansickle.com's uptime for something as basic as
"does the pipeline run at all" -- see bootstrap.sh for the outage
(2026-08-17) that motivated it. This Lambda is what keeps that mirror
current: it re-pulls upstream, verifies the result is actually a static
(no dynamic linker required) binary before trusting it, and only then
replaces the S3 copy. A download or verification failure (e.g. upstream
is down -- the exact same failure mode that motivated the mirror) leaves
the existing mirror untouched rather than replacing a known-good copy
with nothing or something broken.
"""
import os
import tarfile
import tempfile
import urllib.request

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
ARTIFACTS_BUCKET = os.environ["SERMON_ARTIFACTS_BUCKET"]
MIRROR_KEY = os.environ.get("FFMPEG_MIRROR_KEY", "deps/ffmpeg-static-linux-amd64.tar.xz")
UPSTREAM_URL = os.environ.get(
    "FFMPEG_UPSTREAM_URL",
    "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
)

_s3 = boto3.client("s3", region_name=REGION)


def _is_static_elf(path: str) -> bool:
    """
    True if the ELF file has no PT_INTERP program header, i.e. no dynamic
    linker is required to run it. Parsed by hand (no pyelftools dependency,
    and we can't just shell out to `file`/`ldd` -- this binary targets a
    different OS than the Lambda runtime, so it can't be executed here
    either). PT_INTERP's type constant (3) is stable across ELF versions.
    """
    with open(path, "rb") as f:
        header = f.read(64)
        if header[:4] != b"\x7fELF":
            raise ValueError(f"{path}: not an ELF file")
        is_64 = header[4] == 2
        if is_64:
            e_phoff = int.from_bytes(header[32:40], "little")
            e_phentsize = int.from_bytes(header[54:56], "little")
            e_phnum = int.from_bytes(header[56:58], "little")
        else:
            e_phoff = int.from_bytes(header[28:32], "little")
            e_phentsize = int.from_bytes(header[42:44], "little")
            e_phnum = int.from_bytes(header[44:46], "little")
        f.seek(e_phoff)
        ph_table = f.read(e_phentsize * e_phnum)
        for i in range(e_phnum):
            entry = ph_table[i * e_phentsize:(i + 1) * e_phentsize]
            p_type = int.from_bytes(entry[0:4], "little")
            if p_type == 3:  # PT_INTERP
                return False
        return True


def lambda_handler(event, context):
    with tempfile.TemporaryDirectory() as tmp:
        downloaded = os.path.join(tmp, "upstream.tar.xz")
        try:
            urllib.request.urlretrieve(UPSTREAM_URL, downloaded)
        except Exception as e:
            print(f"WARNING: upstream download failed ({e}); leaving existing mirror in place")
            return {"refreshed": False, "reason": f"download failed: {e}"}

        extract_dir = os.path.join(tmp, "extract")
        os.makedirs(extract_dir, exist_ok=True)
        with tarfile.open(downloaded, "r:xz") as tf:
            members = [m for m in tf.getmembers() if m.name.rsplit("/", 1)[-1] in ("ffmpeg", "ffprobe")]
            if len(members) != 2:
                names = [m.name for m in members]
                print(f"WARNING: expected ffmpeg+ffprobe in upstream archive, found {names}; "
                      "leaving existing mirror in place")
                return {"refreshed": False, "reason": "unexpected archive layout"}
            tf.extractall(extract_dir, members=members)

        ffmpeg_path = ffprobe_path = None
        for root, _dirs, files in os.walk(extract_dir):
            for fn in files:
                if fn == "ffmpeg":
                    ffmpeg_path = os.path.join(root, fn)
                elif fn == "ffprobe":
                    ffprobe_path = os.path.join(root, fn)

        if not ffmpeg_path or not ffprobe_path:
            print("WARNING: ffmpeg/ffprobe not found after extraction; leaving existing mirror in place")
            return {"refreshed": False, "reason": "binaries not found in archive"}

        for label, path in (("ffmpeg", ffmpeg_path), ("ffprobe", ffprobe_path)):
            if not _is_static_elf(path):
                print(f"WARNING: upstream {label} is dynamically linked (unexpected for this source); "
                      "leaving existing mirror in place")
                return {"refreshed": False, "reason": f"{label} not statically linked"}

        repack_dir = os.path.join(tmp, "ffmpeg-static")
        os.makedirs(repack_dir, exist_ok=True)
        os.rename(ffmpeg_path, os.path.join(repack_dir, "ffmpeg"))
        os.rename(ffprobe_path, os.path.join(repack_dir, "ffprobe"))
        os.chmod(os.path.join(repack_dir, "ffmpeg"), 0o755)
        os.chmod(os.path.join(repack_dir, "ffprobe"), 0o755)

        repacked = os.path.join(tmp, "ffmpeg-static-linux-amd64.tar.xz")
        with tarfile.open(repacked, "w:xz") as tf:
            tf.add(repack_dir, arcname="ffmpeg-static")

        _s3.upload_file(repacked, ARTIFACTS_BUCKET, MIRROR_KEY)
        print(f"Refreshed s3://{ARTIFACTS_BUCKET}/{MIRROR_KEY} from {UPSTREAM_URL}")
        return {"refreshed": True}
