#!/usr/bin/env python3
"""Download social-media videos with balanced quality and a basic presentation profile."""
from __future__ import annotations

import argparse
import concurrent.futures
import os
import selectors
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PureWindowsPath
from urllib.parse import unquote, urlparse

from constants import (
    CACHE_TTL,
    DEFAULT_FORMAT,
    DEFAULT_MAX_HEIGHT,
    DEFAULT_OUTPUT_DIR,
    DownloadRoute,
    MAX_CONCURRENT_FRAGMENTS,
    MAX_BATCH_BYTES,
    MAX_DOWNLOAD_BYTES,
    MAX_KPI_DAYS,
    MAX_OUTPUT_HEIGHT,
    MAX_URL_WORKERS,
    MIN_FREE_DISK_BYTES,
    ErrorCode,
    RouteResult,
    URL_WORKERS,
    DownloadResult,
    __version__,
    redact_text,
    redact_url,
    sanitize_filename,
    summarize_error,
)
from cache import (
    append_metrics_events,
    cache_entry_is_fresh,
    hash_sensitive_text,
    load_cache,
    make_cache_key,
    merge_cache_entries,
)
from deps import available_cookie_browsers, ensure_dependencies, which_or_none
from file_ops import atomic_commit, free_bytes, non_conflicting_path
from hls import download_hls_via_segments
from kpi import render_kpi_report
from media_probe import (
    cached_file_is_usable,
    make_powerpoint_compatible,
    media_facts,
    probe_media,
)
from net import download_file_via_curl, validate_remote_url
from tiktok_resolver import download_tiktok_via_resolvers
from urls import (
    classify_platform,
    collect_urls,
    is_direct_media_url,
    is_tiktok_url,
    normalize_urls,
)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def bounded_positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download TikTok, Instagram, Facebook, X/Twitter, and YouTube "
            "videos with balanced quality and audio included."
        )
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        help="One or more URLs, or text snippets that contain URLs.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Destination directory. Defaults to ~/Downloads.",
    )
    parser.add_argument(
        "--max-height",
        type=bounded_positive_int,
        default=DEFAULT_MAX_HEIGHT,
        help="Upper bound for video height. Defaults to 720.",
    )
    parser.add_argument(
        "--cookies-from-browser",
        choices=["chrome", "chromium", "edge", "firefox", "safari", "brave"],
        help="Load cookies from a local browser for logged-in downloads.",
    )
    parser.add_argument(
        "--auto-cookies",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Opt in to retrying failed downloads with detected browser cookies.",
    )
    parser.add_argument(
        "--text-file",
        help="Read additional URLs from a text file.",
    )
    parser.add_argument(
        "--install-missing",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Opt in to installing missing yt-dlp/ffmpeg with Homebrew.",
    )
    parser.add_argument(
        "--ppt-compatible",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Normalize each downloaded file to a basic H.264/AAC/MP4 profile for presentation workflows.",
    )
    parser.add_argument(
        "--concurrency",
        type=bounded_positive_int,
        default=URL_WORKERS,
        help="Number of parallel URL downloads. Defaults to 3.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without downloading anything.",
    )
    parser.add_argument(
        "--tiktok-resolver",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Opt in to HTTP resolver providers for TikTok URLs.",
    )
    parser.add_argument(
        "--tiktok-shop",
        action="store_true",
        help="Treat TikTok inputs as known Shop/promoted videos and try HTTP resolver providers first.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing output only when explicitly requested.",
    )
    parser.add_argument(
        "--keep-metadata",
        action="store_true",
        help="Keep extractor metadata in downloaded media when supported.",
    )
    parser.add_argument(
        "--kpi-report",
        nargs="?",
        type=bounded_positive_int,
        const=7,
        metavar="DAYS",
        help="Print a KPI summary for the last N days. Defaults to 7 when provided without a value.",
    )
    args = parser.parse_args()
    if args.max_height > MAX_OUTPUT_HEIGHT:
        parser.error(f"--max-height must be <= {MAX_OUTPUT_HEIGHT}")
    if args.concurrency > MAX_URL_WORKERS:
        parser.error(f"--concurrency must be <= {MAX_URL_WORKERS}")
    if args.kpi_report is not None:
        if args.kpi_report > MAX_KPI_DAYS:
            parser.error(f"KPI DAYS must be <= {MAX_KPI_DAYS}")
        if args.inputs or args.text_file:
            parser.error("--kpi-report cannot be combined with URL inputs")
        return args
    if not args.inputs and not args.text_file:
        parser.error("at least one input URL or --text-file is required")
    if args.tiktok_resolver is None:
        args.tiktok_resolver = bool(args.tiktok_shop)
    return args


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def output_directory(args: argparse.Namespace, *, create: bool = True) -> Path:
    output_dir = Path(os.path.expanduser(args.output_dir)).resolve()
    if create:
        output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def is_absolute_path(text: str) -> bool:
    return Path(text).is_absolute() or PureWindowsPath(text).is_absolute()


def extract_filepaths(stdout: str) -> list[str]:
    paths = []
    for line in stdout.splitlines():
        line = line.strip()
        if is_absolute_path(line) and Path(line).suffix:
            paths.append(line)
    return paths


def _bounded_tail(buffer: bytearray, data: bytes, limit: int = 64 * 1024) -> None:
    buffer.extend(data)
    if len(buffer) > limit:
        del buffer[: len(buffer) - limit]


def looks_like_auth_failure(stderr: str) -> bool:
    lowered = stderr.lower()
    markers = [
        "login required",
        "requires login",
        "sign in",
        "sign in to confirm your age",
        "you need to log in",
        "authentication",
        "private video",
        "this video is private",
        "confirm your age",
    ]
    return any(marker in lowered for marker in markers)


def error_code_from_detail(detail: str) -> ErrorCode:
    lowered = detail.lower()
    if any(token in lowered for token in ["login", "sign in", "private", "authentication"]):
        return ErrorCode.AUTH_NEEDED
    if any(
        token in lowered
        for token in [
            "timed out",
            "ssl",
            "network",
            "connection reset",
            "temporarily unavailable",
        ]
    ):
        return ErrorCode.NETWORK_UNSTABLE
    if "no supported urls were found" in lowered:
        return ErrorCode.INPUT_INVALID
    return ErrorCode.OTHER_FAILURE


def identity_scope(args: argparse.Namespace) -> str:
    if getattr(args, "cookies_from_browser", None):
        return f"browser:{args.cookies_from_browser}"
    if getattr(args, "auto_cookies", False):
        return "auto-browser-cookie"
    return "anonymous"


# ---------------------------------------------------------------------------
# yt-dlp download path
# ---------------------------------------------------------------------------

def build_command(
    url: str,
    args: argparse.Namespace,
    yt_dlp: str,
    ffmpeg: str,
    output_dir: Path,
    browser: str | None = None,
) -> list[str]:
    format_selector = DEFAULT_FORMAT.format(max_height=args.max_height)
    variant = hash_sensitive_text(
        f"{url}\0{args.max_height}\0{args.ppt_compatible}\0"
        f"{getattr(args, 'keep_metadata', False)}\0{identity_scope(args)}"
    )[:8]
    cmd = [
        yt_dlp,
        "--no-playlist",
        "--newline",
        "--extractor-retries",
        "3",
        "--file-access-retries",
        "3",
        "--fragment-retries",
        "10",
        "--retry-sleep",
        "fragment:2",
        "--concurrent-fragments",
        str(MAX_CONCURRENT_FRAGMENTS),
        "--socket-timeout",
        "30",
        "--paths",
        str(output_dir),
        "--output",
        f"%(title).160B [%(id)s] [{variant}].%(ext)s",
        "--format",
        format_selector,
        "--merge-output-format",
        "mp4",
        "--remux-video",
        "mp4",
        "--max-filesize",
        str(MAX_DOWNLOAD_BYTES),
        "--no-overwrites",
        "--print",
        "after_move:%(filepath)s",
        "--ffmpeg-location",
        ffmpeg,
    ]

    if getattr(args, "force", False):
        cmd.remove("--no-overwrites")
        cmd.extend(["--force-overwrites"])
    if getattr(args, "keep_metadata", False):
        cmd.append("--embed-metadata")

    cookie_source = browser or args.cookies_from_browser
    if cookie_source:
        cmd.extend(["--cookies-from-browser", cookie_source])

    cmd.append(url)
    return cmd


def run_download(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    assert process.stderr is not None
    selector.register(process.stdout, selectors.EVENT_READ)
    selector.register(process.stderr, selectors.EVENT_READ)
    stdout_tail = bytearray()
    stderr_tail = bytearray()
    deadline = time.monotonic() + 1800
    try:
        while selector.get_map():
            if time.monotonic() >= deadline:
                process.kill()
                process.wait(timeout=10)
                return subprocess.CompletedProcess(
                    cmd,
                    -9,
                    stdout_tail.decode("utf-8", errors="replace"),
                    "yt-dlp timed out after 30 minutes\n" + stderr_tail.decode("utf-8", errors="replace"),
                )
            for key, _ in selector.select(timeout=0.5):
                try:
                    chunk = os.read(key.fileobj.fileno(), 8192)
                except OSError:
                    chunk = b""
                if chunk:
                    _bounded_tail(stdout_tail if key.fileobj is process.stdout else stderr_tail, chunk)
                else:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
        return subprocess.CompletedProcess(
            cmd,
            process.wait(),
            stdout_tail.decode("utf-8", errors="replace"),
            stderr_tail.decode("utf-8", errors="replace"),
        )
    finally:
        selector.close()


def try_download_with_fallbacks(
    url: str,
    args: argparse.Namespace,
    yt_dlp: str,
    ffmpeg: str,
    output_dir: Path,
    cookie_browsers: list[str],
) -> RouteResult:
    attempts: list[str | None] = [args.cookies_from_browser]
    if args.cookies_from_browser is None and args.auto_cookies:
        attempts.extend(cookie_browsers)

    tried: set[str | None] = set()
    last_error = ""
    for browser in attempts:
        if browser in tried:
            continue
        tried.add(browser)
        cmd = build_command(url, args, yt_dlp, ffmpeg, output_dir, browser)
        print("Running:", redact_text(" ".join(cmd)), file=sys.stderr)
        result = run_download(cmd)
        if result.returncode == 0:
            filepaths = extract_filepaths(result.stdout)
            if filepaths:
                return RouteResult(
                    True,
                    filepaths[-1],
                    DownloadRoute.SOCIAL,
                    browser=browser,
                )
            last_error = "yt-dlp exited successfully but produced no output file path."
            if browser is not None:
                continue
            break

        last_error = summarize_error(result)
        if browser is not None:
            continue
        if not args.auto_cookies or not looks_like_auth_failure(last_error):
            break

    return RouteResult(
        False,
        None,
        DownloadRoute.SOCIAL,
        last_error,
        error_code=error_code_from_detail(last_error),
    )


# ---------------------------------------------------------------------------
# Direct media download
# ---------------------------------------------------------------------------

def direct_media_target(url: str, args: argparse.Namespace) -> Path:
    parsed = urlparse(url)
    path = unquote(parsed.path)
    name = Path(path).name
    parent = Path(path).parent.name

    if name.lower() == "index.m3u8" and parent:
        name = parent

    for suffix in (".m3u8", ".mp4", ".mov", ".m4v", ".webm", ".ts"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break

    if not name and parent:
        name = parent

    ext = ".mp4"
    if ".webm" in path.lower():
        ext = ".webm"

    suffix = hash_sensitive_text(url)[:8]
    target = output_directory(args) / f"{sanitize_filename(name)}-{suffix}{ext}"
    return non_conflicting_path(target, force=getattr(args, "force", False))


def download_direct_media(
    url: str, args: argparse.Namespace, ffmpeg: str
) -> RouteResult:
    try:
        validate_remote_url(url)
        destination = direct_media_target(url, args)
        if ".m3u8" in urlparse(url).path.lower():
            return download_hls_via_segments(
                url,
                destination,
                ffmpeg,
                max_height=getattr(args, "max_height", DEFAULT_MAX_HEIGHT),
                force=getattr(args, "force", False),
            )

        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{destination.stem}.download-",
            suffix=destination.suffix,
            dir=destination.parent,
        )
        os.close(descriptor)
        temp_destination = Path(temp_name)
        try:
            print(f"Running direct download: {redact_url(url)}", file=sys.stderr)
            download_file_via_curl(url, temp_destination, max_bytes=MAX_DOWNLOAD_BYTES)
            atomic_commit(temp_destination, destination, force=getattr(args, "force", False))
        finally:
            temp_destination.unlink(missing_ok=True)
        return RouteResult(True, str(destination), DownloadRoute.DIRECT)
    except Exception as exc:
        return RouteResult(
            False,
            None,
            DownloadRoute.DIRECT,
            redact_text(str(exc)),
            error_code=ErrorCode.DIRECT_DOWNLOAD_FAILED,
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


class BatchBudget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0
        self._condition = threading.Condition()

    def reserve(self, amount: int) -> bool:
        if amount <= 0 or amount > self.limit:
            return False
        with self._condition:
            while self.used + amount > self.limit:
                self._condition.wait(timeout=1)
            self.used += amount
            return True

    def settle(self, reservation: int, actual: int) -> None:
        actual = max(0, min(actual, reservation))
        with self._condition:
            self.used = max(0, self.used - reservation) + actual
            self._condition.notify_all()


def _error_code_for_attempt(attempt: RouteResult) -> ErrorCode:
    if attempt.error_code is not ErrorCode.NONE:
        return attempt.error_code
    if attempt.route is DownloadRoute.DIRECT:
        return ErrorCode.DIRECT_DOWNLOAD_FAILED
    if attempt.route is DownloadRoute.HLS_SEGMENTED:
        return ErrorCode.HLS_DOWNLOAD_FAILED
    if attempt.route is DownloadRoute.TIKTOK_RESOLVER:
        return ErrorCode.TIKTOK_RESOLVER_FAILED
    return error_code_from_detail(attempt.detail)


def _failure_result(
    url: str,
    attempt: RouteResult,
    error_code: ErrorCode,
    message: str,
    started_at: float,
    *,
    media_state: str = "no_media_stream",
    attempt_number: int = 1,
    transcoded: bool = False,
    facts: dict[str, object] | None = None,
) -> DownloadResult:
    facts = facts or {}
    metadata = {
        "duration_ms": int((time.monotonic() - started_at) * 1000),
        "from_cache": False,
        "used_cookies": attempt.browser is not None,
        "used_fallback": attempt.route in {DownloadRoute.HLS_SEGMENTED, DownloadRoute.TIKTOK_RESOLVER},
        "transcoded": transcoded,
        "error_code": error_code.value,
        "media_state": media_state,
        "attempt_number": attempt_number,
        "has_video": bool(facts.get("has_video", False)),
        "has_audio": bool(facts.get("has_audio", False)),
        "basic_ppt_profile": bool(facts.get("basic_ppt_profile", False)),
    }
    return DownloadResult(url, False, message, None, attempt.route, error_code, metadata)


def _run_bounded_route(
    factory: object,
    route: DownloadRoute,
    output_dir: Path,
    batch_budget: BatchBudget | None,
) -> RouteResult:
    try:
        available = free_bytes(output_dir)
    except OSError as exc:
        return RouteResult(
            False,
            None,
            route,
            f"unable to inspect free disk space: {redact_text(str(exc))}",
            error_code=ErrorCode.RESOURCE_LIMIT,
        )
    if available < MIN_FREE_DISK_BYTES:
        return RouteResult(
            False,
            None,
            route,
            f"less than {MIN_FREE_DISK_BYTES} bytes of free disk space remain",
            error_code=ErrorCode.RESOURCE_LIMIT,
        )

    reservation = MAX_DOWNLOAD_BYTES if batch_budget is not None else 0
    if reservation and not batch_budget.reserve(reservation):
        return RouteResult(
            False,
            None,
            route,
            "batch budget is too small for one download",
            error_code=ErrorCode.RESOURCE_LIMIT,
        )
    try:
        attempt = factory()  # type: ignore[operator]
        if not isinstance(attempt, RouteResult):
            ok, path, detail = attempt
            attempt = RouteResult(bool(ok), path, route, detail)
        actual = 0
        if attempt.ok and attempt.path:
            candidate = Path(attempt.path)
            if candidate.exists():
                actual = candidate.stat().st_size
                if actual > MAX_DOWNLOAD_BYTES:
                    attempt = RouteResult(
                        False,
                        None,
                        attempt.route,
                        f"downloaded file exceeded {MAX_DOWNLOAD_BYTES} bytes",
                        attempt.browser,
                        ErrorCode.RESOURCE_LIMIT,
                    )
        if batch_budget is not None:
            batch_budget.settle(reservation, actual if attempt.ok else 0)
        return attempt
    except BaseException:
        if batch_budget is not None:
            batch_budget.settle(reservation, 0)
        raise

def process_url(
    url: str,
    args: argparse.Namespace,
    yt_dlp: str,
    ffmpeg: str,
    output_dir: Path,
    cookie_browsers: list[str],
    cached_entry: dict[str, object] | None,
    *,
    batch_budget: BatchBudget | None = None,
) -> DownloadResult:
    started_at = time.monotonic()
    if cached_entry:
        cached_path = str(cached_entry.get("path", ""))
        if cache_entry_is_fresh(cached_entry) and cached_file_is_usable(cached_path, ffmpeg):
            metadata = {
                "duration_ms": int((time.monotonic() - started_at) * 1000),
                "from_cache": True,
                "used_cookies": False,
                "used_fallback": False,
                "transcoded": False,
                "error_code": ErrorCode.NONE.value,
                "media_state": str(cached_entry.get("media_state", "audio_and_video")),
                "attempt_number": 0,
                "has_video": True,
                "has_audio": bool(cached_entry.get("has_audio", True)),
                "basic_ppt_profile": bool(cached_entry.get("basic_ppt_profile", False)),
                "bytes": Path(cached_path).stat().st_size if Path(cached_path).exists() else 0,
            }
            return DownloadResult(
                url,
                True,
                f"cached file: {cached_path}",
                cached_path,
                DownloadRoute.CACHE,
                ErrorCode.NONE,
                metadata,
            )

    if args.dry_run:
        metadata = {
            "duration_ms": int((time.monotonic() - started_at) * 1000),
            "from_cache": False,
            "used_cookies": False,
            "used_fallback": False,
            "transcoded": False,
            "error_code": ErrorCode.NONE.value,
            "has_video": False,
            "has_audio": False,
            "basic_ppt_profile": False,
        }
        if is_direct_media_url(url):
            message = "would download direct media"
        elif args.tiktok_shop and getattr(args, "tiktok_resolver", False) and is_tiktok_url(url):
            message = "would use HTTP resolver providers first"
        else:
            message = "would use yt-dlp social-page flow"
        return DownloadResult(url, True, message, None, DownloadRoute.DRY_RUN, ErrorCode.NONE, metadata)

    tiktok_url = is_tiktok_url(url)
    resolver_allowed = bool(getattr(args, "tiktok_resolver", False)) and tiktok_url
    resolver_first = args.tiktok_shop and resolver_allowed
    if resolver_first:
        attempt = _run_bounded_route(
            lambda: download_tiktok_via_resolvers(url, args, ffmpeg),
            DownloadRoute.TIKTOK_RESOLVER,
            output_dir,
            batch_budget,
        )
    elif is_direct_media_url(url):
        attempt = _run_bounded_route(
            lambda: download_direct_media(url, args, ffmpeg),
            DownloadRoute.DIRECT,
            output_dir,
            batch_budget,
        )
    else:
        attempt = _run_bounded_route(
            lambda: try_download_with_fallbacks(url, args, yt_dlp, ffmpeg, output_dir, cookie_browsers),
            DownloadRoute.SOCIAL,
            output_dir,
            batch_budget,
        )

    attempt_number = 1
    if not attempt.ok and resolver_allowed and not resolver_first:
        attempt_number += 1
        attempt = _run_bounded_route(
            lambda: download_tiktok_via_resolvers(url, args, ffmpeg),
            DownloadRoute.TIKTOK_RESOLVER,
            output_dir,
            batch_budget,
        )

    facts: dict[str, object] = {
        "has_video": False,
        "has_audio": False,
        "media_state": "no_media_stream",
        "basic_ppt_profile": False,
    }
    probed: dict[str, object] | None = attempt.media_probe
    if attempt.ok and attempt.path:
        if probed is None:
            probed = probe_media(attempt.path, ffmpeg)
        facts = media_facts(attempt.path, ffmpeg, probed=probed)

    if attempt.ok and attempt.path and not facts["has_video"]:
        rejection = (
            "downloaded media contained audio but no video stream"
            if facts["has_audio"]
            else "downloaded output contained neither a video nor an audio stream"
        )
        source_error = ErrorCode.AUDIO_ONLY_RESULT if facts["has_audio"] else ErrorCode.NO_MEDIA_STREAM
        if resolver_allowed and attempt.route is not DownloadRoute.TIKTOK_RESOLVER:
            attempt_number += 1
            resolver_attempt = _run_bounded_route(
                lambda: download_tiktok_via_resolvers(url, args, ffmpeg),
                DownloadRoute.TIKTOK_RESOLVER,
                output_dir,
                batch_budget,
            )
            if resolver_attempt.ok and resolver_attempt.path:
                attempt = resolver_attempt
                probed = attempt.media_probe
                if probed is None:
                    probed = probe_media(attempt.path, ffmpeg)
                facts = media_facts(attempt.path, ffmpeg, probed=probed)
                if not facts["has_video"]:
                    attempt = RouteResult(
                        False,
                        None,
                        resolver_attempt.route,
                        f"{rejection}; resolver output also had no video stream",
                        resolver_attempt.browser,
                        source_error,
                        resolver_attempt.media_probe,
                    )
                    return _failure_result(
                        url,
                        attempt,
                        source_error,
                        attempt.detail,
                        started_at,
                        media_state=str(facts.get("media_state", "no_media_stream")),
                        attempt_number=attempt_number,
                        facts=facts,
                    )
            else:
                attempt = RouteResult(
                    False,
                    None,
                    resolver_attempt.route,
                    f"{rejection}; resolver fallback failed: {resolver_attempt.detail}",
                    error_code=source_error,
                )
                return _failure_result(
                    url,
                    attempt,
                    source_error,
                    attempt.detail,
                    started_at,
                    media_state=str(facts.get("media_state", "no_media_stream")),
                    attempt_number=attempt_number,
                    facts=facts,
                )
        else:
            attempt = RouteResult(
                False,
                None,
                attempt.route,
                rejection,
                attempt.browser,
                source_error,
            )
            return _failure_result(
                url,
                attempt,
                source_error,
                rejection,
                started_at,
                media_state=str(facts.get("media_state", "no_media_stream")),
                attempt_number=attempt_number,
                facts=facts,
            )

    if attempt.ok and attempt.path and not facts["has_audio"]:
        source_can_be_silent = is_direct_media_url(url) or attempt.route in {
            DownloadRoute.DIRECT,
            DownloadRoute.HLS_SEGMENTED,
        }
        if not source_can_be_silent:
            if resolver_allowed and attempt.route is not DownloadRoute.TIKTOK_RESOLVER:
                attempt_number += 1
                resolver_attempt = _run_bounded_route(
                    lambda: download_tiktok_via_resolvers(url, args, ffmpeg),
                    DownloadRoute.TIKTOK_RESOLVER,
                    output_dir,
                    batch_budget,
                )
                if resolver_attempt.ok and resolver_attempt.path:
                    attempt = resolver_attempt
                    probed = attempt.media_probe
                    if probed is None:
                        probed = probe_media(attempt.path, ffmpeg)
                    facts = media_facts(attempt.path, ffmpeg, probed=probed)
                else:
                    attempt = RouteResult(
                        False,
                        None,
                        resolver_attempt.route,
                        "downloaded social media had no audio stream; "
                        f"resolver fallback failed: {resolver_attempt.detail}",
                        error_code=ErrorCode.AUDIO_EXPECTED_BUT_MISSING,
                    )
            if not attempt.ok or not attempt.path or not facts["has_audio"]:
                return _failure_result(
                    url,
                    attempt,
                    ErrorCode.AUDIO_EXPECTED_BUT_MISSING,
                    attempt.detail or "downloaded social media had no audio stream",
                    started_at,
                    media_state="video_only_source",
                    attempt_number=attempt_number,
                    facts=facts,
                )

    if not attempt.ok or not attempt.path:
        return _failure_result(
            url,
            attempt,
            _error_code_for_attempt(attempt),
            attempt.detail or "download failed",
            started_at,
            attempt_number=attempt_number,
        )

    saved_path = attempt.path
    transcoded = False
    compat_note = ""
    compatibility_reservation = 0
    needs_compatibility_transcode = args.ppt_compatible and not bool(
        facts.get("basic_ppt_profile", False)
    )
    if needs_compatibility_transcode and batch_budget is not None:
        compatibility_reservation = MAX_DOWNLOAD_BYTES
        if not batch_budget.reserve(compatibility_reservation):
            return _failure_result(
                url,
                attempt,
                ErrorCode.RESOURCE_LIMIT,
                "batch budget is too small for the compatibility transcode",
                started_at,
                media_state=str(facts.get("media_state", "no_media_stream")),
                attempt_number=attempt_number,
                facts=facts,
            )
    if args.ppt_compatible:
        try:
            saved_path, transcoded = make_powerpoint_compatible(
                saved_path,
                ffmpeg,
                force=getattr(args, "force", False),
                keep_metadata=getattr(args, "keep_metadata", False),
                probed=probed,
            )
            try:
                compatibility_bytes = Path(saved_path).stat().st_size
            except OSError:
                compatibility_bytes = 0
            if compatibility_bytes > MAX_DOWNLOAD_BYTES:
                if transcoded:
                    Path(saved_path).unlink(missing_ok=True)
                if compatibility_reservation:
                    batch_budget.settle(compatibility_reservation, 0)
                    compatibility_reservation = 0
                return _failure_result(
                    url,
                    attempt,
                    ErrorCode.RESOURCE_LIMIT,
                    f"compatibility output exceeded {MAX_DOWNLOAD_BYTES} bytes",
                    started_at,
                    media_state=str(facts.get("media_state", "no_media_stream")),
                    attempt_number=attempt_number,
                    transcoded=transcoded,
                    facts=facts,
                )

            compat_note = " [basic PowerPoint profile]"
            if not transcoded:
                compat_note = " [basic PowerPoint profile, no re-encode needed]"
            if transcoded:
                final_probe = probe_media(saved_path, ffmpeg)
                facts = media_facts(saved_path, ffmpeg, probed=final_probe)
            if not facts["has_video"]:
                if transcoded:
                    Path(saved_path).unlink(missing_ok=True)
                if compatibility_reservation:
                    batch_budget.settle(compatibility_reservation, 0)
                    compatibility_reservation = 0
                return _failure_result(
                    url,
                    attempt,
                    ErrorCode.NO_MEDIA_STREAM,
                    "final output lost its video stream",
                    started_at,
                    media_state="no_media_stream",
                    attempt_number=attempt_number,
                    transcoded=transcoded,
                    facts=facts,
                )
            if not facts["basic_ppt_profile"]:
                if transcoded:
                    Path(saved_path).unlink(missing_ok=True)
                if compatibility_reservation:
                    batch_budget.settle(compatibility_reservation, 0)
                    compatibility_reservation = 0
                return _failure_result(
                    url,
                    attempt,
                    ErrorCode.COMPATIBILITY_VALIDATION_FAILED,
                    "final output did not meet the basic H.264/AAC/MP4 profile",
                    started_at,
                    media_state=str(facts.get("media_state", "no_media_stream")),
                    attempt_number=attempt_number,
                    transcoded=transcoded,
                    facts=facts,
                )
            if compatibility_reservation:
                batch_budget.settle(compatibility_reservation, compatibility_bytes)
                compatibility_reservation = 0
        except BaseException:
            if compatibility_reservation:
                batch_budget.settle(compatibility_reservation, 0)
            raise

    if attempt.route is DownloadRoute.DIRECT:
        route_note = "downloaded_direct"
    elif attempt.route is DownloadRoute.SOCIAL:
        route_note = "downloaded_social_with_cookies" if attempt.browser else "downloaded_social"
    elif attempt.route is DownloadRoute.HLS_SEGMENTED:
        route_note = "downloaded_hls_segmented"
    elif attempt.route is DownloadRoute.TIKTOK_RESOLVER:
        route_note = "downloaded_tiktok_resolver"
    else:
        route_note = attempt.route.value
    if attempt.detail and attempt.route in {DownloadRoute.HLS_SEGMENTED, DownloadRoute.TIKTOK_RESOLVER}:
        route_note = f"{route_note}: {attempt.detail}"
    metadata = {
        "duration_ms": int((time.monotonic() - started_at) * 1000),
        "from_cache": False,
        "used_cookies": attempt.browser is not None,
        "used_fallback": attempt.route in {DownloadRoute.HLS_SEGMENTED, DownloadRoute.TIKTOK_RESOLVER},
        "transcoded": transcoded,
        "error_code": ErrorCode.NONE.value,
        "media_state": facts.get("media_state", "audio_and_video"),
        "attempt_number": attempt_number,
        "bytes": Path(saved_path).stat().st_size if Path(saved_path).exists() else 0,
        "has_video": bool(facts.get("has_video", False)),
        "has_audio": bool(facts.get("has_audio", False)),
        "basic_ppt_profile": bool(facts.get("basic_ppt_profile", False)),
    }
    silent_note = " [source has no audio track]" if facts.get("media_state") == "video_only_source" else ""
    return DownloadResult(
        url,
        True,
        f"{route_note}: {saved_path}{compat_note}{silent_note}",
        saved_path,
        attempt.route,
        ErrorCode.NONE,
        metadata,
    )


def print_summary(results: list[DownloadResult]) -> None:
    groups: dict[str, list[DownloadResult]] = {
        "Succeeded": [],
        "Cached": [],
        "Dry Run": [],
        "Auth Required": [],
        "Network Unstable": [],
        "Restricted": [],
        "Audio Missing": [],
        "No Media": [],
        "Resource Limits": [],
        "HLS Failed": [],
        "Resolver Failed": [],
        "Input Invalid": [],
        "Other Failures": [],
    }
    for result in results:
        if result.route is DownloadRoute.CACHE:
            groups["Cached"].append(result)
        elif result.route is DownloadRoute.DRY_RUN:
            groups["Dry Run"].append(result)
        elif result.ok:
            groups["Succeeded"].append(result)
        elif result.error_code is ErrorCode.AUTH_NEEDED:
            groups["Auth Required"].append(result)
        elif result.error_code is ErrorCode.NETWORK_UNSTABLE:
            groups["Network Unstable"].append(result)
        elif result.error_code in {ErrorCode.AUDIO_ONLY_RESULT, ErrorCode.NO_MEDIA_STREAM}:
            groups["Restricted"].append(result)
        elif result.error_code is ErrorCode.AUDIO_EXPECTED_BUT_MISSING:
            groups["Audio Missing"].append(result)
        elif result.error_code is ErrorCode.RESOURCE_LIMIT:
            groups["Resource Limits"].append(result)
        elif result.error_code is ErrorCode.HLS_DOWNLOAD_FAILED:
            groups["HLS Failed"].append(result)
        elif result.error_code is ErrorCode.TIKTOK_RESOLVER_FAILED:
            groups["Resolver Failed"].append(result)
        elif result.error_code is ErrorCode.INPUT_INVALID:
            groups["Input Invalid"].append(result)
        else:
            groups["Other Failures"].append(result)

    print(f"\nDownload summary ({len(results)} URLs):", file=sys.stderr)
    for title, entries in groups.items():
        if not entries:
            continue
        print(f"\n{title} ({len(entries)}):", file=sys.stderr)
        for result in entries:
            print(f"- {redact_url(result.url)}", file=sys.stderr)
            print(f"  {redact_text(result.message)}", file=sys.stderr)


def cache_key_for(url: str, args: argparse.Namespace, output_dir: Path) -> str:
    payload = {
        "url": url,
        "output_dir": str(output_dir),
        "max_height": args.max_height,
        "ppt_conversion": args.ppt_compatible,
        "keep_metadata": getattr(args, "keep_metadata", False),
        "identity_scope": identity_scope(args),
        "route": "direct" if is_direct_media_url(url) else classify_platform(url),
        "tiktok_shop": args.tiktok_shop,
        "tiktok_resolver": bool(getattr(args, "tiktok_resolver", False)),
        "tool_version": __version__,
    }
    return make_cache_key(payload)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = parse_args()
    try:
        if args.kpi_report is not None:
            print(render_kpi_report(args.kpi_report))
            return 0

        urls = normalize_urls(collect_urls(args))
        out_dir = output_directory(args, create=not args.dry_run)

        # A dry run must not install dependencies, create directories, touch the
        # cache salt, clean stale cache entries, or append KPI events.
        if args.dry_run:
            results: list[DownloadResult] = []
            total = len(urls)
            for index, url in enumerate(urls, start=1):
                print(f"[{index}/{total}] Planning: {redact_url(url)}", file=sys.stderr)
                result = process_url(url, args, "", "", out_dir, [], None)
                results.append(result)
            print_summary(results)
            return 0

        yt_dlp, ffmpeg = ensure_dependencies(args.install_missing)
        cookie_browsers = available_cookie_browsers() if args.auto_cookies else []
        cache = load_cache()
        results: list[DownloadResult] = []
        metrics_events: list[dict[str, object]] = []
        cache_updates: dict[str, dict[str, object]] = {}
        any_failed = False
        total = len(urls)
        max_workers = min(max(1, args.concurrency), MAX_URL_WORKERS, max(1, total))
        batch_budget = BatchBudget(MAX_BATCH_BYTES)
        run_id = uuid.uuid4().hex

        def run_with_progress(index: int, url: str) -> DownloadResult:
            print(f"[{index}/{total}] Starting: {redact_url(url)}", file=sys.stderr)
            result = process_url(
                url,
                args,
                yt_dlp,
                ffmpeg,
                out_dir,
                cookie_browsers,
                cache.get(cache_key_for(url, args, out_dir)),
                batch_budget=batch_budget,
            )
            if result.ok:
                label = Path(result.saved_path).name if result.saved_path else result.message
                print(f"[{index}/{total}] Done: {label}", file=sys.stderr)
            else:
                print(f"[{index}/{total}] Failed: {result.message}", file=sys.stderr)
            return result

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    run_with_progress,
                    index,
                    url,
                ): index
                for index, url in enumerate(urls, start=1)
            }
            indexed_results: dict[int, DownloadResult] = {}
            for future in concurrent.futures.as_completed(futures):
                index = futures[future]
                url = urls[index - 1]
                try:
                    result = future.result()
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception as exc:
                    bounded = redact_text(f"{type(exc).__name__}: {exc}")[:400]
                    result = DownloadResult(
                        url,
                        False,
                        f"worker exception: {bounded}",
                        None,
                        None,
                        ErrorCode.WORKER_EXCEPTION,
                        {
                            "duration_ms": 0,
                            "from_cache": False,
                            "used_cookies": False,
                            "used_fallback": False,
                            "transcoded": False,
                            "error_code": ErrorCode.WORKER_EXCEPTION.value,
                            "media_state": "no_media_stream",
                            "attempt_number": 1,
                            "has_video": False,
                            "has_audio": False,
                            "basic_ppt_profile": False,
                        },
                    )
                if result.ok and result.saved_path and result.route:
                    expires_at = (datetime.now(timezone.utc) + CACHE_TTL).strftime("%Y-%m-%dT%H:%M:%S%z")
                    cache_updates[cache_key_for(result.url, args, out_dir)] = {
                        "path": result.saved_path,
                        "status": result.route.value,
                        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "expires_at": expires_at,
                        "media_state": str(result.metadata.get("media_state", "audio_and_video")),
                        "has_audio": bool(result.metadata.get("has_audio", False)),
                        "max_height": args.max_height,
                        "ppt_conversion": args.ppt_compatible,
                        "basic_ppt_profile": bool(result.metadata.get("basic_ppt_profile", False)),
                        "keep_metadata": args.keep_metadata,
                        "tool_version": __version__,
                    }
                if not result.ok:
                    any_failed = True
                indexed_results[index] = result
                facts = {
                    "has_video": bool(result.metadata.get("has_video", False)),
                    "has_audio": bool(result.metadata.get("has_audio", False)),
                    "basic_ppt_profile": bool(result.metadata.get("basic_ppt_profile", False)),
                }
                metrics_events.append({
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "run_id": run_id,
                    "event_schema_version": 2,
                    "tool_version": __version__,
                    "url_hash": hash_sensitive_text(result.url),
                    "platform": classify_platform(result.url),
                    "route": result.route.value if result.route else "unknown",
                    "terminal_status": "success" if result.ok else "failure",
                    "error_code": result.error_code.value,
                    "attempt_number": result.metadata.get("attempt_number", 1),
                    "from_cache": result.metadata.get("from_cache", False),
                    "used_cookies": result.metadata.get("used_cookies", False),
                    "used_fallback": result.metadata.get("used_fallback", False),
                    "transcoded": result.metadata.get("transcoded", False),
                    "success": result.ok,
                    "duration_ms": result.metadata.get("duration_ms", 0),
                    "bytes": int(result.metadata.get("bytes", 0) or 0),
                    "has_video": facts["has_video"],
                    "has_audio": facts["has_audio"],
                    "basic_ppt_profile": facts["basic_ppt_profile"],
                    "simulated": result.route is DownloadRoute.DRY_RUN,
                })

        results = [indexed_results[index] for index in sorted(indexed_results)]

        merge_cache_entries(cache_updates)
        append_metrics_events(metrics_events)

        print_summary(results)
        return 1 if any_failed else 0
    except Exception as exc:  # pragma: no cover
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
