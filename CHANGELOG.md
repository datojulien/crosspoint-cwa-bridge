# Changelog

All notable changes to this project are documented here. Versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Remove chapter XHTML comments before parsing for X3/X4 downloads, including
  manifest XHTML resources with nonstandard extensions and image-conversion
  fallbacks, while preserving chapter text, CDATA, and processing instructions.
- Strip unused embedded fonts, stylesheet and inline `@font-face` rules, OPF
  font entries, and matching font-obfuscation records from X3/X4 optimized
  derivatives; preserve unrelated assets and encryption metadata.
- Advance the derivative cache version to `crosspoint-standard-v4` so existing
  cached books are regenerated automatically. Original/KOReader profiles and
  source EPUBs remain unchanged.

### Fixed

- Split X3/X4 Recently Added OPDS XML into compact 10-book pages while
  retaining forward/backward navigation across CWA result pages.
- Canonicalize root-contained EPUB ZIP paths from metadata export and collapse
  byte-identical aliases without accepting root escapes or conflicting contents.

## [0.7.3] - 2026-10-06

### Fixed

- Validate X3/X4 EPUB ZIP checksums and chapter XHTML before streaming.
- Repair unclosed charset meta tags introduced by CWA's Kindle EPUB fixer,
  including XHTML manifest resources with an `.xml` extension.
- Preserve repairs when image optimization falls back, and reject unrepairable
  EPUBs or processing-limit bypasses before sending EPUB bytes.
- Validate cached derivative XHTML and advance the optimizer cache version.

## [0.7.2] - 2026-08-26

### Added

- Added `/opds/koreader` as a direct authenticated alias for the byte-identical
  original EPUB profile.

## [0.7.1] - 2026-08-13

### Security

- Constrain streamed derivative files to their expected cache or work root,
  reject symbolic links and non-regular files, and open with no-follow semantics.
- Return a fixed feed-error response without exposing parser exception details.

## [0.7.0] - 2026-08-13

### Added

- Public release documentation, license files, contribution and security
  policies, CI, Dependabot, and reproducible hashed dependency installation.
- Configurable host address, CWA origin, Docker network, UID/GID, and image
  through a local `.env` file.
- Total uncompressed ZIP and archive-entry limits for EPUB processing.
- Explicit Pillow decoder allowlisting for JPEG, PNG, GIF, WebP, and BMP.

### Changed

- Deployment defaults now bind to loopback until the operator supplies a LAN
  address.
- TLS bootstrap requires an explicit bridge IP and no longer assumes a
  username, home directory, or private network.

## [0.6.1] - 2026-08-13

- Added direct `/opds/X3` and `/opds/X4` model entry points while retaining the
  `/opds` selector and canonical profile routes.

## [0.6.0] - 2026-08-13

- Added the public status portal and isolated HTTPS administration console.
- Added pending settings, diagnostics, sanitized activity, cache cleanup,
  password changes, and controlled bridge restart.

## [0.5.0] - 2026-08-13

- Added persistent checksum-validated derivative caching and Raspberry Pi
  performance validation.

## [0.4.0] - 2026-08-13

- Added X3/X4 EPUB optimisation with original-delivery fallback.

## [0.3.0] - 2026-08-13

- Completed OPDS/OpenSearch rewriting and encoded-path compatibility.

[0.7.3]: https://github.com/datojulien/crosspoint-cwa-bridge/releases/tag/v0.7.3
[0.7.2]: https://github.com/datojulien/crosspoint-cwa-bridge/releases/tag/v0.7.2
[0.7.1]: https://github.com/datojulien/crosspoint-cwa-bridge/releases/tag/v0.7.1
[0.7.0]: https://github.com/datojulien/crosspoint-cwa-bridge/releases/tag/v0.7.0
[0.6.1]: https://github.com/datojulien/crosspoint-cwa-bridge/releases/tag/v0.6.1
[0.6.0]: https://github.com/datojulien/crosspoint-cwa-bridge/releases/tag/v0.6.0
[0.5.0]: https://github.com/datojulien/crosspoint-cwa-bridge/releases/tag/v0.5.0
[0.4.0]: https://github.com/datojulien/crosspoint-cwa-bridge/releases/tag/v0.4.0
[0.3.0]: https://github.com/datojulien/crosspoint-cwa-bridge/releases/tag/v0.3.0
