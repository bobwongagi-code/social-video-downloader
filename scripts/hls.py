"""Conservative, bounded fallback for simple unencrypted MPEG-TS HLS."""
from __future__ import annotations

import concurrent.futures
import os
import subprocess
import tempfile
import threading
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from constants import (
    DownloadRoute,
    ErrorCode,
    RouteResult,
    HLS_MAX_PLAYLIST_BYTES,
    HLS_MAX_PLAYLIST_DEPTH,
    HLS_MAX_SEGMENT_BYTES,
    HLS_MAX_SEGMENTS,
    HLS_MAX_TOTAL_BYTES,
    HLS_SEGMENT_WORKERS,
    MIN_FREE_DISK_BYTES,
    summarize_error,
)
from file_ops import atomic_commit, free_bytes
from net import download_file_via_curl, fetch_text_via_curl


class UnsupportedHlsPlaylist(RuntimeError):
    """Raised when the conservative segment fallback cannot safely parse HLS."""


class HlsResourceLimit(RuntimeError):
    """Raised when an HLS download exceeds a local resource boundary."""


_ALLOWED_TAGS = {
    "#EXTM3U",
    "#EXTINF",
    "#EXT-X-VERSION",
    "#EXT-X-TARGETDURATION",
    "#EXT-X-MEDIA-SEQUENCE",
    "#EXT-X-PLAYLIST-TYPE",
    "#EXT-X-INDEPENDENT-SEGMENTS",
    "#EXT-X-ENDLIST",
    "#EXT-X-STREAM-INF",
}


def _same_origin(left: str, right: str) -> bool:
    def origin(value: str) -> tuple[str, str, int]:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()
        port = parsed.port or (443 if scheme == "https" else 80)
        return scheme, host, port

    try:
        return origin(left) == origin(right)
    except ValueError:
        return False


def build_segment_url(playlist_url: str, segment: str) -> str:
    resolved = urljoin(playlist_url, segment)
    parsed_segment = urlsplit(segment)
    base_query = urlsplit(playlist_url).query
    if parsed_segment.query or not base_query or not _same_origin(playlist_url, resolved):
        return resolved
    resolved_parsed = urlsplit(resolved)
    return urlunsplit(
        (
            resolved_parsed.scheme,
            resolved_parsed.netloc,
            resolved_parsed.path,
            base_query,
            resolved_parsed.fragment,
        )
    )


def parse_hls_attribute_list(text: str) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for part in text.split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        attributes[key.strip().upper()] = value.strip().strip('"')
    return attributes


def _tag_name(line: str) -> str:
    return line.split(":", 1)[0].upper()


def _extract_hls_details(
    playlist_text: str,
) -> tuple[list[str], list[tuple[int, int | None, str]]]:
    media_segments: list[str] = []
    variants: list[tuple[int, int | None, str]] = []
    pending_variant: tuple[int, int | None] | None = None

    for raw_line in playlist_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = parse_hls_attribute_list(line.split(":", 1)[1])
            try:
                bandwidth = int(attrs.get("BANDWIDTH", "0"))
            except ValueError:
                bandwidth = 0
            height: int | None = None
            resolution = attrs.get("RESOLUTION", "")
            if "x" in resolution.lower():
                try:
                    height = int(resolution.lower().split("x", 1)[1])
                except ValueError:
                    height = None
            pending_variant = (bandwidth, height)
            continue
        if line.startswith("#"):
            tag = _tag_name(line)
            if tag not in _ALLOWED_TAGS:
                raise UnsupportedHlsPlaylist(
                    f"unsupported HLS tag {tag}; fallback only supports unencrypted MPEG-TS playlists"
                )
            continue
        if pending_variant is not None:
            bandwidth, height = pending_variant
            variants.append((bandwidth, height, line))
            pending_variant = None
            continue
        media_segments.append(line)
        if len(media_segments) > HLS_MAX_SEGMENTS:
            raise UnsupportedHlsPlaylist(
                f"HLS playlist contains more than {HLS_MAX_SEGMENTS} segments"
            )

    if pending_variant is not None:
        raise UnsupportedHlsPlaylist("HLS variant entry has no playlist URL")
    return media_segments, variants


def extract_hls_playlist_entries(playlist_text: str) -> tuple[list[str], list[tuple[int, str]]]:
    """Keep the historical return shape while enforcing the safe tag policy."""
    media_segments, variants = _extract_hls_details(playlist_text)
    return media_segments, [(bandwidth, url) for bandwidth, _, url in variants]


def resolve_hls_media_playlist_url(playlist_url: str, *, max_height: int | None = None) -> str:
    current_url = playlist_url
    seen: set[str] = set()
    for _ in range(HLS_MAX_PLAYLIST_DEPTH):
        if current_url in seen:
            raise UnsupportedHlsPlaylist("HLS master playlist loop detected")
        seen.add(current_url)
        playlist_text = fetch_text_via_curl(current_url, max_bytes=HLS_MAX_PLAYLIST_BYTES)
        media_segments, variants = _extract_hls_details(playlist_text)
        if media_segments:
            return current_url
        if not variants:
            break
        allowed = [
            variant
            for variant in variants
            if max_height is None or variant[1] is None or variant[1] <= max_height
        ]
        selected = max(allowed or variants, key=lambda item: (item[0], item[1] or 0))
        current_url = build_segment_url(current_url, selected[2])
    raise UnsupportedHlsPlaylist("HLS playlist did not resolve to media segments")


def _ensure_free_space(path: Path) -> None:
    if free_bytes(path) < MIN_FREE_DISK_BYTES:
        raise HlsResourceLimit(
            f"not enough free disk space; at least {MIN_FREE_DISK_BYTES} bytes are required"
        )


def _is_mpeg_ts_segment(segment: str) -> bool:
    path = urlsplit(segment).path.lower()
    return not path or path.endswith(".ts")


def _is_endlisted(playlist_text: str) -> bool:
    return any(line.strip().upper() == "#EXT-X-ENDLIST" for line in playlist_text.splitlines())


def _download_segments(
    segments: list[str],
    media_playlist_url: str,
    parts_dir: Path,
) -> int:
    total_bytes = 0
    total_lock = threading.Lock()

    def fetch_segment(item: tuple[int, str]) -> None:
        nonlocal total_bytes
        index, segment = item
        segment_url = build_segment_url(media_playlist_url, segment)
        segment_path = parts_dir / f"{index:05d}.ts"
        size = download_file_via_curl(
            segment_url,
            segment_path,
            max_bytes=HLS_MAX_SEGMENT_BYTES,
        )
        with total_lock:
            total_bytes += size
            if total_bytes > HLS_MAX_TOTAL_BYTES:
                raise HlsResourceLimit(
                    f"HLS download exceeded the {HLS_MAX_TOTAL_BYTES} byte batch limit"
                )

    iterator = iter(enumerate(segments, start=1))
    with concurrent.futures.ThreadPoolExecutor(max_workers=HLS_SEGMENT_WORKERS) as executor:
        pending: set[concurrent.futures.Future[None]] = set()
        for _ in range(HLS_SEGMENT_WORKERS * 2):
            try:
                pending.add(executor.submit(fetch_segment, next(iterator)))
            except StopIteration:
                break

        while pending:
            done, pending = concurrent.futures.wait(
                pending,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                future.result()
                try:
                    pending.add(executor.submit(fetch_segment, next(iterator)))
                except StopIteration:
                    pass
    return total_bytes


def download_hls_via_segments(
    url: str,
    destination: Path,
    ffmpeg: str,
    *,
    max_height: int | None = None,
    force: bool = False,
) -> RouteResult:
    """Download only simple, unencrypted MPEG-TS HLS with bounded work."""
    try:
        _ensure_free_space(destination.parent)
        with tempfile.TemporaryDirectory(prefix="social-video-hls-") as tmp_dir:
            temp_root = Path(tmp_dir)
            parts_dir = temp_root / "parts"
            parts_dir.mkdir(parents=True, exist_ok=True)

            media_playlist_url = resolve_hls_media_playlist_url(url, max_height=max_height)
            playlist_text = fetch_text_via_curl(
                media_playlist_url,
                max_bytes=HLS_MAX_PLAYLIST_BYTES,
            )
            if not _is_endlisted(playlist_text):
                raise UnsupportedHlsPlaylist(
                    "live HLS playlists without #EXT-X-ENDLIST are not supported"
                )
            segments, _ = _extract_hls_details(playlist_text)
            if not segments:
                return RouteResult(
                    False,
                    None,
                    DownloadRoute.HLS_SEGMENTED,
                    "HLS playlist contained no media segments.",
                    error_code=ErrorCode.HLS_DOWNLOAD_FAILED,
                )
            if not all(_is_mpeg_ts_segment(segment) for segment in segments):
                raise UnsupportedHlsPlaylist(
                    "fallback only supports MPEG-TS segment URLs; use yt-dlp/ffmpeg for fMP4 HLS"
                )

            total_bytes = _download_segments(segments, media_playlist_url, parts_dir)
            concat_file = temp_root / "concat.txt"
            concat_lines = [
                f"file '{segment.as_posix()}'\n"
                for segment in sorted(parts_dir.glob("*.ts"))
            ]
            concat_file.write_text("".join(concat_lines), encoding="utf-8")

            descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{destination.stem}.merge-",
                suffix=destination.suffix,
                dir=destination.parent,
            )
            os.close(descriptor)
            temp_destination = Path(temp_name)
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
                "-map_metadata",
                "-1",
                "-map_chapters",
                "-1",
                "-c",
                "copy",
                "-bsf:a",
                "aac_adtstoasc",
                str(temp_destination),
            ]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                if result.returncode != 0:
                    return RouteResult(
                        False,
                        None,
                        DownloadRoute.HLS_SEGMENTED,
                        summarize_error(result),
                        error_code=ErrorCode.HLS_DOWNLOAD_FAILED,
                    )
                atomic_commit(temp_destination, destination, force=force)
            finally:
                temp_destination.unlink(missing_ok=True)
            return RouteResult(
                True,
                str(destination),
                DownloadRoute.HLS_SEGMENTED,
                f"{len(segments)} segments, {total_bytes} bytes",
            )
    except Exception as exc:
        error_code = (
            ErrorCode.RESOURCE_LIMIT
            if isinstance(exc, HlsResourceLimit)
            else ErrorCode.HLS_DOWNLOAD_FAILED
        )
        return RouteResult(
            False,
            None,
            DownloadRoute.HLS_SEGMENTED,
            f"HLS fallback rejected: {exc}",
            error_code=error_code,
        )
