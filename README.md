# Social Video Media

A practical video media tool with two routes: download social videos to `~/Downloads`, or compress local videos into WhatsApp-ready MP4 files.

This repo contains both:

- A Codex skill for natural-language download and compression requests
- A bundled Python downloader script built around `yt-dlp` and `ffmpeg`
- A bundled WhatsApp compression helper for local video files

Project docs:

- [CHANGELOG](./CHANGELOG.md)
- [CONTRIBUTING](./CONTRIBUTING.md)
- [SECURITY](./SECURITY.md)
- [LICENSE](./LICENSE)

## Why This Exists

Most video download setups break down in real work:

- They grab the highest bitrate when you only need something practical
- They download video without audio
- They produce files that play in IINA but fail in QuickTime or PowerPoint
- They work on simple MP4 links but get flaky on social pages or HLS streams

This project is designed around a different goal: stable, presentation-friendly downloads and sharing-ready compressed files with sensible defaults.

## Highlights

- Balanced output: targets practical file size and speed instead of max quality
- Audio-aware: prefers video+audio combinations and avoids silent outputs
- Presentation-ready: normalizes to `H.264 + AAC` only when needed
- Stable download paths: separates social pages, direct media URLs, and HLS playlists
- Recovery built in: supports retries, cookie fallback, HLS stall detection, and segmented fallback
- TikTok restricted-video recovery: can use HTTP resolver providers when normal extraction fails or exposes audio only
- Repeated-work friendly: includes cache reuse and lightweight KPI logging
- WhatsApp-ready local compression: uses two-pass bitrate budgeting and validates the final H.264/AAC file

## What It Supports

Typical supported sources include:

- TikTok
- Instagram reels and posts
- Facebook videos
- X/Twitter videos
- YouTube videos and Shorts
- Xiaohongshu links
- Direct media URLs such as `.mp4` and `.m3u8`
- Local MP4/MOV/M4V/WebM files for WhatsApp compression

Platform support ultimately depends on whether the current `yt-dlp` extractor can access the source.

## Default Behavior

- Output directory: `~/Downloads`
- Quality target: cap height around `720p`
- Audio: prefer video+audio output, avoid silent video files
- Format: MP4 when remuxing or finalizing files
- Playback compatibility: convert to `H.264 + AAC` only when needed
- Cookies: retry with available local browser cookies when anonymous access fails
- HLS: try fast `ffmpeg` capture first, then fall back to segmented recovery if needed
- Cache: reuse a previously downloaded file only if it still exists and still contains both video and audio
- TikTok resolver: normal TikTok links stay local-first; known Shop/promoted links can skip directly to resolver fallback
- Local compression: preserve the source, target the requested size with a 0.90 safety margin, and write a new MP4

## Choose A Route

- Give a supported social URL when the goal is to download a video.
- Give a local video path when the goal is to compress it for WhatsApp or a target file size.
- Ask explicitly for both operations when the downloaded result should also be compressed.

## Requirements

- Python 3.9+
- `ffmpeg` and `ffprobe` (ffprobe is bundled with standard FFmpeg installs)
- `yt-dlp` for the social URL download route

On macOS, the script can auto-install missing `yt-dlp` and `ffmpeg` with Homebrew by default. On other platforms, install them manually before use.

The local compression helper does not install dependencies automatically; it requires `python3`, `ffmpeg`, and `ffprobe`.

## Quick Start

Clone the repo:

```bash
git clone git@github.com:bobwongagi-code/social-video-downloader.git
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

Compress a local video for WhatsApp:

```bash
bash scripts/compress_for_whatsapp.sh "/path/to/input.mp4" "/path/to/output.mp4" 64
```

The helper keeps the original file, chooses a practical resolution from the available bitrate, and verifies that the result is within the requested MiB limit with H.264 video and AAC audio when audio exists.

Download a known TikTok Shop or promoted video without waiting for the ordinary TikTok extraction path:

```bash
python3 scripts/download_social_video.py "<tiktok-url>" --tiktok-shop
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

# 4. Compress a local video for WhatsApp
bash scripts/compress_for_whatsapp.sh "/path/to/input.mp4" "/path/to/output.mp4" 64
```

## Common Flags

- `--output-dir`: write files somewhere other than `~/Downloads`
- `--max-height`: change the default quality cap
- `--no-ppt-compatible`: keep the raw downloaded file instead of normalizing for PowerPoint/QuickTime
- `--cookies-from-browser`: force a specific browser cookie source
- `--concurrency`: control bounded parallel downloads for multi-URL batches
- `--dry-run`: preview without downloading
- `--tiktok-shop`: try TikTok HTTP resolver providers first for known Shop/promoted URLs
- `--no-tiktok-resolver`: disable third-party TikTok resolver fallback
- `--kpi-report`: summarize recent real-run metrics
- `--version`: print the script version

The compression helper accepts `TARGET_MB`, `SAFETY`, `PRESET`, and optional `--force` as positional arguments. It requires `ffmpeg`, `ffprobe`, and `python3`.

## Repository Layout

```text
social-video-downloader/
├── SKILL.md
├── README.md
├── _meta.json
├── agents/
│   └── openai.yaml
└── scripts/
    ├── compress_for_whatsapp.sh
    └── download_social_video.py
```

Local runtime artifacts are intentionally ignored:

- `cache/`
- `metrics/`
- `__pycache__/`

## Notes

- This project is not affiliated with TikTok, Instagram, Facebook, X/Twitter, YouTube, or Xiaohongshu.
- Some platforms require login state depending on region and content restrictions.
- Restricted sources may expose only audio; those are treated as failures instead of fake success.
- For TikTok only, the script may submit the input URL to SnapTik and then SSSTik when `--tiktok-shop` is set or local extraction cannot produce usable video and audio. Use `--no-tiktok-resolver` to disable this third-party fallback.
- The defaults are optimized for day-to-day sharing, playback, and presentation use, not archival-quality collection.
- KPI reports exclude `--dry-run` events from scoring so the metrics stay meaningful.
- Users are responsible for complying with the target platform's terms of service and local laws when downloading content.

## License

This project is licensed under the MIT License. See [LICENSE](./LICENSE).
