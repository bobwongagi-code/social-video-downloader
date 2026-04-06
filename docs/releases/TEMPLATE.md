# Release Notes Template

Suggested GitHub release title:

```text
vX.Y.Z - Short release summary
```

Suggested GitHub release body:

```markdown
## Summary

`vX.Y.Z` is focused on [short summary of what this release improves].

This release includes:

- [major improvement or theme]
- [major improvement or theme]
- [major improvement or theme]

## Highlights

### [Area one]

- [specific change]
- [specific change]
- [specific change]

### [Area two]

- [specific change]
- [specific change]

### [Area three]

- [specific change]
- [specific change]

## Validation

This release was checked with:

```bash
python3 -m py_compile scripts/download_social_video.py scripts/validate_repo.py
python3 scripts/validate_repo.py
git diff --check
```

## Upgrade Notes

- [Call out anything users should know before or after upgrading]
- [Mention changed defaults or migration notes if relevant]

## Notes

- This project is not affiliated with TikTok, Instagram, Facebook, X/Twitter, YouTube, or Xiaohongshu
- Platform support depends on the current `yt-dlp` extractor and the target site's restrictions
- Users are responsible for complying with platform terms and local laws when downloading content
```
