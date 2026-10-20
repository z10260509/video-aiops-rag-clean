#!/usr/bin/env bash
set -euo pipefail

base_url="${1:-http://localhost:8080}"
job_count="${2:-20}"
video_codec="${3:-h264}"
compression_preset="${4:-balanced}"
source_video="${5:-}"
duration_seconds="${VIDEO_DURATION_SECONDS:-20}"
sample_video=""
generated_video="false"

cleanup() {
  if [ "$generated_video" = "true" ]; then
    rm -f "$sample_video"
  fi
}
trap cleanup EXIT

command -v ffmpeg > /dev/null || {
  echo "ffmpeg is required" >&2
  exit 1
}
command -v curl > /dev/null || {
  echo "curl is required" >&2
  exit 1
}

case "$video_codec" in h264 | h265) ;; *)
  echo "video codec must be h264 or h265" >&2
  exit 2
  ;;
esac
case "$compression_preset" in high | balanced | small) ;; *)
  echo "compression preset must be high, balanced, or small" >&2
  exit 2
  ;;
esac

if [ -n "$source_video" ]; then
  [ -f "$source_video" ] && [ -r "$source_video" ] || {
    echo "source video is not readable: $source_video" >&2
    exit 2
  }
  case "${source_video,,}" in *.mp4) ;; *)
    echo "source video must be an MP4 file" >&2
    exit 2
    ;;
  esac
  sample_video="$source_video"
  source_description="custom source: $source_video"
else
  sample_video="$(mktemp --suffix=.mp4)"
  generated_video="true"
  ffmpeg -y -f lavfi -i testsrc2=size=1280x720:rate=30 \
    -t "$duration_seconds" -c:v libx264 -pix_fmt yuv420p "$sample_video" -loglevel error
  source_description="generated ${duration_seconds}s 720p test pattern"
fi

echo "Submitting $job_count jobs to $base_url/api/jobs (720p, $video_codec, $compression_preset; $source_description)"
for _ in $(seq 1 "$job_count"); do
  curl --fail --silent --show-error -X POST "$base_url/api/jobs" \
    -F "file=@$sample_video;type=video/mp4" \
    -F operation=transcode \
    -F target_resolution=720p \
    -F "video_codec=$video_codec" \
    -F "compression_preset=$compression_preset" &
done
wait

echo
echo "Watch scaling with: kubectl get hpa,pods -n video-platform -w"
