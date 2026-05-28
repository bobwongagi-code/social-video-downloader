"""Media probing (ffprobe) and PowerPoint compatibility transcoding (ffmpeg)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from deps import which_or_none
from constants import summarize_error


def ffprobe_path(ffmpeg: str) -> str:
    return which_or_none("ffprobe") or str(Path(ffmpeg).with_name("ffprobe"))


def probe_video_info(path: str, ffmpeg: str) -> dict[str, str]:
    cmd = [
        ffprobe_path(ffmpeg),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,pix_fmt",
        "-of",
        "default=noprint_wrappers=1:nokey=0",
        path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
    except subprocess.TimeoutExpired:
        return {}

    info: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        info[key.strip()] = value.strip()
    return info


def has_video_stream(path: str, ffmpeg: str) -> bool:
    cmd = [
        ffprobe_path(ffmpeg),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "csv=p=0",
        path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0 and "video" in result.stdout


def probe_audio_codec(path: str, ffmpeg: str) -> str:
    cmd = [
        ffprobe_path(ffmpeg),
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
    except subprocess.TimeoutExpired:
        return ""
    return result.stdout.strip()


def has_audio_stream(path: str, ffmpeg: str) -> bool:
    return bool(probe_audio_codec(path, ffmpeg))


def make_powerpoint_compatible(input_path: str, ffmpeg: str) -> tuple[str, bool]:
    source = Path(input_path)
    video_info = probe_video_info(str(source), ffmpeg)
    audio_info = probe_audio_codec(str(source), ffmpeg)
    already_compatible = (
        video_info.get("codec_name") == "h264"
        and video_info.get("pix_fmt") == "yuv420p"
        and audio_info == "aac"
        and source.suffix.lower() == ".mp4"
    )
    if already_compatible:
        return str(source), False

    temp_output = source.with_name(f"{source.stem} [ppt-tmp].mp4")
    final_output = source.with_suffix(".mp4")
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(source),
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(temp_output),
    ]
    print("Transcoding for PowerPoint:", " ".join(cmd), file=sys.stderr)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        temp_output.unlink(missing_ok=True)
        raise RuntimeError("PowerPoint compatibility transcode timed out after 10 minutes")
    if result.returncode != 0:
        temp_output.unlink(missing_ok=True)
        raise RuntimeError(f"PowerPoint compatibility transcode failed: {summarize_error(result)}")
    os.replace(temp_output, final_output)
    if source != final_output and source.exists():
        source.unlink()
    return str(final_output), True


def cached_file_is_usable(path: str, ffmpeg: str) -> bool:
    candidate = Path(path)
    return (
        candidate.exists()
        and candidate.is_file()
        and candidate.stat().st_size > 0
        and has_video_stream(path, ffmpeg)
        and has_audio_stream(path, ffmpeg)
    )


def media_facts(path: str | None, ffmpeg: str) -> dict[str, object]:
    if not path:
        return {
            "has_video": False,
            "has_audio": False,
            "ppt_compatible": False,
            "video_codec": "",
            "audio_codec": "",
        }
    video_info = probe_video_info(path, ffmpeg)
    audio_codec = probe_audio_codec(path, ffmpeg)
    has_video = has_video_stream(path, ffmpeg)
    has_audio = bool(audio_codec)
    ppt_compatible = (
        has_video
        and video_info.get("codec_name") == "h264"
        and video_info.get("pix_fmt") == "yuv420p"
        and audio_codec == "aac"
        and Path(path).suffix.lower() == ".mp4"
    )
    return {
        "has_video": has_video,
        "has_audio": has_audio,
        "ppt_compatible": ppt_compatible,
        "video_codec": video_info.get("codec_name", ""),
        "audio_codec": audio_codec,
    }
