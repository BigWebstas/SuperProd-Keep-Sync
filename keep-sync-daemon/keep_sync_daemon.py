#!/usr/bin/env python3
"""
Reads Google Keep checklist notes via gkeepapi and writes their current
state (title + items + checked flags) to a local JSON file. Intended to
run on a schedule (cron/systemd timer/Task Scheduler) — see README.md.

The Super Productivity plugin (../sp-plugin) reads this file through its
own Node.js execution sandbox; this script never talks to Super
Productivity directly.

On any failure, the previously written state file is left untouched so a
transient Keep/login error never blanks out the plugin's view.
"""
import argparse
import json
import os
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import gkeepapi
import gkeepapi.node


def resolve_state_dir(raw: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(raw))).resolve()


def load_config(config_path: Path) -> dict:
    with config_path.open("r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    if "email" not in cfg:
        raise ValueError(f"{config_path}: missing required 'email' field")
    cfg.setdefault("state_dir", "~/.sp-keep-sync")
    cfg.setdefault("include_archived", False)
    return cfg


def get_master_token(state_dir: Path) -> str:
    token = os.environ.get("KEEP_MASTER_TOKEN")
    if token:
        return token.strip()
    token_file = state_dir / "master_token"
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    raise RuntimeError(
        "No master token found. Set KEEP_MASTER_TOKEN or create "
        f"{token_file} (see README.md / get_master_token.py)."
    )


def atomic_write_json(path: Path, data: dict, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_google_state_cache(cache_path: Path):
    if not cache_path.exists():
        return None
    try:
        with cache_path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None


def collect_checklists(keep: "gkeepapi.Keep", include_archived: bool) -> list:
    notes = []
    for note in keep.all():
        if not isinstance(note, gkeepapi.node.List):
            continue
        if note.trashed:
            continue
        if note.archived and not include_archived:
            continue
        items = [
            {"id": item.id, "text": item.text, "checked": bool(item.checked)}
            for item in note.items
        ]
        notes.append({"id": note.id, "title": note.title, "items": items})
    return notes


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
        cfg = load_config(config_path)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"[keep-sync-daemon] config error: {e}", file=sys.stderr)
        return 2

    state_dir = resolve_state_dir(cfg["state_dir"])
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(state_dir, stat.S_IRWXU)
    except OSError:
        pass

    google_cache_path = state_dir / "google_sync_cache.json"
    output_path = state_dir / "state.json"

    try:
        master_token = get_master_token(state_dir)
    except RuntimeError as e:
        print(f"[keep-sync-daemon] {e}", file=sys.stderr)
        return 2

    keep = gkeepapi.Keep()
    cached_state = load_google_state_cache(google_cache_path)

    try:
        if cached_state:
            keep.authenticate(cfg["email"], master_token, state=cached_state)
        else:
            keep.authenticate(cfg["email"], master_token)
        keep.sync()
    except gkeepapi.exception.LoginException as e:
        print(
            f"[keep-sync-daemon] Google login failed ({e}). "
            "The master token may be stale/revoked, or Google is challenging "
            "this login — see README troubleshooting. Leaving previous "
            "state.json untouched.",
            file=sys.stderr,
        )
        return 1
    except Exception as e:  # network errors, etc.
        print(
            f"[keep-sync-daemon] sync failed: {e}. Leaving previous state.json untouched.",
            file=sys.stderr,
        )
        return 1

    try:
        atomic_write_json(google_cache_path, keep.dump())
    except OSError as e:
        print(f"[keep-sync-daemon] warning: failed to write auth cache: {e}", file=sys.stderr)

    notes = collect_checklists(keep, cfg["include_archived"])
    output = {
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "notes": notes,
    }

    try:
        atomic_write_json(output_path, output, mode=0o644)
    except OSError as e:
        print(f"[keep-sync-daemon] failed to write {output_path}: {e}", file=sys.stderr)
        return 1

    print(f"[keep-sync-daemon] wrote {len(notes)} checklist(s) to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
