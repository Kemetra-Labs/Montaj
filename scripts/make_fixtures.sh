#!/usr/bin/env bash
set -euo pipefail

output_dir="${1:-tests/generated_media}"
mkdir -p -- "$output_dir"

ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i 'sine=frequency=440:sample_rate=16000:duration=4' \
  -ac 1 -c:a pcm_s16le "$output_dir/tone.wav"

ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i 'testsrc2=size=320x180:rate=12:duration=4' \
  -i "$output_dir/tone.wav" -c:v libx264 -threads:v 1 -pix_fmt yuv420p \
  -c:a aac -shortest "$output_dir/pattern.mp4"
