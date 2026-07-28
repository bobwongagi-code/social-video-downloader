"""Single-pass media probing and safe basic presentation-profile transcoding."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from constants import summarize_error
from deps import which_or_none
from file_ops import atomic_commit, non_conflicting_path


def ffprobe_path(ffmpeg: str) -> str:
    return which_or_none("ffprobe") or str(Path(ffmpeg).with_name("ffprobe"))


def probe_media(path: str, ffmpeg: str) -> dict[str, object]:
    cmd = [
        ffprobe_path(ffmpeg),
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        return {}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    streams = payload.get("streams")
    if not isinstance(streams, list):
        streams = []
    video = next((item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"), {})
    audio = next((item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"), {})
    return {
        "video": video,
        "audio": audio,
        "streams": streams,
        "format": payload.get("format", {}) if isinstance(payload.get("format", {}), dict) else {},
    }


def _rotation(video: dict[str, object]) -> int:
    side_data = video.get("side_data_list", [])
    if isinstance(side_data, list):
        for item in side_data:
            if isinstance(item, dict) and "rotation" in item:
                try:
                    return int(round(float(item["rotation"]))) % 360
                except (TypeError, ValueError):
                    pass
    tags = video.get("tags", {})
    if isinstance(tags, dict):
        try:
            return int(round(float(tags.get("rotate", 0)))) % 360
        except (TypeError, ValueError):
            pass
    return 0


def _display_dimensions(video: dict[str, object]) -> tuple[int, int]:
    try:
        width = int(video.get("width", 0))
        height = int(video.get("height", 0))
    except (TypeError, ValueError):
        return 0, 0
    sar = str(video.get("sample_aspect_ratio", "1:1"))
    try:
        numerator, denominator = sar.split(":", 1)
        if int(denominator) > 0:
            width = max(1, round(width * int(numerator) / int(denominator)))
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    if _rotation(video) in {90, 270}:
        width, height = height, width
    return width, height


def probe_video_info(path: str, ffmpeg: str) -> dict[str, str]:
    info = probe_media(path, ffmpeg).get("video", {})
    return dict(info) if isinstance(info, dict) else {}


def has_video_stream(path: str, ffmpeg: str) -> bool:
    return bool(probe_media(path, ffmpeg).get("video"))


def probe_audio_codec(path: str, ffmpeg: str) -> str:
    info = probe_media(path, ffmpeg).get("audio", {})
    return str(info.get("codec_name", "")) if isinstance(info, dict) else ""


def has_audio_stream(path: str, ffmpeg: str) -> bool:
    return bool(probe_media(path, ffmpeg).get("audio"))


def make_powerpoint_compatible(
    input_path: str,
    ffmpeg: str,
    *,
    force: bool = False,
    keep_metadata: bool = False,
    probed: dict[str, object] | None = None,
) -> tuple[str, bool]:
    source = Path(input_path)
    probed = probed if probed is not None else probe_media(str(source), ffmpeg)
    already_compatible = bool(
        media_facts(str(source), ffmpeg, probed=probed)["basic_ppt_profile"]
    )
    if already_compatible:
        return str(source), False

    final_output = non_conflicting_path(
        source.with_name(f"{source.stem}.ppt.mp4"),
        force=force,
    )
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{final_output.stem}.",
        suffix=".mp4",
        dir=final_output.parent,
    )
    os.close(descriptor)
    temp_output = Path(temp_name)
    metadata_args = [] if keep_metadata else ["-map_metadata", "-1", "-map_chapters", "-1"]
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-sn",
        "-dn",
        *metadata_args,
        "-vf",
        "setsar=1",
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
    print("Transcoding for basic presentation profile:", " ".join(cmd), file=sys.stderr)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(
                f"basic presentation-profile transcode failed: {summarize_error(result)}"
            )
        atomic_commit(temp_output, final_output, force=force)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("basic presentation-profile transcode timed out after 10 minutes") from exc
    finally:
        temp_output.unlink(missing_ok=True)
    return str(final_output), True


def cached_file_is_usable(path: str, ffmpeg: str, *, require_audio: bool = False) -> bool:
    candidate = Path(path)
    if not (candidate.exists() and candidate.is_file() and candidate.stat().st_size > 0):
        return False
    facts = media_facts(path, ffmpeg)
    return bool(facts["has_video"]) and (not require_audio or bool(facts["has_audio"]))


def media_facts(
    path: str | None,
    ffmpeg: str,
    *,
    probed: dict[str, object] | None = None,
) -> dict[str, object]:
    if not path:
        return {
            "has_video": False,
            "has_audio": False,
            "media_state": "no_media_stream",
            "basic_ppt_profile": False,
            "video_codec": "",
            "audio_codec": "",
            "video_profile": "",
            "video_tag": "",
            "sample_aspect_ratio": "",
            "video_level": 0,
            "frame_rate": 0.0,
            "audio_channels": 0,
            "audio_sample_rate": 0,
            "format_name": "",
            "display_width": 0,
            "display_height": 0,
            "rotation": 0,
        }
    probed = probed if probed is not None else probe_media(path, ffmpeg)
    video_info = probed.get("video", {})
    audio_info = probed.get("audio", {})
    has_video = isinstance(video_info, dict) and bool(video_info)
    has_audio = isinstance(audio_info, dict) and bool(audio_info)
    video_codec = str(video_info.get("codec_name", "")) if isinstance(video_info, dict) else ""
    video_pix_fmt = str(video_info.get("pix_fmt", "")) if isinstance(video_info, dict) else ""
    video_profile = str(video_info.get("profile", "")) if isinstance(video_info, dict) else ""
    video_tag = str(video_info.get("codec_tag_string", "")) if isinstance(video_info, dict) else ""
    video_sar = str(video_info.get("sample_aspect_ratio", "")) if isinstance(video_info, dict) else ""
    audio_codec = str(audio_info.get("codec_name", "")) if isinstance(audio_info, dict) else ""
    format_info = probed.get("format", {})
    format_name = str(format_info.get("format_name", "")) if isinstance(format_info, dict) else ""
    try:
        video_level = int(video_info.get("level", 0) or 0) if isinstance(video_info, dict) else 0
    except (TypeError, ValueError):
        video_level = 0
    try:
        frame_rate = _frame_rate(video_info if isinstance(video_info, dict) else {})
    except (TypeError, ValueError, ZeroDivisionError):
        frame_rate = 0.0
    try:
        audio_channels = int(audio_info.get("channels", 0) or 0) if isinstance(audio_info, dict) else 0
    except (TypeError, ValueError):
        audio_channels = 0
    try:
        audio_sample_rate = int(audio_info.get("sample_rate", 0) or 0) if isinstance(audio_info, dict) else 0
    except (TypeError, ValueError):
        audio_sample_rate = 0
    display_width, display_height = _display_dimensions(video_info if isinstance(video_info, dict) else {})
    media_state = "video_only_source" if has_video and not has_audio else (
        "audio_and_video" if has_video else ("audio_only_result" if has_audio else "no_media_stream")
    )
    basic_ppt_profile = (
        has_video
        and video_codec == "h264"
        and video_pix_fmt == "yuv420p"
        and "High" in video_profile
        and video_tag == "avc1"
        and video_sar == "1:1"
        and "mp4" in {item.strip().lower() for item in format_name.split(",")}
        and (video_level == 0 or video_level <= 42)
        and (frame_rate == 0.0 or frame_rate <= 60.0)
        and audio_codec in {"aac", ""}
        and (not has_audio or audio_channels in {0, 1, 2})
        and (not has_audio or audio_sample_rate in {0, 44100, 48000})
    )
    return {
        "has_video": has_video,
        "has_audio": has_audio,
        "media_state": media_state,
        "basic_ppt_profile": basic_ppt_profile and Path(path).suffix.lower() == ".mp4",
        "video_codec": video_codec,
        "audio_codec": audio_codec,
        "video_profile": video_profile,
        "video_tag": video_tag,
        "sample_aspect_ratio": video_sar,
        "video_level": video_level,
        "frame_rate": frame_rate,
        "audio_channels": audio_channels,
        "audio_sample_rate": audio_sample_rate,
        "format_name": format_name,
        "display_width": display_width,
        "display_height": display_height,
        "rotation": _rotation(video_info if isinstance(video_info, dict) else {}),
    }


def _frame_rate(video: dict[str, object]) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        value = str(video.get(key, ""))
        if "/" in value:
            numerator, denominator = value.split("/", 1)
            if float(denominator) != 0:
                return float(numerator) / float(denominator)
        elif value:
            return float(value)
    return 0.0
