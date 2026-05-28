"""Download cache and metrics event persistence."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from constants import CACHE_PATH, CACHE_TTL, METRICS_LOG_PATH


def ensure_cache_dir() -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)


def ensure_metrics_dir() -> None:
    METRICS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def cache_entry_expiry(entry: dict[str, str]) -> datetime | None:
    expires_at = entry.get("expires_at")
    if expires_at:
        try:
            return datetime.strptime(expires_at, "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc)
        except ValueError:
            return None

    updated_at = entry.get("updated_at")
    if not updated_at:
        return None
    try:
        timestamp = datetime.strptime(updated_at, "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc)
    except ValueError:
        return None
    return timestamp + CACHE_TTL


def load_cache() -> dict[str, dict[str, str]]:
    ensure_cache_dir()
    if not CACHE_PATH.exists():
        return {}
    try:
        raw_cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw_cache, dict):
        return {}

    now = datetime.now(timezone.utc)
    normalized_cache: dict[str, dict[str, str]] = {}
    changed = False
    for url, entry in raw_cache.items():
        if not isinstance(entry, dict):
            changed = True
            continue
        expiry = cache_entry_expiry(entry)
        if expiry is None:
            changed = True
            continue
        if now > expiry:
            changed = True
            continue
        if "expires_at" not in entry:
            entry = dict(entry)
            entry["expires_at"] = expiry.strftime("%Y-%m-%dT%H:%M:%S%z")
            changed = True
        normalized_cache[url] = entry

    if changed:
        save_cache(normalized_cache)
    return normalized_cache


def save_cache(cache: dict[str, dict[str, str]]) -> None:
    ensure_cache_dir()
    temp_path = CACHE_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, CACHE_PATH)


def cache_entry_is_fresh(entry: dict[str, str]) -> bool:
    expiry = cache_entry_expiry(entry)
    if expiry is None:
        return False
    return datetime.now(timezone.utc) <= expiry


def append_metrics_events(events: list[dict[str, object]]) -> None:
    if not events:
        return
    ensure_metrics_dir()
    with METRICS_LOG_PATH.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def load_metrics_events(days: int) -> list[dict[str, object]]:
    if not METRICS_LOG_PATH.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    events: list[dict[str, object]] = []
    with METRICS_LOG_PATH.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            timestamp = event.get("timestamp")
            if not isinstance(timestamp, str):
                continue
            try:
                dt = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc)
            except ValueError:
                continue
            if dt >= cutoff:
                events.append(event)
    return events
