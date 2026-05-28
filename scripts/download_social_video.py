#!/usr/bin/env python3
"""Download social-media videos with balanced quality and PowerPoint compatibility."""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PureWindowsPath
from urllib.parse import unquote, urlparse

from constants import (
    CACHE_TTL,
    DEFAULT_FORMAT,
    DEFAULT_MAX_HEIGHT,
    DEFAULT_OUTPUT_DIR,
    URL_WORKERS,
    DownloadResult,
    __version__,
    sanitize_filename,
    summarize_error,
)
from cache import (
    append_metrics_events,
    cache_entry_is_fresh,
    load_cache,
    save_cache,
)
from deps import available_cookie_browsers, ensure_dependencies, which_or_none
from hls import download_hls_via_segments, run_hls_fast_path_with_stall_detection
from kpi import classify_error_category, render_kpi_report
from media_probe import (
    cached_file_is_usable,
    has_audio_stream,
    has_video_stream,
    make_powerpoint_compatible,
    media_facts,
)
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
        type=int,
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
        default=True,
        help="Retry failed downloads with browser cookies automatically.",
    )
    parser.add_argument(
        "--text-file",
        help="Read additional URLs from a text file.",
    )
    parser.add_argument(
        "--install-missing",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Automatically install missing yt-dlp/ffmpeg with Homebrew.",
    )
    parser.add_argument(
        "--ppt-compatible",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Convert each downloaded file to H.264 video + AAC audio for QuickTime and PowerPoint compatibility.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
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
        default=True,
        help="Use HTTP resolver providers when TikTok local extraction fails or returns no usable video.",
    )
    parser.add_argument(
        "--tiktok-shop",
        action="store_true",
        help="Treat TikTok inputs as known Shop/promoted videos and try HTTP resolver providers first.",
    )
    parser.add_argument(
        "--kpi-report",
        nargs="?",
        type=int,
        const=7,
        metavar="DAYS",
        help="Print a KPI summary for the last N days. Defaults to 7 when provided without a value.",
    )
    args = parser.parse_args()
    if args.kpi_report is None and not args.inputs:
        parser.error("at least one input URL or --kpi-report is required")
    return args


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def output_directory(args: argparse.Namespace) -> Path:
    output_dir = Path(os.path.expanduser(args.output_dir)).resolve()
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
        "4",
        "--socket-timeout",
        "30",
        "--paths",
        str(output_dir),
        "--output",
        "%(title).180B [%(id)s].%(ext)s",
        "--format",
        format_selector,
        "--merge-output-format",
        "mp4",
        "--remux-video",
        "mp4",
        "--embed-metadata",
        "--print",
        "after_move:%(filepath)s",
        "--ffmpeg-location",
        ffmpeg,
    ]

    cookie_source = browser or args.cookies_from_browser
    if cookie_source:
        cmd.extend(["--cookies-from-browser", cookie_source])

    cmd.append(url)
    return cmd


def run_download(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=1800)


def try_download_with_fallbacks(
    url: str,
    args: argparse.Namespace,
    yt_dlp: str,
    ffmpeg: str,
    output_dir: Path,
    cookie_browsers: list[str],
) -> tuple[bool, str | None, str]:
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
        print("Running:", " ".join(cmd), file=sys.stderr)
        result = run_download(cmd)
        if result.returncode == 0:
            filepaths = extract_filepaths(result.stdout)
            if filepaths:
                return True, filepaths[-1], browser or "none"
            last_error = "yt-dlp exited successfully but produced no output file path."
            if browser is not None:
                continue
            break

        last_error = summarize_error(result)
        if browser is not None:
            continue
        if not args.auto_cookies or not looks_like_auth_failure(last_error):
            break

    return False, None, last_error


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

    suffix = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return output_directory(args) / f"{sanitize_filename(name)}-{suffix}{ext}"


def download_direct_media(
    url: str, args: argparse.Namespace, ffmpeg: str
) -> tuple[bool, str | None, str]:
    destination = direct_media_target(url, args)
    temp_destination = destination.with_name(f"{destination.stem} [download-tmp]{destination.suffix}")

    if ".m3u8" in urlparse(url).path.lower():
        cmd = [
            ffmpeg,
            "-y",
            "-protocol_whitelist",
            "file,http,https,tcp,tls,crypto",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_on_network_error",
            "1",
            "-reconnect_delay_max",
            "5",
            "-i",
            url,
            "-map",
            "0:v:0?",
            "-map",
            "0:a:0?",
            "-dn",
            "-c",
            "copy",
            "-bsf:a",
            "aac_adtstoasc",
            str(temp_destination),
        ]
    else:
        curl = which_or_none("curl")
        if curl is None:
            return False, None, "curl is required for direct media URLs but is not available."
        cmd = [
            curl,
            "-L",
            "--fail",
            "--retry",
            "5",
            "--retry-all-errors",
            "--retry-delay",
            "1",
            "--silent",
            "--show-error",
            "-o",
            str(temp_destination),
            url,
        ]

    print("Running direct download:", " ".join(cmd), file=sys.stderr)
    is_hls = ".m3u8" in urlparse(url).path.lower()
    if is_hls:
        ok, detail = run_hls_fast_path_with_stall_detection(cmd, temp_destination)
        if not ok:
            temp_destination.unlink(missing_ok=True)
            print(f"Direct HLS capture did not finish cleanly: {detail}", file=sys.stderr)
            print("Trying segmented HLS fallback.", file=sys.stderr)
            return download_hls_via_segments(url, destination, ffmpeg)
    else:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if result.returncode != 0:
            temp_destination.unlink(missing_ok=True)
            return False, None, summarize_error(result)

    os.replace(temp_destination, destination)
    return True, str(destination), "direct"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def process_url(
    url: str,
    args: argparse.Namespace,
    yt_dlp: str,
    ffmpeg: str,
    output_dir: Path,
    cookie_browsers: list[str],
    cached_entry: dict[str, str] | None,
) -> DownloadResult:
    started_at = time.monotonic()
    if cached_entry:
        cached_path = cached_entry.get("path", "")
        cached_status = cached_entry.get("status", "success_cached")
        if cache_entry_is_fresh(cached_entry) and cached_file_is_usable(cached_path, ffmpeg):
            duration_ms = int((time.monotonic() - started_at) * 1000)
            metadata = {
                "duration_ms": duration_ms,
                "from_cache": True,
                "used_cookies": False,
                "used_fallback": False,
                "transcoded": False,
                "error_category": "none",
            }
            return DownloadResult(url, True, f"success_cached:{cached_status}: {cached_path}", cached_path, cached_status, metadata)
        if args.dry_run:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            metadata = {
                "duration_ms": duration_ms,
                "from_cache": False,
                "used_cookies": False,
                "used_fallback": False,
                "transcoded": False,
                "error_category": "none",
            }
            return DownloadResult(url, True, "dry_run: cache entry exists but is stale or the file is no longer usable", None, None, metadata)

    if args.dry_run:
        duration_ms = int((time.monotonic() - started_at) * 1000)
        metadata = {
            "duration_ms": duration_ms,
            "from_cache": False,
            "used_cookies": False,
            "used_fallback": False,
            "transcoded": False,
            "error_category": "none",
        }
        if is_direct_media_url(url):
            return DownloadResult(url, True, "dry_run: direct_media -> will download directly", None, None, metadata)
        if args.tiktok_shop and args.tiktok_resolver and is_tiktok_url(url):
            return DownloadResult(url, True, "dry_run: tiktok_shop -> will use HTTP resolver providers first", None, None, metadata)
        return DownloadResult(url, True, "dry_run: social_page -> will use yt-dlp flow", None, None, metadata)

    tiktok_url = is_tiktok_url(url)
    resolver_allowed = args.tiktok_resolver and tiktok_url
    resolver_first = args.tiktok_shop and resolver_allowed

    if resolver_first:
        ok, detail, extra = download_tiktok_via_resolvers(url, args, ffmpeg)
    elif is_direct_media_url(url):
        ok, detail, extra = download_direct_media(url, args, ffmpeg)
    else:
        ok, detail, extra = try_download_with_fallbacks(url, args, yt_dlp, ffmpeg, output_dir, cookie_browsers)

    if not ok and resolver_allowed and not resolver_first:
        ok, detail, extra = download_tiktok_via_resolvers(url, args, ffmpeg)

    if ok and detail:
        facts = media_facts(detail, ffmpeg)
        rejection = ""
        if not facts["has_video"]:
            rejection = (
                "restricted_audio_only: Downloaded media contained no video stream. "
                "The platform currently exposed only audio for this URL; try a logged-in "
                "browser session or a different extractor path."
            )
        elif not facts["has_audio"]:
            rejection = (
                "restricted_audio_only: Downloaded media contained no audio stream. "
                "The source did not provide a usable video-with-audio result for this URL."
            )
        if rejection:
            Path(detail).unlink(missing_ok=True)
            if resolver_allowed and not extra.startswith("success_tiktok_resolver"):
                ok, detail, resolver_result = download_tiktok_via_resolvers(url, args, ffmpeg)
                if not ok:
                    extra = f"{rejection} Resolver fallback failed: {resolver_result}"
                else:
                    extra = resolver_result
            else:
                ok, detail, extra = False, None, rejection

    if ok:
        saved_path = detail or f"Saved under {output_dir}"
        compat_note = ""
        transcoded = False
        if args.ppt_compatible and detail:
            saved_path, transcoded = make_powerpoint_compatible(detail, ffmpeg)
            compat_note = " [PowerPoint-compatible]"
            if not transcoded:
                compat_note = " [PowerPoint-compatible, no re-encode needed]"

        if extra == "direct":
            route_note = "success_direct"
        elif extra == "none":
            route_note = "success_social"
        elif extra.startswith("success_direct_hls_fallback"):
            route_note = extra
        elif extra.startswith("success_tiktok_resolver"):
            route_note = extra
        else:
            route_note = f"success_social_cookies:{extra}"
        duration_ms = int((time.monotonic() - started_at) * 1000)
        metadata = {
            "duration_ms": duration_ms,
            "from_cache": False,
            "used_cookies": extra not in {"none", "direct"}
            and not extra.startswith(("success_direct_hls_fallback", "success_tiktok_resolver")),
            "used_fallback": extra.startswith(("success_direct_hls_fallback", "success_tiktok_resolver")),
            "transcoded": transcoded,
            "error_category": "none",
        }
        return DownloadResult(url, True, f"{route_note}: {saved_path}{compat_note}", saved_path, route_note, metadata)

    lowered = extra.lower()
    error_detail = extra
    if any(token in lowered for token in ["login", "sign in", "private", "authentication"]):
        error_detail = f"auth_needed: {extra}"
    elif any(token in lowered for token in ["timed out", "ssl", "network", "connection reset", "temporarily unavailable"]):
        error_detail = f"network_unstable: {extra}"
    elif "no supported urls were found" in lowered:
        error_detail = f"input_invalid: {extra}"
    duration_ms = int((time.monotonic() - started_at) * 1000)
    metadata = {
        "duration_ms": duration_ms,
        "from_cache": False,
        "used_cookies": False,
        "used_fallback": extra.startswith("tiktok_resolver_failed:") or "Resolver fallback failed:" in extra,
        "transcoded": False,
        "error_category": classify_error_category(error_detail),
    }
    return DownloadResult(url, False, error_detail, None, None, metadata)


def print_summary(results: list[tuple[str, bool, str]]) -> None:
    groups: dict[str, list[tuple[str, str]]] = {
        "Succeeded": [],
        "Cached": [],
        "Dry Run": [],
        "Auth Required": [],
        "Network Unstable": [],
        "Restricted": [],
        "Resolver Failed": [],
        "Input Invalid": [],
        "Other Failures": [],
    }
    for url, ok, detail in results:
        if detail.startswith("success_cached:"):
            groups["Cached"].append((url, detail))
        elif detail.startswith("dry_run:"):
            groups["Dry Run"].append((url, detail))
        elif ok:
            groups["Succeeded"].append((url, detail))
        elif detail.startswith("auth_needed:"):
            groups["Auth Required"].append((url, detail))
        elif detail.startswith("network_unstable:"):
            groups["Network Unstable"].append((url, detail))
        elif detail.startswith("restricted_audio_only:"):
            groups["Restricted"].append((url, detail))
        elif detail.startswith("tiktok_resolver_failed:"):
            groups["Resolver Failed"].append((url, detail))
        elif detail.startswith("input_invalid:"):
            groups["Input Invalid"].append((url, detail))
        else:
            groups["Other Failures"].append((url, detail))

    print(f"\nDownload summary ({len(results)} URLs):", file=sys.stderr)
    for title, entries in groups.items():
        if not entries:
            continue
        print(f"\n{title} ({len(entries)}):", file=sys.stderr)
        for url, detail in entries:
            print(f"- {url}", file=sys.stderr)
            print(f"  {detail}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = parse_args()
    try:
        if args.kpi_report is not None:
            print(render_kpi_report(args.kpi_report))
            return 0

        yt_dlp, ffmpeg = ensure_dependencies(args.install_missing)
        urls = normalize_urls(collect_urls(args))
        out_dir = output_directory(args)
        cookie_browsers = available_cookie_browsers()
        cache = load_cache()
        results: list[tuple[str, bool, str]] = []
        metrics_events: list[dict[str, object]] = []
        any_failed = False
        total = len(urls)
        max_workers = min(max(1, args.concurrency), max(1, total))

        def run_with_progress(index: int, url: str) -> DownloadResult:
            print(f"[{index}/{total}] Starting: {url}", file=sys.stderr)
            result = process_url(
                url,
                args,
                yt_dlp,
                ffmpeg,
                out_dir,
                cookie_browsers,
                cache.get(url),
            )
            _, ok, message, saved_path, _, _ = result
            if ok:
                label = Path(saved_path).name if saved_path else message
                print(f"[{index}/{total}] Done: {label}", file=sys.stderr)
            else:
                print(f"[{index}/{total}] Failed: {message}", file=sys.stderr)
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
            indexed_results: dict[int, tuple[str, bool, str]] = {}
            for future in concurrent.futures.as_completed(futures):
                index = futures[future]
                url, ok, message, saved_path, status, metadata = future.result()
                if ok and saved_path and status:
                    expires_at = (datetime.now(timezone.utc) + CACHE_TTL).strftime("%Y-%m-%dT%H:%M:%S%z")
                    cache[url] = {
                        "path": saved_path,
                        "status": status,
                        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "expires_at": expires_at,
                    }
                if not ok:
                    any_failed = True
                indexed_results[index] = (url, ok, message)
                facts = media_facts(saved_path, ffmpeg) if saved_path else media_facts(None, ffmpeg)
                metrics_events.append({
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "version": __version__,
                    "url_hash": hashlib.sha1(url.encode("utf-8")).hexdigest()[:12],
                    "platform": classify_platform(url),
                    "flow_type": status or classify_error_category(message),
                    "from_cache": metadata["from_cache"],
                    "used_cookies": metadata["used_cookies"],
                    "used_fallback": metadata["used_fallback"],
                    "transcoded": metadata["transcoded"],
                    "success": ok,
                    "error_category": metadata["error_category"] if not ok else "none",
                    "duration_ms": metadata["duration_ms"],
                    "has_video": facts["has_video"],
                    "has_audio": facts["has_audio"],
                    "ppt_compatible": facts["ppt_compatible"],
                    "simulated": message.startswith("dry_run:"),
                })

        results = [indexed_results[index] for index in sorted(indexed_results)]

        save_cache(cache)
        append_metrics_events(metrics_events)

        print_summary(results)
        return 1 if any_failed else 0
    except Exception as exc:  # pragma: no cover
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
