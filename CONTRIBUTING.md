# Contributing

Thanks for considering a contribution.

This project is intentionally small and pragmatic. Please keep changes easy to read, easy to debug, and easy to maintain.

## Good First Contributions

- Platform-specific bug fixes backed by a real failing URL or reproducible extractor output
- Download stability improvements that do not add heavy dependencies
- Output compatibility fixes for QuickTime or PowerPoint
- Documentation improvements for setup, troubleshooting, or supported workflows

## Before You Open an Issue

Please include as much of the following as you safely can:

- Script version from `python3 scripts/download_social_video.py --version`
- Platform and URL type, such as TikTok page, X post, Xiaohongshu page, direct `.mp4`, or `.m3u8`
- Whether cookies were required
- The exact command you ran
- The final error message or summary output

Do not paste browser cookies, session tokens, or other secrets into public issues.

## Local Checks

Run these before opening a pull request:

```bash
python3 -m py_compile scripts/*.py tests/*.py
python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/validate_repo.py
bash -n scripts/compress_for_whatsapp.sh
runtime_root="$(mktemp -d)"
python3 scripts/sync_skill_runtime.py --install --target-root "$runtime_root"
python3 scripts/sync_skill_runtime.py --check --target-root "$runtime_root"
git diff --check
```

The installed skill is a runtime copy, not a second source tree. After changing `SKILL.md`, `agents/openai.yaml`, or files under `scripts/`, refresh the local installation with `python3 scripts/sync_skill_runtime.py --install` and verify it with `python3 scripts/sync_skill_runtime.py --check`. The CI job performs the same install-and-check flow in a temporary Codex home.

For manual stability checks against real long-running platform samples, see:

- `docs/samples/high-difficulty-cases.md`

## Pull Request Guidelines

- Keep the scope tight
- Prefer simple changes over abstraction-heavy rewrites
- Update `CHANGELOG.md` for notable user-facing changes
- If you change the script version, update both:
  - `_meta.json`
  - `scripts/constants.py`

## Platform and Compliance Notes

- This project is not affiliated with TikTok, Instagram, Facebook, X/Twitter, YouTube, or Xiaohongshu
- Contributors should avoid proposing or documenting unsafe handling of user cookies or credentials
- Users remain responsible for complying with platform terms and local law
