#!/usr/bin/env bash
# Compress a local video to a WhatsApp-friendly H.264/AAC MP4.
#
# Usage:
#   ./compress_for_whatsapp.sh [--force] INPUT [OUTPUT] [TARGET_MB] [SAFETY] [PRESET]
#
# TARGET_MB uses MiB (1024 * 1024 bytes). SAFETY defaults to 0.90 so the
# encoded file stays below the requested limit with room for container overhead.

set -euo pipefail

MIN_VIDEO_KBPS=150
FORCE=0

usage() {
  cat <<'USAGE'
Usage:
  compress_for_whatsapp.sh [--force] INPUT [OUTPUT] [TARGET_MB] [SAFETY] [PRESET]

Defaults:
  OUTPUT    INPUT with its extension replaced by _wa.mp4
  TARGET_MB 64
  SAFETY    0.90
  PRESET    slow

The output is an MP4 with H.264 High Profile, AAC audio, yuv420p, and faststart.
Existing output files are not overwritten unless --force is supplied.
USAGE
}

if [ "$#" -eq 0 ] || [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  usage
  [ "$#" -eq 0 ] && exit 2
  exit 0
fi

POSITIONAL=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --force)
      FORCE=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    --*)
      echo "error: unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      POSITIONAL+=("$1")
      shift
      ;;
  esac
done

if [ "${#POSITIONAL[@]}" -lt 1 ] || [ "${#POSITIONAL[@]}" -gt 5 ]; then
  echo "error: expected INPUT and at most four optional positional values" >&2
  usage >&2
  exit 2
fi

INPUT="${POSITIONAL[0]}"
OUTPUT="${POSITIONAL[1]:-${INPUT%.*}_wa.mp4}"
TARGET_MB="${POSITIONAL[2]:-64}"
SAFETY="${POSITIONAL[3]:-0.90}"
PRESET="${POSITIONAL[4]:-slow}"

for required_command in ffmpeg ffprobe python3; do
  if ! command -v "$required_command" >/dev/null 2>&1; then
    echo "error: missing dependency: $required_command" >&2
    exit 1
  fi
done

if [ ! -f "$INPUT" ]; then
  echo "error: input file not found: $INPUT" >&2
  exit 1
fi

case "$PRESET" in
  ultrafast|superfast|veryfast|faster|fast|medium|slow|slower|veryslow|placebo) ;;
  *)
    echo "error: unsupported x264 preset: $PRESET" >&2
    exit 2
    ;;
esac

if ! python3 - "$TARGET_MB" "$SAFETY" <<'PY'
import math
import sys

try:
    target = float(sys.argv[1])
    safety = float(sys.argv[2])
except ValueError:
    raise SystemExit(1)

if not math.isfinite(target) or not math.isfinite(safety):
    raise SystemExit(1)
if target <= 0 or safety <= 0 or safety > 1:
    raise SystemExit(1)
PY
then
  echo "error: TARGET_MB must be positive and SAFETY must be in (0, 1]" >&2
  exit 2
fi

OUTPUT_DIR=$(dirname "$OUTPUT")
mkdir -p "$OUTPUT_DIR"
if ! python3 - "$OUTPUT_DIR" <<'PY'
import shutil
import sys

if shutil.disk_usage(sys.argv[1]).free < 256 * 1024 * 1024:
    raise SystemExit(1)
PY
then
  echo "error: less than 256 MiB of free disk space is available in the output directory" >&2
  exit 1
fi

INPUT_ABS=$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$INPUT")
OUTPUT_ABS=$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$OUTPUT")
if [ "$INPUT_ABS" = "$OUTPUT_ABS" ]; then
  echo "error: output must be different from input" >&2
  exit 2
fi

if [ -e "$OUTPUT" ] && python3 - "$INPUT" "$OUTPUT" <<'PY'
import os
import sys

try:
    same = os.path.samefile(sys.argv[1], sys.argv[2])
except FileNotFoundError:
    same = False
raise SystemExit(0 if same else 1)
PY
then
  echo "error: output must not be the same file or hard link as input" >&2
  exit 2
fi

if [ -e "$OUTPUT" ] && [ "$FORCE" -ne 1 ]; then
  echo "error: output already exists: $OUTPUT (use --force to replace it)" >&2
  exit 1
fi

echo "== Probe input =="
PROBE_JSON=$(ffprobe -v error -show_streams -show_format -of json "$INPUT" 2>/dev/null || true)
PROBE_VALUES=$(python3 - "$PROBE_JSON" <<'PY'
import json
import math
import sys

try:
    payload = json.loads(sys.argv[1])
    streams = payload.get("streams", [])
    video = next(item for item in streams if item.get("codec_type") == "video")
except (KeyError, StopIteration, TypeError, json.JSONDecodeError):
    raise SystemExit(1)

def rate(value):
    try:
        if "/" in str(value):
            numerator, denominator = str(value).split("/", 1)
            result = float(numerator) / float(denominator)
        else:
            result = float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        result = 0.0
    return result if math.isfinite(result) and result > 0 else 0.0

width = int(video.get("width", 0))
height = int(video.get("height", 0))
sar = str(video.get("sample_aspect_ratio", "1:1"))
try:
    numerator, denominator = sar.split(":", 1)
    if int(denominator) > 0:
        width = max(1, round(width * int(numerator) / int(denominator)))
except (TypeError, ValueError, ZeroDivisionError):
    pass

rotation = 0
for side_data in video.get("side_data_list", []):
    if "rotation" in side_data:
        try:
            rotation = int(round(float(side_data["rotation"]))) % 360
        except (TypeError, ValueError):
            pass
        break
if rotation == 0:
    try:
        rotation = int(round(float(video.get("tags", {}).get("rotate", 0)))) % 360
    except (TypeError, ValueError):
        rotation = 0
if rotation in {90, 270}:
    width, height = height, width

duration = float(payload.get("format", {}).get("duration", 0) or 0)
fps = rate(video.get("avg_frame_rate")) or rate(video.get("r_frame_rate"))
audio = any(item.get("codec_type") == "audio" for item in streams)
values = [
    duration,
    width,
    height,
    f"{fps:.6f}",
    "1" if audio else "0",
    rotation,
    video.get("codec_name", ""),
    video.get("pix_fmt", ""),
    video.get("profile", ""),
    video.get("codec_tag_string", ""),
    video.get("sample_aspect_ratio", ""),
]
print("\t".join(str(value) for value in values))
PY
)
if [ -z "$PROBE_VALUES" ]; then
  echo "error: ffprobe could not read a usable media stream" >&2
  exit 1
fi
IFS=$'\t' read -r DURATION WIDTH HEIGHT FPS_RAW AUDIO_FLAG ROTATION SOURCE_VIDEO_CODEC SOURCE_VIDEO_PIX_FMT SOURCE_VIDEO_PROFILE SOURCE_VIDEO_TAG SOURCE_VIDEO_SAR <<EOF
$PROBE_VALUES
EOF
if [ "$AUDIO_FLAG" = "1" ]; then
  HAS_AUDIO=1
  SOURCE_AUDIO_CODEC=$(python3 - "$PROBE_JSON" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
audio = next((item for item in payload.get("streams", []) if item.get("codec_type") == "audio"), {})
print(audio.get("codec_name", ""))
PY
)
else
  HAS_AUDIO=""
  SOURCE_AUDIO_CODEC=""
fi

if ! python3 - "$DURATION" "$WIDTH" "$HEIGHT" <<'PY'
import math
import sys

try:
    duration = float(sys.argv[1])
    width = int(sys.argv[2])
    height = int(sys.argv[3])
except (TypeError, ValueError):
    raise SystemExit(1)

if not math.isfinite(duration) or duration <= 0 or width < 2 or height < 2:
    raise SystemExit(1)
PY
then
  echo "error: input does not contain a usable video stream" >&2
  exit 1
fi

FPS=$(python3 - "$FPS_RAW" <<'PY'
import math
import sys

raw = sys.argv[1]
try:
    if "/" in raw:
        numerator, denominator = raw.split("/", 1)
        fps = float(numerator) / float(denominator)
    else:
        fps = float(raw)
except (TypeError, ValueError, ZeroDivisionError):
    raise SystemExit(1)

if not math.isfinite(fps) or fps <= 0:
    raise SystemExit(1)
print(f"{fps:.6f}")
PY
)
DURATION_INT=$(python3 - "$DURATION" <<'PY'
import sys

print(max(1, round(float(sys.argv[1]))))
PY
)
OUT_FPS=$(python3 - "$FPS" <<'PY'
import sys

print(f"{min(30.0, float(sys.argv[1])):.6f}")
PY
)

echo "duration: ${DURATION_INT}s  display resolution: ${WIDTH}x${HEIGHT}  fps: ${FPS}  rotation: ${ROTATION}  audio: $( [ -n "$HAS_AUDIO" ] && echo yes || echo no )"

LIMIT_BYTES=$(python3 - "$TARGET_MB" <<'PY'
import sys

print(int(float(sys.argv[1]) * 1024 * 1024))
PY
)
SAFE_BYTES=$(python3 - "$LIMIT_BYTES" "$SAFETY" <<'PY'
import sys

print(int(int(sys.argv[1]) * float(sys.argv[2])))
PY
)
echo "limit: ${TARGET_MB} MiB  target with safety margin: $(python3 - "$SAFE_BYTES" <<'PY'
import sys

print(f"{int(sys.argv[1]) / 1024 / 1024:.1f} MiB")
PY
)"

if [ -z "$HAS_AUDIO" ]; then
  AUDIO_KBPS=0
elif [ "$DURATION_INT" -le 60 ]; then
  AUDIO_KBPS=128
elif [ "$DURATION_INT" -le 300 ]; then
  AUDIO_KBPS=96
else
  AUDIO_KBPS=64
fi

# Use the exact duration for the budget. Rounding here can make short outputs overshoot.
TOTAL_KBPS=$(python3 - "$SAFE_BYTES" "$DURATION" <<'PY'
import sys

print((int(sys.argv[1]) * 8) / 1000 / float(sys.argv[2]))
PY
)
VIDEO_KBPS=$(python3 - "$TOTAL_KBPS" "$AUDIO_KBPS" <<'PY'
import sys

print(int(float(sys.argv[1]) * 0.98 - int(sys.argv[2])))
PY
)

if [ "$VIDEO_KBPS" -lt 1 ]; then
  echo "error: target is too small for this video's duration and audio track" >&2
  exit 1
fi
if [ "$VIDEO_KBPS" -lt "$MIN_VIDEO_KBPS" ]; then
  echo "warning: video budget is only ${VIDEO_KBPS} kbps; visible blocking is likely" >&2
fi
echo "audio bitrate: ${AUDIO_KBPS} kbps  video bitrate budget: ${VIDEO_KBPS} kbps"

choose_resolution() {
  local vkbps=$1 srcw=$2 srch=$3 fps=$4
  local long_side short_side is_portrait=0
  if [ "$srcw" -ge "$srch" ]; then
    long_side=$srcw
    short_side=$srch
  else
    long_side=$srch
    short_side=$srcw
    is_portrait=1
  fi

  local source_short_side=$short_side
  # Cap the display size for messaging while keeping the source aspect ratio.
  if [ "$short_side" -gt 1080 ]; then
    short_side=1080
  fi
  # Keep the candidates ordered from the source size down; avoid mapfile so
  # the helper also runs with macOS's system Bash 3.2.
  local raw_candidates=("$short_side" 1080 720 540 480)
  local candidates=()
  local c existing duplicate
  for c in "${raw_candidates[@]}"; do
    if [ "$c" -gt "$short_side" ]; then
      continue
    fi
    duplicate=0
    if [ "${#candidates[@]}" -gt 0 ]; then
      for existing in "${candidates[@]}"; do
        if [ "$existing" -eq "$c" ]; then
          duplicate=1
          break
        fi
      done
    fi
    if [ "$duplicate" -eq 0 ]; then
      candidates+=("$c")
    fi
  done

  local chosen_short=2 chosen_long=2 c_even long_cand bpp ok
  for c in "${candidates[@]}"; do
    c_even=$(python3 - "$c" <<'PY'
import sys

print(max(2, int(sys.argv[1]) - (int(sys.argv[1]) % 2)))
PY
)
    long_cand=$(python3 - "$c_even" "$long_side" "$source_short_side" <<'PY'
import sys

short = int(sys.argv[1])
long_side = int(sys.argv[2])
source_short = int(sys.argv[3])
long = int(short * long_side / source_short)
print(max(2, long - (long % 2)))
PY
)
    if [ "$long_cand" -gt 1920 ]; then
      long_cand=1920
      c_even=$(python3 - "$long_cand" "$source_short_side" "$long_side" <<'PY'
import sys

long_side = int(sys.argv[1])
source_short = int(sys.argv[2])
source_long = int(sys.argv[3])
short = max(2, int(long_side * source_short / source_long))
print(max(2, short - (short % 2)))
PY
)
    fi
    bpp=$(python3 - "$vkbps" "$c_even" "$long_cand" "$fps" <<'PY'
import sys

print((int(sys.argv[1]) * 1000) / (int(sys.argv[2]) * int(sys.argv[3]) * float(sys.argv[4])))
PY
)
    chosen_short=$c_even
    chosen_long=$long_cand
    ok=$(python3 - "$bpp" <<'PY'
import sys

print(1 if float(sys.argv[1]) >= 0.08 else 0)
PY
)
    if [ "$ok" = "1" ]; then
      break
    fi
  done

  if [ "$is_portrait" -eq 1 ]; then
    echo "${chosen_short}x${chosen_long}"
  else
    echo "${chosen_long}x${chosen_short}"
  fi
}

RES=$(choose_resolution "$VIDEO_KBPS" "$WIDTH" "$HEIGHT" "$OUT_FPS")
OUT_W=${RES%x*}
OUT_H=${RES#*x}
echo "output resolution: ${OUT_W}x${OUT_H}  fps: ${OUT_FPS}"

ACHIEVED_BPP=$(python3 - "$VIDEO_KBPS" "$OUT_W" "$OUT_H" "$OUT_FPS" <<'PY'
import sys

print((int(sys.argv[1]) * 1000) / (int(sys.argv[2]) * int(sys.argv[3]) * float(sys.argv[4])))
PY
)
LOW_BPP_OK=$(python3 - "$ACHIEVED_BPP" <<'PY'
import sys

print(1 if float(sys.argv[1]) >= 0.08 else 0)
PY
)
if [ "$LOW_BPP_OK" = "0" ]; then
  echo "warning: final video budget is only ${ACHIEVED_BPP} bits/pixel; fast motion may look blocky" >&2
fi

PASSLOG=$(mktemp "${TMPDIR:-/tmp}/social-video-wa-pass.XXXXXX")
PASS_DIR=$(dirname "$PASSLOG")
PASS_BASE=$(basename "$PASSLOG")
OUTPUT_TMP=$(mktemp "${OUTPUT}.partial.XXXXXX")
SOURCE_BYTES=$(stat -c%s "$INPUT" 2>/dev/null || stat -f%z "$INPUT")

cleanup() {
  find "$PASS_DIR" -maxdepth 1 -type f -name "${PASS_BASE}*" -delete 2>/dev/null || true
  if [ -f "$OUTPUT_TMP" ]; then
    rm -f "$OUTPUT_TMP"
  fi
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

AUDIO_ARGS=()
if [ -n "$HAS_AUDIO" ]; then
  AUDIO_ARGS=(-c:a aac -b:a "${AUDIO_KBPS}k" -ac 2)
else
  AUDIO_ARGS=(-an)
fi

encode() {
  local vkbps=$1
  local maxrate bufsize
  maxrate=$(python3 - "$vkbps" <<'PY'
import sys

print(max(1, int(int(sys.argv[1]) * 1.5)))
PY
)
  bufsize=$(python3 - "$vkbps" <<'PY'
import sys

print(max(2, int(sys.argv[1]) * 2))
PY
)

  ffmpeg -hide_banner -y -i "$INPUT" -map 0:v:0 \
    -c:v libx264 -preset "$PRESET" -profile:v high -coder 1 -bf 2 -8x8dct 1 -tag:v avc1 \
    -b:v "${vkbps}k" -pass 1 -passlogfile "$PASSLOG" \
    -vf "scale=${OUT_W}:${OUT_H}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,setsar=1,pad=${OUT_W}:${OUT_H}:(ow-iw)/2:(oh-ih)/2,fps=${OUT_FPS}" \
    -pix_fmt yuv420p -an -f mp4 -loglevel error /dev/null

  ffmpeg -hide_banner -y -i "$INPUT" -map 0:v:0 -map 0:a:0? \
    -map_metadata -1 -map_chapters -1 \
    -c:v libx264 -preset "$PRESET" -profile:v high -coder 1 -bf 2 -8x8dct 1 -tag:v avc1 \
    -b:v "${vkbps}k" -maxrate "${maxrate}k" -bufsize "${bufsize}k" \
    -pass 2 -passlogfile "$PASSLOG" \
    -vf "scale=${OUT_W}:${OUT_H}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,setsar=1,pad=${OUT_W}:${OUT_H}:(ow-iw)/2:(oh-ih)/2,fps=${OUT_FPS}" \
    -pix_fmt yuv420p "${AUDIO_ARGS[@]}" -movflags +faststart \
    -sn -dn -f mp4 -loglevel error "$OUTPUT_TMP"
}

validate_output() {
  local output=$1
  local video_codec video_pix_fmt video_profile video_tag video_sar audio_codec
  video_codec=$(ffprobe -v error -select_streams v:0 -show_entries stream=codec_name -of csv=p=0 "$output")
  if [ "$video_codec" != "h264" ]; then
    echo "error: final output video codec is ${video_codec:-unknown}, expected h264" >&2
    return 1
  fi
  video_pix_fmt=$(ffprobe -v error -select_streams v:0 -show_entries stream=pix_fmt -of csv=p=0 "$output")
  if [ "$video_pix_fmt" != "yuv420p" ]; then
    echo "error: final output pixel format is ${video_pix_fmt:-unknown}, expected yuv420p" >&2
    return 1
  fi
  video_profile=$(ffprobe -v error -select_streams v:0 -show_entries stream=profile -of csv=p=0 "$output")
  case "$video_profile" in
    *High*) ;;
    *)
      echo "error: final output H.264 profile is ${video_profile:-unknown}, expected High" >&2
      return 1
      ;;
  esac
  video_tag=$(ffprobe -v error -select_streams v:0 -show_entries stream=codec_tag_string -of csv=p=0 "$output")
  if [ "$video_tag" != "avc1" ]; then
    echo "error: final output video tag is ${video_tag:-unknown}, expected avc1" >&2
    return 1
  fi
  video_sar=$(ffprobe -v error -select_streams v:0 -show_entries stream=sample_aspect_ratio -of csv=p=0 "$output")
  if [ "$video_sar" != "1:1" ]; then
    echo "error: final output sample aspect ratio is ${video_sar:-unknown}, expected 1:1" >&2
    return 1
  fi
  if [ -n "$HAS_AUDIO" ]; then
    audio_codec=$(ffprobe -v error -select_streams a:0 -show_entries stream=codec_name -of csv=p=0 "$output")
    if [ "$audio_codec" != "aac" ]; then
      echo "error: final output audio codec is ${audio_codec:-unknown}, expected aac" >&2
      return 1
    fi
  fi
}

atomic_commit_output() {
  python3 - "$OUTPUT_TMP" "$OUTPUT" "$FORCE" <<'PY'
import os
import sys

source = sys.argv[1]
destination = sys.argv[2]
force = sys.argv[3] == "1"
if force:
    os.replace(source, destination)
else:
    try:
        os.link(source, destination)
    except FileExistsError as exc:
        raise SystemExit(f"output appeared during encoding: {destination}") from exc
    os.unlink(source)
PY
}

CUR_KBPS=$VIDEO_KBPS
ACTUAL_BYTES=0
ACTUAL_MIB=0
SUCCESS=0
if [ "$SOURCE_BYTES" -le "$LIMIT_BYTES" ] \
  && [ "$SOURCE_VIDEO_CODEC" = "h264" ] \
  && [ "$SOURCE_VIDEO_PIX_FMT" = "yuv420p" ] \
  && [[ "$SOURCE_VIDEO_PROFILE" == *High* ]] \
  && [ "$SOURCE_VIDEO_TAG" = "avc1" ] \
  && [ "$SOURCE_VIDEO_SAR" = "1:1" ] \
  && { [ -z "$SOURCE_AUDIO_CODEC" ] || [ "$SOURCE_AUDIO_CODEC" = "aac" ]; }; then
  echo "source is already within the requested size and uses a compatible codec; remuxing without re-encoding"
  if ffmpeg -hide_banner -y -i "$INPUT" -map 0:v:0 -map 0:a:0? \
    -sn -dn -map_metadata -1 -map_chapters -1 -c copy -movflags +faststart \
    -f mp4 -loglevel error "$OUTPUT_TMP"; then
    ACTUAL_BYTES=$(stat -c%s "$OUTPUT_TMP" 2>/dev/null || stat -f%z "$OUTPUT_TMP")
    if [ "$ACTUAL_BYTES" -le "$LIMIT_BYTES" ] && validate_output "$OUTPUT_TMP"; then
      SUCCESS=1
      ACTUAL_MIB=$(python3 - "$ACTUAL_BYTES" <<'PY'
import sys

print(f"{int(sys.argv[1]) / 1024 / 1024:.2f}")
PY
)
    fi
  fi
  if [ "$SUCCESS" -ne 1 ]; then
    rm -f "$OUTPUT_TMP"
  fi
fi

if [ "$SUCCESS" -eq 0 ]; then
for attempt in 1 2 3; do
  echo "== Encode pass ${attempt}: video ${CUR_KBPS} kbps =="
  encode "$CUR_KBPS"
  ACTUAL_BYTES=$(stat -c%s "$OUTPUT_TMP" 2>/dev/null || stat -f%z "$OUTPUT_TMP")
  ACTUAL_MIB=$(python3 - "$ACTUAL_BYTES" <<'PY'
import sys

print(f"{int(sys.argv[1]) / 1024 / 1024:.2f}")
PY
)
  echo "output size: ${ACTUAL_MIB} MiB (limit ${TARGET_MB} MiB)"
  if [ "$ACTUAL_BYTES" -le "$LIMIT_BYTES" ]; then
    SUCCESS=1
    break
  fi
  if [ "$attempt" -lt 3 ]; then
    echo "over limit; reducing video bitrate and retrying..." >&2
    CUR_KBPS=$(python3 - "$CUR_KBPS" "$ACTUAL_BYTES" "$SAFE_BYTES" <<'PY'
import sys

current = int(sys.argv[1])
actual = int(sys.argv[2])
safe = int(sys.argv[3])
print(max(1, int(current * safe / actual * 0.96)))
PY
)
  fi
done
fi

if [ "$SUCCESS" -ne 1 ]; then
  echo "error: could not produce an output within ${TARGET_MB} MiB after 3 attempts" >&2
  exit 1
fi

validate_output "$OUTPUT_TMP"
atomic_commit_output

FINAL_BYTES=$(stat -c%s "$OUTPUT" 2>/dev/null || stat -f%z "$OUTPUT")
if [ "$FINAL_BYTES" -gt "$LIMIT_BYTES" ]; then
  echo "error: final output exceeded the requested limit after writing" >&2
  exit 1
fi
FINAL_DURATION=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUTPUT")
ACTUAL_MIB=$(python3 - "$FINAL_BYTES" <<'PY'
import sys

print(f"{int(sys.argv[1]) / 1024 / 1024:.2f}")
PY
)

echo ""
echo "== Complete =="
echo "output: $OUTPUT"
echo "size: ${ACTUAL_MIB} MiB / ${TARGET_MB} MiB"
echo "duration: ${FINAL_DURATION}s"
echo "video: ${OUT_W}x${OUT_H} ${OUT_FPS} fps, ${CUR_KBPS} kbps H.264"
if [ -n "$HAS_AUDIO" ]; then
  echo "audio: ${AUDIO_KBPS} kbps AAC"
else
  echo "audio: none (source had no audio stream)"
fi
echo "Tip: send it as WhatsApp Document/File to avoid another video-quality transcode."
