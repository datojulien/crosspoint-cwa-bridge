#!/usr/bin/env python3
"""Verify that a built wheel contains required licensing material."""

from __future__ import annotations

import sys
from pathlib import Path
from zipfile import ZipFile


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: verify_wheel.py WHEEL", file=sys.stderr)
        return 2
    wheel = Path(sys.argv[1])
    with ZipFile(wheel) as archive:
        names = archive.namelist()
        for required in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
            if not any(name.endswith(f"/licenses/{required}") for name in names):
                print(f"wheel is missing {required}", file=sys.stderr)
                return 1
    print(f"verified licensing material in {wheel.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
