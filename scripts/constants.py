"""Shared constants, types, and tiny helpers for social-video-downloader."""
from __future__ import annotations

import hashlib
import re
import subprocess
from datetime import timedelta
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


__version__ = "0.5.0"

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
HLS_SEGMENT_WORKERS = 4
HLS_MAX_PLAYLIST_DEPTH = 3
HLS_MAX_PLAYLIST_BYTES = 2 * 1024 * 1024
HLS_MAX_SEGMENTS = 2000
HLS_MAX_SEGMENT_BYTES = 256 * 1024 * 1024
HLS_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
URL_WORKERS = 3
MAX_URL_WORKERS = 4
MAX_CONCURRENT_FRAGMENTS = 4
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024
MAX_BATCH_BYTES = 4 * 1024 * 1024 * 1024
MAX_HTTP_RESPONSE_BYTES = 16 * 1024 * 1024
MIN_FREE_DISK_BYTES = 256 * 1024 * 1024
MAX_OUTPUT_HEIGHT = 2160
MAX_KPI_DAYS = 3650
METRICS_MAX_BYTES = 10 * 1024 * 1024
MAX_LOG_TAIL_BYTES = 64 * 1024
TOOL_SEMANTICS_VERSION = "download-v2"
SNAPTIK_HOME_URL = "https://snaptik.app/en2"
SNAPTIK_SUBMIT_URL = "https://snaptik.app/abc2.php"
SSSTIK_HOME_URL = "https://ssstik.io/"
SSSTIK_SUBMIT_URL = "https://ssstik.io/abc?url=dl"
CACHE_PATH = (
    Path.home() / ".codex" / "skills" / "social-video-downloader" / "cache" / "downloads.json"
)
CACHE_SALT_PATH = CACHE_PATH.with_name(".salt")
METRICS_LOG_PATH = (
    Path.home() / ".codex" / "skills" / "social-video-downloader" / "metrics" / "runs.jsonl"
)
CACHE_TTL = timedelta(days=7)
TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {"fbclid", "igshid", "ref"}


class DownloadRoute(str, Enum):
    SOCIAL = "social"
    DIRECT = "direct"
    HLS_SEGMENTED = "hls_segmented"
    TIKTOK_RESOLVER = "tiktok_resolver"
    CACHE = "cache"
    DRY_RUN = "dry_run"


class ErrorCode(str, Enum):
    NONE = "none"
    AUTH_NEEDED = "auth_needed"
    NETWORK_UNSTABLE = "network_unstable"
    AUDIO_ONLY_RESULT = "audio_only_result"
    AUDIO_EXPECTED_BUT_MISSING = "audio_expected_but_missing"
    NO_MEDIA_STREAM = "no_media_stream"
    WORKER_EXCEPTION = "worker_exception"
    RESOURCE_LIMIT = "resource_limit"
    COMPATIBILITY_VALIDATION_FAILED = "compatibility_validation_failed"
    DIRECT_DOWNLOAD_FAILED = "direct_download_failed"
    HLS_DOWNLOAD_FAILED = "hls_download_failed"
    TIKTOK_RESOLVER_FAILED = "tiktok_resolver_failed"
    INPUT_INVALID = "input_invalid"
    OTHER_FAILURE = "other_failure"


@dataclass(frozen=True)
class RouteResult:
    ok: bool
    path: str | None
    route: DownloadRoute
    detail: str = ""
    browser: str | None = None
    error_code: ErrorCode = ErrorCode.NONE
    media_probe: dict[str, object] | None = None


@dataclass(frozen=True)
class DownloadResult:
    url: str
    ok: bool
    message: str
    saved_path: str | None
    route: DownloadRoute | None
    error_code: ErrorCode
    metadata: dict[str, object] = field(default_factory=dict)


def sanitize_filename(name: str) -> str:
    cleaned = SAFE_FILENAME_PATTERN.sub("-", name).strip(" .-_") or "downloaded-video"
    encoded = cleaned.encode("utf-8")
    if len(encoded) <= 180:
        return cleaned
    digest = hashlib.sha1(encoded).hexdigest()[:10]
    prefix = encoded[: 180 - len(digest) - 1].decode("utf-8", errors="ignore").rstrip(" .-_")
    return f"{prefix}-{digest}" or f"downloaded-video-{digest}"


def redact_url(url: str) -> str:
    """Remove query and fragment data before a URL is shown or persisted."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "<redacted-url>"
    if not parsed.scheme or not parsed.netloc:
        return "<redacted-url>"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def redact_text(text: str) -> str:
    """Redact URLs embedded in subprocess errors and diagnostic strings."""
    return re.sub(r"https?://[^\s'\"]+", lambda match: redact_url(match.group(0)), text)


def summarize_error(result: subprocess.CompletedProcess[str]) -> str:
    for source in (result.stderr, result.stdout):
        if not source:
            continue
        lines = [line.strip() for line in source.splitlines() if line.strip()]
        for line in reversed(lines):
            if "ERROR:" in line or "WARNING:" in line:
                return redact_text(line)
        if lines:
            return redact_text(lines[-1])
    return redact_text(f"Command failed with exit code {result.returncode}.")
