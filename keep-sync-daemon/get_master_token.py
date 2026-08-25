#!/usr/bin/env python3
"""
One-time helper to mint a Google "master token" for gkeepapi.

This wraps gpsoauth.exchange_token(), the same call gkeepapi's own docs
point to. It does NOT perform the browser login step for you — Google's
embedded sign-in flow is intentionally hard to automate and changes often.
See keep-sync-daemon/README.md for how to obtain the "OAuth Token" input
this script needs.

The resulting master token grants full account access, same as a password.
This script only prints it; you decide where to store it (env var or the
state_dir/master_token file — see README).
"""
import argparse
import getpass
import sys

import gpsoauth


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", help="Google account email")
    parser.add_argument(
        "--android-id",
        help="16 hex-digit device id, e.g. 0000000000000000 (any value works)",
    )
    args = parser.parse_args()

    email = args.email or input("Email: ").strip()
    oauth_token = getpass.getpass("OAuth Token (from the embedded sign-in flow): ").strip()
    android_id = (args.android_id or input("Android ID [0000000000000000]: ").strip()) or "0000000000000000"

    result = gpsoauth.exchange_token(email, oauth_token, android_id)

    if "Token" not in result:
        print("Failed to exchange token. Response from Google:", file=sys.stderr)
        print(result, file=sys.stderr)
        return 1

    print("\nMaster token:")
    print(result["Token"])
    print(
        "\nStore this as KEEP_MASTER_TOKEN or in <state_dir>/master_token "
        "(see README) — treat it like a password.",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
