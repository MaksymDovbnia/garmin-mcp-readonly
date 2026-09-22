"""One-time interactive sign-in that writes a reusable token store.

This is a terminal program the user runs themselves. It is intentionally kept
out of the MCP server: the password is typed by a human into a hidden prompt,
used once to mint tokens, and never written to disk, config or environment.
"""

from __future__ import annotations

import sys
from getpass import getpass

from garminconnect import Garmin

from .client import token_dir


def main() -> int:
    path = token_dir()
    print(f"Signing in to Garmin Connect. Tokens will be written to {path}")
    print("Your password is used once to mint tokens and is never stored.\n")

    email = input("Garmin email: ").strip()
    password = getpass("Garmin password (hidden): ")
    if not email or not password:
        print("Email and password are both required.", file=sys.stderr)
        return 2

    garmin = Garmin(
        email=email,
        password=password,
        prompt_mfa=lambda: input("MFA code: ").strip(),
    )

    try:
        garmin.login(str(path))
    except Exception as exc:  # noqa: BLE001 - this is the CLI's error boundary
        print(f"\nLogin failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        del password

    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass

    print(f"\nSuccess. Tokens stored in {path}")
    print("Garmin tokens stay valid for roughly a year; re-run this if the "
          "server reports they were rejected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
