"""Download route adapters for social pages and direct media URLs."""
from __future__ import annotations

import os
import selectors
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from pathlib import PureWindowsPath
from urllib.parse import unquote, urlparse

from constants import (
    DEFAULT_FORMAT,
    DEFAULT_MAX_HEIGHT,
    DownloadOptions,
    DownloadRoute,
    ErrorCode,
    MAX_CONCURRENT_FRAGMENTS,
    MAX_DOWNLOAD_BYTES,
    RouteResult,
    redact_text,
    redact_url,
    sanitize_filename,
    summarize_error,
)
from cache import hash_sensitive_text
from file_ops import atomic_commit, non_conflicting_path
from hls import download_hls_via_segments
from net import download_file_via_curl, validate_remote_url


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


def identity_scope(options: DownloadOptions) -> str:
    if options.cookies_from_browser:
        return f"browser:{options.cookies_from_browser}"
    if options.auto_cookies:
        return "auto-browser-cookie"
    return "anonymous"


def build_command(
    url: str,
    options: DownloadOptions,
    yt_dlp: str,
    ffmpeg: str,
    browser: str | None = None,
) -> list[str]:
    format_selector = DEFAULT_FORMAT.format(max_height=options.max_height)
    variant = hash_sensitive_text(
        f"{url}\0{options.max_height}\0{options.ppt_compatible}\0"
        f"{options.keep_metadata}\0{identity_scope(options)}"
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
        str(options.output_dir),
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

    if options.force:
        cmd.remove("--no-overwrites")
        cmd.extend(["--force-overwrites"])
    if options.keep_metadata:
        cmd.append("--embed-metadata")

    cookie_source = browser or options.cookies_from_browser
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
                    "yt-dlp timed out after 30 minutes\n"
                    + stderr_tail.decode("utf-8", errors="replace"),
                )
            for key, _ in selector.select(timeout=0.5):
                try:
                    chunk = os.read(key.fileobj.fileno(), 8192)
                except OSError:
                    chunk = b""
                if chunk:
                    _bounded_tail(
                        stdout_tail if key.fileobj is process.stdout else stderr_tail,
                        chunk,
                    )
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
    options: DownloadOptions,
    yt_dlp: str,
    ffmpeg: str,
    cookie_browsers: tuple[str, ...],
) -> RouteResult:
    attempts: list[str | None] = [options.cookies_from_browser]
    if options.cookies_from_browser is None and options.auto_cookies:
        attempts.extend(cookie_browsers)

    tried: set[str | None] = set()
    last_error = ""
    for browser in attempts:
        if browser in tried:
            continue
        tried.add(browser)
        cmd = build_command(url, options, yt_dlp, ffmpeg, browser)
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
        if not options.auto_cookies or not looks_like_auth_failure(last_error):
            break

    return RouteResult(
        False,
        None,
        DownloadRoute.SOCIAL,
        last_error,
        error_code=error_code_from_detail(last_error),
    )


def direct_media_target(url: str, options: DownloadOptions) -> Path:
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
    target = options.output_dir / f"{sanitize_filename(name)}-{suffix}{ext}"
    return non_conflicting_path(target, force=options.force)


def download_direct_media(
    url: str, options: DownloadOptions, ffmpeg: str
) -> RouteResult:
    try:
        validate_remote_url(url)
        destination = direct_media_target(url, options)
        if ".m3u8" in urlparse(url).path.lower():
            return download_hls_via_segments(
                url,
                destination,
                ffmpeg,
                max_height=options.max_height or DEFAULT_MAX_HEIGHT,
                force=options.force,
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
            atomic_commit(temp_destination, destination, force=options.force)
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
