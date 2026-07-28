"""TikTok video download via HTTP resolver providers (SnapTik, SSSTik)."""
from __future__ import annotations

import argparse
import html
import os
import re
import sys
import tempfile
from pathlib import Path

from constants import (
    DownloadRoute,
    ErrorCode,
    RouteResult,
    SAFE_FILENAME_PATTERN,
    SNAPTIK_HOME_URL,
    SNAPTIK_SUBMIT_URL,
    SSSTIK_HOME_URL,
    SSSTIK_SUBMIT_URL,
    redact_url,
    sanitize_filename,
)
from cache import hash_sensitive_text
from file_ops import atomic_commit, non_conflicting_path
from media_probe import media_facts, probe_media
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
    identifier = tiktok_video_id(url) or hash_sensitive_text(url)[:8]
    filename = sanitize_filename(title or "tiktok-resolved-video")
    target = _output_directory(args) / f"{filename} [{identifier}].mp4"
    return non_conflicting_path(target, force=getattr(args, "force", False))


def download_tiktok_via_resolvers(
    url: str, args: argparse.Namespace, ffmpeg: str
) -> RouteResult:
    provider_errors: list[str] = []
    providers = [("snaptik", snaptik_candidates), ("ssstik", ssstik_candidates)]
    print(
        "TikTok resolver opt-in: submitting the URL to SnapTik/SSSTik; "
        f"URL shown without query data: {redact_url(url)}",
        file=sys.stderr,
    )
    for provider_name, provider in providers:
        try:
            candidates, title = provider(url)
            if not candidates:
                provider_errors.append(f"{provider_name}: no video URL returned")
                continue
            destination = resolver_target(url, title, args)
            for candidate in candidates:
                descriptor, temp_name = tempfile.mkstemp(
                    prefix=f".{destination.stem}.resolver-",
                    suffix=destination.suffix,
                    dir=destination.parent,
                )
                os.close(descriptor)
                temp_destination = Path(temp_name)
                try:
                    download_file_via_curl(candidate, temp_destination)
                except RuntimeError as exc:
                    provider_errors.append(f"{provider_name}: {exc}")
                    temp_destination.unlink(missing_ok=True)
                    continue
                try:
                    probed = probe_media(str(temp_destination), ffmpeg)
                    facts = media_facts(str(temp_destination), ffmpeg, probed=probed)
                    if not facts["has_video"] or not facts["has_audio"]:
                        provider_errors.append(f"{provider_name}: returned media without video and audio")
                        continue
                    atomic_commit(
                        temp_destination,
                        destination,
                        force=getattr(args, "force", False),
                    )
                    return RouteResult(
                        True,
                        str(destination),
                        DownloadRoute.TIKTOK_RESOLVER,
                        provider_name,
                        media_probe=probed,
                    )
                finally:
                    temp_destination.unlink(missing_ok=True)
        except RuntimeError as exc:
            provider_errors.append(f"{provider_name}: {exc}")
    return RouteResult(
        False,
        None,
        DownloadRoute.TIKTOK_RESOLVER,
        "; ".join(provider_errors),
        error_code=ErrorCode.TIKTOK_RESOLVER_FAILED,
    )
