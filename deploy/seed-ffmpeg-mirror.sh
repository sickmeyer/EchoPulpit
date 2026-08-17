#!/usr/bin/env bash
#
# One-time (per architecture) seed of the ffmpeg mirror bootstrap.sh's
# fetch_ffmpeg() tries first: s3://$ARTIFACTS_BUCKET/deps/ffmpeg-static-linux-<arch>.tar.xz
#
# Optional, not required: if this is never run, every worker instance just
# falls back to downloading directly from johnvansickle.com at boot (the
# original behavior, before the mirror existed) until the ffmpeg-mirror-
# refresh Lambda's own weekly schedule happens to succeed and populates it
# for you. Run this if you want mirror-backed reliability starting with
# your very first job instead of waiting up to a week.
#
# Usage:
#   export ARTIFACTS_BUCKET=your-bucket-name
#   export AWS_REGION=us-east-1          # optional, defaults below
#   export WORKER_ARCH=arm64             # optional, must match setup.sh's WORKER_ARCH
#   ./deploy/seed-ffmpeg-mirror.sh
set -euo pipefail

: "${ARTIFACTS_BUCKET:?set ARTIFACTS_BUCKET}"
REGION="${AWS_REGION:-us-east-1}"
ARCH="${WORKER_ARCH:-arm64}"

case "$ARCH" in
  x86_64) UPSTREAM_SUFFIX="amd64" ;;
  arm64)  UPSTREAM_SUFFIX="arm64" ;;
  *) echo "Unrecognized WORKER_ARCH '$ARCH' (expected x86_64 or arm64)" >&2; exit 1 ;;
esac

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

echo "Downloading ffmpeg static build (${UPSTREAM_SUFFIX}) from johnvansickle.com..."
curl -sSL --connect-timeout 10 --max-time 300 \
  -o "$TMP_DIR/ffmpeg.tar.xz" \
  "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-${UPSTREAM_SUFFIX}-static.tar.xz"

mkdir -p "$TMP_DIR/extract" "$TMP_DIR/pkg/ffmpeg-static"
tar -xJf "$TMP_DIR/ffmpeg.tar.xz" -C "$TMP_DIR/extract" --strip-components=1
cp "$TMP_DIR/extract/ffmpeg" "$TMP_DIR/extract/ffprobe" "$TMP_DIR/pkg/ffmpeg-static/"
chmod +x "$TMP_DIR/pkg/ffmpeg-static/ffmpeg" "$TMP_DIR/pkg/ffmpeg-static/ffprobe"
tar -cJf "$TMP_DIR/ffmpeg-static-linux-${ARCH}.tar.xz" -C "$TMP_DIR/pkg" ffmpeg-static

aws s3 cp "$TMP_DIR/ffmpeg-static-linux-${ARCH}.tar.xz" \
  "s3://${ARTIFACTS_BUCKET}/deps/ffmpeg-static-linux-${ARCH}.tar.xz" --region "$REGION"

echo "Seeded s3://${ARTIFACTS_BUCKET}/deps/ffmpeg-static-linux-${ARCH}.tar.xz"
