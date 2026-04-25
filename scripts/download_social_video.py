#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from pathlib import PureWindowsPath
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse


__version__ = "0.3.1"
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
CACHE_PATH = Path.home() / ".codex" / "skills" / "social-video-downloader" / "cache" / "downloads.json"
METRICS_LOG_PATH = Path.home() / ".codex" / "skills" / "social-video-downloader" / "metrics" / "runs.jsonl"
CACHE_TTL = timedelta(days=7)
TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {"fbclid", "igshid", "ref"}


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


def which_or_none(name: str) -> str | None:
    return shutil.which(name)


def install_with_homebrew(packages: list[str]) -> None:
    brew = which_or_none("brew")
    if not brew:
        raise RuntimeError(
            "Missing dependencies: "
            + ", ".join(packages)
            + ". Homebrew is not available for automatic installation."
        )

    cmd = [brew, "install", *packages]
    print("Installing missing dependencies:", " ".join(packages), file=sys.stderr)
    subprocess.run(cmd, check=True)


def ensure_dependencies(install_missing: bool) -> tuple[str, str]:
    yt_dlp = which_or_none("yt-dlp")
    ffmpeg = which_or_none("ffmpeg")
    missing = []
    if yt_dlp is None:
        missing.append("yt-dlp")
    if ffmpeg is None:
        missing.append("ffmpeg")

    if missing:
        if not install_missing:
            raise RuntimeError(
                "Missing required dependencies: "
                + ", ".join(missing)
                + ". Re-run with --install-missing or install them manually."
            )
        install_with_homebrew(missing)
        yt_dlp = which_or_none("yt-dlp")
        ffmpeg = which_or_none("ffmpeg")

    if yt_dlp is None or ffmpeg is None:
        raise RuntimeError("Unable to locate yt-dlp and ffmpeg after installation.")

    return yt_dlp, ffmpeg


def browser_cookies_available(browser: str) -> bool:
    paths = browser_cookie_paths(browser)
    return any(path.exists() for path in paths)


def available_cookie_browsers() -> list[str]:
    return [browser for browser in AUTO_COOKIE_BROWSERS if browser_cookies_available(browser)]


def browser_cookie_paths(browser: str) -> list[Path]:
    home = Path.home()
    local_app_data = os.environ.get("LOCALAPPDATA")
    paths_by_browser: dict[str, list[Path]] = {
        "chrome": [
            home / "Library/Application Support/Google/Chrome/Default/Cookies",
            home / ".config/google-chrome/Default/Cookies",
        ],
        "brave": [
            home / "Library/Application Support/BraveSoftware/Brave-Browser/Default/Cookies",
            home / ".config/BraveSoftware/Brave-Browser/Default/Cookies",
        ],
        "edge": [
            home / "Library/Application Support/Microsoft Edge/Default/Cookies",
            home / ".config/microsoft-edge/Default/Cookies",
        ],
        "chromium": [
            home / "Library/Application Support/Chromium/Default/Cookies",
            home / ".config/chromium/Default/Cookies",
        ],
        "firefox": [
            home / "Library/Application Support/Firefox/Profiles",
            home / ".mozilla/firefox",
        ],
        "safari": [
            home / "Library/Cookies/Cookies.binarycookies",
            home / "Library/Cookies",
        ],
    }
    if local_app_data:
        win_base = Path(local_app_data)
        paths_by_browser["chrome"].append(win_base / "Google/Chrome/User Data/Default/Cookies")
        paths_by_browser["brave"].append(win_base / "BraveSoftware/Brave-Browser/User Data/Default/Cookies")
        paths_by_browser["edge"].append(win_base / "Microsoft/Edge/User Data/Default/Cookies")
        paths_by_browser["chromium"].append(win_base / "Chromium/User Data/Default/Cookies")
        paths_by_browser["firefox"].append(win_base / "Mozilla/Firefox/Profiles")
    return paths_by_browser.get(browser, [])


def output_directory(args: argparse.Namespace) -> Path:
    output_dir = Path(os.path.expanduser(args.output_dir)).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def ensure_cache_dir() -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)


def ensure_metrics_dir() -> None:
    METRICS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def cache_entry_expiry(entry: dict[str, str]) -> datetime | None:
    expires_at = entry.get("expires_at")
    if expires_at:
        try:
            return datetime.strptime(expires_at, "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc)
        except ValueError:
            return None

    updated_at = entry.get("updated_at")
    if not updated_at:
        return None
    try:
        timestamp = datetime.strptime(updated_at, "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc)
    except ValueError:
        return None
    return timestamp + CACHE_TTL


def load_cache() -> dict[str, dict[str, str]]:
    ensure_cache_dir()
    if not CACHE_PATH.exists():
        return {}
    try:
        raw_cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw_cache, dict):
        return {}

    now = datetime.now(timezone.utc)
    normalized_cache: dict[str, dict[str, str]] = {}
    changed = False
    for url, entry in raw_cache.items():
        if not isinstance(entry, dict):
            changed = True
            continue
        expiry = cache_entry_expiry(entry)
        if expiry is None:
            changed = True
            continue
        if now > expiry:
            changed = True
            continue
        if "expires_at" not in entry:
            entry = dict(entry)
            entry["expires_at"] = expiry.strftime("%Y-%m-%dT%H:%M:%S%z")
            changed = True
        normalized_cache[url] = entry

    if changed:
        save_cache(normalized_cache)
    return normalized_cache


def save_cache(cache: dict[str, dict[str, str]]) -> None:
    ensure_cache_dir()
    temp_path = CACHE_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, CACHE_PATH)


def append_metrics_events(events: list[dict[str, object]]) -> None:
    if not events:
        return
    ensure_metrics_dir()
    with METRICS_LOG_PATH.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def load_metrics_events(days: int) -> list[dict[str, object]]:
    if not METRICS_LOG_PATH.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    events: list[dict[str, object]] = []
    with METRICS_LOG_PATH.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            timestamp = event.get("timestamp")
            if not isinstance(timestamp, str):
                continue
            try:
                dt = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc)
            except ValueError:
                continue
            if dt >= cutoff:
                events.append(event)
    return events


def extract_urls_from_text(text: str) -> list[str]:
    return URL_PATTERN.findall(text)


def collect_urls(args: argparse.Namespace) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    chunks = list(args.inputs)
    if args.text_file:
        text_file = Path(args.text_file).expanduser()
        if not text_file.exists():
            raise RuntimeError(f"--text-file does not exist: {text_file}")
        if not text_file.is_file():
            raise RuntimeError(f"--text-file is not a regular file: {text_file}")
        chunks.append(text_file.read_text(encoding="utf-8"))

    for chunk in chunks:
        candidates = [chunk] if chunk.startswith(("http://", "https://")) else extract_urls_from_text(chunk)
        for candidate in candidates:
            if candidate not in seen:
                seen.add(candidate)
                urls.append(candidate)

    if not urls:
        raise RuntimeError("No supported URLs were found in the provided input.")

    return urls


def normalize_urls(urls: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for url in urls:
        cleaned = url.rstrip(".,;!?")
        parsed = urlparse(cleaned)
        query_items = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key not in TRACKING_QUERY_KEYS and not key.startswith(TRACKING_QUERY_PREFIXES)
        ]
        cleaned = urlunparse(parsed._replace(query=urlencode(query_items), fragment=""))
        if cleaned not in seen:
            seen.add(cleaned)
            normalized.append(cleaned)
    return normalized


def classify_platform(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "tiktok.com" in host or "douyin" in host:
        return "tiktok"
    if "instagram.com" in host:
        return "instagram"
    if "facebook.com" in host or "fb.watch" in host:
        return "facebook"
    if "twitter.com" in host or "x.com" in host:
        return "x"
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    return "direct-media" if is_direct_media_url(url) else host or "unknown"


def is_absolute_path(text: str) -> bool:
    return Path(text).is_absolute() or PureWindowsPath(text).is_absolute()


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


def is_direct_media_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(DIRECT_MEDIA_EXTENSIONS) or ".mp4/" in path or ".m3u8/" in path


def sanitize_filename(name: str) -> str:
    cleaned = SAFE_FILENAME_PATTERN.sub("-", name).strip(" .-_")
    return cleaned or "downloaded-video"


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


def build_segment_url(playlist_url: str, segment: str) -> str:
    resolved = urljoin(playlist_url, segment)
    parsed_segment = urlparse(segment)
    base_query = urlparse(playlist_url).query
    if parsed_segment.query or not base_query:
        return resolved
    resolved_parsed = urlparse(resolved)
    return urlunparse(resolved_parsed._replace(query=base_query))


def parse_hls_attribute_list(text: str) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for part in text.split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        attributes[key.strip().upper()] = value.strip().strip('"')
    return attributes


def extract_hls_playlist_entries(playlist_text: str) -> tuple[list[str], list[tuple[int, str]]]:
    media_segments: list[str] = []
    variants: list[tuple[int, str]] = []
    pending_variant_bandwidth: int | None = None

    for raw_line in playlist_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = parse_hls_attribute_list(line.split(":", 1)[1])
            try:
                pending_variant_bandwidth = int(attrs.get("BANDWIDTH", "0"))
            except ValueError:
                pending_variant_bandwidth = 0
            continue
        if line.startswith("#"):
            continue
        if pending_variant_bandwidth is not None:
            variants.append((pending_variant_bandwidth, line))
            pending_variant_bandwidth = None
            continue
        media_segments.append(line)

    return media_segments, variants


def resolve_hls_media_playlist_url(playlist_url: str) -> str:
    current_url = playlist_url
    for _ in range(HLS_MAX_PLAYLIST_DEPTH):
        playlist_text = fetch_text_via_curl(current_url)
        media_segments, variants = extract_hls_playlist_entries(playlist_text)
        if media_segments:
            return current_url
        if not variants:
            break
        _, selected_variant = max(variants, key=lambda item: item[0])
        current_url = build_segment_url(current_url, selected_variant)
    raise RuntimeError("HLS playlist did not resolve to media segments.")


def fetch_text_via_curl(url: str) -> str:
    curl = which_or_none("curl")
    if curl is None:
        raise RuntimeError("curl is required for direct media fallback but is not available.")
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
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(summarize_error(result))
    return result.stdout


def download_file_via_curl(url: str, destination: Path) -> None:
    curl = which_or_none("curl")
    if curl is None:
        raise RuntimeError("curl is required for direct media fallback but is not available.")
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
        str(destination),
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(summarize_error(result))


def download_hls_via_segments(url: str, destination: Path, ffmpeg: str) -> tuple[bool, str | None, str]:
    with tempfile.TemporaryDirectory(prefix="social-video-hls-") as tmp_dir:
        temp_root = Path(tmp_dir)
        parts_dir = temp_root / "parts"
        parts_dir.mkdir(parents=True, exist_ok=True)

        media_playlist_url = resolve_hls_media_playlist_url(url)
        playlist_text = fetch_text_via_curl(media_playlist_url)
        segments, _ = extract_hls_playlist_entries(playlist_text)
        if not segments:
            return False, None, "HLS playlist contained no media segments."

        def fetch_segment(item: tuple[int, str]) -> None:
            index, segment = item
            segment_url = build_segment_url(media_playlist_url, segment)
            segment_path = parts_dir / f"{index:05d}.ts"
            download_file_via_curl(segment_url, segment_path)

        with concurrent.futures.ThreadPoolExecutor(max_workers=HLS_SEGMENT_WORKERS) as executor:
            futures = [
                executor.submit(fetch_segment, (index, segment))
                for index, segment in enumerate(segments, start=1)
            ]
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    for pending in futures:
                        pending.cancel()
                    return False, None, f"HLS segment download failed: {exc}"

        concat_file = temp_root / "concat.txt"
        concat_lines = [f"file '{segment.as_posix()}'\n" for segment in sorted(parts_dir.glob("*.ts"))]
        concat_file.write_text("".join(concat_lines), encoding="utf-8")

        temp_destination = destination.with_name(f"{destination.stem} [merge-tmp]{destination.suffix}")
        cmd = [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
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
        print("Running HLS fallback merge:", " ".join(cmd), file=sys.stderr)
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            temp_destination.unlink(missing_ok=True)
            return False, None, summarize_error(result)

        os.replace(temp_destination, destination)
        return True, str(destination), f"success_direct_hls_fallback ({len(segments)} segments, {HLS_SEGMENT_WORKERS} workers)"


def run_hls_fast_path_with_stall_detection(cmd: list[str], output_path: Path) -> tuple[bool, str]:
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    last_size = -1
    last_progress_at = time.monotonic()

    try:
        while True:
            return_code = process.poll()
            current_size = output_path.stat().st_size if output_path.exists() else 0
            if current_size > last_size:
                last_size = current_size
                last_progress_at = time.monotonic()

            if return_code is not None:
                stdout, stderr = process.communicate()
                result = subprocess.CompletedProcess(cmd, return_code, stdout, stderr)
                if return_code == 0:
                    return True, "completed"
                return False, summarize_error(result)

            if time.monotonic() - last_progress_at >= HLS_STALL_SECONDS:
                process.kill()
                stdout, stderr = process.communicate()
                result = subprocess.CompletedProcess(cmd, -9, stdout, stderr)
                return False, f"HLS fast path stalled for {HLS_STALL_SECONDS}s without file growth. {summarize_error(result)}"

            time.sleep(HLS_PROGRESS_POLL_SECONDS)
    finally:
        if process.poll() is None:
            process.kill()
            try:
                process.communicate()
            except Exception:
                pass


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
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            temp_destination.unlink(missing_ok=True)
            return False, None, summarize_error(result)

    os.replace(temp_destination, destination)
    return True, str(destination), "direct"


def run_download(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True)


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


def extract_filepaths(stdout: str) -> list[str]:
    paths = []
    for line in stdout.splitlines():
        line = line.strip()
        if is_absolute_path(line) and Path(line).suffix:
            paths.append(line)
    return paths


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
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)

    info: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        info[key.strip()] = value.strip()
    return info


def has_video_stream(path: str, ffmpeg: str) -> bool:
    ffprobe = str(Path(ffmpeg).with_name("ffprobe"))
    cmd = [
        ffprobe,
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
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return result.returncode == 0 and "video" in result.stdout


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
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        temp_output.unlink(missing_ok=True)
        raise RuntimeError(f"PowerPoint compatibility transcode failed: {summarize_error(result)}")
    os.replace(temp_output, final_output)
    if source != final_output and source.exists():
        source.unlink()
    return str(final_output), True


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
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return result.stdout.strip()


def cached_file_is_usable(path: str, ffmpeg: str) -> bool:
    candidate = Path(path)
    return (
        candidate.exists()
        and candidate.is_file()
        and candidate.stat().st_size > 0
        and has_video_stream(path, ffmpeg)
        and has_audio_stream(path, ffmpeg)
    )


def cache_entry_is_fresh(entry: dict[str, str]) -> bool:
    expiry = cache_entry_expiry(entry)
    if expiry is None:
        return False
    return datetime.now(timezone.utc) <= expiry


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


def classify_error_category(message: str) -> str:
    if message.startswith("auth_needed:"):
        return "auth_needed"
    if message.startswith("network_unstable:"):
        return "network_unstable"
    if message.startswith("restricted_audio_only:"):
        return "restricted_audio_only"
    if message.startswith("input_invalid:"):
        return "input_invalid"
    return "other_failure"


def render_kpi_report(days: int) -> str:
    events = load_metrics_events(days)
    if not events:
        return f"No KPI events found for the last {days} day(s)."

    simulated_events = [e for e in events if e.get("simulated") is True]
    events = [e for e in events if e.get("simulated") is not True]
    if not events:
        return (
            f"No real download KPI events found for the last {days} day(s). "
            f"Ignored {len(simulated_events)} dry-run event(s)."
        )

    total = len(events)
    success_events = [e for e in events if e.get("success") is True]
    effective_events = [e for e in success_events if e.get("has_video") and e.get("has_audio")]
    first_pass_success = [e for e in effective_events if not e.get("used_cookies") and not e.get("used_fallback")]
    cache_hits = [e for e in events if e.get("from_cache") is True]
    fallback_hits = [e for e in success_events if e.get("used_fallback") is True]
    mis_success = [e for e in success_events if not (e.get("has_video") and e.get("has_audio"))]

    def p50_duration(subset: list[dict[str, object]]) -> int | None:
        values = sorted(int(e["duration_ms"]) for e in subset if isinstance(e.get("duration_ms"), int))
        if not values:
            return None
        return values[len(values) // 2]

    lines = [
        f"KPI report ({days} day window)",
        f"- version: {__version__}",
        f"- total runs: {total}",
        f"- effective delivery rate: {len(effective_events)}/{total} ({len(effective_events) / total:.1%})",
        f"- first-pass success rate: {len(first_pass_success)}/{total} ({len(first_pass_success) / total:.1%})",
        f"- final success rate: {len(success_events)}/{total} ({len(success_events) / total:.1%})",
        f"- false-success rate: {len(mis_success)}/{max(1, len(success_events))} ({len(mis_success) / max(1, len(success_events)):.1%})",
        f"- cache hit rate: {len(cache_hits)}/{total} ({len(cache_hits) / total:.1%})",
        f"- fallback recovery count: {len(fallback_hits)}",
    ]
    if simulated_events:
        lines.append(f"- ignored dry runs: {len(simulated_events)}")

    overall_p50 = p50_duration(events)
    if overall_p50 is not None:
        lines.append(f"- p50 duration: {overall_p50} ms")

    categories: dict[str, int] = {}
    for event in events:
        category = str(event.get("error_category", "none"))
        categories[category] = categories.get(category, 0) + 1
    lines.append("- error categories:")
    for key in sorted(categories):
        lines.append(f"  - {key}: {categories[key]}")

    return "\n".join(lines)


def process_url(
    url: str,
    args: argparse.Namespace,
    yt_dlp: str,
    ffmpeg: str,
    output_dir: Path,
    cookie_browsers: list[str],
    cached_entry: dict[str, str] | None,
) -> tuple[str, bool, str, str | None, str | None, dict[str, object]]:
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
            return url, True, f"success_cached:{cached_status}: {cached_path}", cached_path, cached_status, metadata
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
            return url, True, "dry_run: cache entry exists but is stale or the file is no longer usable", None, None, metadata

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
            return url, True, "dry_run: direct_media -> will download directly", None, None, metadata
        return url, True, "dry_run: social_page -> will use yt-dlp flow", None, None, metadata

    if is_direct_media_url(url):
        ok, detail, extra = download_direct_media(url, args, ffmpeg)
    else:
        ok, detail, extra = try_download_with_fallbacks(url, args, yt_dlp, ffmpeg, output_dir, cookie_browsers)

    if ok:
        saved_path = detail or f"Saved under {output_dir}"
        facts = media_facts(detail, ffmpeg) if detail else media_facts(None, ffmpeg)
        if detail and not facts["has_video"]:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            Path(detail).unlink(missing_ok=True)
            metadata = {
                "duration_ms": duration_ms,
                "from_cache": False,
                "used_cookies": extra not in {"none", "direct"} and not extra.startswith("success_direct_hls_fallback"),
                "used_fallback": extra.startswith("success_direct_hls_fallback"),
                "transcoded": False,
                "error_category": "restricted_audio_only",
            }
            return (
                url,
                False,
                "restricted_audio_only: Downloaded media contained no video stream. The platform currently exposed only audio for this URL; try a logged-in browser session or a different extractor path.",
                None,
                None,
                metadata,
            )
        if detail and not facts["has_audio"]:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            Path(detail).unlink(missing_ok=True)
            metadata = {
                "duration_ms": duration_ms,
                "from_cache": False,
                "used_cookies": extra not in {"none", "direct"} and not extra.startswith("success_direct_hls_fallback"),
                "used_fallback": extra.startswith("success_direct_hls_fallback"),
                "transcoded": False,
                "error_category": "restricted_audio_only",
            }
            return (
                url,
                False,
                "restricted_audio_only: Downloaded media contained no audio stream. The source did not provide a usable video-with-audio result for this URL.",
                None,
                None,
                metadata,
            )

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
        else:
            route_note = f"success_social_cookies:{extra}"
        duration_ms = int((time.monotonic() - started_at) * 1000)
        metadata = {
            "duration_ms": duration_ms,
            "from_cache": False,
            "used_cookies": extra not in {"none", "direct"} and not extra.startswith("success_direct_hls_fallback"),
            "used_fallback": extra.startswith("success_direct_hls_fallback"),
            "transcoded": transcoded,
            "error_category": "none",
        }
        return url, True, f"{route_note}: {saved_path}{compat_note}", saved_path, route_note, metadata

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
        "used_fallback": False,
        "transcoded": False,
        "error_category": classify_error_category(error_detail),
    }
    return url, False, error_detail, None, None, metadata


def print_summary(results: list[tuple[str, bool, str]]) -> None:
    groups: dict[str, list[tuple[str, str]]] = {
        "Succeeded": [],
        "Cached": [],
        "Dry Run": [],
        "Auth Required": [],
        "Network Unstable": [],
        "Restricted": [],
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


def main() -> int:
    args = parse_args()
    try:
        if args.kpi_report is not None:
            print(render_kpi_report(args.kpi_report))
            return 0

        yt_dlp, ffmpeg = ensure_dependencies(args.install_missing)
        urls = normalize_urls(collect_urls(args))
        output_dir = output_directory(args)
        cookie_browsers = available_cookie_browsers()
        cache = load_cache()
        results: list[tuple[str, bool, str]] = []
        metrics_events: list[dict[str, object]] = []
        any_failed = False
        total = len(urls)
        max_workers = min(max(1, args.concurrency), max(1, total))

        def run_with_progress(index: int, url: str) -> tuple[str, bool, str, str | None, str | None, dict[str, object]]:
            print(f"[{index}/{total}] Starting: {url}", file=sys.stderr)
            result = process_url(
                url,
                args,
                yt_dlp,
                ffmpeg,
                output_dir,
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
                        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
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
