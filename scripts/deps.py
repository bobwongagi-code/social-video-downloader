"""Dependency management: locate or install yt-dlp, ffmpeg, and browser cookies."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from constants import AUTO_COOKIE_BROWSERS


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
    subprocess.run(cmd, check=True, timeout=300)


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


def browser_cookies_available(browser: str) -> bool:
    paths = browser_cookie_paths(browser)
    return any(path.exists() for path in paths)


def available_cookie_browsers() -> list[str]:
    return [browser for browser in AUTO_COOKIE_BROWSERS if browser_cookies_available(browser)]
