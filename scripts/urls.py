"""URL collection, normalization, and platform classification."""
from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from constants import DIRECT_MEDIA_EXTENSIONS, TRACKING_QUERY_KEYS, TRACKING_QUERY_PREFIXES, URL_PATTERN


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


def is_direct_media_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(DIRECT_MEDIA_EXTENSIONS) or ".mp4/" in path or ".m3u8/" in path


def is_tiktok_url(url: str) -> bool:
    import re
    host = (urlparse(url).hostname or "").lower()
    return host == "tiktok.com" or host.endswith(".tiktok.com")


def tiktok_video_id(url: str) -> str | None:
    import re
    match = re.search(r"/video/(\d+)", urlparse(url).path)
    return match.group(1) if match else None
