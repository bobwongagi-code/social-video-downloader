"""Private, atomic cache and metrics persistence."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Mapping

from constants import (
    CACHE_PATH,
    CACHE_SALT_PATH,
    CACHE_TTL,
    METRICS_LOG_PATH,
    METRICS_MAX_BYTES,
)
from file_ops import private_path


LOCK_PATH = CACHE_PATH.with_name("downloads.lock")
METRICS_LOCK_PATH = METRICS_LOG_PATH.with_name("runs.lock")


@contextlib.contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    """Use advisory locking on POSIX and remain usable on other platforms."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    private_path(path)
    handle = path.open("r+", encoding="utf-8")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover - Windows fallback
            pass
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except ImportError:  # pragma: no cover - Windows fallback
            pass
        handle.close()


def ensure_cache_dir() -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    private_path(CACHE_PATH.parent, directory=True)


def ensure_metrics_dir() -> None:
    METRICS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    private_path(METRICS_LOG_PATH.parent, directory=True)


def _cache_salt() -> bytes:
    ensure_cache_dir()
    salt_lock = CACHE_SALT_PATH.with_name(".salt.lock")
    with _exclusive_lock(salt_lock):
        try:
            salt = CACHE_SALT_PATH.read_bytes()
            if salt:
                private_path(CACHE_SALT_PATH)
                return salt
        except FileNotFoundError:
            pass

        salt = secrets.token_bytes(32)
        descriptor = os.open(
            CACHE_SALT_PATH,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(salt)
            handle.flush()
            os.fsync(handle.fileno())
        return salt


def hash_sensitive_text(value: str) -> str:
    return hashlib.sha256(_cache_salt() + b"\0" + value.encode("utf-8")).hexdigest()[:16]


def make_cache_key(payload: Mapping[str, object]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(_cache_salt() + b"\0" + canonical.encode("utf-8")).hexdigest()


def cache_entry_expiry(entry: Mapping[str, object]) -> datetime | None:
    expires_at = entry.get("expires_at")
    if isinstance(expires_at, str) and expires_at:
        try:
            return datetime.strptime(expires_at, "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc)
        except ValueError:
            return None

    updated_at = entry.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at:
        return None
    try:
        timestamp = datetime.strptime(updated_at, "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc)
    except ValueError:
        return None
    return timestamp + CACHE_TTL


def _read_cache_unlocked() -> dict[str, dict[str, object]]:
    if not CACHE_PATH.exists():
        return {}
    try:
        raw_cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw_cache, dict):
        return {}

    now = datetime.now(timezone.utc)
    normalized: dict[str, dict[str, object]] = {}
    for key, entry in raw_cache.items():
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[0-9a-f]{64}", key)
            or not isinstance(entry, dict)
        ):
            continue
        expiry = cache_entry_expiry(entry)
        if expiry is None or now > expiry:
            continue
        item = dict(entry)
        item.setdefault("expires_at", expiry.strftime("%Y-%m-%dT%H:%M:%S%z"))
        normalized[key] = item
    return normalized


def load_cache() -> dict[str, dict[str, object]]:
    ensure_cache_dir()
    with _exclusive_lock(LOCK_PATH):
        cache = _read_cache_unlocked()
        _write_cache_unlocked(cache)
        return cache


def _write_cache_unlocked(cache: Mapping[str, Mapping[str, object]]) -> None:
    ensure_cache_dir()
    payload = json.dumps(cache, ensure_ascii=False, indent=2).encode("utf-8")
    descriptor, temp_name = tempfile.mkstemp(prefix=".downloads.", suffix=".tmp", dir=CACHE_PATH.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, CACHE_PATH)
        private_path(CACHE_PATH)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def save_cache(cache: Mapping[str, Mapping[str, object]]) -> None:
    ensure_cache_dir()
    with _exclusive_lock(LOCK_PATH):
        _write_cache_unlocked(cache)


def merge_cache_entries(entries: Mapping[str, Mapping[str, object]]) -> None:
    """Merge successful entries while holding the lock and re-reading state."""
    if not entries:
        return
    ensure_cache_dir()
    with _exclusive_lock(LOCK_PATH):
        cache = _read_cache_unlocked()
        for key, entry in entries.items():
            cache[key] = dict(entry)
        _write_cache_unlocked(cache)


def cache_entry_is_fresh(entry: Mapping[str, object]) -> bool:
    expiry = cache_entry_expiry(entry)
    return expiry is not None and datetime.now(timezone.utc) <= expiry


def append_metrics_events(events: list[dict[str, object]]) -> None:
    if not events:
        return
    ensure_metrics_dir()
    with _exclusive_lock(METRICS_LOCK_PATH):
        if METRICS_LOG_PATH.exists() and METRICS_LOG_PATH.stat().st_size > METRICS_MAX_BYTES:
            rotated = METRICS_LOG_PATH.with_suffix(".1.jsonl")
            os.replace(METRICS_LOG_PATH, rotated)
            private_path(rotated)
        with METRICS_LOG_PATH.open("a", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        private_path(METRICS_LOG_PATH)


def load_metrics_events(days: int) -> list[dict[str, object]]:
    if not METRICS_LOG_PATH.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    events: list[dict[str, object]] = []
    with METRICS_LOG_PATH.open("rb") as handle:
        if METRICS_LOG_PATH.stat().st_size > METRICS_MAX_BYTES:
            handle.seek(-METRICS_MAX_BYTES, os.SEEK_END)
            handle.readline()
        for raw_line in handle:
            line = raw_line.decode("utf-8", errors="replace").strip()
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
