# Social Video Downloader

Download social-media videos to the local `~/Downloads` folder with practical defaults for repeated work:

- Balanced quality instead of maximum bitrate or resolution
- Audio included whenever the source exposes audio
- Final output normalized for QuickTime Player and PowerPoint
- Stable handling for social pages, direct media URLs, and HLS playlists
- Lightweight cache and KPI logging for repeated use and tuning

This repo contains the Codex skill plus the bundled downloader script that powers it.

## What It Supports

The downloader is built around `yt-dlp` plus `ffmpeg`.

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

Run a basic download:

```bash
python3 scripts/download_social_video.py "<url>"
```

Download multiple links:

```bash
python3 scripts/download_social_video.py "<url-1>" "<url-2>"
```

Use a logged-in browser session when needed:

```bash
python3 scripts/download_social_video.py "<url>" --cookies-from-browser chrome
```

Preview a batch without downloading:

```bash
python3 scripts/download_social_video.py "download these https://... and https://..." --dry-run
```

Show KPI summary for recent real runs:

```bash
python3 scripts/download_social_video.py --kpi-report
```

## Common Flags

- `--output-dir`: write files somewhere other than `~/Downloads`
- `--max-height`: change the default quality cap
- `--no-ppt-compatible`: keep the raw downloaded file instead of normalizing for PowerPoint/QuickTime
- `--cookies-from-browser`: force a specific browser cookie source
- `--concurrency`: control bounded parallel downloads for multi-URL batches
- `--dry-run`: preview without downloading
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

## License

No license file is included yet. Treat this repo as private/internal until you decide how you want to share it.
