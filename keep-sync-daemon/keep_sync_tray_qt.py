#!/usr/bin/env python3
"""
Linux/KDE system-tray app for keep-sync-daemon, built on Qt (PySide6)
instead of pystray/tkinter.

pystray's Linux backend (GTK/AppIndicator) often doesn't integrate
cleanly with KDE Plasma's StatusNotifierItem-based tray — icons can be
missing, mis-themed, or the menu can behave oddly. QSystemTrayIcon talks
StatusNotifierItem natively, so this gives a proper native tray icon
under Plasma. Functionally this is the same app as keep_sync_tray.py
(Windows): a one-time setup window for email + OAuth Token, then a tray
icon that re-syncs on its own timer — pulling Keep's state to state.json,
and pushing any Super Productivity edits queued in pending_changes.json
back to Keep (see keep_sync_core.sync_once).

Packaging into a standalone Linux binary (see README.md):
    pyinstaller --onefile --name KeepSyncTrayQt keep_sync_tray_qt.py
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QGridLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSystemTrayIcon,
)

import keep_sync_core as core

APP_NAME = "Keep Sync"

if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).parent
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "keep_sync_tray_qt.log"

log = core.setup_logging(LOG_PATH, relaunch_on_crash=True)

AUTOSTART_DESKTOP_PATH = Path.home() / ".config" / "autostart" / "keep-sync-tray.desktop"


def default_config() -> dict:
    return {
        "email": "",
        "state_dir": core.DEFAULT_STATE_DIR,
        "include_archived": False,
        "sync_interval_minutes": core.DEFAULT_SYNC_INTERVAL_MINUTES,
        "run_at_startup": False,
        "sp_api_base_url": core.DEFAULT_SP_API_BASE_URL,
        "sp_access_token": "",
        "sp_project_id": "",
        "keep_note_title": "",
    }


def load_or_default_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return core.load_config(CONFIG_PATH)
        except (ValueError, OSError, json.JSONDecodeError):
            pass
    return default_config()


def needs_setup(cfg: dict) -> bool:
    if not cfg.get("email"):
        return True
    if not (cfg.get("sp_access_token") and cfg.get("sp_project_id") and cfg.get("keep_note_title")):
        return True
    state_dir = core.resolve_state_dir(cfg.get("state_dir", core.DEFAULT_STATE_DIR))
    return not (state_dir / "master_token").exists()


def set_run_at_startup(enable: bool) -> None:
    """Best-effort XDG autostart entry. No-op if the exe path is unknown
    (e.g. running keep_sync_tray_qt.py directly through a bare `python`
    with no reliable relaunch command)."""
    if enable:
        AUTOSTART_DESKTOP_PATH.parent.mkdir(parents=True, exist_ok=True)
        if getattr(sys, "frozen", False):
            exec_cmd = str(sys.executable)
        else:
            exec_cmd = f'{sys.executable} "{Path(__file__).resolve()}"'
        AUTOSTART_DESKTOP_PATH.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={APP_NAME}\n"
            f"Exec={exec_cmd}\n"
            "X-GNOME-Autostart-enabled=true\n",
            encoding="utf-8",
        )
    else:
        try:
            AUTOSTART_DESKTOP_PATH.unlink()
        except FileNotFoundError:
            pass


def make_icon() -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#4285F4"))
    painter.drawEllipse(2, 2, 60, 60)
    painter.setBrush(QColor("white"))
    painter.drawRect(18, 16, 28, 32)
    painter.setPen(QColor("#4285F4"))
    for y in (24, 32, 40):
        painter.drawLine(22, y, 42, y)
    painter.end()
    return QIcon(pixmap)


class SetupDialog(QDialog):
    """Collects the Google + Super Productivity credentials, then (via the
    "Connect & load lists" button) fetches the Keep checklists and SP
    projects to pick from. `result_cfg` is the finished config on success,
    or None if the dialog was closed without finishing."""

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = dict(cfg)
        self.result_cfg: dict | None = None
        self._project_ids: list[str] = []

        self.setWindowTitle(f"{APP_NAME} — Setup")
        self.setWindowIcon(make_icon())

        layout = QGridLayout(self)
        row = 0

        info = QLabel(
            "Google Keep has no public API, so this connects the same way\n"
            "the Android app does. To get an OAuth Token: open\n"
            "accounts.google.com/EmbeddedSetup in a browser, sign in, then\n"
            "copy the 'oauth_token' cookie's value (dev tools > Application\n"
            "> Cookies). Leave it blank to reuse an already-saved token.\n"
            "The Super Productivity Access Token is in the desktop app under\n"
            "Settings > Misc (enable the local REST API there first)."
        )
        layout.addWidget(info, row, 0, 1, 2)
        row += 1

        layout.addWidget(QLabel("Google account email:"), row, 0)
        self.email_edit = QLineEdit(self.cfg.get("email", ""))
        layout.addWidget(self.email_edit, row, 1)
        row += 1

        layout.addWidget(QLabel("OAuth Token:"), row, 0)
        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setPlaceholderText("blank = reuse saved master token")
        layout.addWidget(self.token_edit, row, 1)
        row += 1

        layout.addWidget(QLabel("SP API base URL:"), row, 0)
        self.sp_url_edit = QLineEdit(self.cfg.get("sp_api_base_url", core.DEFAULT_SP_API_BASE_URL))
        layout.addWidget(self.sp_url_edit, row, 1)
        row += 1

        layout.addWidget(QLabel("SP Access Token:"), row, 0)
        self.sp_token_edit = QLineEdit(self.cfg.get("sp_access_token", ""))
        self.sp_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.sp_token_edit, row, 1)
        row += 1

        self.connect_btn = QPushButton("Connect && load lists")
        self.connect_btn.clicked.connect(self._connect)
        layout.addWidget(self.connect_btn, row, 0, 1, 2)
        row += 1

        layout.addWidget(QLabel("Keep list:"), row, 0)
        self.note_combo = QComboBox()
        self.note_combo.setEnabled(False)
        layout.addWidget(self.note_combo, row, 1)
        row += 1

        layout.addWidget(QLabel("Super Productivity project:"), row, 0)
        self.project_combo = QComboBox()
        self.project_combo.setEnabled(False)
        layout.addWidget(self.project_combo, row, 1)
        row += 1

        layout.addWidget(QLabel("Sync every (minutes):"), row, 0)
        self.interval_spin = QSpinBox()
        self.interval_spin.setMinimum(1)
        self.interval_spin.setMaximum(24 * 60)
        self.interval_spin.setValue(int(self.cfg.get("sync_interval_minutes", core.DEFAULT_SYNC_INTERVAL_MINUTES)))
        layout.addWidget(self.interval_spin, row, 1)
        row += 1

        self.startup_check = QCheckBox("Start automatically when I log in")
        self.startup_check.setChecked(bool(self.cfg.get("run_at_startup", False)))
        layout.addWidget(self.startup_check, row, 0, 1, 2)
        row += 1

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #c0392b;")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label, row, 0, 1, 2)
        row += 1

        self.submit_btn = QPushButton("Save && Start Syncing")
        self.submit_btn.setEnabled(False)
        self.submit_btn.clicked.connect(self._submit)
        layout.addWidget(self.submit_btn, row, 0, 1, 2)

    def _resolve_master_token(self, email: str, oauth_token: str, state_dir) -> str:
        if oauth_token:
            master_token = core.exchange_master_token(email, oauth_token)
            core.save_master_token(state_dir, master_token)
            return master_token
        return core.get_master_token(state_dir)  # raises RuntimeError if none saved

    def _connect(self) -> None:
        email = self.email_edit.text().strip()
        oauth_token = self.token_edit.text().strip()
        sp_url = self.sp_url_edit.text().strip() or core.DEFAULT_SP_API_BASE_URL
        sp_token = self.sp_token_edit.text().strip()

        if not email or not sp_token:
            self.status_label.setText("Email and SP Access Token are required.")
            return

        state_dir = core.resolve_state_dir(self.cfg.get("state_dir", core.DEFAULT_STATE_DIR))
        self.connect_btn.setEnabled(False)
        self.status_label.setText("Connecting to Google and Super Productivity…")
        QApplication.processEvents()

        try:
            master_token = self._resolve_master_token(email, oauth_token, state_dir)
        except Exception as e:
            self.status_label.setText(f"Google sign-in failed: {e}")
            self.connect_btn.setEnabled(True)
            return

        try:
            titles = core.list_keep_checklist_titles(
                email, master_token, state_dir, self.cfg.get("include_archived", False)
            )
        except Exception as e:
            self.status_label.setText(f"Could not read Keep lists: {e}")
            self.connect_btn.setEnabled(True)
            return

        try:
            projects = core.list_sp_projects(sp_url, sp_token)
        except Exception as e:
            self.status_label.setText(f"Could not reach Super Productivity: {e}")
            self.connect_btn.setEnabled(True)
            return

        self.note_combo.clear()
        self.note_combo.addItems(titles or [])
        self.note_combo.setEnabled(bool(titles))
        if self.cfg.get("keep_note_title") in titles:
            self.note_combo.setCurrentText(self.cfg["keep_note_title"])

        self._project_ids = [pid for pid, _ in projects]
        self.project_combo.clear()
        self.project_combo.addItems([title for _, title in projects])
        self.project_combo.setEnabled(bool(projects))
        if self.cfg.get("sp_project_id") in self._project_ids:
            self.project_combo.setCurrentIndex(self._project_ids.index(self.cfg["sp_project_id"]))

        if not titles:
            self.status_label.setText("Connected, but no Keep checklists were found.")
        elif not projects:
            self.status_label.setText("Connected, but no Super Productivity projects were found.")
        else:
            self.status_label.setStyleSheet("color: #27ae60;")
            self.status_label.setText("Connected. Pick a list and a project, then Save.")
            self.submit_btn.setEnabled(True)

        self.connect_btn.setEnabled(True)

    def _submit(self) -> None:
        note_title = self.note_combo.currentText().strip()
        project_idx = self.project_combo.currentIndex()
        if not note_title or project_idx < 0 or project_idx >= len(self._project_ids):
            self.status_label.setStyleSheet("color: #c0392b;")
            self.status_label.setText("Pick a Keep list and a project first (use Connect).")
            return

        state_dir = core.resolve_state_dir(self.cfg.get("state_dir", core.DEFAULT_STATE_DIR))
        self.cfg.update(
            email=self.email_edit.text().strip(),
            state_dir=str(state_dir),
            include_archived=self.cfg.get("include_archived", False),
            sync_interval_minutes=self.interval_spin.value(),
            run_at_startup=self.startup_check.isChecked(),
            sp_api_base_url=self.sp_url_edit.text().strip() or core.DEFAULT_SP_API_BASE_URL,
            sp_access_token=self.sp_token_edit.text().strip(),
            sp_project_id=self._project_ids[project_idx],
            keep_note_title=note_title,
        )
        core.save_config(CONFIG_PATH, self.cfg)

        try:
            set_run_at_startup(self.startup_check.isChecked())
        except OSError:
            pass  # best-effort; setup still succeeded

        self.result_cfg = self.cfg
        self.accept()


def run_setup_dialog(cfg: dict) -> dict | None:
    dialog = SetupDialog(cfg)
    dialog.exec()
    return dialog.result_cfg


class StatusDialog(QDialog):
    def __init__(self, app: "TrayApp", parent=None):
        super().__init__(parent)
        self.app = app

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(make_icon())

        layout = QGridLayout(self)
        layout.addWidget(QLabel(f"Account: {app.cfg.get('email', '')}"), 0, 0, 1, 2)

        self.status_label = QLabel(app.status)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label, 1, 0, 1, 2)

        sync_btn = QPushButton("Sync now")
        sync_btn.clicked.connect(app.sync_now)
        layout.addWidget(sync_btn, 2, 0)

        reconfigure_btn = QPushButton("Reconfigure…")
        reconfigure_btn.clicked.connect(self._reconfigure)
        layout.addWidget(reconfigure_btn, 2, 1)

        version_label = QLabel(f"Version {core.get_version()}")
        version_label.setStyleSheet("color: gray;")
        version_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(version_label, 3, 0, 1, 2)

        app.status_changed.connect(self.status_label.setText)

    def _reconfigure(self) -> None:
        self.accept()
        self.app.reconfigure()


class TrayApp(QObject):
    status_changed = Signal(str)
    # Emitted from the sync worker thread; the connected slot runs on the
    # GUI thread. Touching QSystemTrayIcon off the main thread is undefined
    # behaviour in Qt and can take the process down natively (no Python
    # traceback), so every tray call goes through here.
    notify = Signal(str, str)

    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.status = "Starting..."
        self.stop_event = threading.Event()
        self._scheduler_thread: threading.Thread | None = None
        self._status_dialog: StatusDialog | None = None

        self.tray_icon = QSystemTrayIcon(make_icon())
        self.tray_icon.setToolTip(f"{APP_NAME} — starting…")
        self.tray_icon.activated.connect(self._on_activated)

        # Held on the instance on purpose: QSystemTrayIcon.setContextMenu()
        # does not take ownership, so a menu left as a local would be GC'd
        # and right-clicking the tray icon would crash into freed memory.
        self._menu = QMenu()
        self._menu.addAction("Show status", self.show_status_window)
        self._menu.addAction("Sync now", self.sync_now)
        self._menu.addAction("Open data folder", self.open_data_folder)
        self._menu.addAction("Reconfigure…", self.reconfigure)
        self._menu.addSeparator()
        self._menu.addAction("Quit", self.quit)
        self.tray_icon.setContextMenu(self._menu)

        self.status_changed.connect(self._update_tooltip)
        self.notify.connect(self._show_notification)

    @core.log_callback_errors("tray notification")
    def _show_notification(self, title: str, message: str) -> None:
        self.tray_icon.showMessage(title, message, QSystemTrayIcon.MessageIcon.Warning)

    @core.log_callback_errors("tray activated")
    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # DoubleClick fires reliably on some Linux desktops but not all —
        # Plasma's StatusNotifierItem protocol doesn't guarantee it, so
        # "Show status" in the menu is the dependable fallback either way.
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_status_window()

    @core.log_callback_errors("tooltip update")
    def _update_tooltip(self, text: str) -> None:
        self.tray_icon.setToolTip(f"{APP_NAME} — {text}")

    @core.log_callback_errors("show status window")
    def show_status_window(self) -> None:
        if self._status_dialog is None:
            self._status_dialog = StatusDialog(self)
            self._status_dialog.finished.connect(self._on_status_dialog_closed)
        self._status_dialog.show()
        self._status_dialog.raise_()
        self._status_dialog.activateWindow()

    def _on_status_dialog_closed(self) -> None:
        self._status_dialog = None

    @core.log_callback_errors("sync run")
    def _run_sync(self) -> None:
        start = time.monotonic()
        log.info("sync run starting")
        result = core.sync_once(self.cfg)
        elapsed = time.monotonic() - start
        ts = time.strftime("%H:%M:%S")
        text = f"{'OK' if result.ok else 'error'} @ {ts}: {result.message}"
        self.status = text
        self.status_changed.emit(text)
        log.info("sync run done in %.1fs: ok=%s %s", elapsed, result.ok, result.message)
        if not result.ok:
            self.notify.emit(f"{APP_NAME} sync failed", result.message)

    @core.log_callback_errors("sync now")
    def sync_now(self) -> None:
        threading.Thread(target=self._run_sync, daemon=True, name="sync-now").start()

    @core.log_callback_errors("open data folder")
    def open_data_folder(self) -> None:
        try:
            state_dir = core.resolve_state_dir(self.cfg.get("state_dir", core.DEFAULT_STATE_DIR))
            state_dir.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(["xdg-open", str(state_dir)])
        except OSError:
            log.exception("failed to open data folder")

    def start_scheduler(self) -> None:
        self.stop_event.clear()
        self._scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True, name="scheduler")
        self._scheduler_thread.start()
        log.info("scheduler started")

    def stop_scheduler(self) -> None:
        self.stop_event.set()

    def _scheduler_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._run_sync()
            except Exception:
                # A single bad sync (e.g. one malformed Keep item) must not
                # permanently kill background syncing for every other note.
                log.exception("sync raised unexpectedly; will retry next interval")
            interval_minutes = max(1, int(self.cfg.get("sync_interval_minutes", core.DEFAULT_SYNC_INTERVAL_MINUTES)))
            log.debug("scheduler sleeping %d min until next sync", interval_minutes)
            self.stop_event.wait(interval_minutes * 60)
        log.info("scheduler loop stopped")

    @core.log_callback_errors("reconfigure")
    def reconfigure(self) -> None:
        log.info("reconfigure requested")
        self.stop_scheduler()
        new_cfg = run_setup_dialog(self.cfg)
        if new_cfg is not None:
            self.cfg = new_cfg
        self.start_scheduler()

    @core.log_callback_errors("quit")
    def quit(self) -> None:
        log.info("quit requested")
        self.stop_scheduler()
        self.tray_icon.hide()
        QApplication.instance().quit()

    def show(self) -> None:
        self.tray_icon.show()
        self.start_scheduler()


def run_selftest() -> int:
    """Non-interactive sanity check used by CI after a PyInstaller build:
    builds the tray icon/menu (Qt's offscreen platform plugin, no real
    display needed) without starting the event loop, touching config.json,
    or hitting the network. Catches packaging failures (missing hidden
    imports/plugins), not UX issues."""
    import tempfile

    app = QApplication.instance() or QApplication([])
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("SELFTEST: no system tray available in this environment (expected under Qt's offscreen platform) — skipping tray checks")
    make_icon()
    with tempfile.TemporaryDirectory() as tmp:
        tray = TrayApp({"email": "selftest@example.com", "state_dir": tmp})
        tray.tray_icon.setToolTip("selftest")
    print("SELFTEST OK")
    return 0


def main() -> int:
    log.info("KeepSyncTrayQt starting (frozen=%s, dir=%s)", getattr(sys, "frozen", False), APP_DIR)
    core.install_qt_message_handler(log)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    log.info(
        "Qt up: platform=%s tray_available=%s session=%s desktop=%s wayland=%s",
        app.platformName(),
        QSystemTrayIcon.isSystemTrayAvailable(),
        os.environ.get("XDG_SESSION_TYPE", "?"),
        os.environ.get("XDG_CURRENT_DESKTOP", "?"),
        bool(os.environ.get("WAYLAND_DISPLAY")),
    )
    app.aboutToQuit.connect(lambda: log.info("Qt aboutToQuit"))

    cfg = load_or_default_config()
    if needs_setup(cfg):
        cfg = run_setup_dialog(cfg)
        if cfg is None:
            log.info("setup dialog closed without finishing; exiting")
            return 0  # user closed setup without finishing

    tray = TrayApp(cfg)
    tray.show()
    exit_code = app.exec()
    log.info("KeepSyncTrayQt exiting (code=%s)", exit_code)
    return exit_code


if __name__ == "__main__":
    if os.environ.get("KEEP_SYNC_TRAY_SELFTEST") == "1":
        raise SystemExit(run_selftest())
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        raise
    except BaseException:
        log.critical("KeepSyncTrayQt crashed", exc_info=True)
        # Try to come back up on our own; relaunch_after_crash() only returns
        # once it's crashed too many times in a row to keep trying.
        core.relaunch_after_crash(log)
        try:
            QMessageBox.critical(
                None,
                APP_NAME,
                f"{APP_NAME} keeps crashing and won't restart itself again.\n\n"
                f"See {getattr(log, 'log_path', LOG_PATH)} (and the sibling "
                ".fault.log) for details.",
            )
        except Exception:
            pass
        raise
