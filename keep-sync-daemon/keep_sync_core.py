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

import faulthandler
import functools
import json
import logging
import logging.handlers
import os
import platform
import signal
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import gkeepapi
import gkeepapi.node
import gpsoauth

import sp_client

LOGGER_NAME = "keep_sync"

DEBUG_ENV = "KEEP_SYNC_DEBUG"
_LOG_FORMAT = "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"
_fault_log_handle = None  # kept open for the process lifetime; faulthandler writes here


def _env_flag(name: str) -> bool:
    """True unless the env var is unset or an obvious "off" value."""
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no", "off")


def _first_writable_dir(candidates: "list[Path]") -> Path:
    """First candidate we can create and write a probe file into. A frozen
    tray app dropped in a read-only location (/opt, Program Files) can't
    write next to its own binary — falling back keeps a log instead of the
    logging setup itself becoming the unlogged crash."""
    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".keep_sync_write_test"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return d
        except OSError:
            continue
    return Path(tempfile.gettempdir())


def _flush_logger(logger: logging.Logger) -> None:
    for h in logger.handlers:
        try:
            h.flush()
        except Exception:
            pass


def _enable_faulthandler(path: Path, logger: logging.Logger) -> None:
    """Dump a native traceback (segfault, SIGABRT, C-level crash in Qt/Tk)
    to `path` — the one class of crash that never reaches sys.excepthook,
    so otherwise nothing is written anywhere. On POSIX, SIGUSR1 also
    triggers an on-demand all-threads dump (`kill -USR1 <pid>`)."""
    global _fault_log_handle
    try:
        _fault_log_handle = open(path, "a", buffering=1, encoding="utf-8")
        _fault_log_handle.write(
            f"\n=== faulthandler armed {datetime.now(timezone.utc).isoformat()} "
            f"pid={os.getpid()} ===\n"
        )
        faulthandler.enable(file=_fault_log_handle, all_threads=True)
        if hasattr(faulthandler, "register") and hasattr(signal, "SIGUSR1"):
            faulthandler.register(
                signal.SIGUSR1, file=_fault_log_handle, all_threads=True, chain=False
            )
    except (OSError, RuntimeError, ValueError):
        logger.warning("could not enable faulthandler", exc_info=True)


def setup_logging(log_path: Path, relaunch_on_crash: bool = False) -> logging.Logger:
    """Configures the shared "keep_sync" logger and the process-wide crash
    hooks. The tray apps are --windowed builds with no console, so the log
    file (and the sibling .fault.log) is the only place any error is
    visible.

    Writes to `log_path` (10MB x 3 rotating, 40MB ceiling) if its directory
    is writable, else falls back to <DEFAULT_STATE_DIR> then the temp dir;
    the resolved path is `logger.log_path`. Also adds a stderr handler (for
    terminal runs), arms faulthandler for native crashes, and installs
    sys.excepthook / threading.excepthook so uncaught Python exceptions are
    logged with a traceback instead of vanishing. Set KEEP_SYNC_DEBUG=1 for
    DEBUG-level output.

    relaunch_on_crash: when set (the tray apps do), a crash that reaches
    sys.excepthook — e.g. one Qt routed out of a slot rather than letting
    propagate to __main__ — re-execs the process via relaunch_after_crash()
    before the default handler runs. The CLI daemon leaves this off; it's
    meant to be supervised by cron/systemd.

    Idempotent: a second call only re-applies the log level."""
    import threading

    log_path = Path(log_path)
    logger = logging.getLogger(LOGGER_NAME)
    debug = _env_flag(DEBUG_ENV)
    logger.setLevel(logging.DEBUG if debug else logging.INFO)

    if getattr(logger, "_keep_sync_configured", False):
        return logger

    log_dir = _first_writable_dir([
        log_path.parent,
        Path(os.path.expanduser(DEFAULT_STATE_DIR)),
        Path(tempfile.gettempdir()),
    ])
    resolved = log_dir / log_path.name
    fmt = logging.Formatter(_LOG_FORMAT)

    try:
        file_handler = logging.handlers.RotatingFileHandler(
            resolved, maxBytes=10_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except OSError:
        pass  # stderr handler below is still better than nothing

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    logger.log_path = resolved
    _enable_faulthandler(resolved.with_suffix(".fault.log"), logger)

    def log_uncaught(exc_type, exc_value, exc_tb):
        logger.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
        _flush_logger(logger)
        if relaunch_on_crash and not issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            relaunch_after_crash(logger)  # no return unless the crash-loop cap is hit
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    def log_uncaught_thread(args: "threading.ExceptHookArgs"):
        logger.error(
            "Uncaught exception in thread %r", args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        _flush_logger(logger)

    sys.excepthook = log_uncaught
    threading.excepthook = log_uncaught_thread

    logger._keep_sync_configured = True
    logger.info(
        "logging up: pid=%s python=%s platform=%s frozen=%s debug=%s log=%s",
        os.getpid(), platform.python_version(), platform.platform(),
        getattr(sys, "frozen", False), debug, resolved,
    )
    if resolved != log_path:
        logger.warning("wanted log at %s but it wasn't writable; using %s", log_path, resolved)
    return logger


def install_qt_message_handler(logger: "logging.Logger | None" = None) -> bool:
    """Route Qt's own runtime messages — QObject warnings, tray/platform
    plugin errors, "cannot create children for a parent in a different
    thread" and friends — into the log. Without this they print to stderr,
    which a --windowed build discards. No-op (returns False) if PySide6
    isn't importable."""
    logger = logger or logging.getLogger(LOGGER_NAME)
    try:
        from PySide6 import QtCore
    except Exception:
        return False

    level_for = {
        QtCore.QtMsgType.QtDebugMsg: logging.DEBUG,
        QtCore.QtMsgType.QtInfoMsg: logging.INFO,
        QtCore.QtMsgType.QtWarningMsg: logging.WARNING,
        QtCore.QtMsgType.QtCriticalMsg: logging.ERROR,
        QtCore.QtMsgType.QtFatalMsg: logging.CRITICAL,
    }

    def handler(msg_type, context, message):
        where = ""
        if context is not None and getattr(context, "file", None):
            where = f" ({context.file}:{context.line})"
        logger.log(level_for.get(msg_type, logging.WARNING), "Qt: %s%s", message, where)

    QtCore.qInstallMessageHandler(handler)
    logger.debug("Qt message handler installed")
    return True


def log_callback_errors(label: str, reraise: bool = False):
    """Decorator for GUI slot / tray-menu callback bodies. An exception
    raised inside a Qt slot or a pystray menu handler otherwise either
    takes the whole tray process down or is silently swallowed by the
    toolkit — either way with nothing written. This logs it with a
    traceback first (and by default swallows it so the tray survives)."""
    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception:
                logging.getLogger(LOGGER_NAME).exception("error in %s", label)
                if reraise:
                    raise
        return wrapper
    return decorate


# --- crash auto-relaunch -----------------------------------------------------
#
# The tray apps are long-running --windowed processes with no supervisor, so
# an unhandled crash just leaves the user with no tray icon and no sync. The
# entry points call relaunch_after_crash() to re-exec themselves, capped so a
# hard crash loop eventually stops instead of spinning forever.

CRASH_RELAUNCH_WINDOW_SEC = 60
CRASH_RELAUNCH_MAX = 3
_CRASH_WINDOW_START_ENV = "KEEP_SYNC_CRASH_WINDOW_START"
_CRASH_COUNT_ENV = "KEEP_SYNC_CRASH_COUNT"


def _crash_relaunch_decision(env: dict, now: float) -> "tuple[bool, dict]":
    """Pure policy behind relaunch_after_crash(): given the crash-tracking env
    vars carried across re-execs and the current time, return
    (should_relaunch, env_updates). Allows up to CRASH_RELAUNCH_MAX restarts
    inside a rolling CRASH_RELAUNCH_WINDOW_SEC window; a crash more than that
    window after the first one starts a fresh window."""
    try:
        start = float(env.get(_CRASH_WINDOW_START_ENV, now))
    except ValueError:
        start = now
    try:
        count = int(env.get(_CRASH_COUNT_ENV, "0") or "0")
    except ValueError:
        count = 0
    if now - start > CRASH_RELAUNCH_WINDOW_SEC or count < 0:
        start, count = now, 0
    if count >= CRASH_RELAUNCH_MAX:
        return False, {}
    return True, {_CRASH_WINDOW_START_ENV: repr(start), _CRASH_COUNT_ENV: str(count + 1)}


def relaunch_after_crash(logger) -> bool:
    """Restart this process (frozen exe or `python foo.py` run) after an
    unhandled crash. On POSIX this re-execs and never returns on success; on
    Windows, where exec* semantics are spawn-then-exit, it launches a detached
    replacement with subprocess.Popen and hard-exits the current process.
    Returns False without doing anything once the crash-loop cap is hit, so
    the caller can surface the failure and exit."""
    import subprocess
    import sys
    import time

    relaunch, env_updates = _crash_relaunch_decision(dict(os.environ), time.time())
    if not relaunch:
        logger.critical(
            "crashed %d times within %ds -- not relaunching again so the failure stays visible",
            CRASH_RELAUNCH_MAX, CRASH_RELAUNCH_WINDOW_SEC,
        )
        return False

    if getattr(sys, "frozen", False):
        argv = [sys.executable, *sys.argv[1:]]
    else:
        argv = [sys.executable, os.path.abspath(sys.argv[0]), *sys.argv[1:]]

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    child_env = {**os.environ, **env_updates}
    logger.critical("relaunching after crash: %s", " ".join(argv))

    if os.name == "nt":
        # os.execve on Windows spawns a new process and lets this one fall
        # through, so control returns to whatever launched us before the
        # replacement is up. Spawn a detached child and exit hard instead.
        DETACHED_PROCESS = 0x00000008
        try:
            subprocess.Popen(
                argv,
                env=child_env,
                close_fds=True,
                creationflags=DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        except OSError:
            logger.critical("crash relaunch failed to spawn", exc_info=True)
            return False
        os._exit(1)

    try:
        os.execve(argv[0], argv, child_env)
    except OSError:
        logger.critical("crash relaunch failed to exec", exc_info=True)
        return False

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
    log.debug(
        "note %s (%r): %d Keep items, %d SP tasks, %d mapped",
        note.id, note_title, len(keep_items), len(sp_tasks), len(note_map),
    )

    # 1. Keep -> SP: update mapped tasks, create tasks for new items.
    for item in note.items:
        if not (item.text or "").strip():
            # Blank Keep checklist line (a leftover empty row). SP's REST
            # API rejects an empty task title, so there's nothing to
            # create or push -- skip it until it has real text.
            log.debug("skipping blank Keep item %s", item.id)
            continue
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
        log.info("Keep authenticated and synced (cache_hit=%s)", bool(cached_state))
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
        log.info("pushing %d new + %d changed Keep item(s) back to Google",
                 result.created_keep, result.updated_keep)
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
