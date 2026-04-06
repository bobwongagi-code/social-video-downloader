# Security Policy

## Supported Versions

Security fixes are provided on a best-effort basis for the latest version on `main`.

| Version | Supported |
| --- | --- |
| Latest `main` | Yes |
| Older commits/tags | No |

## Reporting a Vulnerability

Please do not open a public issue for:

- Browser cookies
- Access tokens
- Private URLs
- Credential-handling flaws
- Unsafe downloader behavior that could expose local secrets

If GitHub private vulnerability reporting is enabled for this repository, use that channel. Otherwise, contact the repository owner privately through GitHub.

When reporting a security issue:

- Describe the impact
- Include reproduction steps
- Redact cookies, tokens, and personal data
- Say whether the issue affects local files, browser credentials, or downloaded media handling

## Scope Notes

This project wraps third-party tools and interacts with third-party platforms. Reports may involve:

- `yt-dlp`
- `ffmpeg`
- Platform-side content restrictions or authentication flows

The project does not guarantee bypasses for platform protections or restricted content.
