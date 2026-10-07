"""TikTok video download via HTTP resolver providers (SnapTik, SSSTik)."""
from __future__ import annotations

import base64
import binascii
import hashlib
import html
import json
import os
import re
import shutil
import sys
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

from constants import (
    DownloadRoute,
    ErrorCode,
    DownloadOptions,
    RouteResult,
    SNAPTIK_HOME_URL,
    SNAPTIK_EXTRACT_URL,
    SNAPTIK_TOKEN_URL,
    SSSTIK_HOME_URL,
    redact_url,
    sanitize_filename,
)
from cache import hash_sensitive_text
from file_ops import atomic_commit, non_conflicting_path
from media_probe import media_facts, probe_media
from net import curl_text_request, download_file_via_curl
from urls import tiktok_video_id


SNAPTIK_VERIFY_PREFIX = "sn4pt1k_v3r1fy2026"
MEDIA_URL_KEYS = {
    "downloadurl",
    "download_url",
    "hddownloadurl",
    "hd_download_url",
    "videourl",
    "video_url",
    "playurl",
    "play_url",
}


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


def _normalize_media_url(candidate: str) -> str | None:
    cleaned = html.unescape(candidate).replace(r"\/", "/").replace(r'\"', '"').rstrip("\\")
    try:
        parsed = urlsplit(cleaned)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.hostname in {"snaptik.app", "www.snaptik.app", "ssstik.io", "www.ssstik.io"}:
        return None
    return cleaned


def _json_media_url_candidates(value: object) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add(candidate: object) -> None:
        if not isinstance(candidate, str):
            return
        normalized = _normalize_media_url(candidate)
        if normalized and normalized not in seen:
            seen.add(normalized)
            candidates.append(normalized)

    def visit(node: object, key: str = "") -> None:
        normalized_key = re.sub(r"[^a-z0-9_]", "", key.lower())
        if isinstance(node, dict):
            for child_key, child_value in node.items():
                if re.sub(r"[^a-z0-9_]", "", str(child_key).lower()) in MEDIA_URL_KEYS:
                    add(child_value)
                elif isinstance(child_value, (dict, list)):
                    visit(child_value, str(child_key))
        elif isinstance(node, list):
            for child in node:
                visit(child, normalized_key)

    visit(value)
    return candidates


def media_url_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        normalized = _normalize_media_url(candidate)
        if normalized and normalized not in seen:
            seen.add(normalized)
            candidates.append(normalized)

    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        payload = None
    for candidate in _json_media_url_candidates(payload):
        add(candidate)

    patterns = [
        r'https?://d\.rapidcdn\.app/[^"\s<>]+',
        r'https?://[^"\s<>]+\.mp4(?:\?[^"\s<>]*)?',
        r'''(?:href|data-url|data-download-url)\s*=\s*["'](https?://[^"']+)''',
    ]
    for pattern in patterns:
        for candidate in re.findall(pattern, text):
            add(candidate)
    return candidates


def _json_title(text: str) -> str | None:
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(payload, dict):
        payload = payload.get("data", payload)
    if isinstance(payload, dict) and isinstance(payload.get("title"), str):
        return html.unescape(payload["title"]).strip() or None
    return None


def _response_title(text: str) -> str | None:
    title = _json_title(text)
    if title:
        return title
    patterns = [
        r'class=["\'][^"\']*video-title[^"\']*["\'][^>]*>([^<]+)',
        r'<p[^>]*>([^<]+)</p>',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return html.unescape(match.group(1)).strip() or None
    return None


def _challenge_int(challenge: dict[str, object], key: str) -> int:
    value = challenge.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise RuntimeError(f"SnapTik challenge field {key!r} was invalid.")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"SnapTik challenge field {key!r} was invalid.") from exc


def evaluate_snaptik_challenge(challenge: dict[str, object]) -> int:
    kind = challenge.get("t")
    if kind == "b":
        return ((_challenge_int(challenge, "a") ^ _challenge_int(challenge, "b")) >> _challenge_int(challenge, "s")) & 255
    if kind == "r":
        values = challenge.get("n")
        if not isinstance(values, list):
            raise RuntimeError("SnapTik challenge field 'n' was invalid.")
        return sum(_challenge_int({"value": value}, "value") for value in values) * 2 + 1
    if kind == "c":
        word = challenge.get("w")
        index = _challenge_int(challenge, "i")
        if not isinstance(word, str) or index < 0 or index >= len(word):
            raise RuntimeError("SnapTik challenge field 'w' was invalid.")
        return ord(word[index]) * _challenge_int(challenge, "m")
    if kind == "m":
        return (
            (_challenge_int(challenge, "a") + _challenge_int(challenge, "b")) % 100
        ) * _challenge_int(challenge, "c")
    if kind == "n":
        a = _challenge_int(challenge, "a")
        b = _challenge_int(challenge, "b")
        c = _challenge_int(challenge, "c")
        return a * b + b * c + c * a - a
    raise RuntimeError("SnapTik challenge used an unsupported operation.")


def _decode_base64(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.b64decode(padded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError("SnapTik challenge payload was not valid base64.") from exc


def _decrypt_snaptik_payload(token_id: str, payload: str) -> dict[str, object]:
    encrypted = _decode_base64(payload)
    if len(encrypted) <= 16:
        raise RuntimeError("SnapTik challenge payload was too short.")
    openssl = shutil.which("openssl")
    if openssl is None:
        raise RuntimeError("openssl is required for the SnapTik resolver challenge.")
    key = hashlib.sha256(f"{SNAPTIK_VERIFY_PREFIX}:{token_id}".encode("utf-8")).hexdigest()
    result = subprocess.run(
        [
            openssl,
            "enc",
            "-d",
            "-aes-256-cbc",
            "-K",
            key,
            "-iv",
            encrypted[:16].hex(),
        ],
        input=encrypted[16:],
        capture_output=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("SnapTik challenge decryption failed.")
    try:
        decoded = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("SnapTik challenge response was invalid JSON.") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("SnapTik challenge response was not an object.")
    return decoded


def solve_snaptik_challenge(token_id: str, payload: str) -> str:
    challenge = _decrypt_snaptik_payload(token_id, payload)
    operation = evaluate_snaptik_challenge(challenge)
    if "_e" not in challenge or "_h" not in challenge:
        raise RuntimeError("SnapTik challenge response was missing verification fields.")
    return f"{token_id}:{operation}:{challenge['_e']}:{challenge['_h']}"


def snaptik_candidates(url: str) -> tuple[list[str], str | None]:
    with tempfile.TemporaryDirectory(prefix="social-video-snaptik-") as tmp_dir:
        cookie_jar = Path(tmp_dir) / "cookies.txt"
        curl_text_request(SNAPTIK_HOME_URL, cookie_jar=cookie_jar)
        token_response = curl_text_request(
            SNAPTIK_TOKEN_URL,
            cookie_jar=cookie_jar,
            referer=SNAPTIK_HOME_URL,
            form_fields=[],
            headers=[
                "X-Requested-With: XMLHttpRequest",
                "Content-Type: application/json",
            ],
        )
        try:
            token = json.loads(token_response)
            token_id = str(token["id"])
            encrypted_payload = str(token["p"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("SnapTik token response was invalid.") from exc
        verification = solve_snaptik_challenge(token_id, encrypted_payload)
        extract_url = f"{SNAPTIK_EXTRACT_URL}?url={quote(url, safe='')}"
        response = curl_text_request(
            extract_url,
            cookie_jar=cookie_jar,
            referer=SNAPTIK_HOME_URL,
            headers=[
                "X-Requested-With: XMLHttpRequest",
                f"X-Verify: {verification}",
            ],
        )
    return media_url_candidates(response), _response_title(response)


def ssstik_candidates(url: str) -> tuple[list[str], str | None]:
    with tempfile.TemporaryDirectory(prefix="social-video-ssstik-") as tmp_dir:
        cookie_jar = Path(tmp_dir) / "cookies.txt"
        homepage = curl_text_request(SSSTIK_HOME_URL, cookie_jar=cookie_jar)
        endpoint_match = re.search(r"\bs_furl\s*=\s*['\"]([^'\"]+)", homepage)
        token_match = re.search(r"\bs_tt\s*=\s*['\"]([^'\"]+)", homepage)
        if not endpoint_match or not token_match:
            raise RuntimeError("SSSTik form configuration was not found.")
        submit_url = urljoin(SSSTIK_HOME_URL, f"/{endpoint_match.group(1)}?url=dl")
        response = curl_text_request(
            submit_url,
            cookie_jar=cookie_jar,
            referer=SSSTIK_HOME_URL,
            form_fields=[("id", url), ("locale", "en"), ("tt", token_match.group(1))],
            headers=[
                "HX-Request: true",
                "HX-Current-URL: https://ssstik.io/",
                "HX-Target: target",
                "HX-Trigger: _gcaptcha_pt",
                "Origin: https://ssstik.io",
            ],
        )
    return media_url_candidates(response), _response_title(response)


def resolver_target(url: str, title: str | None, options: DownloadOptions) -> Path:
    identifier = tiktok_video_id(url) or hash_sensitive_text(url)[:8]
    filename = sanitize_filename(title or "tiktok-resolved-video")
    target = options.output_dir / f"{filename} [{identifier}].mp4"
    return non_conflicting_path(target, force=options.force)


def download_tiktok_via_resolvers(
    url: str, options: DownloadOptions, ffmpeg: str
) -> RouteResult:
    provider_errors: list[str] = []
    providers = [("snaptik", snaptik_candidates), ("ssstik", ssstik_candidates)]
    print(
        "TikTok resolver: submitting the URL to SnapTik/SSSTik; "
        f"URL shown without query data: {redact_url(url)}",
        file=sys.stderr,
    )
    for provider_name, provider in providers:
        try:
            candidates, title = provider(url)
            if not candidates:
                provider_errors.append(f"{provider_name}: no video URL returned")
                continue
            destination = resolver_target(url, title, options)
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
                        force=options.force,
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
