"""Safe filesystem operations shared by download and compression flows."""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def non_conflicting_path(path: Path, *, force: bool = False) -> Path:
    """Return a usable destination without selecting an existing path by default."""
    if force or not path.exists():
        return path
    counter = 1
    while True:
        candidate = path.with_name(f"{path.stem} ({counter}){path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def atomic_commit(source: Path, destination: Path, *, force: bool = False) -> None:
    """Commit a same-directory temporary file atomically.

    A hard-link create is the portable no-clobber primitive available on the
    supported POSIX platforms. The temporary file is created in the output
    directory, so the link and unlink happen on the same filesystem.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    if force:
        os.replace(source, destination)
        return

    try:
        os.link(source, destination)
    except FileExistsError as exc:
        raise FileExistsError(
            f"output appeared during processing: {destination}"
        ) from exc
    os.unlink(source)


def private_path(path: Path, *, directory: bool = False) -> None:
    """Best-effort user-private permissions for cache and metrics files."""
    try:
        os.chmod(path, 0o700 if directory else 0o600)
    except FileNotFoundError:
        pass


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free
