#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIRED_FILES = [
    "SKILL.md",
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "LICENSE",
    "_meta.json",
    "agents/openai.yaml",
    "scripts/download_social_video.py",
    "tests/test_download_social_video.py",
]


def fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def require_files() -> int:
    missing = [path for path in REQUIRED_FILES if not (REPO_ROOT / path).exists()]
    if missing:
        return fail(f"Missing required files: {', '.join(missing)}")
    return 0


def parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        raise ValueError("Frontmatter opening '---' not found")
    try:
        _, frontmatter, _ = text.split("---\n", 2)
    except ValueError as exc:
        raise ValueError("Unable to parse frontmatter block") from exc

    values: dict[str, str] = {}
    for line in frontmatter.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip().strip('"')
    return values


def validate_skill_metadata() -> int:
    skill_text = (REPO_ROOT / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = parse_frontmatter(skill_text)
    for key in ("name", "description"):
        if not frontmatter.get(key):
            return fail(f"SKILL.md frontmatter missing '{key}'")

    meta = json.loads((REPO_ROOT / "_meta.json").read_text(encoding="utf-8"))
    if meta.get("name") != frontmatter["name"]:
        return fail("SKILL.md name and _meta.json name do not match")

    script_text = (REPO_ROOT / "scripts" / "download_social_video.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"$', script_text, re.MULTILINE)
    if not match:
        return fail("download_social_video.py is missing __version__")

    script_version = match.group(1)
    meta_version = meta.get("version")
    if meta_version != script_version:
        return fail(f"Version mismatch: _meta.json={meta_version!r}, script={script_version!r}")

    return 0


def main() -> int:
    for check in (require_files, validate_skill_metadata):
        result = check()
        if result != 0:
            return result
    print("Repository validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
