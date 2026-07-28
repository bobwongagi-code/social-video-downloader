"""URL collection, normalization, and platform classification."""
from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import unquote_plus, urlparse, urlsplit, urlunsplit

from constants import (
    DIRECT_MEDIA_EXTENSIONS,
    TRACKING_QUERY_KEYS,
    TRACKING_QUERY_PREFIXES,
    URL_PATTERN,
)


def extract_urls_from_text(text: str) -> list[str]:
    return URL_PATTERN.findall(text)


def collect_urls(args: argparse.Namespace) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    chunks = list(args.inputs)
    text_file_value = getattr(args, "text_file", None)
    if text_file_value:
        text_file = Path(text_file_value).expanduser()
        if not text_file.exists():
            raise RuntimeError(f"--text-file does not exist: {text_file}")
        if not text_file.is_file():
            raise RuntimeError(f"--text-file is not a regular file: {text_file}")
        chunks.append(text_file.read_text(encoding="utf-8"))

    for chunk in chunks:
        candidates = extract_urls_from_text(chunk)
        for candidate in candidates:
            candidate = candidate.rstrip(".,;!?]")
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
        cleaned = url.rstrip(".,;!?]")
        parsed = urlsplit(cleaned)
        host = (parsed.hostname or "").lower()
        if _is_known_social_page_host(host) and not is_direct_media_url(cleaned):
            query_parts = []
            for part in parsed.query.split("&") if parsed.query else []:
                key = unquote_plus(part.split("=", 1)[0]).lower()
                if key in TRACKING_QUERY_KEYS or key.startswith(TRACKING_QUERY_PREFIXES):
                    continue
                query_parts.append(part)
            cleaned = urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path, "&".join(query_parts), "")
            )
        if cleaned not in seen:
            seen.add(cleaned)
            normalized.append(cleaned)
    return normalized


def classify_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if _is_tiktok_host(host) or host == "douyin.com" or host.endswith(".douyin.com"):
        return "tiktok"
    if host == "instagram.com" or host.endswith(".instagram.com"):
        return "instagram"
    if host == "facebook.com" or host.endswith(".facebook.com") or host == "fb.watch":
        return "facebook"
    if host in {"twitter.com", "x.com"} or host.endswith(".twitter.com"):
        return "x"
    if host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be":
        return "youtube"
    return "direct-media" if is_direct_media_url(url) else host or "unknown"


def is_direct_media_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(DIRECT_MEDIA_EXTENSIONS) or ".mp4/" in path or ".m3u8/" in path


def is_tiktok_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return _is_tiktok_host(host)


def tiktok_video_id(url: str) -> str | None:
    import re
    match = re.search(r"/video/(\d+)", urlparse(url).path)
    return match.group(1) if match else None


def _is_tiktok_host(host: str) -> bool:
    return host == "tiktok.com" or host.endswith(".tiktok.com")


def _is_known_social_page_host(host: str) -> bool:
    return (
        _is_tiktok_host(host)
        or host == "instagram.com"
        or host.endswith(".instagram.com")
        or host == "facebook.com"
        or host.endswith(".facebook.com")
        or host == "fb.watch"
        or host == "twitter.com"
        or host.endswith(".twitter.com")
        or host == "x.com"
        or host.endswith(".x.com")
        or host == "youtube.com"
        or host.endswith(".youtube.com")
        or host == "youtu.be"
    )
