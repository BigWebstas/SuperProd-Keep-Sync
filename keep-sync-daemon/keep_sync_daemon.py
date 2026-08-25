#!/usr/bin/env python3
"""
Reads Google Keep checklist notes via gkeepapi and writes their current
state (title + items + checked flags) to a local JSON file. Intended to
run on a schedule (cron/systemd timer/Task Scheduler) — see README.md.

For a Windows app that runs its own schedule from a system-tray icon
instead of an external scheduler, see keep_sync_tray.py.

The Super Productivity plugin (../sp-plugin) reads this file through its
own Node.js execution sandbox; this script never talks to Super
Productivity directly.

On any failure, the previously written state file is left untouched so a
transient Keep/login error never blanks out the plugin's view.
"""
import argparse
import json
import sys
from pathlib import Path

import keep_sync_core as core


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=str(Path(__file__).parent / "config.json"),
        help="Path to config.json (see config.example.json)",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    try:
        cfg = core.load_config(config_path)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"[keep-sync-daemon] config error: {e}", file=sys.stderr)
        return 2

    result = core.sync_once(cfg)
    if result.ok:
        print(f"[keep-sync-daemon] {result.message}")
        return 0
    print(f"[keep-sync-daemon] {result.message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
