#!/usr/bin/env python3
"""
Shared logic behind both keep_sync_daemon.py (CLI, run via cron/systemd/Task
Scheduler) and keep_sync_tray.py (Windows tray app with its own scheduler).
Not meant to be run directly.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import gkeepapi
import gkeepapi.node
import gpsoauth

DEFAULT_ANDROID_ID = "0000000000000000"
DEFAULT_STATE_DIR = "~/.sp-keep-sync"
DEFAULT_SYNC_INTERVAL_MINUTES = 5


def resolve_state_dir(raw: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(raw))).resolve()


def load_config(config_path: Path) -> dict:
    with config_path.open("r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    if "email" not in cfg:
        raise ValueError(f"{config_path}: missing required 'email' field")
    cfg.setdefault("state_dir", DEFAULT_STATE_DIR)
    cfg.setdefault("include_archived", False)
    cfg.setdefault("sync_interval_minutes", DEFAULT_SYNC_INTERVAL_MINUTES)
    return cfg


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


def save_config(config_path: Path, cfg: dict) -> None:
    atomic_write_json(config_path, cfg, mode=0o600)


def get_master_token(state_dir: Path) -> str:
    token = os.environ.get("KEEP_MASTER_TOKEN")
    if token:
        return token.strip()
    token_file = state_dir / "master_token"
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    raise RuntimeError(
        "No master token found. Set KEEP_MASTER_TOKEN or provide one via "
        f"{token_file} (see README.md)."
    )


def exchange_master_token(email: str, oauth_token: str, android_id: str = DEFAULT_ANDROID_ID) -> str:
    """Exchanges a Google embedded-sign-in OAuth Token for a long-lived master
    token, the same call gkeepapi's own docs point to."""
    result = gpsoauth.exchange_token(email, oauth_token, android_id)
    if "Token" not in result:
        raise RuntimeError(f"Google rejected the token exchange: {result}")
    return result["Token"]


def save_master_token(state_dir: Path, master_token: str) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(state_dir, stat.S_IRWXU)
    except OSError:
        pass
    token_path = state_dir / "master_token"
    token_path.write_text(master_token, encoding="utf-8")
    try:
        os.chmod(token_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return token_path


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


@dataclass
class SyncResult:
    ok: bool
    message: str
    notes_count: int = 0


def sync_once(cfg: dict) -> SyncResult:
    """Runs one Keep -> state.json sync pass. Never raises: on any failure
    the previous state.json is left untouched, matching the CLI daemon's
    contract, so a transient Keep/login error never blanks out the plugin's
    view."""
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
        return SyncResult(False, str(e))

    keep = gkeepapi.Keep()
    cached_state = load_google_state_cache(google_cache_path)

    try:
        if cached_state:
            keep.authenticate(cfg["email"], master_token, state=cached_state)
        else:
            keep.authenticate(cfg["email"], master_token)
        keep.sync()
    except gkeepapi.exception.LoginException as e:
        return SyncResult(
            False,
            f"Google login failed ({e}). The master token may be stale/revoked, "
            "or Google is challenging this login. Leaving previous state.json untouched.",
        )
    except Exception as e:  # network errors, etc.
        return SyncResult(False, f"sync failed: {e}. Leaving previous state.json untouched.")

    try:
        atomic_write_json(google_cache_path, keep.dump())
    except OSError:
        pass  # non-fatal: next login just won't reuse Google's sync cursor

    notes = collect_checklists(keep, cfg.get("include_archived", False))
    output = {
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "notes": notes,
    }

    try:
        atomic_write_json(output_path, output, mode=0o644)
    except OSError as e:
        return SyncResult(False, f"failed to write {output_path}: {e}")

    return SyncResult(True, f"wrote {len(notes)} checklist(s) to {output_path}", len(notes))
