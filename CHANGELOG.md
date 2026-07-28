# Changelog

All notable changes to this project will be documented in this file.

This changelog starts from the current public-repository baseline.

## [Unreleased]

### Changed

- Split the downloader entrypoint into route adapters and workflow orchestration without changing the CLI contract
- Added typed download options/services at the CLI boundary and made batch-budget exhaustion fail fast instead of waiting forever after completed downloads
- Added a source-to-installed-runtime synchronization tool and CI verification to prevent skill drift

## [0.5.0] - 2026-07-28

### Added

- WhatsApp compression helper for local videos with exact-duration bitrate budgeting, two-pass H.264 encoding, and final media validation
- Real FFmpeg integration coverage for size limits, stream validation, rotation, silent sources, and overwrite protection

### Changed

- Clarified the skill as a dual-route video media workflow: social URL download and local WhatsApp compression
- Disabled implicit dependency installation, browser-cookie access, and third-party TikTok resolver calls
- Added private salted cache keys, atomic no-clobber output commits, bounded HLS/network handling, and read-only dry runs
- Classified video-only, audio-only, missing-audio, and no-media outcomes separately
- Added metadata-policy-aware cache keys, live-playlist rejection, preflight disk/batch reservations, and structured route/error results
- Clarified that the download compatibility check is a basic H.264/AAC/MP4 profile rather than a universal PowerPoint guarantee

## [0.4.0] - 2026-05-26

### Added

- HTTP-only TikTok resolver fallback through SnapTik and SSSTik for failed or unusable local extraction results
- `--tiktok-shop` routing for known Shop/promoted videos and `--no-tiktok-resolver` opt-out
- Offline tests for resolver decoding and TikTok fallback routing

### Changed

- Resolver-returned media is accepted only after the same video-and-audio validation used by existing downloads

## [0.3.1] - 2026-04-06

### Added

- Public repository documentation, including README, CONTRIBUTING, SECURITY, and MIT licensing
- A lightweight KPI reporting path for recent real download runs
- Local cache reuse and metrics logging for repeated workflows
- Stable handling for direct media URLs, social-page URLs, and HLS fallback paths

### Changed

- Default download flow favors balanced quality, audio-preserving output, and presentation-friendly MP4 results
- README now documents the project as a reusable public repository rather than a private/internal tool

### Fixed

- Reduced false-success cases by treating audio-only restricted sources as failures
- Improved compatibility output so downloaded media works more reliably in QuickTime Player and PowerPoint
