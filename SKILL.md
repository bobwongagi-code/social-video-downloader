---
name: social-video-downloader
description: Download social videos and compress local videos to practical WhatsApp-ready MP4 files. Use when a user provides a supported social-media URL to download, or a local video path and asks to compress it to a target size for WhatsApp or file sharing. Outputs include audio when available and use a basic H.264 + AAC MP4 media profile that generally works well in QuickTime, PowerPoint, and WhatsApp workflows.
---

# Social Video Media

Use this skill for two video operations: download a video from a supported social URL, or compress a local video for WhatsApp/file sharing.

When the user gives a local video path and asks to compress it to a target size, use the bundled WhatsApp compression helper:

```bash
bash "${CODEX_HOME:-$HOME/.codex}/skills/social-video-downloader/scripts/compress_for_whatsapp.sh" "<input>" "<output>" "<target-mib>"
```

Prefer the bundled script so the behavior stays consistent:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/social-video-downloader/scripts/download_social_video.py" "<url>"
```

## Route Selection

- If the input contains a supported social URL and the user asks to download or save it, use `download_social_video.py`.
- If the input is a local video path and the user asks to compress it, make it WhatsApp-ready, or fit a target size, use `compress_for_whatsapp.sh`.
- If the user explicitly asks to download and then compress, run the two routes in that order and report both outputs.
- Do not send a local file path to the URL downloader or send a social URL to the local compression helper.

## Download Workflow

1. Accept one or more URLs from TikTok, Instagram, Facebook, X/Twitter, YouTube, or YouTube Shorts.
2. Save output to `~/Downloads` unless the user explicitly asks for another directory.
3. Use the bundled script instead of hand-writing `yt-dlp` commands.
4. Route normal social-media page URLs through `yt-dlp`, but route direct media URLs such as `.m3u8` and `.mp4` through the script's dedicated direct-download path instead of forcing page-extractor logic.
5. For a TikTok URL explicitly described as Shop, promoted, commerce, or restricted, pass `--tiktok-shop` so the script explicitly discloses and submits it to the HTTP resolver providers before ordinary TikTok extraction. Do not use browser automation for this first-stage path.
6. For other TikTok URLs, keep `yt-dlp` as the first path. Do not submit the URL to third-party resolver providers unless the user explicitly enables `--tiktok-resolver`.
7. Prefer balanced quality capped at roughly 720p for normal social-page downloads. Do not intentionally download the highest available bitrate or resolution unless the user asks for it.
8. For direct media URLs, prioritize stable capture over format negotiation. Download the supplied media stream directly, then only normalize it afterward if compatibility work is needed.
9. For direct `.m3u8` URLs, use the safe simple-HLS fallback only for bounded, unencrypted MPEG-TS playlists. Reject encrypted, fMP4, alternate-audio, byte-range, discontinuity, and live-refresh tags instead of guessing.
10. Keep HLS segment downloads bounded by playlist depth, segment count, per-segment bytes, total bytes, and a small worker pool.
11. Ensure audio is present when the source is expected to have it. Retain a legitimate direct video-only source and report it as `video_only_source`; report a social extraction that loses expected audio as `audio_expected_but_missing`.
12. Normalize the downloaded result to a basic `H.264 + AAC` MP4 media profile unless the user explicitly says not to, but skip the re-encode entirely when the downloaded file already meets that profile. This is a codec/profile check, not a guarantee for every PowerPoint or platform version.
13. Never install dependencies or read browser cookies implicitly. Use `--install-missing` or `--cookies-from-browser`; use `--auto-cookies` only after the user explicitly permits browser-cookie access.
14. Reuse a previously downloaded local file only when the cache key matches URL, output directory, quality, basic-profile setting, metadata policy, identity scope, route, and tool version, and the file still contains a video stream.
15. Read the download summary and report back which files succeeded and where they were saved.
16. Treat resolver results as untrusted until the final file passes both video-stream and audio-stream validation; never report an audio-only result as success.
17. For multi-URL work, keep parallelism bounded, show per-URL start/finish updates during the run, and preserve the final summary in the original input order.

## Local Compression Workflow

1. Use the compression helper for local files, not the social URL downloader.
2. Preserve the original input file. The helper writes a new MP4 and refuses to overwrite an existing output unless `--force` is explicitly supplied.
3. Use the requested target size as a hard upper bound. The default `0.90` safety factor reserves room for MP4 overhead while two-pass encoding allocates the remaining budget between video and audio.
4. Keep H.264 High Profile, AAC audio when the source has audio, `yuv420p`, and `faststart` so the result works well when sent as a WhatsApp Document/File.
5. Verify the final file with `ffprobe` and report its path, size, duration, and streams. If the target forces an extremely low video bitrate, report the quality risk instead of presenting the result as lossless.

## Natural-Language Triggers

Trigger this skill when the user asks to download/save a supported social URL or asks to compress a local video for WhatsApp/file sharing.

Common examples:

- `下载这个视频 https://...`
- `帮我把这个 TikTok 存到下载目录 https://...`
- `download this reel https://...`
- `save this youtube short https://...`
- `把这个 x 视频下载下来 https://...`
- `download the facebook video from this link https://...`
- `把 /path/to/video.mp4 压缩到 64M 发 WhatsApp`
- `compress this local video to 20MB for WhatsApp`

Do not require the user to mention the skill name. A supported social-media URL plus download intent, or a local video path plus compression intent, is enough.

## Defaults

- Output directory: `~/Downloads`
- Local compression output: same directory as the input unless the user supplies an explicit output path
- Quality target: cap height at `720`
- Container preference: MP4 when remuxing, merging, or finalizing the file
- Audio requirement: always prefer formats with audio; do not accept silent video unless the source itself has no audio track
- Playback profile: normalize the final result to a basic `H.264 + AAC` MP4 profile that generally works better in QuickTime Player and PowerPoint, but skip re-encoding when the file already meets that profile
- Cookie behavior: browser cookies are never read by default; `--cookies-from-browser` or `--auto-cookies` is required
- Direct-media behavior: if the URL already points to media such as `.m3u8` or `.mp4`, bypass social-page extraction and download the media directly
- HLS fallback behavior: only simple, unencrypted MPEG-TS playlists are handled; advanced HLS is rejected with a clear reason
- Playlist handling: download only the requested item unless the user explicitly asks for a playlist
- Batch behavior: accept multiple URLs directly or extract multiple URLs from a pasted text block
- Restricted-source behavior: classify `audio_only_result`, `no_media_stream`, `audio_expected_but_missing`, and `video_only_source` separately; never report an audio-only result as success
- TikTok resolver behavior: for a known TikTok Shop/promoted URL, use `--tiktok-shop` to explicitly submit it to HTTP resolver providers first; other TikTok links remain local-only unless `--tiktok-resolver` is explicit
- Retry behavior: use extractor, file, and fragment retries plus concurrent fragment downloads to improve resilience and speed on unstable HLS/media endpoints
- Result behavior: summarize outcomes with stable status labels such as direct success, HLS fallback success, auth-needed failure, network instability, or restricted audio-only failure
- Cache behavior: cache keys are salted digests and include all output constraints, including metadata policy; cache files and metrics are user-private and atomically updated
- Batch UX behavior: print progress as each URL starts or finishes, and group the final summary by success, cache hit, auth issues, network instability, restricted source, and invalid input
- KPI behavior: record lightweight per-run metrics locally and expose a CLI report so the downloader can be tuned against delivery, speed, cache-hit, and fallback-recovery goals; `--dry-run` does not write events
- Local compression behavior: use `scripts/compress_for_whatsapp.sh`; it requires `ffmpeg`, `ffprobe`, and `python3`, uses exact-duration bitrate budgeting, and validates the final H.264/AAC streams before reporting success

## Dependency Handling

For the URL route, run the downloader first. It checks for `yt-dlp`, `ffmpeg`, and `ffprobe`.

If a downloader dependency is missing, stop and tell the user which dependency is missing. Only use `--install-missing` after the user explicitly permits Homebrew installation.

The local compression helper does not install dependencies automatically. Check for `ffmpeg`, `ffprobe`, and `python3` first; ask before making system-level dependency changes.

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

Use resolver-first routing when the user identifies a TikTok Shop or promoted video:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/social-video-downloader/scripts/download_social_video.py" "<tiktok-url>" --tiktok-shop
```

Explicitly allow third-party TikTok resolver submission:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/social-video-downloader/scripts/download_social_video.py" "<tiktok-url>" --tiktok-resolver
```

Choose a different folder only when the user asks:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/social-video-downloader/scripts/download_social_video.py" "<url>" --output-dir /custom/path
```

Disable the compatibility transcode only when the user explicitly wants the raw downloaded file:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/social-video-downloader/scripts/download_social_video.py" "<url>" --no-ppt-compatible
```

Compress a local video for WhatsApp:

```bash
bash "${CODEX_HOME:-$HOME/.codex}/skills/social-video-downloader/scripts/compress_for_whatsapp.sh" \
  "/path/to/input.mp4" \
  "/path/to/output [whatsapp].mp4" \
  64
```

Use `--force` only when replacing an existing output is intentional:

```bash
bash "${CODEX_HOME:-$HOME/.codex}/skills/social-video-downloader/scripts/compress_for_whatsapp.sh" \
  "/path/to/input.mp4" \
  "/path/to/output.mp4" \
  64 0.90 slow \
  --force
```

## Notes

- Instagram, Facebook, X/Twitter, and some TikTok links can require logged-in cookies, depending on region and platform changes.
- The script prints a per-URL summary at the end so the caller can see which files were saved and which links failed.
- The script also prints per-URL start/finish progress during batch runs so long jobs do not feel silent.
- The script records lightweight local run metrics under `~/.codex/skills/social-video-downloader/metrics/` so you can inspect recent KPI trends with `--kpi-report`.
- The default output is intentionally optimized for Mac playback and presentation workflows, not archival purity; the basic media profile is not a guarantee for every PowerPoint or platform version.
- The script now avoids wasting time on browsers that are not installed and avoids re-encoding files that already meet its basic H.264/AAC/MP4 profile.
- TikTok fallback providers are HTTP-only in this stage; no browser or headless-browser automation is required.
- When `--tiktok-shop` or explicit `--tiktok-resolver` is used, the script discloses that it is submitting the URL to SnapTik and SSSTik. No third-party resolver is called by default.
- If the user asks for only audio, this skill is not the right default. Use a separate audio-only flow.
- If the user asks for the highest quality, pass `--max-height 1080` or run `yt-dlp` manually with an explicit quality request instead of changing the skill default.
- If a download fails because the platform changed, inspect the `yt-dlp` error first and update the script rather than replacing the workflow.
- For WhatsApp sharing, recommend sending the result as a Document/File when preserving the encoded quality matters; ordinary gallery/video sending may apply another platform transcode.

## Resource

Use [download_social_video.py](./scripts/download_social_video.py) for social downloads and [compress_for_whatsapp.sh](./scripts/compress_for_whatsapp.sh) for local WhatsApp compression.
