"""TikTok video download via HTTP resolver providers (SnapTik, SSSTik)."""
from __future__ import annotations

import argparse
import hashlib
import html
import os
import re
import tempfile
from pathlib import Path

from constants import (
    SAFE_FILENAME_PATTERN,
    SNAPTIK_HOME_URL,
    SNAPTIK_SUBMIT_URL,
    SSSTIK_HOME_URL,
    SSSTIK_SUBMIT_URL,
    sanitize_filename,
)
from media_probe import has_audio_stream, has_video_stream
from net import curl_text_request, download_file_via_curl
from urls import tiktok_video_id


def _output_directory(args: argparse.Namespace) -> Path:
    return Path(os.path.expanduser(args.output_dir)).resolve()


def decode_snaptik_response(script: str) -> str:
    match = re.search(
        r'\}\("(?P<payload>[^"]+)",\d+,"(?P<symbols>[^"]+)",'
        r"(?P<offset>\d+),(?P<base>\d+),\d+\)\)",
        script,
    )
    if not match:
        raise RuntimeError("SnapTik response format was not recognized.")

    symbols = match.group("symbols")
    base = int(match.group("base"))
    offset = int(match.group("offset"))
    if base <= 1 or base >= len(symbols):
        raise RuntimeError("SnapTik response used an unsupported encoding base.")
    delimiter = symbols[base]
    mapping = {symbol: str(index) for index, symbol in enumerate(symbols)}
    decoded: list[str] = []
    for encoded_char in match.group("payload").split(delimiter):
        number_text = "".join(mapping.get(char, char) for char in encoded_char)
        try:
            decoded.append(chr(int(number_text, base) - offset))
        except (ValueError, OverflowError) as exc:
            raise RuntimeError("SnapTik response decoding failed.") from exc
    return html.unescape("".join(decoded).replace(r"\/", "/").replace(r"\"", '"'))


def media_url_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    patterns = [
        r'https?://d\.rapidcdn\.app/[^"\s<>]+',
        r'https?://[^"\s<>]+\.mp4(?:\?[^"\s<>]*)?',
    ]
    for pattern in patterns:
        for candidate in re.findall(pattern, text):
            cleaned = html.unescape(candidate).replace(r"\/", "/").rstrip("\\")
            if cleaned not in seen:
                seen.add(cleaned)
                candidates.append(cleaned)
    return candidates


def snaptik_candidates(url: str) -> tuple[list[str], str | None]:
    with tempfile.TemporaryDirectory(prefix="social-video-snaptik-") as tmp_dir:
        cookie_jar = Path(tmp_dir) / "cookies.txt"
        homepage = curl_text_request(SNAPTIK_HOME_URL, cookie_jar=cookie_jar)
        token_match = re.search(r'name="token"\s+value="([^"]+)"', homepage)
        if not token_match:
            raise RuntimeError("SnapTik token was not found.")
        response = curl_text_request(
            SNAPTIK_SUBMIT_URL,
            cookie_jar=cookie_jar,
            referer=SNAPTIK_HOME_URL,
            form_fields=[("url", url), ("lang", "en2"), ("token", token_match.group(1))],
            headers=["X-Requested-With: XMLHttpRequest"],
        )
    decoded = decode_snaptik_response(response)
    title_match = re.search(r'class="video-title">([^<]+)', decoded)
    title = html.unescape(title_match.group(1)).strip() if title_match else None
    return media_url_candidates(decoded), title


def ssstik_candidates(url: str) -> tuple[list[str], str | None]:
    with tempfile.TemporaryDirectory(prefix="social-video-ssstik-") as tmp_dir:
        cookie_jar = Path(tmp_dir) / "cookies.txt"
        curl_text_request(SSSTIK_HOME_URL, cookie_jar=cookie_jar)
        response = curl_text_request(
            SSSTIK_SUBMIT_URL,
            cookie_jar=cookie_jar,
            referer=SSSTIK_HOME_URL,
            form_fields=[("id", url), ("locale", "en")],
            headers=[
                "HX-Request: true",
                "HX-Current-URL: https://ssstik.io/",
                "HX-Target: target",
            ],
        )
    title_match = re.search(r"<p[^>]*>([^<]+)</p>", response)
    title = html.unescape(title_match.group(1)).strip() if title_match else None
    return media_url_candidates(response), title


def resolver_target(url: str, title: str | None, args: argparse.Namespace) -> Path:
    identifier = tiktok_video_id(url) or hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    filename = sanitize_filename(title or "tiktok-resolved-video")
    return _output_directory(args) / f"{filename} [{identifier}].mp4"


def download_tiktok_via_resolvers(
    url: str, args: argparse.Namespace, ffmpeg: str
) -> tuple[bool, str | None, str]:
    provider_errors: list[str] = []
    providers = [("snaptik", snaptik_candidates), ("ssstik", ssstik_candidates)]
    for provider_name, provider in providers:
        try:
            candidates, title = provider(url)
            if not candidates:
                provider_errors.append(f"{provider_name}: no video URL returned")
                continue
            destination = resolver_target(url, title, args)
            temp_destination = destination.with_name(f"{destination.stem} [resolver-tmp]{destination.suffix}")
            for candidate in candidates:
                temp_destination.unlink(missing_ok=True)
                try:
                    download_file_via_curl(candidate, temp_destination)
                except RuntimeError as exc:
                    provider_errors.append(f"{provider_name}: {exc}")
                    continue
                if not has_video_stream(str(temp_destination), ffmpeg) or not has_audio_stream(str(temp_destination), ffmpeg):
                    temp_destination.unlink(missing_ok=True)
                    provider_errors.append(f"{provider_name}: returned media without video and audio")
                    continue
                os.replace(temp_destination, destination)
                return True, str(destination), f"success_tiktok_resolver:{provider_name}"
        except RuntimeError as exc:
            provider_errors.append(f"{provider_name}: {exc}")
    return False, None, "tiktok_resolver_failed: " + "; ".join(provider_errors)
