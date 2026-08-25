#!/usr/bin/env python3
"""
One-time helper to mint a Google "master token" for gkeepapi.

This wraps gpsoauth.exchange_token(), the same call gkeepapi's own docs
point to. It does NOT perform the browser login step for you — Google's
embedded sign-in flow is intentionally hard to automate and changes often.
See keep-sync-daemon/README.md for how to obtain the "OAuth Token" input
this script needs.

Email and state_dir default to whatever's in config.json (next to this
script), so the only thing you need to paste is the OAuth Token. The
resulting master token grants full account access, same as a password —
it's written straight to <state_dir>/master_token (0600) rather than
printed, unless you pass --print.

On Windows, keep_sync_tray.py wraps this same exchange in a GUI so you
never have to touch a terminal.
"""
import argparse
import getpass
import json
import sys
from pathlib import Path

import keep_sync_core as core


def load_config_defaults(config_path: Path) -> dict:
    if not config_path.exists():
        return {}
    try:
        with config_path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=str(Path(__file__).parent / "config.json"),
        help="Path to config.json to read email/state_dir defaults from",
    )
    parser.add_argument("--email", help="Overrides email from config.json")
    parser.add_argument("--state-dir", help="Overrides state_dir from config.json")
    parser.add_argument(
        "--android-id",
        default=core.DEFAULT_ANDROID_ID,
        help=f"16 hex-digit device id (default: {core.DEFAULT_ANDROID_ID})",
    )
    parser.add_argument(
        "--print",
        action="store_true",
        dest="do_print",
        help="Also print the token to stdout (e.g. to use KEEP_MASTER_TOKEN instead)",
    )
    args = parser.parse_args()

    cfg = load_config_defaults(Path(args.config))
    email = args.email or cfg.get("email") or input("Email: ").strip()
    state_dir = core.resolve_state_dir(args.state_dir or cfg.get("state_dir", core.DEFAULT_STATE_DIR))

    if not email:
        print("No email found — pass --email or set it in config.json", file=sys.stderr)
        return 2

    oauth_token = getpass.getpass(f"OAuth Token for {email} (from the embedded sign-in flow): ").strip()
    if not oauth_token:
        print("No token pasted, aborting.", file=sys.stderr)
        return 2

    try:
        master_token = core.exchange_master_token(email, oauth_token, args.android_id)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1

    token_path = core.save_master_token(state_dir, master_token)

    print(f"\nMaster token saved to {token_path} (0600).")
    if args.do_print:
        print("\nMaster token:")
        print(master_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
