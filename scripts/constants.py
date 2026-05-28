"""Shared constants, types, and tiny helpers for social-video-downloader."""
from __future__ import annotations

import re
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import NamedTuple


__version__ = "0.4.0"

DEFAULT_OUTPUT_DIR = Path.home() / "Downloads"
DEFAULT_MAX_HEIGHT = 720
AUTO_COOKIE_BROWSERS = ["chrome", "brave", "edge", "firefox", "safari", "chromium"]
DEFAULT_FORMAT = (
    "bestvideo*[height<={max_height}][ext=mp4]+bestaudio[ext=m4a]/"
    "bestvideo*[height<={max_height}]+bestaudio/"
    "best[height<={max_height}][acodec!=none]/"
    "best[acodec!=none]"
)
URL_PATTERN = re.compile(r"https?://[^\s<>'\"()]+")
SAFE_FILENAME_PATTERN = re.compile(r"[^A-Za-z0-9._ -]+")
DIRECT_MEDIA_EXTENSIONS = (".mp4", ".m4v", ".mov", ".webm", ".m3u8")
HLS_STALL_SECONDS = 20
HLS_PROGRESS_POLL_SECONDS = 2
HLS_SEGMENT_WORKERS = 4
HLS_MAX_PLAYLIST_DEPTH = 3
URL_WORKERS = 3
SNAPTIK_HOME_URL = "https://snaptik.app/en2"
SNAPTIK_SUBMIT_URL = "https://snaptik.app/abc2.php"
SSSTIK_HOME_URL = "https://ssstik.io/"
SSSTIK_SUBMIT_URL = "https://ssstik.io/abc?url=dl"
CACHE_PATH = (
    Path.home() / ".codex" / "skills" / "social-video-downloader" / "cache" / "downloads.json"
)
METRICS_LOG_PATH = (
    Path.home() / ".codex" / "skills" / "social-video-downloader" / "metrics" / "runs.jsonl"
)
CACHE_TTL = timedelta(days=7)
TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {"fbclid", "igshid", "ref"}


class DownloadResult(NamedTuple):
    url: str
    ok: bool
    message: str
    saved_path: str | None
    status: str | None
    metadata: dict[str, object]


def sanitize_filename(name: str) -> str:
    cleaned = SAFE_FILENAME_PATTERN.sub("-", name).strip(" .-_")
    return cleaned or "downloaded-video"


def summarize_error(result: subprocess.CompletedProcess[str]) -> str:
    for source in (result.stderr, result.stdout):
        if not source:
            continue
        lines = [line.strip() for line in source.splitlines() if line.strip()]
        for line in reversed(lines):
            if "ERROR:" in line or "WARNING:" in line:
                return line
        if lines:
            return lines[-1]
    return f"Command failed with exit code {result.returncode}."
