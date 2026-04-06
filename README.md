# Social Video Downloader

A practical social-media video downloader for everyday work: save videos to `~/Downloads` with audio included, balanced quality, and output that already works in QuickTime Player and PowerPoint.

This repo contains both:

- A Codex skill for natural-language download requests
- A bundled Python downloader script built around `yt-dlp` and `ffmpeg`

## Why This Exists

Most video download setups break down in real work:

- They grab the highest bitrate when you only need something practical
- They download video without audio
- They produce files that play in IINA but fail in QuickTime or PowerPoint
- They work on simple MP4 links but get flaky on social pages or HLS streams

This project is designed around a different goal: stable, presentation-friendly downloads with sensible defaults.

## Highlights

- Balanced output: targets practical file size and speed instead of max quality
- Audio-aware: prefers video+audio combinations and avoids silent outputs
- Presentation-ready: normalizes to `H.264 + AAC` only when needed
- Stable download paths: separates social pages, direct media URLs, and HLS playlists
- Recovery built in: supports retries, cookie fallback, HLS stall detection, and segmented fallback
- Repeated-work friendly: includes cache reuse and lightweight KPI logging

## What It Supports

Typical supported sources include:

- TikTok
- Instagram reels and posts
- Facebook videos
- X/Twitter videos
- YouTube videos and Shorts
- Xiaohongshu links
- Direct media URLs such as `.mp4` and `.m3u8`

Platform support ultimately depends on whether the current `yt-dlp` extractor can access the source.

## Default Behavior

- Output directory: `~/Downloads`
- Quality target: cap height around `720p`
- Audio: prefer video+audio output, avoid silent video files
- Format: MP4 when remuxing or finalizing files
- Playback compatibility: convert to `H.264 + AAC` only when needed
- Cookies: retry with available local browser cookies when anonymous access fails
- HLS: try fast `ffmpeg` capture first, then fall back to segmented recovery if needed
- Cache: reuse a previously downloaded file only if it still exists and passes a basic media sanity check

## Requirements

- Python 3.9+
- `yt-dlp`
- `ffmpeg`

On macOS, the script can auto-install missing `yt-dlp` and `ffmpeg` with Homebrew by default. On other platforms, install them manually before use.

## Quick Start

Clone the repo:

```bash
git clone git@github-personal:bobwongagi-code/social-video-downloader.git
cd social-video-downloader
```

Download one URL:

```bash
python3 scripts/download_social_video.py "<url>"
```

Download a batch:

```bash
python3 scripts/download_social_video.py "<url-1>" "<url-2>"
```

See recent KPI trends from real runs:

```bash
python3 scripts/download_social_video.py --kpi-report
```

Use a logged-in browser session when needed:

```bash
python3 scripts/download_social_video.py "<url>" --cookies-from-browser chrome
```

## Example Workflow

Typical day-to-day usage looks like this:

```bash
# 1. Download a single social-media URL
python3 scripts/download_social_video.py "https://www.instagram.com/reel/..."

# 2. Download a small batch with bounded concurrency
python3 scripts/download_social_video.py "https://x.com/i/status/..." "https://www.youtube.com/watch?v=..." --concurrency 3

# 3. Review recent real-run KPI trends
python3 scripts/download_social_video.py --kpi-report
```

## Common Flags

- `--output-dir`: write files somewhere other than `~/Downloads`
- `--max-height`: change the default quality cap
- `--no-ppt-compatible`: keep the raw downloaded file instead of normalizing for PowerPoint/QuickTime
- `--cookies-from-browser`: force a specific browser cookie source
- `--concurrency`: control bounded parallel downloads for multi-URL batches
- `--dry-run`: preview without downloading
- `--kpi-report`: summarize recent real-run metrics
- `--version`: print the script version

## Repository Layout

```text
social-video-downloader/
├── SKILL.md
├── README.md
├── _meta.json
├── agents/
│   └── openai.yaml
└── scripts/
    └── download_social_video.py
```

Local runtime artifacts are intentionally ignored:

- `cache/`
- `metrics/`
- `__pycache__/`

## Notes

- Some platforms require login state depending on region and content restrictions.
- Restricted sources may expose only audio; those are treated as failures instead of fake success.
- The defaults are optimized for day-to-day sharing, playback, and presentation use, not archival-quality collection.
- KPI reports exclude `--dry-run` events from scoring so the metrics stay meaningful.
- Users are responsible for complying with the target platform's terms of service and local laws when downloading content.

## License

This project is licensed under the MIT License. See [LICENSE](./LICENSE).
