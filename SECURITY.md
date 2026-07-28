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

## Network and Privacy Boundaries

- Direct and HLS requests allow only public `http`/`https` destinations; private, loopback, link-local, multicast, unspecified, and metadata-service addresses are rejected.
- Redirect targets are revalidated and pinned to a public address. HLS fallback rejects encrypted, fMP4, alternate-audio, and other unsupported playlist features.
- Browser cookies, Homebrew installation, and SnapTik/SSSTik submission are disabled unless the caller explicitly opts in.
- URL query data is redacted from normal logs and stored only as salted digests in cache and metrics keys.
- Outputs use atomic no-clobber commits by default; `--force` is required to replace an existing file.
