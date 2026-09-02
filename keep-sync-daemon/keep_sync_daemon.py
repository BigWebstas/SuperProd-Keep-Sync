#!/usr/bin/env python3
"""
Runs one Keep <-> Super Productivity reconcile pass (see
keep_sync_core.sync_once): pulls a Google Keep checklist via gkeepapi and
reconciles it against one SP project through SP's Local REST API, creating
and updating tasks on the SP side and pushing checked/renamed/new items
back to Keep. Intended to run on a schedule (cron/systemd timer/Task
Scheduler) — see README.md.

For a GUI app that runs its own schedule from a system-tray icon instead
of an external scheduler, see keep_sync_tray.py (Windows) /
keep_sync_tray_qt.py (Linux).

config.json must carry the SP fields (sp_access_token, sp_project_id,
keep_note_title) — see config.example.json. The SP desktop app has to be
running with its local REST API enabled for a pass to touch the SP side.

On any failure both sides are left untouched, so a transient Keep/login
error or SP being closed never corrupts either side.
"""
import argparse
import json
import os
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

    # Rotating file log next to the other state files, plus a native-crash
    # dump (.fault.log). Set KEEP_SYNC_DEBUG=1 for DEBUG detail. stdout/
    # stderr still carry the one-line result for cron redirects.
    core.setup_logging(core.resolve_state_dir(cfg["state_dir"]) / "keep_sync_daemon.log")

    result = core.sync_once(cfg)
    if result.ok:
        print(f"[keep-sync-daemon] {result.message}")
        return 0
    print(f"[keep-sync-daemon] {result.message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    if os.environ.get(core.KEEP_WORKER_ENV) == "1":
        raise SystemExit(core.run_keep_worker())
    raise SystemExit(main())
