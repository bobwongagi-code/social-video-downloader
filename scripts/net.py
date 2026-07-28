"""Bounded HTTP helpers with an SSRF-safe URL and redirect policy."""
from __future__ import annotations

import ipaddress
import os
import socket
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from constants import MAX_DOWNLOAD_BYTES, MAX_HTTP_RESPONSE_BYTES, summarize_error
from deps import which_or_none


MAX_REDIRECTS = 5


class UnsafeRemoteUrlError(ValueError):
    """Raised when a URL is not safe for an outbound media request."""


def _public_address(url: str) -> tuple[str, int, str]:
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeRemoteUrlError("only http and https URLs are allowed")
    if not parsed.hostname or parsed.username or parsed.password:
        raise UnsafeRemoteUrlError("URL must contain a public host without credentials")
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise UnsafeRemoteUrlError("URL contains an invalid port") from exc

    try:
        addresses = socket.getaddrinfo(
            parsed.hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise UnsafeRemoteUrlError(f"could not resolve remote host: {parsed.hostname}") from exc
    if not addresses:
        raise UnsafeRemoteUrlError(f"could not resolve remote host: {parsed.hostname}")

    normalized: list[str] = []
    for address in addresses:
        candidate = ipaddress.ip_address(address[4][0])
        # is_global excludes loopback, link-local, private, multicast,
        # unspecified, reserved, documentation, and shared address ranges.
        if not candidate.is_global:
            raise UnsafeRemoteUrlError(
                f"remote host resolves to a non-public address: {candidate}"
            )
        normalized.append(str(candidate))
    return parsed.hostname, port, normalized[0]


def validate_remote_url(url: str) -> str:
    """Validate a URL and return the pinned public address used by curl."""
    _, _, address = _public_address(url)
    return address


def _header_value(header_text: str, name: str) -> str | None:
    blocks = [block for block in header_text.replace("\r\n", "\n").split("\n\n") if block.strip()]
    if not blocks:
        return None
    values: list[str] = []
    for line in blocks[-1].splitlines()[1:]:
        key, separator, value = line.partition(":")
        if separator and key.lower().strip() == name.lower():
            values.append(value.strip())
    return values[-1] if values else None


def _status_code(header_text: str) -> int:
    blocks = [block for block in header_text.replace("\r\n", "\n").split("\n\n") if block.strip()]
    if not blocks:
        return 0
    first_line = blocks[-1].splitlines()[0] if blocks[-1].splitlines() else ""
    try:
        return int(first_line.split()[1])
    except (IndexError, ValueError):
        return 0


def _curl_to_file(
    url: str,
    body_path: Path,
    *,
    max_bytes: int,
    cookie_jar: Path | None = None,
    referer: str | None = None,
    form_fields: list[tuple[str, str]] | None = None,
    headers: list[str] | None = None,
) -> tuple[Path, str]:
    """Fetch one URL at a time, validating and pinning every redirect target."""
    curl = which_or_none("curl")
    if curl is None:
        raise RuntimeError("curl is required for direct media fallback but is not available.")

    current_url = url
    for _ in range(MAX_REDIRECTS + 1):
        host, port, address = _public_address(current_url)
        header_path = body_path.with_name(f".{body_path.name}.headers")
        body_path.unlink(missing_ok=True)
        header_path.unlink(missing_ok=True)
        cmd = [
            curl,
            "--proto",
            "=http,https",
            "--proto-redir",
            "=http,https",
            "--max-redirs",
            "0",
            "--connect-timeout",
            "15",
            "--max-time",
            "120",
            "--fail",
            "--retry",
            "3",
            "--retry-all-errors",
            "--retry-delay",
            "1",
            "--silent",
            "--show-error",
            "--max-filesize",
            str(max_bytes),
            "--dump-header",
            str(header_path),
            "--output",
            str(body_path),
        ]
        parsed = urlsplit(current_url)
        if parsed.hostname and not _is_ip_literal(parsed.hostname):
            cmd.extend(["--resolve", f"{host}:{port}:{address}"])
        if cookie_jar:
            cmd.extend(["--cookie-jar", str(cookie_jar), "--cookie", str(cookie_jar)])
        if referer:
            cmd.extend(["--referer", referer])
        for header in headers or []:
            cmd.extend(["--header", header])
        if form_fields is not None:
            cmd.extend(["--request", "POST"])
            for key, value in form_fields:
                cmd.extend(["--data-urlencode", f"{key}={value}"])
        cmd.append(current_url)

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=150)
        header_text = header_path.read_text(encoding="iso-8859-1", errors="replace") if header_path.exists() else ""
        header_path.unlink(missing_ok=True)
        if result.returncode != 0:
            body_path.unlink(missing_ok=True)
            raise RuntimeError(summarize_error(result))

        status = _status_code(header_text)
        location = _header_value(header_text, "location")
        if status in {301, 302, 303, 307, 308} and location:
            current_url = urljoin(current_url, location)
            continue
        if status >= 400:
            body_path.unlink(missing_ok=True)
            raise RuntimeError(f"HTTP request failed with status {status}.")
        if body_path.stat().st_size > max_bytes:
            body_path.unlink(missing_ok=True)
            raise RuntimeError(f"HTTP response exceeded the {max_bytes} byte limit.")
        return body_path, current_url

    body_path.unlink(missing_ok=True)
    raise RuntimeError(f"HTTP redirect limit exceeded ({MAX_REDIRECTS}).")


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def fetch_text_via_curl(url: str, *, max_bytes: int = MAX_HTTP_RESPONSE_BYTES) -> str:
    with tempfile.TemporaryDirectory(prefix="social-video-http-") as tmp_dir:
        body_path = Path(tmp_dir) / "response"
        _curl_to_file(url, body_path, max_bytes=max_bytes)
        return body_path.read_text(encoding="utf-8", errors="replace")


def download_file_via_curl(
    url: str,
    destination: Path,
    *,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".curl",
        dir=destination.parent,
    )
    os.close(descriptor)
    temp_destination = Path(temp_name)
    try:
        _curl_to_file(url, temp_destination, max_bytes=max_bytes)
        size = temp_destination.stat().st_size
        os.replace(temp_destination, destination)
        return size
    finally:
        temp_destination.unlink(missing_ok=True)


def curl_text_request(
    url: str,
    *,
    cookie_jar: Path | None = None,
    referer: str | None = None,
    form_fields: list[tuple[str, str]] | None = None,
    headers: list[str] | None = None,
) -> str:
    with tempfile.TemporaryDirectory(prefix="social-video-http-") as tmp_dir:
        body_path = Path(tmp_dir) / "response"
        _curl_to_file(
            url,
            body_path,
            max_bytes=MAX_HTTP_RESPONSE_BYTES,
            cookie_jar=cookie_jar,
            referer=referer,
            form_fields=form_fields,
            headers=headers,
        )
        return body_path.read_text(encoding="utf-8", errors="replace")
