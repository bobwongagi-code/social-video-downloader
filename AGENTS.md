# Maintainer Instructions

- Treat the repository files as the source of truth for the installed skill runtime.
- Run `python3 scripts/sync_skill_runtime.py --check` before committing runtime changes.
- Refresh a local Codex installation with `python3 scripts/sync_skill_runtime.py --install`; do not edit files under `~/.codex/skills/social-video-downloader` directly.
- Run the checks documented in `CONTRIBUTING.md` before pushing.
