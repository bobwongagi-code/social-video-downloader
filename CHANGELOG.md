# Changelog

All notable changes to this project will be documented in this file.

This changelog starts from the current public-repository baseline.

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
