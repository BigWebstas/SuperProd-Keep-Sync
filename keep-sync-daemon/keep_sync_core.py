#!/usr/bin/env python3
"""
Shared logic behind keep_sync_daemon.py (CLI, run via cron/systemd/Task
Scheduler), keep_sync_tray.py (Windows tray app), and keep_sync_tray_qt.py
(Linux/KDE tray app). Not meant to be run directly.

Each sync pass authenticates to Google Keep, then reconciles one Keep
checklist against one Super Productivity project through SP's Local REST
API (see sp_client.py) -- there is no longer a state.json file bridge or
an sp-plugin. SP itself turns the resulting addTask/updateTask calls into
sync operations and pushes them to whatever backend (Super Sync, Dropbox,
WebDAV, ...) the user has configured. See reconcile_sp().

What syncs, unchanged from the old design: checking off or renaming an
item syncs in whichever direction it changed; a new Keep item creates an
SP task; a new top-level SP task creates a Keep item; deletes never
propagate either way.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import gkeepapi
import gkeepapi.node
import gpsoauth

import sp_client

LOGGER_NAME = "keep_sync"


def setup_logging(log_path: Path) -> logging.Logger:
    """Configures the shared "keep_sync" logger to write to log_path (a
    10MB x 3 rotating file, 40MB ceiling) and installs sys.excepthook /
    threading.excepthook so uncaught exceptions get logged instead of
    vanishing — the tray apps are --windowed builds with no console, so a
    log file is the only place errors are visible at all."""
    import sys
    import threading

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=10_000_000, backupCount=3, encoding="utf-8")
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
DEFAULT_SP_API_BASE_URL = sp_client.DEFAULT_BASE_URL


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
    cfg.setdefault("sp_api_base_url", DEFAULT_SP_API_BASE_URL)
    cfg.setdefault("sp_access_token", "")
    cfg.setdefault("sp_project_id", "")
    cfg.setdefault("keep_note_title", "")
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


def load_item_map(item_map_path: Path) -> dict:
    """The Keep<->SP mapping this module owns now that there's no plugin to
    keep it in SP's synced data. Shape: {noteId: {itemId: {taskId, text,
    checked}}} -- text/checked are the last values we reconciled, so the
    next pass can tell which side changed."""
    if not item_map_path.exists():
        return {}
    try:
        with item_map_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_item_map(item_map_path: Path, item_map: dict) -> None:
    atomic_write_json(item_map_path, item_map, mode=0o600)


def find_keep_list(keep: "gkeepapi.Keep", title: str, include_archived: bool):
    """The one checklist note this sync targets, matched by title (same as
    the old plugin did). Trashed notes are always skipped; archived ones
    unless include_archived."""
    for note in keep.all():
        if not isinstance(note, gkeepapi.node.List):
            continue
        if note.trashed:
            continue
        if note.archived and not include_archived:
            continue
        if note.title == title:
            return note
    return None


def list_keep_checklist_titles(
    email: str, master_token: str, state_dir: Path, include_archived: bool = False
) -> list[str]:
    """One Keep pull, returning the titles of every checklist note -- used
    by the tray setup dialogs to populate the "Keep list" dropdown."""
    keep = gkeepapi.Keep()
    cache_path = state_dir / "google_sync_cache.json"
    cached = load_google_state_cache(cache_path)
    if cached:
        keep.authenticate(email, master_token, state=cached)
    else:
        keep.authenticate(email, master_token)
    keep.sync()
    titles = []
    for note in keep.all():
        if not isinstance(note, gkeepapi.node.List):
            continue
        if note.trashed:
            continue
        if note.archived and not include_archived:
            continue
        if note.title:
            titles.append(note.title)
    try:
        atomic_write_json(cache_path, keep.dump())
    except OSError:
        pass
    return sorted(set(titles), key=str.casefold)


def list_sp_projects(base_url: str, token: str) -> list[tuple[str, str]]:
    """(id, title) for every non-archived SP project -- used by the tray
    setup dialogs to populate the "Super Productivity project" dropdown."""
    sp = sp_client.SPClient(base_url or DEFAULT_SP_API_BASE_URL, token)
    return [(p.id, p.title) for p in sp.list_projects() if not p.is_archived]


@dataclass
class ReconcileResult:
    created_sp: int = 0
    updated_sp: int = 0
    created_keep: int = 0
    updated_keep: int = 0
    keep_dirty: bool = False
    note_id: str = ""
    # note_map keys added by step 3 this pass -- caller drops these from the
    # persisted map if the follow-up keep.sync() push fails, so the Keep
    # item (which was never actually created server-side) is retried next
    # pass instead of being recorded as done.
    new_keep_item_ids: list = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.created_sp + self.updated_sp + self.created_keep + self.updated_keep


def reconcile_sp(cfg: dict, keep: "gkeepapi.Keep", sp: "sp_client.SPClient", item_map: dict) -> ReconcileResult:
    """Reconciles one Keep checklist against one SP project through the
    Local REST API. Mutates `keep`'s in-memory graph (caller pushes with
    keep.sync() if keep_dirty) and `item_map` in place (caller persists).

    Rules, matching the old plugin/daemon behavior:
      - Keep item mapped to a task     -> push text/checked changes to SP
      - Keep item not yet mapped       -> create the SP task
      - mapped task changed vs. last   -> push text/checked back to Keep
      - top-level SP task not mapped   -> create the Keep item
      - either side deleted            -> leave the counterpart alone
    On a text/checked conflict Keep wins (its value is applied to SP first,
    then the refreshed task no longer looks changed on the way back)."""
    log = logging.getLogger(LOGGER_NAME)
    note_title = cfg.get("keep_note_title") or ""
    project_id = cfg.get("sp_project_id") or ""

    note = find_keep_list(keep, note_title, cfg.get("include_archived", False))
    if note is None:
        raise LookupError(
            f'Keep list "{note_title}" not found (trashed, or archived without include_archived).'
        )

    note_map = item_map.setdefault(note.id, {})
    res = ReconcileResult(note_id=note.id)

    sp_tasks = {t.id: t for t in sp.list_tasks(project_id)}
    keep_items = {item.id: item for item in note.items}

    # 1. Keep -> SP: update mapped tasks, create tasks for new items.
    for item in note.items:
        entry = note_map.get(item.id)
        if entry and entry.get("taskId"):
            task = sp_tasks.get(entry["taskId"])
            patch = {}
            if entry.get("text") != item.text:
                patch["title"] = item.text
            if bool(entry.get("checked")) != bool(item.checked):
                patch["isDone"] = bool(item.checked)
            if patch:
                if task is None:
                    # Mapped task is gone from SP (deleted/archived) -- deletes
                    # don't propagate, so just resync our snapshot and move on.
                    log.info("mapped task %s no longer in SP project; skipping", entry["taskId"])
                else:
                    sp.update_task(entry["taskId"], patch)
                    res.updated_sp += 1
                    if "title" in patch:
                        task.title = patch["title"]
                    if "isDone" in patch:
                        task.is_done = patch["isDone"]
            entry["text"] = item.text
            entry["checked"] = bool(item.checked)
        else:
            task_id = sp.add_task(item.text, project_id, bool(item.checked))
            note_map[item.id] = {"taskId": task_id, "text": item.text, "checked": bool(item.checked)}
            res.created_sp += 1

    # 2. SP -> Keep: mapped task changed independently of Keep.
    for item_id, entry in list(note_map.items()):
        task_id = entry.get("taskId")
        if not task_id:
            continue
        task = sp_tasks.get(task_id)
        keep_item = keep_items.get(item_id)
        if task is None or keep_item is None:
            continue  # deleted on one side -- don't propagate
        changed = False
        if task.title != entry.get("text"):
            keep_item.text = task.title
            entry["text"] = task.title
            changed = True
        if bool(task.is_done) != bool(entry.get("checked")):
            keep_item.checked = bool(task.is_done)
            entry["checked"] = bool(task.is_done)
            changed = True
        if changed:
            res.updated_keep += 1
            res.keep_dirty = True

    # 3. New top-level SP tasks -> new Keep items. Subtasks are skipped:
    #    Keep has no subtask concept and the Keep -> SP direction only ever
    #    creates flat items too (see the top-level README).
    mapped_task_ids = {e.get("taskId") for e in note_map.values() if e.get("taskId")}
    for task in sp_tasks.values():
        if task.parent_id or task.id in mapped_task_ids:
            continue
        new_item = note.add(task.title, bool(task.is_done))
        note_map[new_item.id] = {
            "taskId": task.id,
            "text": task.title,
            "checked": bool(task.is_done),
        }
        res.new_keep_item_ids.append(new_item.id)
        res.created_keep += 1
        res.keep_dirty = True

    log.info(
        "reconciled %r: SP +%d/~%d, Keep +%d/~%d",
        note_title, res.created_sp, res.updated_sp, res.created_keep, res.updated_keep,
    )
    return res


@dataclass
class SyncResult:
    ok: bool
    message: str
    notes_count: int = 0  # kept name for the tray apps; now = number of items changed


def sync_once(cfg: dict) -> SyncResult:
    """Runs one Keep <-> Super Productivity reconcile pass. Never raises:
    on any failure both sides are left untouched, so a transient Keep
    login error or SP being closed never corrupts either side."""
    log = logging.getLogger(LOGGER_NAME)
    log.info("sync starting for %s", cfg.get("email"))

    state_dir = resolve_state_dir(cfg["state_dir"])
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(state_dir, stat.S_IRWXU)
    except OSError:
        pass

    google_cache_path = state_dir / "google_sync_cache.json"
    item_map_path = state_dir / "item_map.json"
    debug_path = state_dir / "last_sync.json"

    for field in ("sp_access_token", "sp_project_id", "keep_note_title"):
        if not cfg.get(field):
            return SyncResult(False, f"not configured yet: missing {field} (open Reconfigure…)")

    try:
        master_token = get_master_token(state_dir)
    except RuntimeError as e:
        log.warning("sync aborted: %s", e)
        return SyncResult(False, str(e))

    sp = sp_client.SPClient(
        cfg.get("sp_api_base_url", DEFAULT_SP_API_BASE_URL),
        cfg.get("sp_access_token", ""),
    )

    keep = gkeepapi.Keep()
    cached_state = load_google_state_cache(google_cache_path)
    item_map = load_item_map(item_map_path)

    try:
        if cached_state:
            keep.authenticate(cfg["email"], master_token, state=cached_state)
        else:
            keep.authenticate(cfg["email"], master_token)
        keep.sync()
    except gkeepapi.exception.LoginException as e:
        log.error("Google login failed", exc_info=True)
        return SyncResult(
            False,
            f"Google login failed ({e}). The master token may be stale/revoked, "
            "or Google is challenging this login. Nothing changed.",
        )
    except Exception as e:  # network errors, etc.
        log.error("Keep sync failed", exc_info=True)
        return SyncResult(False, f"Keep sync failed: {e}. Nothing changed.")

    try:
        result = reconcile_sp(cfg, keep, sp, item_map)
    except LookupError as e:
        log.warning("reconcile aborted: %s", e)
        return SyncResult(False, str(e))
    except sp_client.SPError as e:
        log.warning("reconcile aborted: %s", e)
        return SyncResult(False, str(e))
    except Exception as e:
        log.error("reconcile failed", exc_info=True)
        return SyncResult(False, f"sync failed: {e}. Nothing changed on Keep's side.")

    push_error = None
    if result.keep_dirty:
        try:
            keep.sync()
        except Exception as e:
            log.error("failed pushing SP edits to Keep", exc_info=True)
            push_error = e

    # Persist bookkeeping either way: the SP task ids in item_map are already
    # committed, so keeping them prevents duplicate tasks next pass. But if
    # the Keep push failed, drop the items step 3 just added -- they don't
    # exist server-side yet, so they must be retried, not recorded as done.
    if push_error is not None and result.new_keep_item_ids:
        note_map = item_map.get(result.note_id, {})
        for item_id in result.new_keep_item_ids:
            note_map.pop(item_id, None)

    try:
        atomic_write_json(google_cache_path, keep.dump())
    except OSError:
        pass  # non-fatal: next login just won't reuse Google's sync cursor

    try:
        save_item_map(item_map_path, item_map)
    except OSError as e:
        log.error("failed to write %s", item_map_path, exc_info=True)
        return SyncResult(False, f"failed to write {item_map_path}: {e}")

    if push_error is not None:
        return SyncResult(
            False,
            f"SP tasks updated, but pushing item edits back to Keep failed: {push_error}",
        )

    try:
        note = find_keep_list(keep, cfg.get("keep_note_title", ""), cfg.get("include_archived", False))
        atomic_write_json(
            debug_path,
            {
                "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "noteTitle": cfg.get("keep_note_title"),
                "projectId": cfg.get("sp_project_id"),
                "keepItems": [
                    {"id": it.id, "text": it.text, "checked": bool(it.checked)}
                    for it in (note.items if note else [])
                ],
                "result": {
                    "createdSp": result.created_sp,
                    "updatedSp": result.updated_sp,
                    "createdKeep": result.created_keep,
                    "updatedKeep": result.updated_keep,
                },
            },
            mode=0o600,
        )
    except OSError:
        pass  # debug dump only

    msg = (
        f"SP: {result.created_sp} created, {result.updated_sp} updated; "
        f"Keep: {result.created_keep} created, {result.updated_keep} updated"
    )
    log.info("sync OK: %s", msg)
    return SyncResult(True, msg, result.total)
