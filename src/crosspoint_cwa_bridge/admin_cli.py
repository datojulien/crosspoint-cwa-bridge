"""Interactive bridge-administrator credential setup."""

from __future__ import annotations

import argparse
from getpass import getpass
import os
from pathlib import Path

from .admin_state import ADMIN_USERNAME, PasswordStore, ensure_private_directory


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("set-password",))
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(
            os.environ.get("ADMIN_STATE_DIR", "/var/lib/crosspoint-cwa-bridge/admin")
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_private_directory(args.state_dir)
    print(f"Setting the password for {ADMIN_USERNAME}.")
    password = getpass("New password (at least 12 characters): ")
    confirmation = getpass("Confirm new password: ")
    if password != confirmation:
        raise SystemExit("Passwords did not match; nothing was changed.")
    try:
        PasswordStore(args.state_dir).set_password(password)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print("Bridge administrator password updated. No plaintext password was stored.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
