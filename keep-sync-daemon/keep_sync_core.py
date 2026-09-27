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

import contextlib
import faulthandler
import functools
import gc
import json
import logging
import logging.handlers
import os
import platform
import re
import signal
import stat
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import gkeepapi
import gkeepapi.node
import gpsoauth
import requests

import sp_client

LOGGER_NAME = "keep_sync"

DEBUG_ENV = "KEEP_SYNC_DEBUG"
KEEP_WORKER_ENV = "KEEP_SYNC_WORKER"
KEEP_WORKER_REQUEST_ENV = "KEEP_SYNC_WORKER_REQUEST"
KEEP_WORKER_OUT_ENV = "KEEP_SYNC_WORKER_OUT"
_LOG_FORMAT = "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"
_fault_log_handle = None  # kept open for the process lifetime; faulthandler writes here


def get_version() -> str:
    """Best-effort app version string for the status UI and the log banner.

    A packaged build carries it in a generated `_keep_sync_version.py` (the build
    workflow writes `__version__` there from `git describe` before running
    PyInstaller). A plain source checkout derives it live from git. Neither
    available -> "unknown"."""
    try:
        import _keep_sync_version  # generated at build time; gitignored
        v = getattr(_keep_sync_version, "__version__", "").strip()
        if v:
            return v
    except Exception:
        pass
    if not getattr(sys, "frozen", False):
        try:
            import subprocess
            out = subprocess.run(
                ["git", "describe", "--tags", "--always", "--dirty"],
                cwd=os.path.dirname(os.path.abspath(__file__)),
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except Exception:
            pass
    return "unknown"


GITHUB_REPO = "BigWebstas/SuperProd-Keep-Sync"
RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"
_GITHUB_LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
# Google's embedded sign-in page -- where a user gets the OAuth Token setup
# needs (see SetupWindow/SetupDialog's "Open sign-in page" button).
GOOGLE_EMBEDDED_SETUP_URL = "https://accounts.google.com/EmbeddedSetup"
_VERSION_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
UPDATE_CHECK_MIN_INTERVAL_HOURS = 24.0
# Filename prefix the "Build tray app" workflow's installer step gives the
# Inno Setup output (installer/KeepSyncTray.iss's OutputBaseFilename) --
# how _pick_update_asset() recognizes it in a release's asset list. Windows
# only: there's no installer for the Linux binary, so that platform's
# UpdateInfo.asset_url is always None and the tray falls back to a link.
WINDOWS_INSTALLER_ASSET_PREFIX = "KeepSyncTray-Setup-"


def _version_tuple(v: str) -> "tuple[int, int, int] | None":
    m = _VERSION_RE.match(v or "")
    return tuple(int(g) for g in m.groups()) if m else None


@dataclass
class UpdateInfo:
    """A newer release than the one running. `asset_url`/`asset_name` are
    the direct download for this platform's installer, or both None when
    the release has nothing installable for this platform (Linux today)."""
    version: str
    url: str
    asset_url: "str | None" = None
    asset_name: "str | None" = None


def _pick_update_asset(release: dict) -> "tuple[str | None, str | None]":
    """(download_url, filename) for this platform's installer among a
    GitHub release's assets, or (None, None) if there isn't one."""
    if sys.platform != "win32":
        return None, None
    for a in release.get("assets") or []:
        name, url = a.get("name") or "", a.get("browser_download_url") or ""
        if name.startswith(WINDOWS_INSTALLER_ASSET_PREFIX) and url:
            return url, name
    return None, None


def check_for_update(current_version: "str | None" = None, timeout: float = 5.0) -> "UpdateInfo | None":
    """The latest GitHub release as an UpdateInfo if it's newer than
    `current_version` (default get_version()), else None -- including when
    GitHub is unreachable, rate-limits the request, or either version
    string isn't a clean `vX.Y.Z` release tag (a dev/dirty git-describe
    build skips the check rather than nagging on every commit). Never
    raises: this is a courtesy notice, not something that should ever break
    a sync pass or a tray startup."""
    current_version = get_version() if current_version is None else current_version
    current = _version_tuple(current_version)
    if current is None:
        return None
    try:
        resp = requests.get(
            _GITHUB_LATEST_RELEASE_API, timeout=timeout,
            headers={"Accept": "application/vnd.github+json"},
        )
        resp.raise_for_status()
        release = resp.json() or {}
    except Exception:
        return None
    tag = release.get("tag_name", "")
    latest = _version_tuple(tag)
    if latest is None or latest <= current:
        return None
    asset_url, asset_name = _pick_update_asset(release)
    return UpdateInfo(version=tag, url=release.get("html_url") or RELEASES_URL,
                       asset_url=asset_url, asset_name=asset_name)


def maybe_check_for_update(
    state_dir: Path, current_version: "str | None" = None,
    min_interval_hours: float = UPDATE_CHECK_MIN_INTERVAL_HOURS,
) -> "UpdateInfo | None":
    """check_for_update(), throttled to once per `min_interval_hours` via
    `<state_dir>/update_check.json`. The CLI daemon and both tray apps can
    share one state_dir and each calls this on its own schedule (a cron run,
    a multi-minute sync loop) -- without the cache that adds up to hammering
    GitHub's API. Returns the same as check_for_update(): a newer UpdateInfo,
    or None. Never raises."""
    cache_path = state_dir / "update_check.json"
    now = datetime.now(timezone.utc).timestamp()
    current_version = get_version() if current_version is None else current_version

    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        cached = None

    if isinstance(cached, dict) and now - float(cached.get("checked_at", 0) or 0) < min_interval_hours * 3600:
        info = cached.get("info")
        if not info:
            return None
        current, latest = _version_tuple(current_version), _version_tuple(info.get("version") or "")
        return UpdateInfo(**info) if (current and latest and latest > current) else None

    found = check_for_update(current_version)
    try:
        atomic_write_json(
            cache_path, {"checked_at": now, "info": asdict(found) if found else None}, mode=0o600,
        )
    except OSError:
        pass
    return found


def download_update_asset(
    info: UpdateInfo, dest_dir: Path, progress: "Callable[[int], None] | None" = None,
    timeout: float = 600.0,
) -> Path:
    """Downloads `info`'s installer to `dest_dir/info.asset_name`, calling
    `progress(percent)` (0-100) as bytes arrive -- never called if the
    server doesn't send Content-Length (progress is unknowable, not just
    slow). Unlike check_for_update(), this DOES raise on failure: a user
    explicitly clicked "download", so a bad network or a release with
    nothing installable for this platform should surface, not vanish."""
    if not info.asset_url or not info.asset_name:
        raise RuntimeError("No downloadable installer in this release for this platform.")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / info.asset_name
    with requests.get(info.asset_url, stream=True, timeout=timeout) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length") or 0)
        written, last_percent = 0, -1
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                if not chunk:
                    continue
                fh.write(chunk)
                written += len(chunk)
                if progress and total > 0:
                    percent = min(100, int(written * 100 / total))
                    if percent != last_percent:
                        last_percent = percent
                        progress(percent)
    return dest


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
    triggers an on-demand all-threads dump (`kill -USR1 <pid>`).

    Best-effort: anything going wrong here (a PyInstaller --windowed build
    with no usable stderr, a locked file) must not take the app down, so
    every failure is swallowed after a warning.

    On Windows faulthandler.enable() installs a *vectored* (first-chance)
    exception handler, so it writes "Windows fatal exception" dumps for
    exceptions that pystray/ctypes catch and handle a frame later
    (RPC_E_DISCONNECTED, a benign breakpoint) -- pure noise that reads as a
    fatal crash. So there we skip enable() and rely on sys.excepthook /
    threading.excepthook / the __main__ handler for Python-level crashes.
    On POSIX its SIGSEGV/SIGABRT handler is genuinely useful, so keep it."""
    global _fault_log_handle
    if os.name == "nt":
        logger.info("faulthandler exception handler left off on Windows (first-chance noise)")
        return
    try:
        # Truncate: the file is per-launch, and an append-only history just
        # confuses "paste the fault log" (stale runs on top).
        _fault_log_handle = open(path, "w", encoding="utf-8")
        _fault_log_handle.write(
            f"=== faulthandler armed {datetime.now(timezone.utc).isoformat()} "
            f"pid={os.getpid()} version={get_version()} ===\n"
        )
        _fault_log_handle.flush()
        faulthandler.enable(file=_fault_log_handle, all_threads=True)
        # Dump every thread's stack on a polite kill too (a session logout,
        # `systemctl --user stop`, a `killall`). These fire at the C level,
        # so unlike a Python signal handler they still run while Qt's C++
        # event loop has the main thread. chain=True lets the default action
        # (terminate) proceed afterwards. SIGKILL stays uncatchable.
        if hasattr(faulthandler, "register"):
            for signame in ("SIGTERM", "SIGHUP", "SIGUSR1"):
                sig = getattr(signal, signame, None)
                if sig is None:
                    continue
                faulthandler.register(
                    sig, file=_fault_log_handle, all_threads=True,
                    chain=(signame != "SIGUSR1"),
                )
        logger.info("faulthandler armed -> %s", path)
    except Exception:
        logger.error("could not enable faulthandler -- native crashes will be invisible", exc_info=True)


def setup_logging(log_path: Path, relaunch_on_crash: bool = False) -> logging.Logger:
    """Configures the shared "keep_sync" logger and the process-wide crash
    hooks. The tray apps are --windowed builds with no console, so the log
    file (and the sibling .fault.log) is the only place any error is
    visible.

    Writes to `log_path` (rotated daily at midnight, 7 days kept) if its
    directory is writable, else falls back to <DEFAULT_STATE_DIR> then the temp dir;
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

    Never raises: a --windowed PyInstaller build with no console gives a
    partial/None stdio, and a broken logging setup must not be what takes
    the app down. Anything past the rotating file handler is best-effort.

    Idempotent: a second call only re-applies the log level."""
    import threading

    log_path = Path(log_path)
    logger = logging.getLogger(LOGGER_NAME)
    debug = _env_flag(DEBUG_ENV)
    logger.setLevel(logging.DEBUG if debug else logging.INFO)

    if getattr(logger, "_keep_sync_configured", False):
        return logger
    logger._keep_sync_configured = True

    try:
        log_dir = _first_writable_dir([
            log_path.parent,
            Path(os.path.expanduser(DEFAULT_STATE_DIR)),
            Path(tempfile.gettempdir()),
        ])
    except Exception:
        log_dir = Path(tempfile.gettempdir())
    resolved = log_dir / log_path.name
    logger.log_path = resolved
    fmt = logging.Formatter(_LOG_FORMAT)

    try:
        # Daily rollover at midnight, seven days kept. On a short-lived cron
        # run the handler computes its next rollover from the file's mtime,
        # so a pass that starts after midnight still rotates on the first
        # write even though the process never spans the boundary itself.
        file_handler = logging.handlers.TimedRotatingFileHandler(
            resolved, when="midnight", backupCount=7, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except OSError:
        pass  # stderr handler below is still better than nothing

    # Only when there's a real stream to write to — a --windowed frozen
    # build has sys.stderr = None, and StreamHandler(None) just raises on
    # every emit. The Keep worker keeps stdout/stderr clean for its parent.
    if sys.stderr is not None and not _env_flag(KEEP_WORKER_ENV):
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(fmt)
        logger.addHandler(stream_handler)

    # Neither the self-test build nor the Keep worker may relaunch on a
    # crash: the self-test would fork-bomb into the modal PyInstaller error
    # dialog, and the worker must let its crash surface as an exit code.
    if _env_flag("KEEP_SYNC_TRAY_SELFTEST") or _env_flag(KEEP_WORKER_ENV):
        relaunch_on_crash = False

    try:
        _enable_faulthandler(resolved.with_name(resolved.stem + ".fault.log"), logger)
    except Exception:
        logger.warning("faulthandler setup failed", exc_info=True)

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

    logger.info(
        "logging up: version=%s pid=%s python=%s platform=%s frozen=%s debug=%s log=%s",
        get_version(), os.getpid(), platform.python_version(), sys.platform,
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


class LogTailer:
    """Incremental reader for the status window's live log view: tracks a
    byte offset into `path` (the same file setup_logging() already writes),
    so a poll timer can grab just what's new since the last call instead of
    re-reading the whole file. A rotated/truncated file (size < last known
    offset -- TimedRotatingFileHandler starts a fresh one at midnight, or a
    frozen build's log got recreated) just restarts from the top rather
    than raising. Never raises: a log file that's momentarily locked or
    missing just means this poll returns nothing, not a broken window."""

    def __init__(self, path: "Path"):
        self.path = Path(path)
        self._offset = 0

    def seed(self, max_lines: int = 200) -> str:
        """Initial content for the view: up to the last `max_lines` of the
        file, and positions the tailer at EOF so a following read_new()
        only returns text appended after this call."""
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
            self._offset = self.path.stat().st_size
        except OSError:
            self._offset = 0
            return ""
        lines = text.splitlines()
        return "\n".join(lines[-max_lines:])

    def read_new(self) -> str:
        """Text appended since the last seed()/read_new() call, or "" if
        there's nothing new (or the file is temporarily unreadable)."""
        try:
            size = self.path.stat().st_size
        except OSError:
            return ""
        if size < self._offset:
            self._offset = 0  # rotated or truncated -- start over
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(self._offset)
                text = fh.read()
                self._offset = fh.tell()
        except OSError:
            return ""
        return text


def log_callback_errors(label: str, reraise: bool = False):
    """Decorator for a Qt slot or a background thread target: logs any
    exception with a traceback (and by default swallows it so the tray
    survives) instead of letting it vanish into the toolkit.

    Do NOT put this on a pystray menu action -- pystray validates an
    action by reading action.__code__.co_argcount, and this wrapper's
    (*args, **kwargs) signature makes that 0, which pystray rejects with
    ValueError. Guard the body with a plain try/except there instead."""
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
    # Display-only: the project/tag titles picked in setup, alongside the ids
    # sync actually uses -- so the status window can show names instead of
    # raw ids without a live SP call every time it opens. Same idea as
    # keep_note_title, which has always stored the title directly.
    cfg.setdefault("sp_project_title", "")
    cfg.setdefault("sp_new_task_tag_ids", [])
    cfg.setdefault("sp_new_task_tag_titles", [])
    cfg.setdefault("sp_default_task_minutes", 0)
    cfg.setdefault("keep_note_title", "")
    cfg.setdefault("keep_sort_alphabetically", False)
    cfg.setdefault("ai_merchant_rename_enabled", False)
    cfg.setdefault("anthropic_api_key", "")
    cfg.setdefault("ai_merchant_rename_model", "")

    # Pre-multi-select configs carried a single "sp_new_task_tag_id" string.
    # Fold it into the new list field so an old config.json keeps working.
    old_tag_id = (cfg.pop("sp_new_task_tag_id", "") or "").strip()
    if old_tag_id and not cfg["sp_new_task_tag_ids"]:
        cfg["sp_new_task_tag_ids"] = [old_tag_id]
    return cfg


def _default_task_estimate_ms(cfg: dict) -> "int | None":
    """Config's `sp_default_task_minutes` as milliseconds for SP's
    `timeEstimate`, or None when unset / zero / unparseable. SP only takes
    a time estimate at task creation, so this rides on add_task only."""
    try:
        minutes = int(cfg.get("sp_default_task_minutes") or 0)
    except (TypeError, ValueError):
        return None
    return minutes * 60_000 if minutes > 0 else None


# --- AI merchant-prefix rename ------------------------------------------------
#
# Optional: a Keep item dictated as free text ("from walmart add toilet
# tablets") gets rewritten to "Walmart - Toilet tablets" before the SP task
# is created from it. One Claude API call per genuinely new item -- never
# retried once it's been looked at (see load_ai_rename_record), whether or
# not a merchant was found, so this is a one-shot cost, not a per-pass one.

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
DEFAULT_AI_RENAME_MODEL = "claude-haiku-4-5-20251001"
_AI_RENAME_SYSTEM_PROMPT = (
    "You clean up shopping-list entries dictated as free text. If the text names a "
    "specific merchant, store, or brand, rewrite it as \"Merchant - Item\" (merchant "
    "in title case, item in sentence case, filler words like 'from'/'add'/'buy' "
    "removed). Reply with ONLY the rewritten text, nothing else. If no merchant is "
    "named, reply with exactly NONE."
)
# Already looks hand-formatted as "Merchant - Item" (ours or the user's own) --
# skip it without spending an API call either way.
_ALREADY_PREFIXED_RE = re.compile(r"\S.*\s-\s\S")


def get_anthropic_api_key(cfg: dict) -> str:
    """Config's `anthropic_api_key`, falling back to ANTHROPIC_API_KEY --
    same env-var-first precedent as the Keep master token."""
    return (cfg.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def looks_already_prefixed(text: str) -> bool:
    return bool(_ALREADY_PREFIXED_RE.search(text or ""))


def check_anthropic_connection(api_key: str, timeout: float = 5.0) -> bool:
    """True if `api_key` can reach Anthropic's API right now -- for a status
    display, not the rename path itself. Hits the models-list endpoint
    (free, no completion tokens spent) rather than running a real rename
    just to answer "is this working". Never raises: any failure (bad key,
    network, timeout) is just False."""
    if not api_key:
        return False
    try:
        resp = requests.get(
            ANTHROPIC_MODELS_URL,
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            timeout=timeout,
        )
        return resp.status_code == 200
    except Exception:
        return False


def ai_merchant_prefix(
    text: str, api_key: str, model: "str | None" = None, timeout: float = 15.0,
) -> "str | None":
    """Asks Claude to rewrite `text` as "Merchant - Item" if it names a
    merchant, else None (left as-is). Never raises: a bad key, network
    hiccup, or malformed response just means this item doesn't get renamed
    this pass -- reconcile_sp still creates the SP task with the original
    text, and the miss still gets recorded so it isn't retried forever."""
    log = logging.getLogger(LOGGER_NAME)
    if not text.strip() or not api_key:
        return None
    try:
        resp = requests.post(
            ANTHROPIC_MESSAGES_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model or DEFAULT_AI_RENAME_MODEL,
                "max_tokens": 60,
                "system": _AI_RENAME_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": text}],
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        blocks = (resp.json() or {}).get("content") or []
        reply = (blocks[0].get("text") if blocks else "") or ""
        reply = reply.strip()
    except Exception:
        log.warning("AI merchant-rename call failed", exc_info=True)
        return None
    if not reply or reply.upper() == "NONE":
        return None
    return reply


def atomic_write_json(path: Path, data: dict, mode: int = 0o600, indent: "int | None" = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=indent)
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


# gkeepapi's sync-cursor cache is the whole account's node graph. It's only an
# optimisation -- skipping it just means more Google login challenges. Past this
# size we don't touch it; set KEEP_SYNC_NO_GOOGLE_CACHE=1 to disable it entirely.
GOOGLE_CACHE_MAX_BYTES = 3_000_000
NO_GOOGLE_CACHE_ENV = "KEEP_SYNC_NO_GOOGLE_CACHE"


@contextlib.contextmanager
def _gc_paused(collect_after: bool = False):
    """Hold the cyclic GC off for a block.

    The frozen Windows build hard-crashes (0x80000003 breakpoint, reported
    as "Garbage-collecting" inside json's C raw_decode) whenever a GC pass
    fires while `_json` is parsing a large payload -- gkeepapi's sync
    response, the sync-cursor cache, any big JSON. Pausing GC around those
    operations removes the window; refcounting still frees everything
    except genuine reference cycles, which `collect_after` mops up once the
    block is done."""
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()
            if collect_after:
                try:
                    gc.collect()
                except Exception:
                    pass


def load_google_state_cache(cache_path: Path):
    log = logging.getLogger(LOGGER_NAME)
    if _env_flag(NO_GOOGLE_CACHE_ENV):
        return None
    if not cache_path.exists():
        return None
    try:
        size = cache_path.stat().st_size
        if size > GOOGLE_CACHE_MAX_BYTES:
            log.warning(
                "google_sync_cache.json is %d bytes (> %d) -- ignoring and removing it",
                size, GOOGLE_CACHE_MAX_BYTES,
            )
            _discard_google_cache(cache_path)
            return None
        text = cache_path.read_text(encoding="utf-8")
        with _gc_paused():
            return json.loads(text)
    except (json.JSONDecodeError, OSError, ValueError):
        log.warning("google_sync_cache.json unreadable -- discarding it", exc_info=True)
        _discard_google_cache(cache_path)
        return None


def _discard_google_cache(cache_path: Path) -> None:
    try:
        cache_path.unlink()
    except OSError:
        pass


def write_google_state_cache(cache_path: Path, keep: "gkeepapi.Keep") -> None:
    """Serialise gkeepapi's sync state to `cache_path`, compact, but only if
    it comes out under GOOGLE_CACHE_MAX_BYTES -- otherwise drop it (see the
    note on that constant). Never raises; the cache is optional."""
    log = logging.getLogger(LOGGER_NAME)
    if _env_flag(NO_GOOGLE_CACHE_ENV):
        _discard_google_cache(cache_path)
        return
    try:
        with _gc_paused():
            blob = json.dumps(keep.dump(), separators=(",", ":"))
    except Exception:
        log.warning("could not serialise Google sync cache", exc_info=True)
        return
    if len(blob) > GOOGLE_CACHE_MAX_BYTES:
        log.warning(
            "Google sync cache would be %d bytes (> %d) -- not caching it",
            len(blob), GOOGLE_CACHE_MAX_BYTES,
        )
        _discard_google_cache(cache_path)
        return
    try:
        _atomic_write_text(cache_path, blob)
    except OSError:
        log.warning("could not write Google sync cache", exc_info=True)


def _atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


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
    atomic_write_json(item_map_path, item_map, mode=0o600, indent=None)


def load_ai_rename_record(path: Path) -> dict:
    """Which Keep item ids the AI merchant-prefix step has already looked
    at, keyed by item id -> {"result": "renamed"|"no_merchant", "at": iso}.
    Recording a miss (no merchant found) too is what makes this a one-shot
    cost per item rather than an API call every pass forever."""
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_ai_rename_record(path: Path, record: dict) -> None:
    atomic_write_json(path, record, mode=0o600, indent=None)


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
    by the tray setup dialogs to populate the "Keep list" dropdown.

    Deliberately does NOT touch google_sync_cache.json: this is a one-shot
    interactive call (the user is watching the dialog), a login challenge
    here is harmless, and parsing/writing that big cache blob on the GUI
    thread is exactly what has been crashing "Connect & load lists". Only
    the background sync loop bothers with the cache."""
    keep = gkeepapi.Keep()
    titles = []
    with _gc_paused(collect_after=True):
        keep.authenticate(email, master_token)
        keep.sync()
        for note in keep.all():
            if not isinstance(note, gkeepapi.node.List):
                continue
            if note.trashed:
                continue
            if note.archived and not include_archived:
                continue
            if note.title:
                titles.append(note.title)
    return sorted(set(titles), key=str.casefold)


def _spawn_keep_worker(request: dict, timeout: float) -> dict:
    """Re-invoke this same exe/script with KEEP_SYNC_WORKER=1 to do one
    Keep operation in a throwaway process, so a native crash (the frozen
    Windows build hard-faults in _json parsing Google's response) becomes a
    reportable error instead of taking the app down.

    Request (carries secrets) goes via a base64 env var, not argv. Reply
    comes back through a temp file, not stdout -- a --windowed exe's
    sys.stdout can be None. Raises RuntimeError on crash / timeout / a
    worker-reported error."""
    import base64
    import subprocess
    import tempfile

    if getattr(sys, "frozen", False):
        argv = [sys.executable]
    else:
        argv = [sys.executable, os.path.abspath(sys.argv[0])]
    fd, out_path = tempfile.mkstemp(prefix="keepworker-", suffix=".json")
    os.close(fd)
    env = {
        **os.environ,
        KEEP_WORKER_ENV: "1",
        KEEP_WORKER_REQUEST_ENV: base64.b64encode(
            json.dumps(request).encode("utf-8")
        ).decode("ascii"),
        KEEP_WORKER_OUT_ENV: out_path,
    }
    try:
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout, env=env,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("the Keep helper timed out")
        except OSError as e:
            raise RuntimeError(f"could not start the Keep helper: {e}")

        try:
            resp = json.loads(Path(out_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            resp = None

        if resp is None:
            stderr = (proc.stderr or "").strip()
            if stderr:
                logging.getLogger(LOGGER_NAME).error(
                    "Keep helper (exit %s) stderr:\n%s", proc.returncode, stderr
                )
            tail = (stderr.splitlines() or [""])[-1]
            raise RuntimeError(
                f"the Keep helper crashed (exit {proc.returncode}"
                + (f", {tail}" if tail else "") + ")"
            )
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error") or "Keep helper failed")
        return resp
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def list_keep_checklist_titles_isolated(
    email: str, master_token: str, state_dir: Path, include_archived: bool = False,
    timeout: float = 120.0,
) -> list[str]:
    """list_keep_checklist_titles() run in a child process (see
    _spawn_keep_worker). A crash surfaces as RuntimeError for the setup
    dialog instead of killing it."""
    resp = _spawn_keep_worker({
        "op": "titles",
        "email": email,
        "master_token": master_token,
        "state_dir": str(state_dir),
        "include_archived": bool(include_archived),
    }, timeout)
    return list(resp.get("titles") or [])


def sync_once_isolated(cfg: dict, timeout: float = 300.0) -> SyncResult:
    """sync_once() for the tray apps: on Windows it runs the pass in a
    child process so the frozen build's _json crash can't take the tray
    down -- a crash just fails this pass, the scheduler retries next
    interval. Elsewhere it's a plain sync_once() (that crash is
    Windows-frozen-only, and re-spawning a 25-90MB binary every few
    minutes isn't worth it). Never raises, like sync_once()."""
    if os.name != "nt":
        return sync_once(cfg)
    try:
        resp = _spawn_keep_worker({"op": "sync", "cfg": cfg}, timeout)
    except RuntimeError as e:
        log = logging.getLogger(LOGGER_NAME)
        log.error("isolated sync failed: %s", e)
        return SyncResult(False, str(e))
    r = resp.get("result") or {}
    return SyncResult(bool(r.get("ok")), str(r.get("message") or ""), int(r.get("count") or 0))


def run_keep_worker() -> int:
    """Child-process entry (KEEP_SYNC_WORKER=1). Reads the base64 JSON
    request from KEEP_SYNC_WORKER_REQUEST, does the Keep operation, writes
    the JSON reply to the file named by KEEP_SYNC_WORKER_OUT. A hard crash
    here leaves that file absent -- the parent treats that as a crash."""
    import base64

    # The worker is throwaway and its stderr is a pipe the parent reads, so
    # arm faulthandler to stderr even on Windows -- a native crash here
    # (the thing this whole worker exists to contain) then leaves a real
    # traceback the parent can log, instead of just an exit code.
    try:
        if sys.stderr is not None:
            faulthandler.enable(file=sys.stderr, all_threads=True)
    except Exception:
        pass

    out_path = os.environ.get(KEEP_WORKER_OUT_ENV, "")
    try:
        raw = base64.b64decode(os.environ.get(KEEP_WORKER_REQUEST_ENV, "") or "")
        req = json.loads(raw or b"{}")
        if req.get("op") == "titles":
            titles = list_keep_checklist_titles(
                req["email"], req["master_token"],
                Path(req["state_dir"]), req.get("include_archived", False),
            )
            reply = {"ok": True, "titles": titles}
        elif req.get("op") == "sync":
            res = sync_once(req["cfg"])
            reply = {"ok": True, "result": {
                "ok": res.ok, "message": res.message, "count": res.notes_count,
            }}
        else:
            reply = {"ok": False, "error": f"unknown op {req.get('op')!r}"}
    except BaseException as e:
        reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    try:
        Path(out_path).write_text(json.dumps(reply), encoding="utf-8")
    except OSError:
        return 1
    return 0 if reply.get("ok") else 1


def list_sp_projects(base_url: str, token: str) -> list[tuple[str, str]]:
    """(id, title) for every non-archived SP project -- used by the tray
    setup dialogs to populate the "Super Productivity project" dropdown."""
    sp = sp_client.SPClient(base_url or DEFAULT_SP_API_BASE_URL, token)
    return [(p.id, p.title) for p in sp.list_projects() if not p.is_archived]


def list_sp_tags(base_url: str, token: str) -> list[tuple[str, str]]:
    """(id, title) for every SP tag -- used by the tray setup dialogs to
    populate the optional "tag new tasks with" dropdown."""
    sp = sp_client.SPClient(base_url or DEFAULT_SP_API_BASE_URL, token)
    return [(t.id, t.title) for t in sp.list_tags()]


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


def reconcile_sp(
    cfg: dict, keep: "gkeepapi.Keep", sp: "sp_client.SPClient", item_map: dict,
    ai_rename_record: "dict | None" = None,
) -> ReconcileResult:
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
    then the refreshed task no longer looks changed on the way back).

    `ai_rename_record` (mutated in place like item_map, caller persists) is
    only consulted/used when both it and cfg['ai_merchant_rename_enabled']
    are truthy -- pass None to skip the feature entirely (e.g. in tests that
    don't care about it)."""
    log = logging.getLogger(LOGGER_NAME)
    note_title = cfg.get("keep_note_title") or ""
    project_id = cfg.get("sp_project_id") or ""
    # Optional: stamp every task this sync creates from a Keep item with one
    # or more SP tags (chosen in the setup dialog). SP's REST API only takes
    # tagIds at creation, so this never touches tasks that already exist.
    new_task_tag_ids = [t.strip() for t in (cfg.get("sp_new_task_tag_ids") or []) if t and t.strip()] or None
    # Optional: give every task created from a Keep item a default time
    # estimate (SP's `timeEstimate`). Like tags, SP's REST API only accepts
    # this at creation, so existing tasks are never touched.
    new_task_estimate_ms = _default_task_estimate_ms(cfg)
    # Optional: rewrite a freely-dictated new item ("from walmart add toilet
    # tablets") to "Merchant - Item" before the SP task is created from it.
    # One API call per item, ever -- see ai_rename_record's docstring.
    ai_rename_enabled = bool(cfg.get("ai_merchant_rename_enabled")) and ai_rename_record is not None
    ai_rename_api_key = get_anthropic_api_key(cfg) if ai_rename_enabled else ""
    ai_rename_model = (cfg.get("ai_merchant_rename_model") or "").strip() or None

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
            title = item.text
            if (
                ai_rename_enabled and ai_rename_api_key
                and item.id not in ai_rename_record
                and not looks_already_prefixed(title)
            ):
                renamed = ai_merchant_prefix(title, ai_rename_api_key, ai_rename_model)
                ai_rename_record[item.id] = {
                    "result": "renamed" if renamed else "no_merchant",
                    "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                }
                if renamed:
                    log.info("AI merchant-rename: %r -> %r", title, renamed)
                    title = renamed
                    item.text = renamed  # keep both sides showing the same title
                    res.keep_dirty = True
            task_id = sp.add_task(
                title, project_id, bool(item.checked),
                tag_ids=new_task_tag_ids, time_estimate_ms=new_task_estimate_ms,
            )
            note_map[item.id] = {"taskId": task_id, "text": title, "checked": bool(item.checked)}
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

    # Optional: keep the Keep checklist alphabetized. Checked first against
    # the current order so an already-sorted list doesn't get touched (and
    # pushed to Google) on a pass where nothing changed -- sort_items()
    # unconditionally reassigns every item's sort value, which would
    # otherwise mean a Keep write on every single pass forever.
    if cfg.get("keep_sort_alphabetically"):
        current_order = [i.text for i in note.items]
        if current_order != sorted(current_order, key=str.casefold):
            note.sort_items(key=lambda i: i.text.casefold())
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


# A stale/revoked master token or a network outage silences the sync
# entirely -- the tray keeps retrying every interval, but a user who isn't
# watching the tray (or runs the headless CLI daemon) can go a long time
# without noticing. One flagged task in the synced project surfaces it
# somewhere they're already looking. Fixed title so it's find-or-create,
# not a new task every failed pass.
KEEP_AUTH_ALERT_TITLE = "⚠️ Keep Sync needs attention"


def _notify_sp_of_keep_failure(sp: "sp_client.SPClient", project_id: str, detail: str) -> None:
    """Best-effort: create KEEP_AUTH_ALERT_TITLE in `project_id` if one isn't
    already open, so a stale master token or an unreachable Google shows up
    in Super Productivity, not just the tray/log. Skips if there's no
    project configured yet. Never raises -- this is a diagnostic breadcrumb,
    not a sync step, and the one deliberate exception to "both sides
    untouched on failure": Keep failed, but SP is still reachable and
    getting told about it. If SP is also unreachable this just logs and
    moves on, same as any other failed SP call."""
    log = logging.getLogger(LOGGER_NAME)
    if not project_id:
        return
    try:
        already_open = any(
            t.title == KEEP_AUTH_ALERT_TITLE and not t.is_done
            for t in sp.list_tasks(project_id)
        )
        if already_open:
            log.debug("Keep-failure alert task already open in %s; not duplicating", project_id)
            return
        sp.add_task(KEEP_AUTH_ALERT_TITLE, project_id, notes=detail)
        log.info("created Keep-failure alert task in %s", project_id)
    except Exception:
        log.warning("could not create Keep-failure alert task in SP", exc_info=True)


def _clear_sp_keep_alert(sp: "sp_client.SPClient", project_id: str) -> None:
    """The other half of _notify_sp_of_keep_failure: once Keep auth/sync
    succeeds again, mark any open KEEP_AUTH_ALERT_TITLE task done so it
    doesn't sit there stale -- an alert that never clears trains the user to
    ignore it. Best-effort, never raises."""
    log = logging.getLogger(LOGGER_NAME)
    if not project_id:
        return
    try:
        for t in sp.list_tasks(project_id):
            if t.title == KEEP_AUTH_ALERT_TITLE and not t.is_done:
                sp.update_task(t.id, {"isDone": True})
                log.info("cleared Keep-failure alert task %s in %s", t.id, project_id)
    except Exception:
        log.warning("could not clear Keep-failure alert task in SP", exc_info=True)


def sync_once(cfg: dict) -> SyncResult:
    """Runs one Keep <-> Super Productivity reconcile pass. Never raises:
    on any failure both sides are left untouched, so a transient Keep
    login error or SP being closed never corrupts either side -- except a
    stale-token/unreachable-Google failure also best-effort drops one alert
    task in SP (see _notify_sp_of_keep_failure); that's a deliberate,
    idempotent side note, not a sync mutation.

    The whole pass runs with the cyclic GC paused (see _gc_paused): a GC
    sweep landing inside `_json` while it parses Google's or SP's response
    hard-crashes the frozen Windows build. GC is re-enabled and run once
    when the pass finishes."""
    with _gc_paused(collect_after=True):
        return _sync_once(cfg)


def _sync_once(cfg: dict) -> SyncResult:
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
    ai_rename_path = state_dir / "ai_renamed.json"
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
    ai_rename_record = load_ai_rename_record(ai_rename_path)

    try:
        if cached_state:
            keep.authenticate(cfg["email"], master_token, state=cached_state)
        else:
            keep.authenticate(cfg["email"], master_token)
        keep.sync()
        log.info("Keep authenticated and synced (cache_hit=%s)", bool(cached_state))
    except gkeepapi.exception.LoginException as e:
        log.error("Google login failed", exc_info=True)
        message = (
            f"Google login failed ({e}). The master token may be stale/revoked, "
            "or Google is challenging this login. Nothing changed."
        )
        _notify_sp_of_keep_failure(sp, cfg.get("sp_project_id", ""), message)
        return SyncResult(False, message)
    except Exception as e:  # network errors, etc.
        log.error("Keep sync failed", exc_info=True)
        message = f"Keep sync failed: {e}. Nothing changed."
        _notify_sp_of_keep_failure(sp, cfg.get("sp_project_id", ""), message)
        return SyncResult(False, message)

    _clear_sp_keep_alert(sp, cfg.get("sp_project_id", ""))

    try:
        result = reconcile_sp(cfg, keep, sp, item_map, ai_rename_record)
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

    # keep.dump() walks the whole gkeepapi node graph (every note in the
    # account) -- the heaviest thing left in the pass and a suspect for a
    # silent death here. write_google_state_cache serialises it once,
    # bounds it, and never raises. Non-fatal: next login just won't reuse
    # Google's sync cursor.
    log.info("serialising Google sync cache")
    write_google_state_cache(google_cache_path, keep)
    log.info("Google sync cache done")

    try:
        save_item_map(item_map_path, item_map)
        log.info("item map written")
    except OSError as e:
        log.error("failed to write %s", item_map_path, exc_info=True)
        return SyncResult(False, f"failed to write {item_map_path}: {e}")

    # Best-effort, unlike item_map: this is a cost-tracking record ("did we
    # already ask the AI about this item"), not something either side's
    # state depends on, so a write failure here shouldn't fail the pass.
    try:
        save_ai_rename_record(ai_rename_path, ai_rename_record)
    except OSError:
        log.warning("failed to write %s", ai_rename_path, exc_info=True)

    if push_error is not None:
        return SyncResult(
            False,
            f"SP tasks updated, but pushing item edits back to Keep failed: {push_error}",
        )

    log.debug("writing last_sync.json debug dump")
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
