#!/usr/bin/env python3
"""Download social-media videos with balanced quality and a basic presentation profile."""
from __future__ import annotations

import argparse
import concurrent.futures
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from constants import (
    CACHE_TTL,
    DEFAULT_MAX_HEIGHT,
    DEFAULT_OUTPUT_DIR,
    DownloadOptions,
    DownloadResult,
    DownloadRoute,
    ErrorCode,
    MAX_BATCH_BYTES,
    MAX_KPI_DAYS,
    MAX_OUTPUT_HEIGHT,
    MAX_URL_WORKERS,
    URL_WORKERS,
    __version__,
    redact_text,
    redact_url,
)
from cache import (
    append_metrics_events,
    hash_sensitive_text,
    load_cache,
    make_cache_key,
    merge_cache_entries,
)
from deps import available_cookie_browsers, ensure_dependencies
from download_routes import (
    build_command,
    direct_media_target,
    download_direct_media,
    error_code_from_detail,
    extract_filepaths,
    identity_scope,
    is_absolute_path,
    looks_like_auth_failure,
    run_download,
    try_download_with_fallbacks,
)
from download_workflow import (
    BatchBudget,
    DownloadServices,
    _error_code_for_attempt,
    _failure_result,
    _run_bounded_route,
    download_tiktok_via_resolvers,
    free_bytes,
    media_facts,
    process_url,
)
from kpi import render_kpi_report
from urls import classify_platform, collect_urls, is_direct_media_url, normalize_urls


# Route and workflow symbols remain importable here for callers that used the
# original single-module entrypoint. Internal tests patch their owning module.

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


def output_directory(args: argparse.Namespace, *, create: bool = True) -> Path:
    output_dir = Path(os.path.expanduser(args.output_dir)).resolve()
    if create:
        output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def options_for_args(args: argparse.Namespace, output_dir: Path) -> DownloadOptions:
    return DownloadOptions(
        output_dir=output_dir,
        max_height=args.max_height,
        cookies_from_browser=getattr(args, "cookies_from_browser", None),
        auto_cookies=getattr(args, "auto_cookies", False),
        ppt_compatible=getattr(args, "ppt_compatible", True),
        tiktok_resolver=bool(getattr(args, "tiktok_resolver", False)),
        tiktok_shop=getattr(args, "tiktok_shop", False),
        force=getattr(args, "force", False),
        keep_metadata=getattr(args, "keep_metadata", False),
        dry_run=getattr(args, "dry_run", False),
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
    options = options_for_args(args, output_dir)
    payload = {
        "url": url,
        "output_dir": str(options.output_dir),
        "max_height": options.max_height,
        "ppt_conversion": options.ppt_compatible,
        "keep_metadata": options.keep_metadata,
        "identity_scope": identity_scope(options),
        "route": "direct" if is_direct_media_url(url) else classify_platform(url),
        "tiktok_shop": options.tiktok_shop,
        "tiktok_resolver": options.tiktok_resolver,
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
        options = options_for_args(args, out_dir)

        # A dry run must not install dependencies, create directories, touch the
        # cache salt, clean stale cache entries, or append KPI events.
        if args.dry_run:
            results: list[DownloadResult] = []
            total = len(urls)
            for index, url in enumerate(urls, start=1):
                print(f"[{index}/{total}] Planning: {redact_url(url)}", file=sys.stderr)
                result = process_url(url, options, DownloadServices("", ""), None)
                results.append(result)
            print_summary(results)
            return 0

        yt_dlp, ffmpeg = ensure_dependencies(args.install_missing)
        cookie_browsers = available_cookie_browsers() if args.auto_cookies else []
        services = DownloadServices(yt_dlp, ffmpeg, tuple(cookie_browsers))
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
                options,
                services,
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
