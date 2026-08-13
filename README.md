# CrossPoint–CWA Bridge

[![CI](https://github.com/datojulien/crosspoint-cwa-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/datojulien/crosspoint-cwa-bridge/actions/workflows/ci.yml)
[![License: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue.svg)](LICENSE)

An independent, local OPDS compatibility bridge between stock CrossPoint
readers and [Calibre-Web-Automated](https://github.com/crocodilestick/Calibre-Web-Automated)
(CWA). CWA remains authoritative for authentication, permissions, visibility,
shelves, searches, and source downloads.

This project is not affiliated with or endorsed by CrossPoint or
Calibre-Web-Automated. Product names and trademarks belong to their respective
owners.

## What it does

Version 0.7.0 provides:

- a model selector at `/opds` and direct `/opds/X3` and `/opds/X4` endpoints;
- byte-preserving OPDS proxying with Basic Auth and CWA permissions intact;
- X3/X4 image optimisation, EPUB repair, and checksum-validated derivatives;
- safe delivery of the original EPUB when conversion cannot be completed;
- a privacy-minimal status portal on HTTP port `8094`;
- a separate HTTPS administration console on port `8095`;
- bridge-only settings, diagnostics, anonymous activity, cache cleanup,
  password changes, and controlled restarts; and
- entry-count, total-expansion, per-file, decoded-pixel, and image-format
  boundaries for untrusted EPUB archives.

It does not mount the Calibre library, expose a Docker socket, administer CWA,
or control the host.

## Requirements

- A Linux host that runs Docker Engine with the Compose plugin. Raspberry Pi
  OS 64-bit on ARM64 is supported.
- An existing CWA container reachable through an external Docker network.
- A fixed LAN address for the bridge host.
- OpenSSL for the one-time self-signed certificate setup.

## Quick start

Clone the repository and create the local configuration:

```sh
git clone https://github.com/datojulien/crosspoint-cwa-bridge.git
cd crosspoint-cwa-bridge
cp .env.example .env
```

Edit `.env` and replace `192.168.1.50` with the host's LAN address. If CWA's
Compose project or service uses different names, also change
`CWA_DOCKER_NETWORK` and `CWA_UPSTREAM_URL`. The checked-in defaults bind only
to `127.0.0.1`; the example makes the service reachable on the LAN.

Create the private state and data directories as the same UID/GID configured
in `.env`, then generate a certificate containing the LAN IP as a SAN:

```sh
mkdir -p cache work state
chmod 700 state
./scripts/bootstrap_admin_tls.sh ./state 192.168.1.50
```

Record and verify the printed SHA-256 fingerprint before accepting the
self-signed certificate warning in a browser. Set a separate bridge password;
the fixed administration username is `bridge-admin`:

```sh
docker compose build
docker compose run --rm --no-deps bridge \
  python -m crosspoint_cwa_bridge.admin_cli set-password
docker compose up -d
docker compose ps
```

The password prompt does not echo. Only a salted `scrypt` record is stored in
`state/`; CWA credentials are never stored by the bridge.

With `BRIDGE_BIND_ADDRESS=192.168.1.50`, the useful addresses are:

```text
Landing page:  http://192.168.1.50:8094/
OPDS selector: http://192.168.1.50:8094/opds
CrossPoint X3: http://192.168.1.50:8094/opds/X3
CrossPoint X4: http://192.168.1.50:8094/opds/X4
Administration: https://192.168.1.50:8095/admin
```

Use the normal CWA account when the reader requests Basic Auth. Never place a
username or password in an OPDS URL.

## Configuration

`.env.example` contains the host-specific Compose values. Runtime limits live
in `compose.yaml` and can be reviewed through the administration console:

- JPEG quality: 85;
- maximum decoded pixels per image: 40 million;
- maximum source EPUB and total uncompressed ZIP content: 2 GiB;
- maximum ZIP entries: 10,000;
- maximum individual raster: 256 MiB;
- maximum XML/CSS document: 16 MiB; and
- CWA connect/read timeouts: 10/60 seconds.

The source and destination are always different files. Derivatives are
published atomically under `cache/`; temporary files are isolated under
`work/`. A cache hit still makes an authenticated request to CWA first, so a
cached derivative cannot bypass changed CWA permissions.

See [Administration](docs/admin-console.md) for the console security model,
certificate renewal, settings promotion, cache cleanup, and rollback.

## Development

The lock file pins and hashes all build and runtime dependencies:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
.venv/bin/python -m unittest discover -s tests -v
```

Tests use synthetic local EPUB and CWA fixtures. They do not access a Calibre
library. See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

## Upgrade and rollback

Before an upgrade, tag the currently running local image and copy the effective
Compose and `.env` files to a private location outside Git:

```sh
docker image tag crosspoint-cwa-bridge:local crosspoint-cwa-bridge:rollback
git pull --ff-only
docker compose build
docker compose up -d --force-recreate
```

To roll back without deleting cache or state:

```sh
docker image tag crosspoint-cwa-bridge:rollback crosspoint-cwa-bridge:local
docker compose up -d --no-build --force-recreate
```

Stopping or replacing this Compose project does not restart CWA. Do not run
`docker compose down -v`; persistent bridge data intentionally uses bind
mounts and should be removed only as a separate, explicit operation.

## Security and privacy

Public status output contains aggregate operational state only. Activity
records expire after seven days, are capped at 10,000 rows, and omit client
addresses, credentials, titles, authors, searches, URLs, cache keys, and book
identifiers. Missing or corrupt administration credentials/TLS state disables
administration while OPDS continues operating.

Please report vulnerabilities according to [SECURITY.md](SECURITY.md), not in
a public issue.

## License and upstream work

This project is licensed under
[GNU GPL version 3 or later](LICENSE). The EPUB optimisation behavior adapts
MIT-licensed CrossPoint Reader work; attribution and the applicable upstream
license are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Pinned compatibility references:

- CrossPoint Reader `develop`: `9b1fb712de83b87d518f6dc12a02977b6499bba2`;
- CWA `v4.0.6`: `1b80f9a74b3db1fe9b35978e38d2e62fcb6a7246`.
