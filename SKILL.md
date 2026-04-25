---
name: social-video-downloader
description: Download TikTok videos, Instagram reels/posts, Facebook videos, X/Twitter videos, YouTube videos, and YouTube Shorts to the local Downloads folder with balanced quality rather than maximum bitrate or resolution. Use when a user provides one or more social video URLs and wants the media saved locally with audio included, practical file sizes, reliable defaults for repeated downloading work, and final files that are compatible with QuickTime Player and PowerPoint. Trigger this skill for natural-language requests such as "下载这个视频", "帮我下载这个链接", "download this video", "save this reel", or similar requests that include a supported social-media URL.
---

# Social Video Downloader

Use this skill when the user gives a TikTok, Instagram, Facebook, X/Twitter, or YouTube URL and wants the video downloaded locally.

Prefer the bundled script so the behavior stays consistent:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/social-video-downloader/scripts/download_social_video.py" "<url>"
```

## Workflow

1. Accept one or more URLs from TikTok, Instagram, Facebook, X/Twitter, YouTube, or YouTube Shorts.
2. Save output to `~/Downloads` unless the user explicitly asks for another directory.
3. Use the bundled script instead of hand-writing `yt-dlp` commands.
4. Route social-media page URLs through `yt-dlp`, but route direct media URLs such as `.m3u8` and `.mp4` through the script's dedicated direct-download path instead of forcing page-extractor logic.
5. Prefer balanced quality capped at roughly 720p for normal social-page downloads. Do not intentionally download the highest available bitrate or resolution unless the user asks for it.
6. For direct media URLs, prioritize stable capture over format negotiation. Download the supplied media stream directly, then only normalize it afterward if compatibility work is needed.
7. For direct `.m3u8` URLs, try fast `ffmpeg` capture first. If that fails or stalls without making file-size progress for a while, fall back to downloading the playlist segments and merging them so unstable CDN/HLS links still have a recovery path.
8. Keep the HLS fallback conservative but not slow: use a small bounded worker pool for segment downloads so recovery is faster without turning into an unstable high-concurrency fetch storm.
9. Ensure audio is present. Prefer `video+audio` merged into MP4 when possible, and fall back to progressive formats that already contain audio.
10. Convert the downloaded result to PowerPoint-compatible `H.264 + AAC` MP4 unless the user explicitly says not to, but skip the re-encode entirely when the downloaded file is already compatible.
11. Let the script auto-retry with browser cookies when anonymous access fails. If a specific browser matters, pass `--cookies-from-browser`. Otherwise, only try browsers that actually appear to exist on the machine.
12. Reuse a previously downloaded local file for the same URL only when the cache says it exists and the file still contains both video and audio. This improves speed without sacrificing confidence.
13. Read the download summary and report back which files succeeded and where they were saved.
14. Treat TikTok Shop and similar restricted social-commerce URLs specially: if the platform exposes only audio and no video stream, report that the source appears restricted instead of pretending the download succeeded.
15. For multi-URL work, keep parallelism bounded, show per-URL start/finish updates during the run, and preserve the final summary in the original input order.

## Natural-Language Triggers

Trigger this skill when the user includes a supported URL and asks in natural language to download or save the video.

Common examples:

- `下载这个视频 https://...`
- `帮我把这个 TikTok 存到下载目录 https://...`
- `download this reel https://...`
- `save this youtube short https://...`
- `把这个 x 视频下载下来 https://...`
- `download the facebook video from this link https://...`

Do not require the user to mention the skill name. The combination of a supported social-media URL and an obvious download intent is enough.

## Defaults

- Output directory: `~/Downloads`
- Quality target: cap height at `720`
- Container preference: MP4 when remuxing, merging, or finalizing the file
- Audio requirement: always prefer formats with audio; do not accept silent video unless the source itself has no audio track
- Playback compatibility: transcode the final result to `H.264 + AAC` so it works better in QuickTime Player and PowerPoint, but skip re-encoding when the file is already compatible
- Cookie behavior: retry with local browser cookies automatically when the first unauthenticated attempt fails, but only for browsers that appear to have a local cookie store
- Direct-media behavior: if the URL already points to media such as `.m3u8` or `.mp4`, bypass social-page extraction and download the media directly
- HLS fallback behavior: if direct `ffmpeg` capture of an `.m3u8` URL fails or stalls, download the HLS segments and merge them instead of giving up immediately
- Playlist handling: download only the requested item unless the user explicitly asks for a playlist
- Batch behavior: accept multiple URLs directly or extract multiple URLs from a pasted text block
- Restricted-source behavior: if a supported platform only exposes an audio stream and no video stream, treat the download as failed; this is especially common with TikTok Shop or other protected commerce/media pages
- Retry behavior: use extractor, file, and fragment retries plus concurrent fragment downloads to improve resilience and speed on unstable HLS/media endpoints
- Result behavior: summarize outcomes with stable status labels such as direct success, HLS fallback success, auth-needed failure, network instability, or restricted audio-only failure
- Cache behavior: reuse previously downloaded successful outputs only when the cached file still exists and still contains both video and audio
- Batch UX behavior: print progress as each URL starts or finishes, and group the final summary by success, cache hit, auth issues, network instability, restricted source, and invalid input
- KPI behavior: record lightweight per-run metrics locally and expose a CLI report so the downloader can be tuned against delivery, speed, cache-hit, and fallback-recovery goals; `--dry-run` events are logged but excluded from KPI scoring

## Dependency Handling

Run the script first. It checks for `yt-dlp` and `ffmpeg`.

If either tool is missing, let the script auto-install them through Homebrew by keeping the default `--install-missing` behavior. If Homebrew is unavailable or the user does not want automatic installs, stop and tell the user which dependency is missing.

## Commands

Basic download:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/social-video-downloader/scripts/download_social_video.py" "<url>"
```

Multiple URLs:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/social-video-downloader/scripts/download_social_video.py" "<url-1>" "<url-2>"
```

Extract and download all supported URLs from a pasted text block:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/social-video-downloader/scripts/download_social_video.py" "请下载这些链接： https://... 还有 https://..."
```

Read URLs from a text file:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/social-video-downloader/scripts/download_social_video.py" "下载这个文件里的链接" --text-file /path/to/links.txt
```

Preview what a batch would do without downloading:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/social-video-downloader/scripts/download_social_video.py" "请下载这些链接： https://... 还有 https://..." --dry-run
```

Tune parallel download count when the user explicitly wants it:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/social-video-downloader/scripts/download_social_video.py" "<url-1>" "<url-2>" "<url-3>" --concurrency 4
```

Review recent KPI trends without downloading anything:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/social-video-downloader/scripts/download_social_video.py" --kpi-report
```

Use cookies from Chrome for sites that need login state:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/social-video-downloader/scripts/download_social_video.py" "<url>" --cookies-from-browser chrome
```

Choose a different folder only when the user asks:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/social-video-downloader/scripts/download_social_video.py" "<url>" --output-dir /custom/path
```

Disable the compatibility transcode only when the user explicitly wants the raw downloaded file:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/social-video-downloader/scripts/download_social_video.py" "<url>" --no-ppt-compatible
```

## Notes

- Instagram, Facebook, X/Twitter, and some TikTok links can require logged-in cookies, depending on region and platform changes.
- The script prints a per-URL summary at the end so the caller can see which files were saved and which links failed.
- The script also prints per-URL start/finish progress during batch runs so long jobs do not feel silent.
- The script records lightweight local run metrics under `~/.codex/skills/social-video-downloader/metrics/` so you can inspect recent KPI trends with `--kpi-report`.
- The default output is intentionally optimized for Mac playback and PowerPoint embedding, not archival purity.
- The script now avoids wasting time on browsers that are not installed and avoids re-encoding files that are already PowerPoint-safe.
- TikTok Shop or other commercial/restricted links may expose only audio to third-party tools. When that happens, treat the URL as restricted and tell the user the platform did not provide a usable video stream.
- If the user asks for only audio, this skill is not the right default. Use a separate audio-only flow.
- If the user asks for the highest quality, pass `--max-height 1080` or run `yt-dlp` manually with an explicit quality request instead of changing the skill default.
- If a download fails because the platform changed, inspect the `yt-dlp` error first and update the script rather than replacing the workflow.

## Resource

Use [download_social_video.py](./scripts/download_social_video.py) for all normal work.
