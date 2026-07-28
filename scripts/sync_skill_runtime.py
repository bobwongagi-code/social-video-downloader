#!/usr/bin/env python3
"""Synchronize the tracked skill runtime into a Codex installation."""
from __future__ import annotations

import argparse
import filecmp
import os
import shutil
import stat
import sys
from pathlib import Path


SKILL_NAME = "social-video-downloader"
ROOT_RUNTIME_FILES = ("SKILL.md", "_meta.json")
RUNTIME_DIRECTORIES = ("agents", "scripts")


def source_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_target_root() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()


def runtime_files(root: Path) -> tuple[Path, ...]:
    files = [root / relative for relative in ROOT_RUNTIME_FILES]
    for directory in RUNTIME_DIRECTORIES:
        base = root / directory
        files.extend(
            path
            for path in base.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
        )
    return tuple(sorted(files, key=lambda path: path.relative_to(root).as_posix()))


def target_directory(target_root: Path) -> Path:
    return target_root / "skills" / SKILL_NAME


def relative_path(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def mismatches(root: Path, target: Path) -> list[str]:
    differences: list[str] = []
    for source in runtime_files(root):
        relative = relative_path(source, root)
        destination = target / relative
        if not destination.is_file():
            differences.append(f"missing: {relative}")
            continue
        if not filecmp.cmp(source, destination, shallow=False):
            differences.append(f"stale: {relative}")
            continue
        if stat.S_IMODE(source.stat().st_mode) != stat.S_IMODE(destination.stat().st_mode):
            differences.append(f"mode differs: {relative}")
    return differences


def install(root: Path, target: Path) -> int:
    count = 0
    for source in runtime_files(root):
        relative = relative_path(source, root)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.sync-{os.getpid()}")
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        count += 1
    return count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check or refresh the installed Codex skill runtime from this repository."
    )
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--check", action="store_true", help="Fail if the installed runtime differs.")
    actions.add_argument("--install", action="store_true", help="Copy the tracked runtime into the target.")
    parser.add_argument(
        "--target-root",
        type=Path,
        help="Codex home directory. Defaults to CODEX_HOME or ~/.codex.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = source_root()
    target = target_directory((args.target_root or default_target_root()).expanduser().resolve())

    if args.install:
        count = install(root, target)
        print(f"Installed {count} runtime files into {target}")

    differences = mismatches(root, target)
    if differences:
        for difference in differences:
            print(difference, file=sys.stderr)
        return 1

    print(f"Skill runtime matches source: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
