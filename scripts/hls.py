"""HLS playlist parsing, fast-path download with stall detection, and segmented fallback."""
from __future__ import annotations

import concurrent.futures
import os
import subprocess
import sys
import time
from pathlib import Path

from constants import (
    HLS_MAX_PLAYLIST_DEPTH,
    HLS_PROGRESS_POLL_SECONDS,
    HLS_SEGMENT_WORKERS,
    HLS_STALL_SECONDS,
    summarize_error,
)
from net import download_file_via_curl, fetch_text_via_curl
from urllib.parse import urljoin, urlparse, urlunparse


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


def download_hls_via_segments(url: str, destination: Path, ffmpeg: str) -> tuple[bool, str | None, str]:
    import tempfile

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
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            temp_destination.unlink(missing_ok=True)
            return False, None, summarize_error(result)

        os.replace(temp_destination, destination)
        return True, str(destination), f"success_direct_hls_fallback ({len(segments)} segments, {HLS_SEGMENT_WORKERS} workers)"
