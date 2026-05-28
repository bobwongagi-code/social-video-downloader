"""Low-level curl wrappers shared by HLS and TikTok resolver modules."""
from __future__ import annotations

import subprocess
from pathlib import Path

from deps import which_or_none
from constants import summarize_error


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
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
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
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(summarize_error(result))


def curl_text_request(
    url: str,
    *,
    cookie_jar: Path | None = None,
    referer: str | None = None,
    form_fields: list[tuple[str, str]] | None = None,
    headers: list[str] | None = None,
) -> str:
    curl = which_or_none("curl")
    if curl is None:
        raise RuntimeError("curl is required for TikTok resolver fallback but is not available.")
    cmd = [curl, "-L", "--fail", "--silent", "--show-error"]
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
    cmd.append(url)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(summarize_error(result))
    return result.stdout
