# Contributing

Thank you for helping improve the bridge. Keep changes focused, privacy-safe,
and compatible with stock CrossPoint devices and CWA's authorization model.

## Development setup

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
.venv/bin/python -m unittest discover -s tests -v
```

Use synthetic fixtures for tests. Do not add real library files, book metadata,
credentials, client addresses, URLs containing private routes, certificate
keys, activity databases, cache entries, deployment fingerprints, or container
identifiers.

## Pull requests

- Open an issue first for substantial behavior or route changes.
- Add regression tests for changed proxy, optimizer, cache, admin, or privacy
  behavior.
- Preserve `/healthz` and `/opds` wire compatibility unless a breaking release
  is explicitly proposed.
- Keep browser assets dependency-free and administration isolated from the
  public HTTP listener.
- Update documentation and `CHANGELOG.md` for operator-visible changes.
- Confirm the full test suite and Docker build pass.

By contributing, you agree that your contribution is licensed under the
repository's GNU GPL-3.0-or-later license.
