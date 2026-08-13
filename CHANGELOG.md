# Changelog

All notable changes to this project are documented here. Versions follow
[Semantic Versioning](https://semver.org/).

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

[0.7.0]: https://github.com/datojulien/crosspoint-cwa-bridge/releases/tag/v0.7.0
[0.6.1]: https://github.com/datojulien/crosspoint-cwa-bridge/releases/tag/v0.6.1
[0.6.0]: https://github.com/datojulien/crosspoint-cwa-bridge/releases/tag/v0.6.0
[0.5.0]: https://github.com/datojulien/crosspoint-cwa-bridge/releases/tag/v0.5.0
[0.4.0]: https://github.com/datojulien/crosspoint-cwa-bridge/releases/tag/v0.4.0
[0.3.0]: https://github.com/datojulien/crosspoint-cwa-bridge/releases/tag/v0.3.0
