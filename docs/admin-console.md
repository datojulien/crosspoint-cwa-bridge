# Administration console

Administration is intentionally isolated from the public OPDS listener. With
the example LAN address, HTTP port `8094` serves the portal and OPDS routes;
HTTPS port `8095` serves `/admin`. The HTTP listener does not expose an admin
route.

## Initial setup

Generate bridge-owned TLS state with the actual host LAN IP:

```sh
./scripts/bootstrap_admin_tls.sh ./state 192.168.1.50
```

The script refuses symbolic links, incomplete TLS pairs, and non-regular state
files. It creates a 3072-bit RSA self-signed certificate with the supplied IP
in `subjectAltName`, restricts private-key permissions, and prints the SHA-256
fingerprint. Verify that fingerprint before accepting the browser warning.

Create or replace the administration credential interactively:

```sh
docker compose run --rm --no-deps bridge \
  python -m crosspoint_cwa_bridge.admin_cli set-password
```

The account name is always `bridge-admin`. The prompt does not echo, the
password is unrelated to CWA, and only a salted `scrypt` record is persisted.

## Security model

- Sessions use opaque Secure, HttpOnly, SameSite=Strict cookies. They expire
  after 30 idle minutes or eight total hours and are invalidated by a restart
  or password change.
- Authenticated writes require a session-specific CSRF token. Login attempts
  are throttled in memory; client addresses are not persisted.
- Responses set a restrictive content security policy, frame denial, HSTS,
  no-referrer, and MIME-sniffing protections.
- Activity accepts only fixed anonymous operational fields, expires after
  seven days, and is capped at 10,000 rows.
- The container is read-only, drops Linux capabilities, enables
  `no-new-privileges`, and has no Docker socket or Calibre-library mount.
- Missing or invalid TLS/credential state fails closed for administration and
  does not disable OPDS.

The self-signed certificate protects transport but is not automatically trusted.
The fingerprint check is what ties the first browser connection to the
certificate generated on the host.

## Settings and restart

Editable settings are limited to JPEG quality, XML feed size, EPUB size,
decoded-image pixels, and CWA connect/read timeouts. CWA origin, listener
addresses, device dimensions, and host/container controls are not editable.

Saved values remain pending and are displayed as a diff. Confirming **Restart
bridge** atomically promotes the pending file and returns HTTP `202`; Compose's
`unless-stopped` policy then starts the process with the new settings.

## Cache cleanup

Cleanup can target X3, X4, or both derivative profiles. It waits for active
conversions and open cached downloads, removes only bridge-owned cache files,
and never touches CWA source EPUBs or Calibre metadata. A later authenticated
request rebuilds a removed derivative.

## Certificate renewal

Stop only the bridge, move the old `tls.crt` and `tls.key` to a private backup,
and rerun the bootstrap script with the current LAN IP. The script deliberately
does not overwrite an existing pair. Restart the bridge and verify the new
fingerprint before accepting it in a browser.

## Backup and rollback

Back up `state/` privately with mode-preserving tools. Never commit or share
its credential, TLS key, settings, or activity files. The derivative `cache/`
can be backed up but is always rebuildable.

For rollback, restore the previously tagged bridge image and its matching
Compose configuration, then run:

```sh
docker compose up -d --no-build --force-recreate
```

Do not remove volumes or bind directories. CWA is outside this Compose project
and should retain the same container identity and restart count throughout a
bridge upgrade or rollback.
