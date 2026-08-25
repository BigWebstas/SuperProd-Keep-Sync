#!/usr/bin/env python3
"""
Shared logic behind keep_sync_daemon.py (CLI, run via cron/systemd/Task
Scheduler), keep_sync_tray.py (Windows tray app), and keep_sync_tray_qt.py
(Linux/KDE tray app). Not meant to be run directly.

Each sync pass is mostly Keep -> state.json, but also pushes queued
Super Productivity edits back to Keep first: the sp-plugin writes item
checked/text changes it observes to <state_dir>/pending_changes.json,
and sync_once() applies + pushes those before re-reading Keep's now
up-to-date state. See apply_pending_changes().
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import gkeepapi
import gkeepapi.node
import gpsoauth

LOGGER_NAME = "keep_sync"


def setup_logging(log_path: Path) -> logging.Logger:
    """Configures the shared "keep_sync" logger to write to log_path (a
    1MB x 3 rotating file) and installs sys.excepthook /
    threading.excepthook so uncaught exceptions get logged instead of
    vanishing — the tray apps are --windowed builds with no console, so a
    log file is the only place errors are visible at all."""
    import sys
    import threading

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s"))
    logger.addHandler(handler)

    def log_uncaught(exc_type, exc_value, exc_tb):
        logger.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    def log_uncaught_thread(args: "threading.ExceptHookArgs"):
        logger.error(
            "Uncaught exception in thread %r", args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = log_uncaught
    threading.excepthook = log_uncaught_thread
    return logger

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


def load_pending_changes(pending_path: Path) -> dict:
    if not pending_path.exists():
        return {}
    try:
        with pending_path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}


def apply_pending_changes(keep: "gkeepapi.Keep", pending: dict) -> int:
    """Applies queued Super Productivity -> Keep edits (checked/text,
    written by the sp-plugin's TASK_UPDATE hook) to the in-memory Keep
    graph. Returns how many item changes were applied; a note or item
    that's gone missing since it was queued (e.g. trashed) is skipped,
    not fatal. Caller must still call keep.sync() to push the result."""
    applied = 0
    for note_id, items in pending.items():
        note = keep.get(note_id)
        if note is None or not isinstance(note, gkeepapi.node.List):
            continue
        by_id = {item.id: item for item in note.items}
        for item_id, change in items.items():
            item = by_id.get(item_id)
            if item is None:
                continue
            if change.get("text") is not None:
                item.text = change["text"]
            if change.get("checked") is not None:
                item.checked = bool(change["checked"])
            applied += 1
    return applied


def load_pending_creates(pending_creates_path: Path) -> dict:
    if not pending_creates_path.exists():
        return {}
    try:
        with pending_creates_path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}


def apply_pending_creates(keep: "gkeepapi.Keep", pending_creates: dict) -> dict:
    """Creates new Keep list items queued by the sp-plugin for tasks that
    were created directly in Super Productivity (no matching Keep item).
    pending_creates is keyed by note id, then by an opaque "task id" the
    plugin supplied purely so it can map the result back to the SP task
    that caused it — this module never interprets it. Returns
    {task_id: {"noteId": ..., "itemId": ...}} for each item actually
    created; a note that's gone missing since queued is skipped, not
    fatal. Caller must still call keep.sync() to push the result."""
    created = {}
    for note_id, tasks in pending_creates.items():
        note = keep.get(note_id)
        if note is None or not isinstance(note, gkeepapi.node.List):
            continue
        for task_id, data in tasks.items():
            item = note.add(data.get("text") or "", bool(data.get("checked")))
            created[task_id] = {"noteId": note_id, "itemId": item.id}
    return created


def save_created_items(created_items_path: Path, new_entries: dict) -> None:
    """Merge-writes newly created item mappings so a previous, not-yet
    consumed by the plugin batch never gets clobbered."""
    existing = {}
    if created_items_path.exists():
        try:
            with created_items_path.open("r", encoding="utf-8") as fh:
                existing = json.load(fh)
        except (json.JSONDecodeError, OSError):
            existing = {}
    existing.update(new_entries)
    atomic_write_json(created_items_path, existing, mode=0o644)


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
    log = logging.getLogger(LOGGER_NAME)
    log.info("sync starting for %s", cfg.get("email"))

    state_dir = resolve_state_dir(cfg["state_dir"])
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(state_dir, stat.S_IRWXU)
    except OSError:
        pass

    google_cache_path = state_dir / "google_sync_cache.json"
    output_path = state_dir / "state.json"
    pending_path = state_dir / "pending_changes.json"
    pending_creates_path = state_dir / "pending_creates.json"
    created_items_path = state_dir / "created_items.json"

    try:
        master_token = get_master_token(state_dir)
    except RuntimeError as e:
        log.warning("sync aborted: %s", e)
        return SyncResult(False, str(e))

    keep = gkeepapi.Keep()
    cached_state = load_google_state_cache(google_cache_path)

    try:
        if cached_state:
            keep.authenticate(cfg["email"], master_token, state=cached_state)
        else:
            keep.authenticate(cfg["email"], master_token)
        keep.sync()

        pending = load_pending_changes(pending_path)
        pending_creates = load_pending_creates(pending_creates_path)
        needs_push = False

        if pending:
            applied = apply_pending_changes(keep, pending)
            log.info("applied %d pending Super Productivity edit(s)", applied)
            needs_push = needs_push or bool(applied)

        created_items = {}
        if pending_creates:
            created_items = apply_pending_creates(keep, pending_creates)
            log.info("created %d new Keep item(s) from Super Productivity tasks", len(created_items))
            needs_push = needs_push or bool(created_items)

        if needs_push:
            keep.sync()  # push the queued Super Productivity edits/creates to Google

        if pending:
            try:
                pending_path.unlink()
            except OSError:
                pass
        if pending_creates:
            try:
                pending_creates_path.unlink()
            except OSError:
                pass
        if created_items:
            save_created_items(created_items_path, created_items)
    except gkeepapi.exception.LoginException as e:
        log.error("Google login failed", exc_info=True)
        return SyncResult(
            False,
            f"Google login failed ({e}). The master token may be stale/revoked, "
            "or Google is challenging this login. Leaving previous state.json untouched.",
        )
    except Exception as e:  # network errors, etc.
        log.error("sync failed", exc_info=True)
        return SyncResult(False, f"sync failed: {e}. Leaving previous state.json untouched.")

    try:
        atomic_write_json(google_cache_path, keep.dump())
    except OSError:
        pass  # non-fatal: next login just won't reuse Google's sync cursor

    notes = collect_checklists(keep, cfg.get("include_archived", False))
    log.info(
        "collected %d checklist note(s): %s",
        len(notes),
        ", ".join(f"{n['title']!r} ({len(n['items'])} item(s))" for n in notes) or "(none)",
    )
    output = {
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "notes": notes,
    }

    try:
        atomic_write_json(output_path, output, mode=0o644)
    except OSError as e:
        log.error("failed to write %s", output_path, exc_info=True)
        return SyncResult(False, f"failed to write {output_path}: {e}")

    log.info("sync OK: wrote %d checklist(s) to %s", len(notes), output_path)
    return SyncResult(True, f"wrote {len(notes)} checklist(s) to {output_path}", len(notes))
